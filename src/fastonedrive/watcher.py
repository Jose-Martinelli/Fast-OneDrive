import ctypes
import ctypes.util
import os
import struct
import threading
import time
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

_libc: ctypes.CDLL | None = None
_timerLock: threading.Lock = threading.Lock()
_timers: dict[str, threading.Timer] = {}
_watchThread: threading.Thread | None = None


def _lib() -> ctypes.CDLL:
	global _libc
	if _libc is not None:
		return _libc
	name: str | None = ctypes.util.find_library("c")
	_libc = ctypes.CDLL(name or "libc.so.6", use_errno=True)
	return _libc


def debounceSync(itemPath: str) -> None:
	if itemPath == "" or isEphemeralPath(itemPath):
		return
	markDirty(itemPath)

	def fire(path: str = itemPath) -> None:
		with _timerLock:
			_timers.pop(path, None)
		_runSync(path)

	with _timerLock:
		previous: threading.Timer | None = _timers.get(itemPath)
		if previous is not None:
			previous.cancel()
		timer: threading.Timer = threading.Timer(DEBOUNCE_SEC, fire)
		timer.daemon = True
		_timers[itemPath] = timer
		timer.start()


def _runSync(itemPath: str) -> None:
	from fastonedrive.syncpins import syncPins

	try:
		syncPins(itemPath=itemPath)
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
		debounceSync(itemPath)


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


