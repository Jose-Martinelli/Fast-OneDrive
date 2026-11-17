import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.disk import indexIsRunning, isEphemeralName, isEphemeralPath, tryExclusiveLock
from fastonedrive.log import logError, logInfo
from fastonedrive.graph import (
	GraphDeltaError,
	GraphDeltaExpired,
	iterDriveDelta,
	iterItemDelta,
	parseDeltaItem,
)
from fastonedrive.paths import remoteItemPath
from fastonedrive.rclone import contentTokenFromStat, isContentToken, iterLsjson, remoteStat

SCHEMA_SQL: str = """
CREATE TABLE files (
	path TEXT PRIMARY KEY,
	name TEXT NOT NULL,
	parent TEXT NOT NULL,
	isDir INTEGER NOT NULL,
	size INTEGER NOT NULL,
	modTime TEXT NOT NULL,
	mimeType TEXT NOT NULL,
	etag TEXT NOT NULL,
	itemId TEXT NOT NULL DEFAULT '',
	pinned INTEGER NOT NULL DEFAULT 0,
	updatedAt TEXT NOT NULL,
	baseModTime TEXT NOT NULL DEFAULT '',
	baseEtag TEXT NOT NULL DEFAULT '',
	syncState TEXT NOT NULL DEFAULT '',
	syncError TEXT NOT NULL DEFAULT '',
	unixMode INTEGER
)
"""

DELTA_STATE_SQL: str = """
CREATE TABLE IF NOT EXISTS delta_state (
	scope TEXT PRIMARY KEY,
	driveId TEXT NOT NULL,
	itemId TEXT NOT NULL,
	deltaLink TEXT NOT NULL,
	prefix TEXT NOT NULL,
	updatedAt TEXT NOT NULL
)
"""

UPSERT_SQL: str = """
INSERT INTO files (
	path, name, parent, isDir, size, modTime, mimeType, etag, itemId, pinned, updatedAt,
	baseModTime, baseEtag, syncState, syncError
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, '', '')
ON CONFLICT(path) DO UPDATE SET
	name = excluded.name,
	parent = excluded.parent,
	isDir = excluded.isDir,
	size = excluded.size,
	modTime = excluded.modTime,
	mimeType = excluded.mimeType,
	etag = excluded.etag,
	itemId = CASE WHEN excluded.itemId = '' THEN files.itemId ELSE excluded.itemId END,
	updatedAt = excluded.updatedAt,
	syncState = CASE
		WHEN files.syncState IN ('dirty', 'syncing')
			AND excluded.etag != ''
			AND files.baseEtag != ''
			AND (
				(
					(
						(files.baseEtag LIKE 'qxh:%' AND excluded.etag LIKE 'qxh:%')
						OR (files.baseEtag LIKE 'sha1:%' AND excluded.etag LIKE 'sha1:%')
						OR (files.baseEtag LIKE 'mt:%' AND excluded.etag LIKE 'mt:%')
					)
					AND excluded.etag != files.baseEtag
				)
				OR (
					NOT (
						(files.baseEtag LIKE 'qxh:%' AND excluded.etag LIKE 'qxh:%')
						OR (files.baseEtag LIKE 'sha1:%' AND excluded.etag LIKE 'sha1:%')
						OR (files.baseEtag LIKE 'mt:%' AND excluded.etag LIKE 'mt:%')
					)
					AND (
						(files.size > 0 AND excluded.size > 0 AND files.size != excluded.size)
						OR (
							files.baseModTime != ''
							AND excluded.modTime != ''
							AND substr(files.baseModTime, 1, 19) != substr(excluded.modTime, 1, 19)
						)
					)
				)
			)
		THEN 'conflict'
		ELSE files.syncState
	END
"""

FILE_SELECT: str = (
	"path, name, parent, isDir, size, modTime, mimeType, etag, itemId, "
	"pinned, updatedAt, baseModTime, baseEtag, syncState, syncError, unixMode"
)

FILE_INSERT_SQL: str = """
INSERT INTO files (
	path, name, parent, isDir, size, modTime, mimeType, etag, itemId, pinned, updatedAt,
	baseModTime, baseEtag, syncState, syncError, unixMode
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

SYNC_DIRTY: str = "dirty"
SYNC_SYNCING: str = "syncing"
SYNC_SYNCED: str = "synced"
SYNC_CONFLICT: str = "conflict"
SYNC_ERROR: str = "error"
PROTECTED_SYNC: frozenset[str] = frozenset(
	{SYNC_DIRTY, SYNC_SYNCING, SYNC_CONFLICT, SYNC_ERROR}
)

INDEXING_ENV: str = "FASTONEDRIVE_INDEXING"
SCHEMA_VERSION: int = 10

FILE_CHUNKS_SQL: str = """
CREATE TABLE IF NOT EXISTS file_chunks (
	path TEXT PRIMARY KEY,
	etag TEXT NOT NULL,
	size INTEGER NOT NULL,
	bitmap BLOB NOT NULL
)
"""

CHUNK_WRITES_SQL: str = """
CREATE TABLE IF NOT EXISTS chunk_writes (
	path TEXT NOT NULL,
	start INTEGER NOT NULL,
	length INTEGER NOT NULL
)
"""
_listingStamp: dict[str, float] = {}
_listingBusy: set[str] = set()
_listingLock: threading.Lock = threading.Lock()


def connectDb() -> sqlite3.Connection:
	config.ensureDataDir()
	connection: sqlite3.Connection = sqlite3.connect(
		config.dbPath(),
		timeout=15.0,
	)
	connection.row_factory = sqlite3.Row
	connection.execute("PRAGMA journal_mode = WAL")
	connection.execute("PRAGMA busy_timeout = 8000")
	connection.execute("PRAGMA synchronous = NORMAL")
	connection.execute("PRAGMA foreign_keys = ON")
	_ensureFilesTable(connection)
	_migrateSchemaIfNeeded(connection)
	return connection


def rebuildIndex(remotePath: str | None = None) -> int:
	root: str = remotePath if remotePath is not None else config.remoteRoot()
	os.environ[INDEXING_ENV] = "1"
	with tryExclusiveLock("index.lock") as acquired:
		if not acquired:
			return -1
		startedAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
		_setIndexJobStatus(
			root,
			0,
			startedAt,
			mode="completo",
		)
		connection: sqlite3.Connection = connectDb()
		try:
			try:
				logInfo("indexação incremental iniciada", extra={"root": root})
				count: int = _rebuild(connection, root, startedAt)
				finishedAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
				job: dict[str, Any] = _readIndexJobFile()
				_setIndexJobStatus(
					root,
					count,
					startedAt,
					finishedAt,
					None,
					str(job.get("mode") or "completo"),
				)
				logInfo("indexação incremental concluída", extra={"count": count})
				return count
			except Exception as error:
				job = _readIndexJobFile()
				failAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
				_setIndexJobStatus(
					root,
					int(job.get("counted") or 0),
					str(job.get("startedAt") or startedAt),
					failAt,
					str(error),
					str(job.get("mode") or ""),
				)
				raise
		finally:
			connection.close()


def startBackgroundIndex(force: bool = False) -> bool:
	if os.environ.get(INDEXING_ENV) == "1":
		return False
	if indexIsRunning():
		return False
	if not force and indexHasRows():
		return False
	command: list[str] | None = _indexCommand()
	if command is None:
		logError("CLI fastonedrive não encontrado para indexar em segundo plano")
		return False
	config.ensureDataDir()
	handle = config.indexJobLogPath().open("ab")
	try:
		try:
			subprocess.Popen(
				command,
				stdin=subprocess.DEVNULL,
				stdout=handle,
				stderr=subprocess.STDOUT,
				start_new_session=True,
				close_fds=True,
			)
		except OSError as error:
			logError(f"falha ao arrancar indexação em segundo plano: {error}")
			return False
	finally:
		handle.close()
	logInfo("indexação em segundo plano iniciada")
	return True


def _indexCommand() -> list[str] | None:
	command: list[str] | None = None
	argv0: str = os.path.abspath(sys.argv[0]) if sys.argv else ""
	if argv0 != "" and Path(argv0).is_file() and Path(argv0).name == "fastonedrive":
		command = [argv0, "index"]
	else:
		try:
			command = [config.cliBin(), "index"]
		except FastOneDriveError:
			return None
	profile: config.Profile | None = config.activeProfile()
	if profile is None:
		return command
	return [*command[:-1], "--profile", profile.id, command[-1]]


def refreshParentListing(parentPath: str) -> int:
	if parentPath != "" and not _listedPathIsDirectory(parentPath):
		forgetFileDescendants(parentPath)
		return 0
	remote: str = config.remotePath(parentPath)
	items: list[dict[str, Any]] = list(
		iterLsjson(
			remote,
			recursive=False,
			maxDepth=1,
			useHash=True,
		)
	)
	return upsertLiveChildren(parentPath, items)


def forgetFileDescendants(itemPath: str) -> None:
	if itemPath == "":
		return
	name: str = itemPath.rsplit("/", 1)[-1]
	if name == "":
		return
	prefix: str = f"{_likeEscape(itemPath)}/%"
	connection: sqlite3.Connection = connectDb()
	try:
		rows: list[sqlite3.Row] = list(
			connection.execute(
				"SELECT path FROM files WHERE path LIKE ? ESCAPE '\\'",
				(prefix,),
			)
		)
		paths: list[str] = [str(row["path"]) for row in rows]
		phantom: set[str] = set()
		path: str
		for path in paths:
			extra: str = path[len(itemPath) + 1 :]
			parts: list[str] = [part for part in extra.split("/") if part != ""]
			if len(parts) > 0 and all(part == name for part in parts):
				phantom.add(path)
		doomed: list[str] = []
		for path in phantom:
			childPrefix: str = f"{path}/"
			if any(
				other.startswith(childPrefix) and other not in phantom
				for other in paths
			):
				continue
			doomed.append(path)
		if len(doomed) == 0:
			return
		for path in sorted(doomed, key=len, reverse=True):
			connection.execute("DELETE FROM files WHERE path = ?", (path,))
			connection.execute("DELETE FROM file_chunks WHERE path = ?", (path,))
			connection.execute("DELETE FROM chunk_writes WHERE path = ?", (path,))
		connection.commit()
	finally:
		connection.close()


def _listedPathIsDirectory(itemPath: str) -> bool:
	if isEphemeralPath(itemPath):
		return False
	entry = getEntry(itemPath)
	if entry is not None:
		return bool(entry["isDir"])
	stat: dict[str, Any] | None = remoteStat(config.remotePath(itemPath))
	if stat is None:
		return False
	return bool(stat.get("IsDir"))


def indexHasRows() -> bool:
	if not config.dbPath().is_file():
		return False
	connection: sqlite3.Connection = connectDb()
	try:
		row: sqlite3.Row | None = connection.execute(
			"SELECT 1 FROM files LIMIT 1"
		).fetchone()
		return row is not None
	finally:
		connection.close()


def _listingTtlPath() -> Path:
	return config.dataDir() / "listing-ttl.json"


def _listingDonePath() -> Path:
	return config.dataDir() / "listing-done.json"


def _readListingStamps(stampPath: Path) -> dict[str, float]:
	if not stampPath.is_file():
		return {}
	try:
		raw: Any = json.loads(stampPath.read_text(encoding="utf-8"))
	except (OSError, ValueError, TypeError):
		return {}
	if not isinstance(raw, dict):
		return {}
	stamps: dict[str, float] = {}
	for key, value in raw.items():
		try:
			stamps[str(key)] = float(value)
		except (TypeError, ValueError):
			continue
	return stamps


@contextmanager
def _listingTtlLock() -> Iterator[None]:
	config.ensureDataDir()
	lockPath: Path = config.dataDir() / "listing-ttl.lock"
	handle: TextIO = lockPath.open("a+", encoding="utf-8")
	try:
		fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
		yield
	finally:
		fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
		handle.close()


def _claimListingRefresh(parentPath: str) -> bool:
	now: float = time.time()
	with _listingTtlLock():
		stampPath: Path = _listingTtlPath()
		stamps: dict[str, float] = _readListingStamps(stampPath)
		stamp: float = stamps.get(parentPath, 0.0)
		if now - stamp < config.READDIR_TTL_SEC:
			return False
		cutoff: float = now - 3600.0
		kept: dict[str, float] = {
			key: value for key, value in stamps.items() if value >= cutoff
		}
		kept[parentPath] = now
		tmpPath: Path = stampPath.with_suffix(".json.tmp")
		tmpPath.write_text(json.dumps(kept), encoding="utf-8")
		tmpPath.replace(stampPath)
		return True


def listingCompleted(parentPath: str) -> bool:
	with _listingTtlLock():
		return parentPath in _readListingStamps(_listingDonePath())


def markListingCompleted(parentPath: str) -> None:
	with _listingTtlLock():
		stampPath: Path = _listingDonePath()
		done: dict[str, float] = _readListingStamps(stampPath)
		done[parentPath] = time.time()
		tmpPath: Path = stampPath.with_suffix(".json.tmp")
		tmpPath.write_text(json.dumps(done), encoding="utf-8")
		tmpPath.replace(stampPath)


def ensureFolderListed(parentPath: str) -> None:
	if listingCompleted(parentPath):
		return
	with _profileListingLock():
		if listingCompleted(parentPath):
			return
		refreshParentListing(parentPath)
		markListingCompleted(parentPath)


def _clearListingStamp(parentPath: str) -> None:
	with _listingTtlLock():
		stampPath: Path = _listingTtlPath()
		stamps: dict[str, float] = _readListingStamps(stampPath)
		if parentPath not in stamps:
			return
		stamps.pop(parentPath, None)
		tmpPath: Path = stampPath.with_suffix(".json.tmp")
		tmpPath.write_text(json.dumps(stamps), encoding="utf-8")
		tmpPath.replace(stampPath)


def _listingJobLockPath() -> Path:
	return config.dataDir() / "listing.lock"


LISTING_QUEUE_MAX: int = 64


def _listingQueuePath() -> Path:
	return config.dataDir() / "listing-queue.json"


@contextmanager
def _listingQueueLock() -> Iterator[None]:
	config.ensureDataDir()
	lockPath: Path = config.dataDir() / "listing-queue.lock"
	handle: TextIO = lockPath.open("a+", encoding="utf-8")
	try:
		fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
		yield
	finally:
		fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
		handle.close()


@contextmanager
def _profileListingLock() -> Iterator[None]:
	config.ensureDataDir()
	handle: TextIO = _listingJobLockPath().open("a+", encoding="utf-8")
	try:
		fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
		yield
	finally:
		fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
		handle.close()


def _emptyListingJob() -> dict[str, Any]:
	return {"pid": 0, "born": "", "paths": []}


def _readListingJob() -> dict[str, Any]:
	path: Path = _listingQueuePath()
	if not path.is_file():
		return _emptyListingJob()
	try:
		raw: Any = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, ValueError, TypeError):
		return _emptyListingJob()
	if not isinstance(raw, dict):
		return _emptyListingJob()
	pid: int = 0
	try:
		pid = int(raw.get("pid") or 0)
	except (TypeError, ValueError):
		pid = 0
	born: str = str(raw.get("born") or "")
	paths: list[str] = []
	found: Any = raw.get("paths")
	if isinstance(found, list):
		item: Any
		for item in found:
			text: str = str(item)
			if text not in paths:
				paths.append(text)
	return {"pid": pid, "born": born, "paths": paths}


def _writeListingJob(job: dict[str, Any]) -> None:
	path: Path = _listingQueuePath()
	tmpPath: Path = path.with_suffix(".json.tmp")
	tmpPath.write_text(json.dumps(job), encoding="utf-8")
	tmpPath.replace(path)


def _processStart(pid: int) -> str:
	if pid <= 0:
		return ""
	try:
		stat: str = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
	except OSError:
		return ""
	mark: int = stat.rfind(")")
	if mark < 0:
		return ""
	fields: list[str] = stat[mark + 2 :].split()
	if len(fields) < 20:
		return ""
	return fields[19]


def _processAlive(pid: int) -> bool:
	if pid <= 0:
		return False
	try:
		os.kill(pid, 0)
	except OSError:
		return False
	try:
		stat: str = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
	except OSError:
		return False
	mark: int = stat.rfind(")")
	if mark < 0 or mark + 2 >= len(stat):
		return True
	if stat[mark + 2] != "Z":
		return True
	try:
		os.waitpid(pid, os.WNOHANG)
	except OSError:
		pass
	return False


def _listingOwnerAlive(pid: int, born: str) -> bool:
	watched: int = -pid if pid < 0 else pid
	if not _processAlive(watched):
		return False
	current: str = _processStart(watched)
	if current == "" or born == "":
		return True
	return current == born


def _enqueueListingPath(parentPath: str, claimWorker: bool) -> bool:
	with _listingQueueLock():
		job: dict[str, Any] = _readListingJob()
		paths: list[str] = [
			item for item in list(job["paths"]) if item != parentPath
		]
		paths.insert(0, parentPath)
		if len(paths) > LISTING_QUEUE_MAX:
			del paths[LISTING_QUEUE_MAX:]
		job["paths"] = paths
		spawn: bool = False
		if claimWorker and not _listingOwnerAlive(int(job["pid"]), str(job["born"])):
			job["pid"] = -os.getpid()
			job["born"] = _processStart(os.getpid())
			spawn = True
		_writeListingJob(job)
		return spawn


def _dropListingClaim(parentPath: str, placeholder: int) -> None:
	with _listingQueueLock():
		job: dict[str, Any] = _readListingJob()
		if int(job["pid"]) == placeholder:
			job["pid"] = 0
			job["born"] = ""
		job["paths"] = [
			item for item in list(job["paths"]) if item != parentPath
		]
		_writeListingJob(job)


def _publishListingWorker(childPid: int, placeholder: int) -> None:
	with _listingQueueLock():
		job: dict[str, Any] = _readListingJob()
		if int(job["pid"]) == placeholder:
			job["pid"] = childPid
			job["born"] = _processStart(childPid)
			_writeListingJob(job)


def _claimListingWorker(parentPath: str) -> bool:
	myPid: int = os.getpid()
	with _listingQueueLock():
		job: dict[str, Any] = _readListingJob()
		paths: list[str] = list(job["paths"])
		if parentPath not in paths:
			paths.append(parentPath)
		job["paths"] = paths
		owner: int = int(job["pid"])
		if (
			owner == myPid
			or owner == -os.getppid()
			or not _listingOwnerAlive(owner, str(job["born"]))
		):
			job["pid"] = myPid
			job["born"] = _processStart(myPid)
			_writeListingJob(job)
			return True
		_writeListingJob(job)
		return False


def _popListingPath() -> str | None:
	with _listingQueueLock():
		job: dict[str, Any] = _readListingJob()
		if int(job["pid"]) != os.getpid():
			return None
		paths: list[str] = list(job["paths"])
		if len(paths) == 0:
			job["pid"] = 0
			job["born"] = ""
			_writeListingJob(job)
			return None
		parentPath: str = str(paths.pop(0))
		job["paths"] = paths
		_writeListingJob(job)
		return parentPath


def _releaseListingWorkerIfIdle() -> None:
	with _listingQueueLock():
		job: dict[str, Any] = _readListingJob()
		if int(job["pid"]) != os.getpid():
			return
		if len(list(job["paths"])) > 0:
			return
		job["pid"] = 0
		job["born"] = ""
		_writeListingJob(job)


def scheduleParentRefresh(parentPath: str) -> None:
	with _listingLock:
		if parentPath in _listingBusy:
			return
	if not _claimListingRefresh(parentPath):
		return
	placeholder: int = -os.getpid()
	try:
		spawn: bool = _enqueueListingPath(parentPath, True)
	except Exception:
		_clearListingStamp(parentPath)
		raise
	if not spawn:
		return
	childPid: int | None = _spawnListingRefresh(parentPath)
	if childPid is None:
		_dropListingClaim(parentPath, placeholder)
		_clearListingStamp(parentPath)
		return
	_publishListingWorker(childPid, placeholder)


def runParentRefresh(parentPath: str) -> None:
	if not _claimListingWorker(parentPath):
		return
	try:
		_drainListingQueue()
	finally:
		_releaseListingWorkerIfIdle()


def _drainListingQueue() -> None:
	while True:
		parentPath: str | None = _popListingPath()
		if parentPath is None:
			return
		_refreshQueuedPath(parentPath)


def _refreshParentCommand(parentPath: str) -> list[str] | None:
	binary: str | None = None
	argv0: str = os.path.abspath(sys.argv[0]) if sys.argv else ""
	if argv0 != "" and Path(argv0).is_file() and Path(argv0).name == "fastonedrive":
		binary = argv0
	else:
		try:
			binary = config.cliBin()
		except FastOneDriveError:
			return None
	command: list[str] = [binary]
	profile: config.Profile | None = config.activeProfile()
	if profile is not None:
		command.extend(["--profile", profile.id])
	command.extend(["refresh-parent", "--", parentPath])
	return command


def _spawnListingRefresh(parentPath: str) -> int | None:
	command: list[str] | None = _refreshParentCommand(parentPath)
	if command is None:
		logError("CLI fastonedrive não encontrado para actualizar a pasta")
		return None
	try:
		process: subprocess.Popen[bytes] = subprocess.Popen(
			command,
			stdin=subprocess.DEVNULL,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			start_new_session=True,
			close_fds=True,
		)
	except OSError as error:
		logError(f"falha ao actualizar a pasta em segundo plano: {error}")
		return None
	return int(process.pid)


def invalidateListing(parentPath: str) -> None:
	with _listingLock:
		_listingStamp.pop(parentPath, None)
	_clearListingStamp(parentPath)


def _refreshQueuedPath(parentPath: str) -> None:
	listed: bool = False
	try:
		with _listingLock:
			_listingBusy.add(parentPath)
		before: set[tuple[str, int, int, str, int, str]] = _childSignature(parentPath)
		with _profileListingLock():
			refreshParentListing(parentPath)
		listed = True
		after: set[tuple[str, int, int, str, int, str]] = _childSignature(parentPath)
		markListingCompleted(parentPath)
		if before != after:
			from fastonedrive.notify import notifyDirChanged

			notifyDirChanged(parentPath)
	except Exception as error:
		logError(
			f"listagem remota em segundo plano falhou: {error}",
			extra={"parent": parentPath},
		)
	finally:
		with _listingLock:
			_listingBusy.discard(parentPath)
			if not listed:
				_listingStamp.pop(parentPath, None)
		if not listed:
			_clearListingStamp(parentPath)


def _childSignature(parentPath: str) -> set[tuple[str, int, int, str, int, str]]:
	connection: sqlite3.Connection = connectDb()
	try:
		rows: list[sqlite3.Row] = list(
			connection.execute(
				"""
				SELECT name, isDir, size, modTime, pinned, syncState
				FROM files
				WHERE parent = ?
				""",
				(parentPath,),
			)
		)
		return {
			(
				str(row["name"]),
				int(row["isDir"]),
				int(row["size"]),
				str(row["modTime"] or ""),
				int(row["pinned"]),
				str(row["syncState"] or ""),
			)
			for row in rows
		}
	finally:
		connection.close()


def upsertLiveChildren(parentPath: str, items: list[dict[str, Any]]) -> int:
	updatedAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
	batch: list[tuple[Any, ...]] = []
	seen: set[str] = set()
	for item in items:
		row: tuple[Any, ...] | None = _rowFromLsjson(
			item,
			parentPath,
			updatedAt,
		)
		if row is None:
			continue
		batch.append(row)
		seen.add(str(row[0]))
	incomingById: dict[str, str] = {}
	for rowTuple in batch:
		itemId: str = str(rowTuple[8] or "")
		if itemId != "":
			incomingById[itemId] = str(rowTuple[0])
	connection: sqlite3.Connection = connectDb()
	try:
		existing: list[sqlite3.Row] = list(
			connection.execute(
				"SELECT path, itemId FROM files WHERE parent = ?",
				(parentPath,),
			)
		)
		relocated: set[str] = _relocateKnownIds(connection, batch, updatedAt)
		if len(seen) > 0:
			for row in existing:
				child: str = str(row["path"])
				if child in seen or child in relocated:
					continue
				foundId: str = str(row["itemId"] or "")
				moved: str = incomingById.get(foundId, "")
				if foundId == "" or moved == "" or moved == child:
					continue
				_rewriteDescendantPaths(connection, child, moved, updatedAt)
				relocated.add(child)
		if batch:
			connection.executemany(UPSERT_SQL, batch)
		if not seen and existing:
			logInfo(
				"lsjson sem filhos, a não apagar o índice",
				extra={"parent": parentPath},
			)
		else:
			for row in existing:
				child = str(row["path"])
				if child in seen or child in relocated:
					continue
				if _underAny(child, relocated):
					continue
				if str(row["itemId"] or "") != "":
					continue
				_releaseVanishedTree(connection, child, keepFiltered=True)
		connection.commit()
	finally:
		connection.close()
	return len(batch)


def listPinned() -> list[sqlite3.Row]:
	connection: sqlite3.Connection = connectDb()
	try:
		return list(
			connection.execute(
				"""
				SELECT """ + FILE_SELECT + """
				FROM files
				WHERE pinned = 1
				ORDER BY path
				"""
			)
		)
	finally:
		connection.close()


def hasDescendants(itemPath: str) -> bool:
	if itemPath == "":
		return False
	prefix: str = f"{_likeEscape(itemPath)}/%"
	connection: sqlite3.Connection = connectDb()
	try:
		row: sqlite3.Row | None = connection.execute(
			"SELECT 1 FROM files WHERE path LIKE ? ESCAPE '\\' LIMIT 1",
			(prefix,),
		).fetchone()
		return row is not None
	finally:
		connection.close()


def listDirtyPaths(root: str) -> list[str]:
	connection: sqlite3.Connection = connectDb()
	try:
		if root == "":
			rows: list[sqlite3.Row] = list(
				connection.execute(
					"""
					SELECT path FROM files
					WHERE syncState = ?
					ORDER BY length(path), path
					""",
					(SYNC_DIRTY,),
				)
			)
		else:
			prefix: str = f"{_likeEscape(root)}/%"
			rows = list(
				connection.execute(
					"""
					SELECT path FROM files
					WHERE syncState = ? AND path LIKE ? ESCAPE '\\'
					ORDER BY length(path), path
					""",
					(SYNC_DIRTY, prefix),
				)
			)
		return [str(row["path"]) for row in rows]
	finally:
		connection.close()


def listDescendants(itemPath: str) -> list[sqlite3.Row]:
	prefix: str = f"{_likeEscape(itemPath)}/%"
	connection: sqlite3.Connection = connectDb()
	try:
		return list(
			connection.execute(
				"""
				SELECT """ + FILE_SELECT + """
				FROM files
				WHERE path = ? OR path LIKE ? ESCAPE '\\'
				ORDER BY path
				""",
				(itemPath, prefix),
			)
		)
	finally:
		connection.close()


def storedUnixMode(row: sqlite3.Row) -> int | None:
	try:
		value: object = row["unixMode"]
	except (IndexError, KeyError):
		return None
	if value is None:
		return None
	return int(value) & 0o777


def setUnixMode(itemPath: str, mode: int) -> None:
	if itemPath == "":
		raise FastOneDriveError("não é possível alterar atributos da raiz")
	stored: int = int(mode) & 0o777
	connection: sqlite3.Connection = connectDb()
	try:
		cursor: sqlite3.Cursor = connection.execute(
			"UPDATE files SET unixMode = ? WHERE path = ?",
			(stored, itemPath),
		)
		connection.commit()
		if cursor.rowcount == 0:
			raise FastOneDriveError(f"item não encontrado no índice: {itemPath}")
	finally:
		connection.close()


def updateFileMeta(itemPath: str, size: int, modTime: str, etag: str) -> None:
	updatedAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
	connection: sqlite3.Connection = connectDb()
	try:
		connection.execute(
			"""
			UPDATE files
			SET size = ?, modTime = ?, etag = ?, updatedAt = ?
			WHERE path = ?
			""",
			(size, modTime, etag, updatedAt, itemPath),
		)
		connection.commit()
	finally:
		connection.close()


def getEntry(itemPath: str) -> sqlite3.Row | None:
	connection: sqlite3.Connection = connectDb()
	try:
		return connection.execute(
			f"SELECT {FILE_SELECT} FROM files WHERE path = ?",
			(itemPath,),
		).fetchone()
	finally:
		connection.close()


def _rowText(row: sqlite3.Row, key: str) -> str:
	try:
		value: object = row[key]
	except (IndexError, KeyError):
		return ""
	return str(value or "")


def _moveRowPayload(
	row: sqlite3.Row,
	newPath: str,
	name: str,
	updatedAt: str,
) -> tuple[Any, ...]:
	return (
		newPath,
		name,
		parentPath(newPath),
		int(row["isDir"]),
		int(row["size"]),
		str(row["modTime"] or ""),
		str(row["mimeType"] or ""),
		str(row["etag"] or ""),
		str(row["itemId"] or ""),
		int(row["pinned"]),
		updatedAt,
		_rowText(row, "baseModTime"),
		_rowText(row, "baseEtag"),
		_rowText(row, "syncState"),
		_rowText(row, "syncError"),
		storedUnixMode(row),
	)


def markDirty(itemPath: str) -> None:
	if itemPath == "":
		return
	setSyncState(itemPath, SYNC_DIRTY)


def setSyncState(itemPath: str, state: str, error: str = "") -> None:
	if itemPath == "":
		return
	updatedAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
	connection: sqlite3.Connection = connectDb()
	try:
		connection.execute(
			"""
			UPDATE files
			SET syncState = ?, syncError = ?, updatedAt = ?
			WHERE path = ?
			""",
			(state, error, updatedAt, itemPath),
		)
		connection.commit()
	finally:
		connection.close()
	if state in (SYNC_ERROR, SYNC_CONFLICT):
		from fastonedrive.notify import notifyDirChanged

		notifyDirChanged(itemPath)


def setBaseline(
	itemPath: str,
	modTime: str,
	etag: str,
	size: int | None = None,
	keepSyncState: bool = False,
) -> None:
	if itemPath == "":
		return
	updatedAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
	connection: sqlite3.Connection = connectDb()
	try:
		if keepSyncState and size is None:
			connection.execute(
				"""
				UPDATE files
				SET baseModTime = ?, baseEtag = ?, modTime = ?, etag = ?,
					syncError = '', updatedAt = ?
				WHERE path = ?
				""",
				(modTime, etag, modTime, etag, updatedAt, itemPath),
			)
		elif keepSyncState:
			connection.execute(
				"""
				UPDATE files
				SET baseModTime = ?, baseEtag = ?, modTime = ?, etag = ?, size = ?,
					syncError = '', updatedAt = ?
				WHERE path = ?
				""",
				(modTime, etag, modTime, etag, size, updatedAt, itemPath),
			)
		elif size is None:
			connection.execute(
				"""
				UPDATE files
				SET baseModTime = ?, baseEtag = ?, modTime = ?, etag = ?,
					syncState = ?, syncError = '', updatedAt = ?
				WHERE path = ?
				""",
				(modTime, etag, modTime, etag, SYNC_SYNCED, updatedAt, itemPath),
			)
		else:
			connection.execute(
				"""
				UPDATE files
				SET baseModTime = ?, baseEtag = ?, modTime = ?, etag = ?, size = ?,
					syncState = ?, syncError = '', updatedAt = ?
				WHERE path = ?
				""",
				(
					modTime,
					etag,
					modTime,
					etag,
					size,
					SYNC_SYNCED,
					updatedAt,
					itemPath,
				),
			)
		connection.commit()
	finally:
		connection.close()


def clearItemBaseline(itemPath: str) -> None:
	if itemPath == "":
		return
	connection: sqlite3.Connection = connectDb()
	try:
		connection.execute(
			"UPDATE files SET baseModTime = '', baseEtag = '' WHERE path = ?",
			(itemPath,),
		)
		connection.commit()
	finally:
		connection.close()


def detachLocalIdentity(itemPath: str) -> None:
	if itemPath == "":
		return
	prefix: str = f"{_likeEscape(itemPath)}/%"
	updatedAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
	connection: sqlite3.Connection = connectDb()
	try:
		connection.execute(
			"""
			UPDATE files
			SET itemId = '',
				baseModTime = '',
				baseEtag = '',
				syncState = CASE WHEN isDir = 1 THEN '' ELSE ? END,
				updatedAt = ?
			WHERE path = ? OR path LIKE ? ESCAPE '\\'
			""",
			(SYNC_DIRTY, updatedAt, itemPath, prefix),
		)
		connection.commit()
	finally:
		connection.close()


def clearBaselineTree(itemPath: str) -> None:
	if itemPath == "":
		return
	prefix: str = f"{_likeEscape(itemPath)}/%"
	connection: sqlite3.Connection = connectDb()
	try:
		connection.execute(
			"""
			UPDATE files
			SET baseModTime = '', baseEtag = ''
			WHERE path = ? OR path LIKE ? ESCAPE '\\'
			""",
			(itemPath, prefix),
		)
		connection.commit()
	finally:
		connection.close()


def setPinnedTree(itemPath: str, pinned: bool) -> int:
	prefix: str = f"{_likeEscape(itemPath)}/%"
	updatedAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
	connection: sqlite3.Connection = connectDb()
	try:
		cursor: sqlite3.Cursor = connection.execute(
			"""
			UPDATE files
			SET pinned = ?, updatedAt = ?
			WHERE path = ? OR path LIKE ? ESCAPE '\\'
			""",
			(1 if pinned else 0, updatedAt, itemPath, prefix),
		)
		connection.commit()
		return int(cursor.rowcount)
	finally:
		connection.close()


def setPinned(itemPath: str, pinned: bool) -> bool:
	updatedAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
	connection: sqlite3.Connection = connectDb()
	try:
		cursor: sqlite3.Cursor = connection.execute(
			"UPDATE files SET pinned = ?, updatedAt = ? WHERE path = ?",
			(1 if pinned else 0, updatedAt, itemPath),
		)
		connection.commit()
		return cursor.rowcount > 0
	finally:
		connection.close()


def parentPath(itemPath: str) -> str:
	if "/" not in itemPath:
		return ""
	return itemPath.rsplit("/", 1)[0]


def isPinnedHereOrAbove(itemPath: str) -> bool:
	current: str = itemPath
	while current != "":
		entry = getEntry(current)
		if entry is not None and bool(entry["pinned"]):
			return True
		current = parentPath(current)
	return False


def upsertEntry(
	itemPath: str,
	name: str,
	isDir: bool,
	size: int,
	pinned: bool | None = None,
	modTime: str = "",
	mimeType: str = "",
	etag: str = "",
	itemId: str = "",
) -> None:
	if itemPath == "":
		raise FastOneDriveError("não é possível gravar a raiz no índice")
	parent: str = parentPath(itemPath)
	updatedAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
	existing = getEntry(itemPath)
	pinnedValue: int
	if pinned is not None:
		pinnedValue = 1 if pinned else 0
	elif existing is not None:
		pinnedValue = 1 if bool(existing["pinned"]) else 0
	else:
		pinnedValue = 0
	modTimeValue: str = modTime
	mimeValue: str = mimeType
	etagValue: str = etag
	itemIdValue: str = itemId
	if itemIdValue == "" and etag != "" and not isContentToken(etag):
		itemIdValue = etag
	if existing is not None:
		if modTimeValue == "":
			modTimeValue = str(existing["modTime"] or "")
		if mimeValue == "":
			mimeValue = str(existing["mimeType"] or "")
		if etagValue == "":
			etagValue = str(existing["etag"] or "")
		if itemIdValue == "":
			itemIdValue = str(existing["itemId"] or "")
	baseMod: str = ""
	baseTag: str = ""
	if existing is not None:
		baseMod = _rowText(existing, "baseModTime")
		baseTag = _rowText(existing, "baseEtag")
	connection: sqlite3.Connection = connectDb()
	try:
		connection.execute(
			"""
			INSERT INTO files (
				path, name, parent, isDir, size, modTime, mimeType, etag, itemId, pinned, updatedAt,
				baseModTime, baseEtag, syncState, syncError
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '')
			ON CONFLICT(path) DO UPDATE SET
				name = excluded.name,
				parent = excluded.parent,
				isDir = excluded.isDir,
				size = excluded.size,
				modTime = excluded.modTime,
				mimeType = excluded.mimeType,
				etag = excluded.etag,
				itemId = CASE WHEN excluded.itemId = '' THEN files.itemId ELSE excluded.itemId END,
				pinned = excluded.pinned,
				updatedAt = excluded.updatedAt
			""",
			(
				itemPath,
				name,
				parent,
				1 if isDir else 0,
				size,
				modTimeValue,
				mimeValue,
				etagValue,
				itemIdValue,
				pinnedValue,
				updatedAt,
				baseMod,
				baseTag,
			),
		)
		connection.commit()
	finally:
		connection.close()


def _retargetChunks(connection: sqlite3.Connection, oldPath: str, newPath: str) -> None:
	prefix: str = f"{_likeEscape(oldPath)}/%"
	rows: list[sqlite3.Row] = list(
		connection.execute(
			"""
			SELECT path FROM file_chunks
			WHERE path = ? OR path LIKE ? ESCAPE '\\'
			UNION
			SELECT path FROM chunk_writes
			WHERE path = ? OR path LIKE ? ESCAPE '\\'
			""",
			(oldPath, prefix, oldPath, prefix),
		)
	)
	row: sqlite3.Row
	for row in rows:
		path: str = str(row["path"])
		moved: str = newPath if path == oldPath else f"{newPath}{path[len(oldPath):]}"
		if moved != path:
			connection.execute("DELETE FROM file_chunks WHERE path = ?", (moved,))
			connection.execute("DELETE FROM chunk_writes WHERE path = ?", (moved,))
		connection.execute(
			"UPDATE file_chunks SET path = ? WHERE path = ?",
			(moved, path),
		)
		connection.execute(
			"UPDATE chunk_writes SET path = ? WHERE path = ?",
			(moved, path),
		)


def deleteIndexTree(itemPath: str) -> int:
	if itemPath == "":
		raise FastOneDriveError("não é possível apagar a raiz do índice")
	prefix: str = f"{_likeEscape(itemPath)}/%"
	connection: sqlite3.Connection = connectDb()
	try:
		cursor: sqlite3.Cursor = connection.execute(
			"DELETE FROM files WHERE path = ? OR path LIKE ? ESCAPE '\\'",
			(itemPath, prefix),
		)
		connection.execute(
			"DELETE FROM file_chunks WHERE path = ? OR path LIKE ? ESCAPE '\\'",
			(itemPath, prefix),
		)
		connection.execute(
			"DELETE FROM chunk_writes WHERE path = ? OR path LIKE ? ESCAPE '\\'",
			(itemPath, prefix),
		)
		connection.commit()
		return int(cursor.rowcount)
	finally:
		connection.close()


def renameIndexPath(oldPath: str, newPath: str) -> None:
	if oldPath == "":
		raise FastOneDriveError("não é possível renomear a raiz do índice")
	if newPath == "":
		raise FastOneDriveError("destino de rename vazio")
	if oldPath == newPath:
		return
	if newPath.startswith(f"{oldPath}/"):
		raise FastOneDriveError("não é possível mover para dentro de si")
	rows: list[sqlite3.Row] = listDescendants(oldPath)
	if not rows:
		raise FastOneDriveError(f"item não encontrado no índice: {oldPath}")
	payload: list[tuple[Any, ...]] = []
	updatedAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
	for row in rows:
		path: str = str(row["path"])
		newItem: str
		if path == oldPath:
			newItem = newPath
		else:
			newItem = f"{newPath}{path[len(oldPath):]}"
		parent: str = parentPath(newItem)
		payload.append(
			_moveRowPayload(
				row,
				newItem,
				str(row["name"]) if path != oldPath else newPath.rsplit("/", 1)[-1],
				updatedAt,
			)
		)
	connection: sqlite3.Connection = connectDb()
	try:
		prefix: str = f"{_likeEscape(newPath)}/%"
		connection.execute(
			"DELETE FROM files WHERE path = ? OR path LIKE ? ESCAPE '\\'",
			(newPath, prefix),
		)
		oldPrefix: str = f"{_likeEscape(oldPath)}/%"
		connection.execute(
			"DELETE FROM files WHERE path = ? OR path LIKE ? ESCAPE '\\'",
			(oldPath, oldPrefix),
		)
		connection.executemany(FILE_INSERT_SQL, payload)
		_deleteForeignChunks(connection, newPath, oldPath)
		_retargetChunks(connection, oldPath, newPath)
		connection.commit()
	finally:
		connection.close()


def upsertPinned(
	itemPath: str,
	pinned: bool,
	name: str,
	isDir: bool,
	size: int,
) -> None:
	parent: str
	if "/" in itemPath:
		parent = itemPath.rsplit("/", 1)[0]
	else:
		parent = ""
	updatedAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
	connection: sqlite3.Connection = connectDb()
	try:
		connection.execute(
			"""
			INSERT INTO files (
				path, name, parent, isDir, size, modTime, mimeType, etag, pinned, updatedAt
			) VALUES (?, ?, ?, ?, ?, '', '', '', ?, ?)
			ON CONFLICT(path) DO UPDATE SET
				pinned = excluded.pinned,
				name = excluded.name,
				isDir = excluded.isDir,
				size = excluded.size,
				updatedAt = excluded.updatedAt
			""",
			(
				itemPath,
				name,
				parent,
				1 if isDir else 0,
				size,
				1 if pinned else 0,
				updatedAt,
			),
		)
		connection.commit()
	finally:
		connection.close()


def indexStats() -> dict[str, Any]:
	db: Path = config.dbPath()
	if not db.is_file():
		return {
			"exists": False,
			"files": 0,
			"dirs": 0,
			"updatedAt": None,
		}
	connection: sqlite3.Connection = connectDb()
	try:
		files: int = int(
			connection.execute("SELECT COUNT(*) FROM files WHERE isDir = 0").fetchone()[0]
		)
		dirs: int = int(
			connection.execute("SELECT COUNT(*) FROM files WHERE isDir = 1").fetchone()[0]
		)
		updatedRow: sqlite3.Row | None = connection.execute(
			"SELECT MAX(updatedAt) AS updatedAt FROM files"
		).fetchone()
		updatedAt: str | None = updatedRow["updatedAt"] if updatedRow is not None else None
		return {
			"exists": True,
			"files": files,
			"dirs": dirs,
			"updatedAt": updatedAt,
		}
	finally:
		connection.close()


def indexProgress() -> dict[str, Any]:
	stats: dict[str, Any] = indexStats()
	job: dict[str, Any] = _readIndexJobFile()
	running: bool = indexIsRunning()
	error: str = str(job.get("error") or "")
	finishedAt: str = str(job.get("finishedAt") or "")
	state: str
	if running:
		state = "running"
	elif error != "":
		state = "error"
	elif finishedAt != "":
		state = "done"
	elif not bool(stats["exists"]):
		state = "empty"
	else:
		state = "idle"
	return {
		"state": state,
		"running": running,
		"root": str(job.get("root") or config.remoteRoot()),
		"counted": int(job.get("counted") or 0),
		"mode": str(job.get("mode") or ""),
		"startedAt": str(job.get("startedAt") or "") or None,
		"finishedAt": finishedAt or None,
		"updatedAt": str(job.get("updatedAt") or "") or None,
		"error": error or None,
		"exists": bool(stats["exists"]),
		"files": int(stats["files"]),
		"dirs": int(stats["dirs"]),
		"indexUpdatedAt": stats["updatedAt"],
	}


def _readIndexJobFile() -> dict[str, Any]:
	path: Path = config.indexStatusPath()
	if not path.is_file():
		return {}
	try:
		payload: Any = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		return {}
	if not isinstance(payload, dict):
		return {}
	return payload


def _setIndexJobStatus(
	root: str,
	counted: int,
	startedAt: str,
	finishedAt: str | None = None,
	error: str | None = None,
	mode: str = "",
) -> None:
	config.ensureDataDir()
	path: Path = config.indexStatusPath()
	tmp: Path = path.with_name(f"{path.name}.tmp")
	payload: dict[str, Any] = {
		"root": root,
		"counted": counted,
		"startedAt": startedAt,
		"finishedAt": finishedAt,
		"error": error,
		"mode": mode,
		"updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
	}
	tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
	tmp.replace(path)


def _ensureFilesTable(connection: sqlite3.Connection) -> None:
	row: sqlite3.Row | None = connection.execute(
		"SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'files'"
	).fetchone()
	if row is not None:
		return
	connection.executescript(SCHEMA_SQL)
	_createNameIndexes(connection, "files")
	connection.commit()


def _migrateSchemaIfNeeded(connection: sqlite3.Connection) -> None:
	version: int = int(connection.execute("PRAGMA user_version").fetchone()[0])
	if version >= SCHEMA_VERSION:
		return
	_migrateSchema(connection)
	connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
	connection.commit()


def _migrateSchema(connection: sqlite3.Connection) -> None:
	columns: list[str] = [
		str(row[1]) for row in connection.execute("PRAGMA table_info(files)")
	]
	if "itemId" not in columns:
		connection.execute(
			"ALTER TABLE files ADD COLUMN itemId TEXT NOT NULL DEFAULT ''"
		)
	if "baseModTime" not in columns:
		connection.execute(
			"ALTER TABLE files ADD COLUMN baseModTime TEXT NOT NULL DEFAULT ''"
		)
	if "baseEtag" not in columns:
		connection.execute(
			"ALTER TABLE files ADD COLUMN baseEtag TEXT NOT NULL DEFAULT ''"
		)
	if "syncState" not in columns:
		connection.execute(
			"ALTER TABLE files ADD COLUMN syncState TEXT NOT NULL DEFAULT ''"
		)
	if "syncError" not in columns:
		connection.execute(
			"ALTER TABLE files ADD COLUMN syncError TEXT NOT NULL DEFAULT ''"
		)
	if "unixMode" not in columns:
		connection.execute("ALTER TABLE files ADD COLUMN unixMode INTEGER")
	connection.execute(
		"""
		UPDATE files SET itemId = etag
		WHERE itemId = '' AND etag != ''
			AND etag NOT LIKE 'qxh:%'
			AND etag NOT LIKE 'sha1:%'
			AND etag NOT LIKE 'mt:%'
		"""
	)
	_migrateBaselineTokens(connection)
	_clearDuplicateItemIds(connection)
	connection.execute(
		"CREATE UNIQUE INDEX IF NOT EXISTS idx_files_itemId ON files(itemId) WHERE itemId != ''"
	)
	connection.executescript(DELTA_STATE_SQL)
	connection.executescript(FILE_CHUNKS_SQL)
	connection.executescript(CHUNK_WRITES_SQL)
	connection.commit()


def _migrateBaselineTokens(connection: sqlite3.Connection) -> None:
	rows: list[sqlite3.Row] = list(
		connection.execute(
			"SELECT path, baseEtag, isDir FROM files"
		)
	)
	row: sqlite3.Row
	for row in rows:
		base: str = str(row["baseEtag"] or "")
		path: str = str(row["path"])
		isDir: bool = int(row["isDir"] or 0) == 1
		if isDir:
			if base.startswith("migrated:"):
				connection.execute(
					"UPDATE files SET baseEtag = '', baseModTime = '' WHERE path = ?",
					(path,),
				)
			continue
		if base == "" or isContentToken(base) or base.startswith("migrated:"):
			continue
		stamp: str = _diskModStamp(connection, path)
		connection.execute(
			"UPDATE files SET baseEtag = ?, baseModTime = ? WHERE path = ?",
			("migrated:", stamp, path),
		)


def _cacheHasHoles(connection: sqlite3.Connection, itemPath: str) -> bool:
	found: sqlite3.Row | None = connection.execute(
		"SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'file_chunks'"
	).fetchone()
	if found is None:
		return False
	row: sqlite3.Row | None = connection.execute(
		"SELECT 1 FROM file_chunks WHERE path = ?",
		(itemPath,),
	).fetchone()
	return row is not None


def _diskModStamp(connection: sqlite3.Connection, itemPath: str) -> str:
	from fastonedrive.search import cacheLocalPath, pinnedLocalPath

	pinned: Path = pinnedLocalPath(itemPath)
	cached: Path = cacheLocalPath(itemPath)
	holes: bool = _cacheHasHoles(connection, itemPath)
	local: Path
	for local in (pinned, cached):
		if not local.is_file():
			continue
		if holes and local == cached:
			continue
		when: datetime = datetime.fromtimestamp(local.stat().st_mtime, tz=timezone.utc)
		return when.isoformat()
	return ""


def _clearDuplicateItemIds(connection: sqlite3.Connection) -> None:
	duplicates: list[str] = [
		str(row[0])
		for row in connection.execute(
			"""
			SELECT itemId FROM files
			WHERE itemId != ''
			GROUP BY itemId
			HAVING COUNT(*) > 1
			"""
		)
	]
	for itemId in duplicates:
		paths: list[str] = [
			str(row[0])
			for row in connection.execute(
				"SELECT path FROM files WHERE itemId = ? ORDER BY path",
				(itemId,),
			)
		]
		for extraPath in paths[1:]:
			connection.execute(
				"UPDATE files SET itemId = '' WHERE path = ?",
				(extraPath,),
			)


def _createNameIndexes(connection: sqlite3.Connection, table: str) -> None:
	if table not in ("files", "files_new"):
		raise FastOneDriveError("tabela de índice inválida")
	connection.execute(
		f"CREATE INDEX IF NOT EXISTS idx_{table}_name ON {table}(name COLLATE NOCASE)"
	)
	connection.execute(
		f"CREATE INDEX IF NOT EXISTS idx_{table}_parent ON {table}(parent)"
	)


def _rebuild(connection: sqlite3.Connection, root: str, startedAt: str) -> int:
	_ensureFilesTable(connection)
	_migrateSchema(connection)
	_createNameIndexes(connection, "files")
	prefix: str = _pathPrefix(root)
	if prefix != "":
		return _rebuildRclone(
			connection,
			root,
			startedAt,
			"rclone",
			False,
		)
	try:
		return _rebuildGraph(connection, root, startedAt)
	except GraphDeltaExpired:
		connection.execute("DELETE FROM delta_state")
		connection.commit()
		logInfo("token de delta expirado; a fazer enumeração completa")
		try:
			return _rebuildGraph(connection, root, startedAt)
		except GraphDeltaError as error:
			logError(f"Graph indisponível, a usar rclone: {error}")
			return _rebuildRclone(
				connection,
				root,
				startedAt,
				"rclone",
				True,
			)
	except GraphDeltaError as error:
		logError(f"Graph indisponível, a usar rclone: {error}")
		return _rebuildRclone(
			connection,
			root,
			startedAt,
			"rclone",
			True,
		)


def _rebuildGraph(connection: sqlite3.Connection, root: str, startedAt: str) -> int:
	state: dict[str, str] | None = _loadDeltaState(connection, "root")
	deltaLink: str = str(state["deltaLink"]) if state is not None else ""
	mode: str = "incremental" if deltaLink != "" else "completo"
	_setIndexJobStatus(root, 0, startedAt, mode=mode)
	ids: dict[str, str] = _loadItemIdMap(connection)
	parents: dict[str, str] = _loadParentIdMap(connection)
	seen: set[str] = set()
	count: int = 0
	shared: dict[str, tuple[str, str, str]] = _loadSharedScopes(connection)
	pages, holder = iterDriveDelta(deltaLink)
	count, shared, orphans = _applyDeltaFeed(
		connection,
		pages,
		ids,
		parents,
		seen,
		count,
		root,
		startedAt,
		mode,
		"",
		"",
		shared,
	)
	incomplete: bool = orphans > 0
	newRootLink: str = holder[0]
	if orphans == 0:
		if newRootLink == "":
			raise GraphDeltaExpired("delta sem deltaLink")
		_saveDeltaState(connection, "root", "", "", newRootLink, "")
	else:
		logError(
			"delta com itens por resolver; cursor mantido",
			extra={"orphans": orphans, "scope": "root"},
		)
	for scope, (driveId, remoteId, prefixPath) in list(shared.items()):
		sharedState: dict[str, str] | None = _loadDeltaState(connection, scope)
		sharedLink: str = str(sharedState["deltaLink"]) if sharedState is not None else ""
		try:
			sharedPages, sharedHolder = iterItemDelta(driveId, remoteId, sharedLink)
			count, shared, sharedOrphans = _applyDeltaFeed(
				connection,
				sharedPages,
				ids,
				parents,
				seen,
				count,
				root,
				startedAt,
				mode,
				prefixPath,
				remoteId,
				shared,
			)
		except GraphDeltaExpired:
			raise
		except GraphDeltaError as error:
			if _sharedScopeMissing(error):
				_dropDeltaScope(connection, scope)
				shared.pop(scope, None)
				logInfo(
					"pasta partilhada já não está no delta",
					extra={"scope": scope},
				)
				continue
			raise
		if sharedOrphans == 0:
			sharedLinkNext: str = sharedHolder[0]
			if sharedLinkNext == "":
				incomplete = True
				logError(
					"delta da pasta partilhada sem deltaLink",
					extra={"scope": scope},
				)
				if sharedLink == "":
					_saveDeltaState(
						connection,
						scope,
						driveId,
						remoteId,
						"",
						prefixPath,
					)
			else:
				_saveDeltaState(
					connection,
					scope,
					driveId,
					remoteId,
					sharedLinkNext,
					prefixPath,
				)
		else:
			incomplete = True
			logError(
				"delta com itens por resolver; cursor mantido",
				extra={"orphans": sharedOrphans, "scope": scope},
			)
	if mode == "completo" and not incomplete:
		_dropUnseenRows(connection, seen, "", startedAt)
	connection.commit()
	return count


def _rebuildRclone(
	connection: sqlite3.Connection,
	root: str,
	startedAt: str,
	mode: str,
	useDelta: bool,
) -> int:
	_setIndexJobStatus(root, 0, startedAt, mode=mode)
	connection.execute("DROP TABLE IF EXISTS files_new")
	connection.execute("DROP TABLE IF EXISTS index_seen")
	connection.execute("CREATE TEMP TABLE index_seen (path TEXT PRIMARY KEY)")
	updatedAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
	prefix: str = _pathPrefix(root)
	count: int = 0
	batch: list[tuple[Any, ...]] = []
	for item in iterLsjson(root, useOnedriveDelta=useDelta, useHash=True):
		row = _rowFromLsjson(
			item,
			prefix,
			updatedAt,
		)
		if row is None:
			continue
		if config.pathIsExcluded(str(row[0])):
			continue
		batch.append(row)
		count += 1
		if len(batch) >= config.INDEX_BATCH_SIZE:
			_relocateKnownIds(connection, batch, updatedAt)
			_commitIndexBatch(connection, batch)
			batch = []
			_setIndexJobStatus(
				root,
				count,
				startedAt,
				mode=mode,
			)
			print(f"Indexando… {count} itens", file=sys.stderr)
	if batch:
		_relocateKnownIds(connection, batch, updatedAt)
		_commitIndexBatch(connection, batch)
		_setIndexJobStatus(
			root,
			count,
			startedAt,
			mode=mode,
		)
	_deleteUnseen(connection, prefix, updatedAt)
	connection.commit()
	return count


def _applyDeltaFeed(
	connection: sqlite3.Connection,
	pages: Any,
	ids: dict[str, str],
	parents: dict[str, str],
	seen: set[str],
	count: int,
	root: str,
	startedAt: str,
	mode: str,
	prefix: str,
	scopeRootId: str,
	shared: dict[str, tuple[str, str, str]],
) -> tuple[int, dict[str, tuple[str, str, str]], int]:
	pending: list[dict[str, Any]] = []
	deletes: dict[str, dict[str, Any]] = {}
	deferred: list[dict[str, Any]] = []
	applied: int = 0
	base: int = count

	def takePending(batchPending: list[dict[str, Any]]) -> None:
		nonlocal applied
		added: int
		left: list[dict[str, Any]]
		added, left = _flushDeltaPending(
			connection,
			batchPending,
			ids,
			parents,
			seen,
			prefix,
			scopeRootId,
			shared,
		)
		applied += added
		deferred.extend(left)

	for raw in pages:
		parsed: dict[str, Any] | None = parseDeltaItem(raw)
		if parsed is None:
			continue
		itemId: str = str(parsed["itemId"])
		if bool(parsed["deleted"]):
			deletes[itemId] = parsed
			continue
		deletes.pop(itemId, None)
		pending.append(parsed)
		if len(pending) >= config.INDEX_BATCH_SIZE:
			takePending(pending)
			pending = []
			count = base + applied
			_setIndexJobStatus(
				root,
				count,
				startedAt,
				mode=mode,
			)
			print(f"Indexando… {count} itens", file=sys.stderr)
	if pending:
		takePending(pending)
	if deferred:
		addedOrphans: int
		addedOrphans, deferred = _flushDeltaPending(
			connection,
			deferred,
			ids,
			parents,
			seen,
			prefix,
			scopeRootId,
			shared,
		)
		applied += addedOrphans
	applied += _flushDeltaDeletes(connection, list(deletes.values()), ids, seen, shared)
	count = base + applied
	if applied > 0:
		_setIndexJobStatus(
			root,
			count,
			startedAt,
			mode=mode,
		)
	return count, shared, len(deferred)


def _forgetReleasedIds(ids: dict[str, str], oldPath: str, kept: set[str]) -> None:
	if oldPath == "":
		return
	prefix: str = f"{oldPath}/"
	stale: list[str] = []
	itemId: str
	path: str
	for itemId, path in ids.items():
		if path != oldPath and not path.startswith(prefix):
			continue
		if path in kept:
			continue
		if any(path.startswith(f"{held}/") for held in kept):
			continue
		stale.append(itemId)
	for itemId in stale:
		del ids[itemId]


def _retargetIds(ids: dict[str, str], oldPath: str, newPath: str) -> None:
	if oldPath == "" or oldPath == newPath:
		return
	prefix: str = f"{oldPath}/"
	updates: list[tuple[str, str]] = []
	itemId: str
	path: str
	for itemId, path in ids.items():
		if path != oldPath and not path.startswith(prefix):
			continue
		moved: str = newPath if path == oldPath else f"{newPath}{path[len(oldPath):]}"
		updates.append((itemId, moved))
	for itemId, path in updates:
		ids[itemId] = path


def _retargetSharedPrefixes(
	connection: sqlite3.Connection,
	shared: dict[str, tuple[str, str, str]],
	oldPath: str,
	newPath: str,
) -> None:
	if oldPath == "" or oldPath == newPath:
		return
	prefix: str = f"{oldPath}/"
	scope: str
	for scope, (driveId, remoteId, path) in list(shared.items()):
		if path != oldPath and not path.startswith(prefix):
			continue
		moved: str = newPath if path == oldPath else f"{newPath}{path[len(oldPath):]}"
		shared[scope] = (driveId, remoteId, moved)
		stored: dict[str, str] | None = _loadDeltaState(connection, scope)
		link: str = stored["deltaLink"] if stored is not None else ""
		_saveDeltaState(
			connection,
			scope,
			driveId,
			remoteId,
			link,
			moved,
		)


def _rememberSharedScope(
	connection: sqlite3.Connection,
	parsed: dict[str, Any],
	itemPath: str,
	shared: dict[str, tuple[str, str, str]],
) -> None:
	driveId: str = str(parsed.get("remoteDriveId") or "")
	remoteId: str = str(parsed.get("remoteItemId") or "")
	if itemPath == "" or driveId == "" or remoteId == "":
		return
	scopeId: str = str(parsed["itemId"])
	shared[scopeId] = (driveId, remoteId, itemPath)
	stored: dict[str, str] | None = _loadDeltaState(connection, scopeId)
	storedLink: str = ""
	storedPrefix: str = ""
	storedDrive: str = ""
	storedItem: str = ""
	if stored is not None:
		storedLink = stored["deltaLink"]
		storedPrefix = stored["prefix"]
		storedDrive = stored["driveId"]
		storedItem = stored["itemId"]
	if (
		stored is not None
		and storedPrefix == itemPath
		and storedDrive == driveId
		and storedItem == remoteId
	):
		return
	_saveDeltaState(
		connection,
		scopeId,
		driveId,
		remoteId,
		storedLink,
		itemPath,
	)


def _flushDeltaPending(
	connection: sqlite3.Connection,
	pending: list[dict[str, Any]],
	ids: dict[str, str],
	parents: dict[str, str],
	seen: set[str],
	prefix: str,
	scopeRootId: str,
	shared: dict[str, tuple[str, str, str]],
) -> tuple[int, list[dict[str, Any]]]:
	updatedAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
	remaining: list[dict[str, Any]] = list(pending)
	applied: int = 0
	allowHint: bool = False
	while remaining:
		nextRound: list[dict[str, Any]] = []
		batch: list[tuple[Any, ...]] = []
		placed: list[dict[str, Any]] = []
		resolved: int = 0
		waitingIds: set[str] = {
			str(item["itemId"])
			for item in remaining
			if str(item.get("itemId") or "") != ""
		}
		for parsed in remaining:
			itemPath: str = _resolveDeltaPath(
				parsed,
				ids,
				parents,
				prefix,
				scopeRootId,
				allowHint,
				waitingIds,
			)
			if itemPath == "":
				nextRound.append(parsed)
				continue
			resolved += 1
			itemId: str = str(parsed["itemId"])
			oldPath: str = ids.get(itemId, "")
			if config.pathIsExcluded(itemPath):
				if oldPath != "" and oldPath != itemPath and not config.pathIsExcluded(oldPath):
					kept: set[str] = _releaseVanishedTree(
						connection,
						oldPath,
						keepUnsynced=False,
					)
					_forgetReleasedIds(
						ids,
						oldPath,
						kept,
					)
				if str(parsed.get("remoteItemId") or "") != "":
					shared.pop(itemId, None)
					_dropDeltaScope(connection, itemId)
				continue
			if oldPath != "" and oldPath != itemPath:
				conflictPath: str = _rewriteDescendantPaths(
					connection,
					oldPath,
					itemPath,
					updatedAt,
				)
				if conflictPath != "":
					_retargetIds(
						ids,
						itemPath,
						conflictPath,
					)
					_retargetSharedPrefixes(
						connection,
						shared,
						itemPath,
						conflictPath,
					)
				_retargetIds(
					ids,
					oldPath,
					itemPath,
				)
				_retargetSharedPrefixes(
					connection,
					shared,
					oldPath,
					itemPath,
				)
			_rememberSharedScope(
				connection,
				parsed,
				itemPath,
				shared,
			)
			ids[itemId] = itemPath
			parents[itemId] = str(parsed["parentId"])
			placed.append(parsed)
		parsedItem: dict[str, Any]
		for parsedItem in placed:
			placedId: str = str(parsedItem["itemId"])
			placedPath: str = ids.get(placedId, "")
			if placedPath == "" or config.pathIsExcluded(placedPath):
				continue
			batch.append(
				(
					placedPath,
					str(parsedItem["name"]),
					parentPath(placedPath),
					1 if bool(parsedItem["isDir"]) else 0,
					int(parsedItem["size"]),
					str(parsedItem["modTime"]),
					str(parsedItem["mimeType"]),
					str(parsedItem["etag"]),
					placedId,
					updatedAt,
					"",
					"",
				)
			)
			seen.add(placedPath)
			applied += 1
		if batch:
			_commitIndexBatch(connection, batch)
		if resolved == 0 and not allowHint:
			allowHint = True
			continue
		if resolved == 0:
			break
		remaining = nextRound
		allowHint = False
	return applied, remaining


def _flushDeltaDeletes(
	connection: sqlite3.Connection,
	deletes: list[dict[str, Any]],
	ids: dict[str, str],
	seen: set[str],
	shared: dict[str, tuple[str, str, str]] | None = None,
) -> int:
	removed: int = 0
	paths: list[str] = []
	for parsed in deletes:
		itemId: str = str(parsed["itemId"])
		if shared is not None:
			shared.pop(itemId, None)
		_dropDeltaScope(connection, itemId)
		row: sqlite3.Row | None = connection.execute(
			"SELECT path FROM files WHERE itemId = ?",
			(itemId,),
		).fetchone()
		if row is None:
			ids.pop(itemId, None)
			continue
		itemPath: str = str(row["path"])
		ids[itemId] = itemPath
		paths.append(itemPath)
	paths.sort(key=lambda value: value.count("/"), reverse=True)
	for itemPath in paths:
		keptPaths: set[str] = _releaseVanishedTree(connection, itemPath)
		if itemPath in keptPaths:
			connection.execute(
				"""
				UPDATE files
				SET syncState = ?, syncError = ?, updatedAt = ?
				WHERE path = ? AND syncState IN (?, ?, ?, ?)
				""",
				(
					SYNC_CONFLICT,
					"apagado na nuvem",
					datetime.now(timezone.utc).isoformat(timespec="seconds"),
					itemPath,
					SYNC_DIRTY,
					SYNC_SYNCING,
					SYNC_CONFLICT,
					SYNC_ERROR,
				),
			)
		stale: list[str] = []
		for key, value in ids.items():
			if value != itemPath and not value.startswith(f"{itemPath}/"):
				continue
			if value in keptPaths:
				continue
			stale.append(key)
		for key in stale:
			del ids[key]
		if itemPath not in keptPaths:
			seen.discard(itemPath)
			removed += 1
	if paths:
		connection.commit()
	return removed


def _releaseVanishedTree(
	connection: sqlite3.Connection,
	itemPath: str,
	keepFiltered: bool = False,
	keepUnsynced: bool = True,
) -> set[str]:
	likePrefix: str = f"{_likeEscape(itemPath)}/%"
	rows: list[sqlite3.Row] = list(
		connection.execute(
			"""
			SELECT path, syncState, baseEtag, isDir FROM files
			WHERE path = ? OR path LIKE ? ESCAPE '\\'
			""",
			(itemPath, likePrefix),
		)
	)
	rows.sort(key=lambda row: str(row["path"]).count("/"), reverse=True)
	kept: set[str] = set()
	row: sqlite3.Row
	for row in rows:
		path: str = str(row["path"])
		state: str = str(row["syncState"] or "")
		base: str = str(row["baseEtag"] or "")
		if keepFiltered and _hiddenFromListing(path):
			kept.add(path)
			continue
		if _keepVanished(state) or (keepUnsynced and base == ""):
			kept.add(path)
			continue
		childPrefix: str = f"{path}/"
		if any(other.startswith(childPrefix) for other in kept):
			kept.add(path)
			continue
		_dropVanishedPath(connection, path, bool(row["isDir"]))
	return kept


def _dropVanishedPath(connection: sqlite3.Connection, itemPath: str, isDir: bool) -> None:
	if not _removeVanishedCopies(itemPath, isDir):
		return
	connection.execute("DELETE FROM files WHERE path = ?", (itemPath,))
	connection.execute("DELETE FROM file_chunks WHERE path = ?", (itemPath,))
	connection.execute("DELETE FROM chunk_writes WHERE path = ?", (itemPath,))


def _keepVanished(state: str) -> bool:
	return state in (SYNC_DIRTY, SYNC_SYNCING, SYNC_CONFLICT, SYNC_ERROR)


def _hiddenFromListing(itemPath: str) -> bool:
	return config.pathIsExcluded(itemPath)


def _removeVanishedCopies(itemPath: str, isDir: bool) -> bool:
	from fastonedrive.search import cacheLocalPath, pinnedLocalPath

	removed: bool = True
	local: Path
	for local in (pinnedLocalPath(itemPath), cacheLocalPath(itemPath)):
		if not local.exists() and not local.is_symlink():
			continue
		if isDir and local.is_dir() and not local.is_symlink():
			try:
				local.rmdir()
			except OSError:
				removed = False
			continue
		if (not isDir) and (local.is_file() or local.is_symlink()):
			try:
				local.unlink(missing_ok=True)
			except OSError:
				removed = False
			continue
		removed = False
	return removed


def _resolveDeltaPath(
	parsed: dict[str, Any],
	ids: dict[str, str],
	parents: dict[str, str],
	prefix: str,
	scopeRootId: str,
	allowHint: bool = False,
	waitingIds: set[str] | None = None,
) -> str:
	itemId: str = str(parsed["itemId"])
	name: str = str(parsed["name"])
	parentId: str = str(parsed["parentId"])
	pendingIds: set[str] = waitingIds if waitingIds is not None else set()
	if scopeRootId != "" and itemId == scopeRootId:
		return prefix
	if scopeRootId != "" and parentId == scopeRootId:
		if prefix == "":
			return name
		return f"{prefix}/{name}"
	if parentId != "":
		parentItemPath: str = ids.get(parentId, "")
		if parentItemPath != "":
			return f"{parentItemPath}/{name}"
	if prefix != "":
		return ""
	if bool(parsed.get("parentAtRoot")):
		return name
	if parentId != "" and not allowHint:
		return ""
	if parentId != "" and parentId in pendingIds and ids.get(parentId, "") == "":
		return ""
	hint: str = str(parsed.get("parentHint") or "")
	if hint != "":
		return f"{hint}/{name}"
	if parentId == "":
		return name
	return ""


def _underAny(path: str, roots: set[str]) -> bool:
	root: str
	for root in roots:
		if path == root or path.startswith(f"{root}/"):
			return True
	return False


def _deleteChunksAt(connection: sqlite3.Connection, paths: list[str]) -> None:
	if len(paths) == 0:
		return
	pairs: list[tuple[str]] = [(path,) for path in paths]
	connection.executemany("DELETE FROM file_chunks WHERE path = ?", pairs)
	connection.executemany("DELETE FROM chunk_writes WHERE path = ?", pairs)


def _deleteForeignChunks(
	connection: sqlite3.Connection,
	destPath: str,
	sourcePath: str,
) -> None:
	if destPath == "" or destPath == sourcePath:
		return
	prefix: str = f"{_likeEscape(destPath)}/%"
	rows: list[sqlite3.Row] = list(
		connection.execute(
			"""
			SELECT path FROM file_chunks
			WHERE path = ? OR path LIKE ? ESCAPE '\\'
			UNION
			SELECT path FROM chunk_writes
			WHERE path = ? OR path LIKE ? ESCAPE '\\'
			""",
			(destPath, prefix, destPath, prefix),
		)
	)
	sourcePrefix: str = f"{sourcePath}/" if sourcePath != "" else ""
	doomed: list[str] = []
	row: sqlite3.Row
	for row in rows:
		path: str = str(row["path"])
		if sourcePath != "" and (path == sourcePath or path.startswith(sourcePrefix)):
			continue
		doomed.append(path)
	_deleteChunksAt(connection, doomed)


def _displaceOccupiedTree(
	connection: sqlite3.Connection,
	oldPath: str,
	newPath: str,
	updatedAt: str,
) -> str:
	if newPath == "" or oldPath == "" or newPath == oldPath:
		return ""
	if oldPath.startswith(f"{newPath}/"):
		return ""
	destRow: sqlite3.Row | None = connection.execute(
		"SELECT itemId FROM files WHERE path = ?",
		(newPath,),
	).fetchone()
	if destRow is None:
		return ""
	sourceRow: sqlite3.Row | None = connection.execute(
		"SELECT itemId FROM files WHERE path = ?",
		(oldPath,),
	).fetchone()
	destId: str = str(destRow["itemId"] or "")
	sourceId: str = str(sourceRow["itemId"] or "") if sourceRow is not None else ""
	if destId != "" and destId == sourceId:
		return ""
	dirty: bool = _storedTreeDirty(connection, newPath)
	conflict: str = _uniqueConflictItem(connection, newPath)
	from fastonedrive.search import relocateStoredFiles

	relocateStoredFiles(newPath, conflict)
	_moveIndexPaths(connection, newPath, conflict, updatedAt)
	if dirty:
		_markTreeDirty(connection, conflict)
	return conflict


def _rewriteDescendantPaths(
	connection: sqlite3.Connection,
	oldPath: str,
	newPath: str,
	updatedAt: str,
) -> str:
	if oldPath == "" or oldPath == newPath:
		return ""
	if newPath.startswith(f"{oldPath}/"):
		raise FastOneDriveError("não é possível mover para dentro de si")
	conflict: str = _displaceOccupiedTree(connection, oldPath, newPath, updatedAt)
	from fastonedrive.search import relocateStoredFiles

	relocateStoredFiles(oldPath, newPath)
	_moveIndexPaths(connection, oldPath, newPath, updatedAt)
	return conflict


def _storedTreeDirty(connection: sqlite3.Connection, itemPath: str) -> bool:
	prefix: str = f"{_likeEscape(itemPath)}/%"
	rows: list[sqlite3.Row] = list(
		connection.execute(
			"""
			SELECT path, isDir, syncState, baseModTime, baseEtag FROM files
			WHERE path = ? OR path LIKE ? ESCAPE '\\'
			""",
			(itemPath, prefix),
		)
	)
	if len(rows) == 0:
		from fastonedrive.disk import pathPresent
		from fastonedrive.search import cacheLocalPath, pinnedLocalPath

		return pathPresent(pinnedLocalPath(itemPath)) or pathPresent(cacheLocalPath(itemPath))
	from fastonedrive.rclone import parseRemoteModTime
	from fastonedrive.search import cacheLocalPath, pinnedLocalPath

	row: sqlite3.Row
	for row in rows:
		state: str = str(row["syncState"] or "")
		if state in PROTECTED_SYNC:
			return True
		if bool(row["isDir"]):
			continue
		path: str = str(row["path"])
		if str(row["baseEtag"] or "") == "":
			for pending in (pinnedLocalPath(path), cacheLocalPath(path)):
				if pending.is_file():
					return True
		base = parseRemoteModTime(str(row["baseModTime"] or ""))
		local: Path | None = None
		for candidate in (pinnedLocalPath(path), cacheLocalPath(path)):
			if candidate.is_file():
				local = candidate
				break
		if _localFileDirty(path, base) or (local is not None and _mtimeDirty(local, base)):
			return True
	return False


def _localFileDirty(itemPath: str, base: datetime | None) -> bool:
	from fastonedrive.search import cacheLocalPath, pinnedLocalPath

	local: Path | None = None
	for candidate in (pinnedLocalPath(itemPath), cacheLocalPath(itemPath)):
		if candidate.is_file():
			local = candidate
			break
	if local is None:
		return False
	return _mtimeDirty(local, base)


def _mtimeDirty(local: Path, base: datetime | None) -> bool:
	if base is None:
		return False
	localTime: datetime = datetime.fromtimestamp(local.stat().st_mtime, tz=timezone.utc)
	return localTime > base + timedelta(seconds=2)


def _uniqueConflictItem(connection: sqlite3.Connection, itemPath: str) -> str:
	from fastonedrive.disk import pathPresent
	from fastonedrive.search import cacheLocalPath, pinnedLocalPath

	parent: str = parentPath(itemPath)
	name: str = itemPath.rsplit("/", 1)[-1]
	dot: int = name.rfind(".")
	stem: str = name[:dot] if dot > 0 else name
	suffix: str = name[dot:] if dot > 0 else ""
	stamp: str = datetime.now().strftime("%Y-%m-%d %H%M%S")
	number: int = 1
	while True:
		extra: str = "" if number == 1 else f"-{number}"
		conflictName: str = f"{stem} (conflito local {stamp}{extra}){suffix}"
		full: str = f"{parent}/{conflictName}" if parent != "" else conflictName
		taken: sqlite3.Row | None = connection.execute(
			"SELECT 1 FROM files WHERE path = ?",
			(full,),
		).fetchone()
		if (
			taken is None
			and not pathPresent(pinnedLocalPath(full))
			and not pathPresent(cacheLocalPath(full))
		):
			return full
		number += 1


def _markTreeDirty(connection: sqlite3.Connection, itemPath: str) -> None:
	prefix: str = f"{_likeEscape(itemPath)}/%"
	updatedAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
	connection.execute(
		"""
		UPDATE files
		SET syncState = ?, syncError = '', updatedAt = ?
		WHERE (path = ? OR path LIKE ? ESCAPE '\\') AND isDir = 0
		""",
		(SYNC_DIRTY, updatedAt, itemPath, prefix),
	)
	connection.commit()


def _moveIndexPaths(
	connection: sqlite3.Connection,
	oldPath: str,
	newPath: str,
	updatedAt: str,
) -> None:
	rows: list[sqlite3.Row] = list(
		connection.execute(
			"""
			SELECT """ + FILE_SELECT + """
			FROM files
			WHERE path = ? OR path LIKE ? ESCAPE '\\'
			ORDER BY path
			""",
			(oldPath, f"{_likeEscape(oldPath)}/%"),
		)
	)
	payload: list[tuple[Any, ...]] = []
	for row in rows:
		path: str = str(row["path"])
		newItem: str
		if path == oldPath:
			newItem = newPath
		else:
			newItem = f"{newPath}{path[len(oldPath):]}"
		payload.append(
			_moveRowPayload(
				row,
				newItem,
				str(row["name"]) if path != oldPath else newPath.rsplit("/", 1)[-1],
				updatedAt,
			)
		)
	destPaths: list[str] = [str(item[0]) for item in payload]
	if newPath != oldPath:
		_deleteChunksAt(connection, destPaths)
	_retargetChunks(connection, oldPath, newPath)
	connection.execute(
		"DELETE FROM files WHERE path = ? OR path LIKE ? ESCAPE '\\'",
		(oldPath, f"{_likeEscape(oldPath)}/%"),
	)
	if newPath != oldPath:
		connection.executemany(
			"DELETE FROM files WHERE path = ?",
			[(path,) for path in destPaths],
		)
	connection.executemany(FILE_INSERT_SQL, payload)
	connection.commit()


def _relocateKnownIds(
	connection: sqlite3.Connection,
	batch: list[tuple[Any, ...]],
	updatedAt: str,
) -> set[str]:
	relocated: set[str] = set()
	ids: dict[str, str] = _loadItemIdMap(connection)
	ordered: list[tuple[Any, ...]] = sorted(batch, key=lambda item: str(item[0]).count("/"))
	rowTuple: tuple[Any, ...]
	for rowTuple in ordered:
		itemId: str = str(rowTuple[8] or "")
		newPath: str = str(rowTuple[0])
		oldPath: str = ids.get(itemId, "")
		if itemId == "" or oldPath == "" or oldPath == newPath:
			continue
		_rewriteDescendantPaths(connection, oldPath, newPath, updatedAt)
		ids = _loadItemIdMap(connection)
		relocated.add(oldPath)
	return relocated


def _loadItemIdMap(connection: sqlite3.Connection) -> dict[str, str]:
	return {
		str(row["itemId"]): str(row["path"])
		for row in connection.execute(
			"SELECT itemId, path FROM files WHERE itemId != ''"
		)
	}


def _loadParentIdMap(connection: sqlite3.Connection) -> dict[str, str]:
	return {}


def _loadDeltaState(connection: sqlite3.Connection, scope: str) -> dict[str, str] | None:
	row: sqlite3.Row | None = connection.execute(
		"SELECT scope, driveId, itemId, deltaLink, prefix, updatedAt FROM delta_state WHERE scope = ?",
		(scope,),
	).fetchone()
	if row is None:
		return None
	return {
		"scope": str(row["scope"]),
		"driveId": str(row["driveId"]),
		"itemId": str(row["itemId"]),
		"deltaLink": str(row["deltaLink"]),
		"prefix": str(row["prefix"]),
		"updatedAt": str(row["updatedAt"]),
	}


def _sharedScopeMissing(error: GraphDeltaError) -> bool:
	text: str = str(error).lower()
	return "itemnotfound" in text or "item not found" in text


def _dropDeltaScope(connection: sqlite3.Connection, scope: str) -> None:
	connection.execute("DELETE FROM delta_state WHERE scope = ?", (scope,))


def _saveDeltaState(
	connection: sqlite3.Connection,
	scope: str,
	driveId: str,
	itemId: str,
	deltaLink: str,
	prefix: str,
) -> None:
	updatedAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
	connection.execute(
		"""
		INSERT INTO delta_state (scope, driveId, itemId, deltaLink, prefix, updatedAt)
		VALUES (?, ?, ?, ?, ?, ?)
		ON CONFLICT(scope) DO UPDATE SET
			driveId = excluded.driveId,
			itemId = excluded.itemId,
			deltaLink = excluded.deltaLink,
			prefix = excluded.prefix,
			updatedAt = excluded.updatedAt
		""",
		(scope, driveId, itemId, deltaLink, prefix, updatedAt),
	)
	connection.commit()


def _loadSharedScopes(connection: sqlite3.Connection) -> dict[str, tuple[str, str, str]]:
	result: dict[str, tuple[str, str, str]] = {}
	for row in connection.execute(
		"SELECT scope, driveId, itemId, prefix FROM delta_state WHERE scope != 'root'"
	):
		scope: str = str(row["scope"])
		driveId: str = str(row["driveId"])
		itemId: str = str(row["itemId"])
		prefix: str = str(row["prefix"])
		if driveId == "" or itemId == "":
			continue
		result[scope] = (driveId, itemId, prefix)
	return result


def _dropUnseenRows(
	connection: sqlite3.Connection,
	seen: set[str] | None,
	prefix: str = "",
	rebuiltBefore: str = "",
) -> None:
	if seen is None:
		return
	rows: list[sqlite3.Row] = list(
		connection.execute(
			"SELECT path, pinned, syncState, baseEtag, isDir, updatedAt FROM files"
		)
	)
	missing: list[tuple[str, bool]] = []
	keepUnder: set[str] = set()
	for row in rows:
		path: str = str(row["path"])
		if prefix != "" and path != prefix and not path.startswith(f"{prefix}/"):
			continue
		if path in seen:
			continue
		updatedAt: str = str(row["updatedAt"] or "")
		if rebuiltBefore != "" and updatedAt >= rebuiltBefore:
			continue
		state: str = str(row["syncState"] or "")
		base: str = str(row["baseEtag"] or "")
		if _hiddenFromListing(path):
			keepUnder.add(path)
			continue
		if _keepVanished(state) or base == "":
			keepUnder.add(path)
			continue
		missing.append((path, bool(row["isDir"])))
	missing.sort(key=lambda item: item[0].count("/"), reverse=True)
	dropped: tuple[str, bool]
	for dropped in missing:
		path, isDir = dropped
		childPrefix: str = f"{path}/"
		if any(other.startswith(childPrefix) for other in keepUnder):
			continue
		_dropVanishedPath(connection, path, isDir)
	connection.commit()


def _commitIndexBatch(connection: sqlite3.Connection, batch: list[tuple[Any, ...]]) -> None:
	connection.executemany(UPSERT_SQL, batch)
	try:
		connection.executemany(
			"INSERT OR IGNORE INTO index_seen(path) VALUES (?)",
			[(row[0],) for row in batch],
		)
	except sqlite3.OperationalError:
		pass
	connection.commit()


def _deleteUnseen(connection: sqlite3.Connection, prefix: str, rebuiltAt: str) -> None:
	seen: set[str] = {
		str(row[0])
		for row in connection.execute("SELECT path FROM index_seen")
	}
	_dropUnseenRows(connection, seen, prefix, rebuiltAt)


def _pathPrefix(root: str) -> str:
	return remoteItemPath(root)


def _rowFromLsjson(
	item: dict[str, Any],
	prefix: str,
	updatedAt: str,
) -> tuple[Any, ...] | None:
	name: str = str(item.get("Name") or "")
	relative: str = str(item.get("Path") or name).replace("\\", "/").strip("/")
	if name == "" or relative in ("", ".", "/"):
		return None
	if isEphemeralName(name):
		return None
	path: str
	if prefix == "":
		path = relative
	elif relative == prefix or relative.startswith(f"{prefix}/"):
		path = relative
	else:
		path = f"{prefix}/{relative}"
	if path == prefix:
		return None
	parent: str
	if "/" in path:
		parent = path.rsplit("/", 1)[0]
	else:
		parent = ""
	isDir: int = 1 if bool(item.get("IsDir")) else 0
	size: int = int(item.get("Size") or 0)
	modTime: str = str(item.get("ModTime") or "")
	mimeType: str = str(item.get("MimeType") or "")
	etag: str = contentTokenFromStat(item)
	itemId: str = str(item.get("ID") or "")
	return (
		path,
		name,
		parent,
		isDir,
		size,
		modTime,
		mimeType,
		etag,
		itemId,
		updatedAt,
		"",
		"",
	)


def _likeEscape(text: str) -> str:
	return (
		text.replace("\\", "\\\\")
		.replace("%", "\\%")
		.replace("_", "\\_")
	)
