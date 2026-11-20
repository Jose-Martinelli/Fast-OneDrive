"""Listagem de pastas em segundo plano (lsjson --max-depth 1): TTL por pasta, fila de
pastas a listar e um só worker por perfil. Separado de ``index.py``; os nomes públicos
continuam a ser importáveis de ``fastonedrive.index``."""

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
from pathlib import Path
from typing import Any, TextIO

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.log import logError

_listingStamp: dict[str, float] = {}
_listingBusy: set[str] = set()
_listingLock: threading.Lock = threading.Lock()


def _connectDb() -> sqlite3.Connection:
	from fastonedrive.index import connectDb

	return connectDb()


def _refreshParentListing(parentPath: str) -> int:
	from fastonedrive.index import refreshParentListing

	return refreshParentListing(parentPath)


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
		_refreshParentListing(parentPath)
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
			_refreshParentListing(parentPath)
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
	connection: sqlite3.Connection = _connectDb()
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
