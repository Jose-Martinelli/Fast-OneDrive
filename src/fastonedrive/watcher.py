import ctypes
import ctypes.util
import os
import struct
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastonedrive import config
from fastonedrive.disk import isEphemeralName, isEphemeralPath, isJobArtifactName, isSuppressed
from fastonedrive.index import getEntry, markDirty
from fastonedrive.log import logError, logInfo

IN_CLOSE_WRITE: int = 0x00000008
IN_MOVED_FROM: int = 0x00000040
IN_MOVED_TO: int = 0x00000080
IN_CREATE: int = 0x00000100
IN_DELETE_SELF: int = 0x00000400
IN_MOVE_SELF: int = 0x00000800
IN_IGNORED: int = 0x00008000
IN_ISDIR: int = 0x40000000
IN_NONBLOCK: int = 0x00000800
IN_CLOEXEC: int = 0x00080000
EVENT_STRUCT: str = "iIII"
EVENT_SIZE: int = struct.calcsize(EVENT_STRUCT)
WATCH_MASK: int = IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE
DEBOUNCE_SEC: float = 0.4
# Gravações seguidas (cópia de muitos ficheiros) não adiam o envio para sempre.
MAX_BATCH_WAIT_SEC: float = 3.0
RETRY_SEC: float = 2.0
MTIME_SKEW_SEC: float = 2.0

_libc: ctypes.CDLL | None = None
_timerLock: threading.Lock = threading.Lock()
_pendingPaths: set[str] = set()
_pendingTimer: threading.Timer | None = None
_batchStarted: float = 0.0
_watchThread: threading.Thread | None = None


def _lib() -> ctypes.CDLL:
	global _libc
	if _libc is not None:
		return _libc
	name: str | None = ctypes.util.find_library("c")
	_libc = ctypes.CDLL(name or "libc.so.6", use_errno=True)
	return _libc


def debounceSync(itemPath: str, delay: float | None = None) -> None:
	if itemPath == "" or isEphemeralPath(itemPath):
		return
	markDirty(itemPath)
	_armBatch({itemPath}, DEBOUNCE_SEC if delay is None else delay, delay is None)


def _armBatch(paths: set[str], wait: float, capped: bool) -> None:
	global _pendingTimer, _batchStarted
	with _timerLock:
		now: float = time.monotonic()
		if len(_pendingPaths) == 0 or _pendingTimer is None:
			_batchStarted = now
		_pendingPaths.update(paths)
		if capped:
			wait = min(wait, max(0.0, MAX_BATCH_WAIT_SEC - (now - _batchStarted)))
		if _pendingTimer is not None:
			_pendingTimer.cancel()
		timer: threading.Timer = threading.Timer(wait, _fireBatch)
		timer.daemon = True
		_pendingTimer = timer
		timer.start()


def _fireBatch() -> None:
	global _pendingTimer
	with _timerLock:
		batch: list[str] = sorted(_pendingPaths)
		_pendingPaths.clear()
		_pendingTimer = None
	if len(batch) > 0:
		_runSync(batch)


def _runSync(itemPaths: list[str]) -> None:
	from fastonedrive.disk import pinsLock
	from fastonedrive.synccontrol import pauseReason, pauseRetryDelay
	from fastonedrive.syncpins import _syncPinsLocked

	reason: str = pauseReason()
	if reason != "":
		# Os caminhos ficam na fila (dirty) e sobem quando a pausa acabar, sem esperar
		# pelo timer de 15 min.
		retry: float = pauseRetryDelay()
		logInfo(f"sync após gravação adiado: {reason}", extra={"paths": len(itemPaths), "retry": int(retry)})
		_armBatch(set(itemPaths), retry, False)
		return
	with pinsLock():
		for itemPath in itemPaths:
			try:
				_syncPinsLocked(None, itemPath)
			except Exception as error:
				logError(f"sync após gravação falhou: {error}", extra={"path": itemPath})


def startLocalWatch() -> None:
	global _watchThread
	if _watchThread is not None and _watchThread.is_alive():
		return
	_watchThread = threading.Thread(target=_watchLoop, name="fod-inotify", daemon=True)
	_watchThread.start()


def _watchLoop() -> None:
	libc: ctypes.CDLL = _lib()
	fd: int = int(libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC))
	if fd < 0:
		logError("inotify_init1 falhou")
		return
	watches: dict[int, Path] = {}
	partialMoves: set[int] = set()
	try:
		_addTree(libc, fd, config.ensurePinDir(), watches)
		_addTree(libc, fd, config.ensureCacheDir(), watches)
		logInfo("inotify pinned/cache")
		while True:
			try:
				_readEvents(libc, fd, watches, partialMoves)
			except Exception as error:
				logError(f"inotify falhou: {error}")
			time.sleep(0.2)
	finally:
		os.close(fd)


def _addTree(
	libc: ctypes.CDLL,
	fd: int,
	root: Path,
	watches: dict[int, Path],
) -> None:
	if not root.is_dir():
		return
	_addWatch(libc, fd, root, watches)
	for child in root.rglob("*"):
		if child.is_dir():
			_addWatch(libc, fd, child, watches)


def _addWatch(
	libc: ctypes.CDLL,
	fd: int,
	path: Path,
	watches: dict[int, Path],
) -> None:
	encoded: bytes = str(path).encode("utf-8")
	wd: int = int(libc.inotify_add_watch(fd, encoded, WATCH_MASK))
	if wd >= 0:
		watches[wd] = path


def _readEvents(
	libc: ctypes.CDLL,
	fd: int,
	watches: dict[int, Path],
	partialMoves: set[int],
) -> None:
	try:
		buf: bytes = os.read(fd, 65536)
	except BlockingIOError:
		return
	except OSError:
		return
	offset: int = 0
	while offset + EVENT_SIZE <= len(buf):
		wd, mask, cookie, nameLen = struct.unpack_from(EVENT_STRUCT, buf, offset)
		offset += EVENT_SIZE
		name: str = ""
		if nameLen > 0:
			raw: bytes = buf[offset : offset + nameLen]
			offset += nameLen
			name = raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
		directory: Path | None = watches.get(int(wd))
		if directory is None:
			continue
		if mask & IN_IGNORED:
			watches.pop(int(wd), None)
			continue
		if name == "":
			continue
		if (mask & IN_MOVED_FROM) and isJobArtifactName(name):
			partialMoves.add(int(cookie))
			continue
		if (mask & IN_MOVED_TO) and int(cookie) in partialMoves:
			partialMoves.discard(int(cookie))
			landed: Path = directory / name
			if (mask & IN_ISDIR) and landed.is_dir():
				_addTree(libc, fd, landed, watches)
			continue
		full: Path = directory / name
		if (mask & IN_ISDIR) and full.is_dir() and (mask & (IN_CREATE | IN_MOVED_TO)):
			_addTree(libc, fd, full, watches)
		if (mask & IN_CREATE) and (mask & IN_ISDIR):
			continue
		if (mask & IN_MOVED_TO) and (mask & IN_ISDIR):
			_scheduleMovedTree(full)
			continue
		if not (mask & (IN_CLOSE_WRITE | IN_MOVED_TO)):
			continue
		if mask & IN_ISDIR:
			continue
		if isEphemeralName(name):
			continue
		itemPath: str | None = _itemPathFromLocal(full)
		if itemPath is None or isSuppressed(itemPath):
			continue
		if (mask & IN_MOVED_TO) and not (mask & IN_CLOSE_WRITE) and _matchesBaseline(itemPath, full):
			continue
		debounceSync(itemPath)


def _scheduleMovedTree(root: Path) -> None:
	from fastonedrive.search import pinnedLocalPath

	paths: list[Path] = [root]
	if root.is_dir():
		paths.extend(child for child in root.rglob("*") if child.is_file())
	path: Path
	for path in paths:
		if isEphemeralName(path.name):
			continue
		itemPath: str | None = _itemPathFromLocal(path)
		if itemPath is None or isSuppressed(itemPath) or isEphemeralPath(itemPath):
			continue
		if path.is_dir():
			entry = getEntry(itemPath)
			pinnedHere: bool = entry is not None and bool(entry["pinned"])
			if not pinnedHere and not pinnedLocalPath(itemPath).is_dir():
				continue
		elif _matchesBaseline(itemPath, path):
			continue
		debounceSync(itemPath)


def _matchesBaseline(itemPath: str, path: Path) -> bool:
	from fastonedrive.rclone import parseRemoteModTime

	entry = getEntry(itemPath)
	if entry is None or bool(entry["isDir"]):
		return False
	if str(entry["syncState"] or "") != "" and str(entry["syncState"]) != "synced":
		return False
	if str(entry["baseEtag"] or "") == "":
		return False
	baseTime: datetime | None = parseRemoteModTime(str(entry["baseModTime"] or ""))
	if baseTime is None:
		return False
	try:
		info: os.stat_result = path.stat()
	except OSError:
		return False
	if int(info.st_size) != int(entry["size"]):
		return False
	localTime: datetime = datetime.fromtimestamp(info.st_mtime, tz=timezone.utc)
	return abs((localTime - baseTime).total_seconds()) <= MTIME_SKEW_SEC


def _itemPathFromLocal(path: Path) -> str | None:
	resolved: Path
	try:
		resolved = path.resolve()
	except OSError:
		return None
	for rootFn in (config.pinDir, config.cacheDir):
		root: Path = rootFn().resolve()
		try:
			rel: str = resolved.relative_to(root).as_posix()
		except ValueError:
			continue
		if rel in ("", "."):
			return None
		return rel
	return None


