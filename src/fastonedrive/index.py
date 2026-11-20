import json
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
		-- Só "dirty": em "syncing" o envio é nosso e a versão nova que o delta traz pode
		-- ser a que acabámos de enviar (o envio fixa a base no fim). Os tokens chegam
		-- já na forma canónica (canonicalToken), por isso a comparação é texto.
		WHEN files.syncState = 'dirty'
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
DELTA_ONLY_ENV: str = "FASTONEDRIVE_DELTA_ONLY"
SCHEMA_VERSION: int = 15

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


_schemaReady: set[str] = set()
_schemaLock: threading.Lock = threading.Lock()


def connectDb() -> sqlite3.Connection:
	dbFile: Path = config.dbPath()
	key: str = str(dbFile)
	ready: bool = key in _schemaReady and dbFile.is_file()
	if not ready:
		config.ensureDataDir()
	connection: sqlite3.Connection = sqlite3.connect(
		dbFile,
		timeout=15.0,
	)
	connection.row_factory = sqlite3.Row
	connection.execute("PRAGMA busy_timeout = 8000")
	connection.execute("PRAGMA synchronous = NORMAL")
	connection.execute("PRAGMA foreign_keys = ON")
	if ready:
		return connection
	# journal_mode e o schema persistem no ficheiro: só a primeira ligação de cada
	# processo os verifica. O FUSE abre uma ligação por getattr; isto tira-lhe as
	# consultas a sqlite_master e user_version.
	with _schemaLock:
		connection.execute("PRAGMA journal_mode = WAL")
		_ensureFilesTable(connection)
		_migrateSchemaIfNeeded(connection)
		_schemaReady.add(key)
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
			from fastonedrive.searchindex import populateSearchIndexIfNeeded

			if populateSearchIndexIfNeeded(connection):
				logInfo("índice de busca sem acentos preenchido")
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


def upsertLiveChildren(parentPath: str, items: list[dict[str, Any]]) -> int:
	updatedAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
	batch: list[tuple[Any, ...]] = []
	seen: set[str] = set()
	for item in items:
		parsedRow: tuple[Any, ...] | None = _rowFromLsjson(
			item,
			parentPath,
			updatedAt,
		)
		if parsedRow is None:
			continue
		batch.append(parsedRow)
		seen.add(str(parsedRow[0]))
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
		value: Any = row["unixMode"]
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


def setItemIdIfMissing(itemPath: str, itemId: str) -> None:
	"""Depois de um envio: com o id, renomear/apagar vão logo pelo Graph em vez de
	esperarem pelo delta (até 1 min) ou caírem no rclone."""
	if itemPath == "" or itemId == "":
		return
	connection: sqlite3.Connection = connectDb()
	try:
		connection.execute(
			"UPDATE files SET itemId = ? WHERE path = ? AND itemId = ''",
			(itemId, itemPath),
		)
		connection.commit()
	except sqlite3.IntegrityError:
		# Outra linha ainda tem este id (item movido na nuvem): o delta resolve.
		connection.rollback()
	finally:
		connection.close()


def markDirty(itemPath: str) -> None:
	if itemPath == "":
		return
	setSyncState(itemPath, SYNC_DIRTY)


def setSyncState(itemPath: str, state: str, error: str = "") -> None:
	if itemPath == "":
		return
	updatedAt: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
	connection: sqlite3.Connection = connectDb()
	previous: str = ""
	previousError: str = ""
	try:
		before: sqlite3.Row | None = connection.execute(
			"SELECT syncState, syncError FROM files WHERE path = ?",
			(itemPath,),
		).fetchone()
		if before is None:
			return
		previous = str(before[0] or "")
		previousError = str(before[1] or "")
		if previous == state and previousError == error and state not in (SYNC_ERROR, SYNC_CONFLICT):
			# markDirty repetido (cada gravação, cada sync adiado): sem commit nem busctl.
			return
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
	if state == previous and state in (SYNC_ERROR, SYNC_CONFLICT):
		# O sync de cada minuto volta a marcar o mesmo conflito: não repetir avisos.
		return
	if state in (SYNC_ERROR, SYNC_CONFLICT):
		from fastonedrive.notify import notifyDirChanged

		notifyDirChanged(itemPath)
		if state == SYNC_CONFLICT:
			from fastonedrive.notify import notifyConflict

			notifyConflict(itemPath)
	else:
		from fastonedrive.notify import notifyStateChanged

		notifyStateChanged(itemPath)


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
	if not keepSyncState:
		from fastonedrive.notify import notifyStateChanged

		notifyStateChanged(itemPath)


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
	from fastonedrive.searchindex import ensureChangeTracking, ensureSearchIndex, ensureStateIndex

	ensureSearchIndex(connection)
	ensureStateIndex(connection)
	ensureChangeTracking(connection)
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
	_canonicalizeTokens(connection)
	_clearDuplicateItemIds(connection)
	connection.execute(
		"CREATE UNIQUE INDEX IF NOT EXISTS idx_files_itemId ON files(itemId) WHERE itemId != ''"
	)
	connection.executescript(DELTA_STATE_SQL)
	connection.executescript(FILE_CHUNKS_SQL)
	connection.executescript(CHUNK_WRITES_SQL)
	connection.commit()


def _canonicalizeTokens(connection: sqlite3.Connection) -> None:
	"""Schema 15: QuickXor em base64 em todo o lado (antes hex vindo do rclone/graphStat
	e base64 vindo do delta). Linhas em "conflict" cuja nuvem é a própria base eram
	falsos conflitos dessa mistura: voltam a "dirty" e sobem sem cópia de conflito."""
	from fastonedrive.rclone import canonicalToken

	rows: list[sqlite3.Row] = list(
		connection.execute(
			"SELECT path, etag, baseEtag, syncState FROM files "
			"WHERE etag LIKE 'qxh:%' OR baseEtag LIKE 'qxh:%' OR etag LIKE 'sha1:%' OR baseEtag LIKE 'sha1:%'"
		)
	)
	updates: list[tuple[str, str, str, str]] = []
	row: sqlite3.Row
	for row in rows:
		etag: str = str(row["etag"] or "")
		base: str = str(row["baseEtag"] or "")
		newEtag: str = canonicalToken(etag)
		newBase: str = canonicalToken(base)
		state: str = str(row["syncState"] or "")
		if state == SYNC_CONFLICT and newBase != "" and newBase == newEtag:
			state = SYNC_DIRTY
		if newEtag != etag or newBase != base or state != str(row["syncState"] or ""):
			updates.append((newEtag, newBase, state, str(row["path"])))
	if len(updates) > 0:
		connection.executemany(
			"UPDATE files SET etag = ?, baseEtag = ?, syncState = ? WHERE path = ?",
			updates,
		)


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
			if os.environ.get(DELTA_ONLY_ENV, "") == "1":
				raise FastOneDriveError(
					f"Graph indisponível; poll adiado: {error}"
				) from error
			logError(f"Graph indisponível, a usar rclone: {error}")
			return _rebuildRclone(
				connection,
				root,
				startedAt,
				"rclone",
				True,
			)
	except GraphDeltaError as error:
		if os.environ.get(DELTA_ONLY_ENV, "") == "1":
			# O poll de cada minuto não pode cair numa listagem completa do Drive
			# (minutos de rclone e escrita no SQLite que bloqueia o FUSE).
			raise FastOneDriveError(f"Graph indisponível; poll adiado: {error}") from error
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
		# Em modo completo o cursor da raiz só avança se a limpeza puder correr;
		# falha numa partilha não pode deixar o índice sem purge para sempre.
		if mode != "completo":
			_saveDeltaState(connection, "root", "", "", newRootLink, "")
	else:
		logError(
			"delta com itens por resolver; cursor mantido",
			extra={"orphans": orphans, "scope": "root"},
		)
	for scope, (driveId, remoteId, prefixPath) in list(shared.items()):
		sharedState: dict[str, str] | None = _loadDeltaState(connection, scope)
		# Em modo completo a partilha também enumera desde o início; senão
		# _dropUnseenRows apaga pins e linhas que o delta incremental não viu.
		sharedLink: str = ""
		if mode != "completo" and sharedState is not None:
			sharedLink = str(sharedState["deltaLink"])
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
				scopeId=scope,
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
			incomplete = True
			logError(
				f"delta da pasta partilhada falhou: {error}",
				extra={"scope": scope},
			)
			continue
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
	if mode == "completo" and orphans == 0 and not incomplete:
		_saveDeltaState(connection, "root", "", "", newRootLink, "")
		_dropUnseenRows(connection, seen, "", startedAt)
	elif mode == "completo" and incomplete:
		logError(
			"ciclo completo incompleto; cursor da raiz mantido",
			extra={"orphans": orphans},
		)
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
	scopeId: str = "",
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
			scopeId,
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
			scopeId,
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
	scopeId: str = "",
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
			# A raiz do scope mantém o id do atalho no SQLite; o id remoto
			# fica só no mapa em memória para resolver filhos do delta.
			if scopeId != "" and itemPath == prefix:
				ids[scopeId] = itemPath
			placed.append(parsed)
		parsedItem: dict[str, Any]
		for parsedItem in placed:
			placedId: str = str(parsedItem["itemId"])
			placedPath: str = ids.get(placedId, "")
			if placedPath == "" or config.pathIsExcluded(placedPath):
				continue
			storedId: str = placedId
			if scopeId != "" and placedPath == prefix:
				storedId = scopeId
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
					storedId,
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
		# Sem baseline, sem estado protegido e sem cópia local, o item sai.
		keptPaths: set[str] = _releaseVanishedTree(
			connection,
			itemPath,
			keepUnsynced=False,
		)
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
		if not _dropVanishedPath(connection, path, bool(row["isDir"])):
			# Cópia local não saiu: a linha fica para o ciclo seguinte tentar de novo.
			kept.add(path)
	return kept


def _dropVanishedPath(connection: sqlite3.Connection, itemPath: str, isDir: bool) -> bool:
	if not _removeVanishedCopies(itemPath, isDir):
		return False
	connection.execute("DELETE FROM files WHERE path = ?", (itemPath,))
	connection.execute("DELETE FROM file_chunks WHERE path = ?", (itemPath,))
	connection.execute("DELETE FROM chunk_writes WHERE path = ?", (itemPath,))
	return True


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
	from fastonedrive.disk import pathPresent
	from fastonedrive.search import cacheLocalPath, pinnedLocalPath, relocateStoredFiles

	destRow: sqlite3.Row | None = connection.execute(
		"SELECT itemId FROM files WHERE path = ?",
		(newPath,),
	).fetchone()
	hasLocal: bool = pathPresent(pinnedLocalPath(newPath)) or pathPresent(
		cacheLocalPath(newPath)
	)
	if destRow is None and not hasLocal:
		return ""
	if destRow is None:
		# Cópia só no disco, sem linha: não pode ser apagada pelo rename da nuvem.
		conflict: str = _uniqueConflictItem(connection, newPath)
		relocateStoredFiles(newPath, conflict)
		return conflict
	sourceRow: sqlite3.Row | None = connection.execute(
		"SELECT itemId FROM files WHERE path = ?",
		(oldPath,),
	).fetchone()
	destId: str = str(destRow["itemId"] or "")
	sourceId: str = str(sourceRow["itemId"] or "") if sourceRow is not None else ""
	if destId != "" and destId == sourceId:
		return ""
	dirty: bool = _storedTreeDirty(connection, newPath)
	conflict = _uniqueConflictItem(connection, newPath)
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
		if _keepVanished(state):
			keepUnder.add(path)
			continue
		# Baseline vazia só protege se ainda há cópia local; ficheiros só na
		# nuvem apagados no ciclo completo têm de sair do índice.
		if base == "":
			from fastonedrive.disk import pathPresent
			from fastonedrive.search import cacheLocalPath, pinnedLocalPath

			if pathPresent(pinnedLocalPath(path)) or pathPresent(cacheLocalPath(path)):
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
		if not _dropVanishedPath(connection, path, isDir):
			keepUnder.add(path)
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
	else:
		# lsjson devolve Path relativo à pasta listada; prefixar sempre.
		path = f"{prefix}/{relative}"
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


# Listagem em segundo plano vive em listing.py; os nomes públicos continuam a sair daqui.
from fastonedrive.listing import (  # noqa: E402
	ensureFolderListed as ensureFolderListed,
	invalidateListing as invalidateListing,
	listingCompleted as listingCompleted,
	markListingCompleted as markListingCompleted,
	runParentRefresh as runParentRefresh,
	scheduleParentRefresh as scheduleParentRefresh,
)
