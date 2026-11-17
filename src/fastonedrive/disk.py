import fcntl
import hashlib
import os
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from fastonedrive import FastOneDriveError
from fastonedrive import config

FREE_SPACE_MARGIN_BYTES: int = 256 * 1024 * 1024
DEFAULT_MAX_CACHE_BYTES: int = 8 * 1024 * 1024 * 1024
_suppressUntil: dict[str, float] = {}
_suppressLock: threading.Lock = threading.Lock()


def suppressPath(itemPath: str, seconds: float = 3.0) -> None:
	if itemPath == "":
		return
	with _suppressLock:
		_suppressUntil[itemPath] = time.monotonic() + seconds


def writeGuardFile(itemPath: str) -> Path:
	digest: str = hashlib.sha256(itemPath.encode("utf-8")).hexdigest()
	root: Path = config.dataDir() / "write-guards"
	root.mkdir(parents=True, exist_ok=True)
	return root / digest


def openWriteGuard(itemPath: str) -> int:
	if itemPath == "":
		return -1
	fd: int = os.open(str(writeGuardFile(itemPath)), os.O_CREAT | os.O_RDWR, 0o644)
	try:
		fcntl.flock(fd, fcntl.LOCK_SH)
	except OSError:
		os.close(fd)
		raise
	return fd


def closeWriteGuard(fd: int) -> None:
	if fd < 0:
		return
	try:
		fcntl.flock(fd, fcntl.LOCK_UN)
	except OSError:
		pass
	os.close(fd)


def writeGuardHeld(itemPath: str) -> bool:
	if itemPath == "":
		return False
	path: Path = config.dataDir() / "write-guards" / hashlib.sha256(itemPath.encode("utf-8")).hexdigest()
	if not path.exists():
		return False
	fd: int = os.open(str(path), os.O_RDWR)
	try:
		try:
			fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except BlockingIOError:
			return True
		fcntl.flock(fd, fcntl.LOCK_UN)
		return False
	finally:
		os.close(fd)


@contextmanager
def tryExclusiveWriteGuard(itemPath: str) -> Iterator[bool]:
	if itemPath == "":
		yield True
		return
	fd: int = os.open(str(writeGuardFile(itemPath)), os.O_CREAT | os.O_RDWR, 0o644)
	try:
		fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
	except (BlockingIOError, OSError):
		os.close(fd)
		yield False
		return
	try:
		yield True
	finally:
		try:
			fcntl.flock(fd, fcntl.LOCK_UN)
		except OSError:
			pass
		os.close(fd)


def isSuppressed(itemPath: str) -> bool:
	if itemPath == "":
		return False
	now: float = time.monotonic()
	with _suppressLock:
		until: float = _suppressUntil.get(itemPath, 0.0)
		if until <= now:
			_suppressUntil.pop(itemPath, None)
			return False
		return True


def assertFreeSpace(dest: Path, needBytes: int) -> None:
	path: Path = dest
	while not path.exists() and path != path.parent:
		path = path.parent
	if not path.exists():
		raise FastOneDriveError(f"destino inválido: {dest}")
	usage = shutil.disk_usage(path)
	required: int = max(0, int(needBytes)) + FREE_SPACE_MARGIN_BYTES
	if usage.free < required:
		needMb: int = config.bytesToMb(required)
		freeMb: int = config.bytesToMb(int(usage.free))
		raise FastOneDriveError(
			f"espaço insuficiente em {path}: precisa {needMb} MB, livres {freeMb} MB"
		)


def isJobArtifactName(name: str) -> bool:
	return _isJobArtifactName(name, True)


def isEphemeralName(name: str) -> bool:
	if name == "":
		return False
	lower: str = name.lower()
	if _isJobArtifactName(name, includePartials=True):
		return True
	if name.startswith(".goutputstream-"):
		return True
	if name.startswith(".xdp-") or ".~lock." in name or name.startswith(".~"):
		return True
	if name.startswith("~$"):
		return True
	if lower.endswith(".swp") or lower.endswith(".swo") or lower.endswith(".tmp"):
		return True
	if name.startswith(".") and lower.endswith(".swp"):
		return True
	if name.startswith(".bak-") or lower.endswith(".partial"):
		return True
	return False


def isEphemeralPath(itemPath: str) -> bool:
	parts: list[str] = [part for part in itemPath.split("/") if part != ""]
	return any(isEphemeralName(part) for part in parts)


def isStagingRel(rel: str) -> bool:
	parts: list[str] = [part for part in rel.split("/") if part != ""]
	for part in parts:
		if _isJobArtifactName(part, includePartials=True):
			return True
		if part.startswith(".bak-") or part.endswith(".partial"):
			return True
	return False


def cleanupJobArtifacts(parent: Path, name: str) -> None:
	if name == "":
		return
	for path in (
		parent / f".fod-staging-{name}",
		parent / f".fod-bak-{name}",
		parent / f"{name}.fod-partial",
		parent / f".staging-{name}",
		parent / f".bak-{name}",
		parent / f"{name}.partial",
	):
		if pathPresent(path):
			removePath(path, ignoreErrors=True)


def pruneJobArtifacts(root: Path, includePartials: bool = False, recursive: bool = False) -> int:
	if not root.is_dir():
		return 0
	children: list[Path]
	if recursive:
		children = sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True)
	else:
		children = list(root.iterdir())
	deleted: int = 0
	for child in children:
		if not _isJobArtifactName(child.name, includePartials):
			continue
		removePath(child, ignoreErrors=True)
		if not pathPresent(child):
			deleted += 1
	return deleted


def pathPresent(path: Path) -> bool:
	return path.exists() or path.is_symlink()


def _isJobArtifactName(name: str, includePartials: bool) -> bool:
	if name.startswith(".fod-staging-") or name.startswith(".fod-bak-"):
		return True
	if name.startswith(".staging-"):
		return True
	if includePartials and name.endswith(".fod-partial"):
		return True
	return False


def removePath(path: Path, ignoreErrors: bool = False) -> None:
	try:
		if path.is_symlink() or path.is_file():
			path.unlink(missing_ok=True)
			return
		if path.is_dir():
			shutil.rmtree(path, ignore_errors=ignoreErrors)
	except Exception:
		if not ignoreErrors:
			raise


def swapIntoPlace(staging: Path, target: Path) -> None:
	backup: Path | None = None
	try:
		target.parent.mkdir(parents=True, exist_ok=True)
		if pathPresent(target):
			backup = target.with_name(f".fod-bak-{target.name}")
			if pathPresent(backup):
				removePath(backup)
			legacyBak: Path = target.with_name(f".bak-{target.name}")
			if pathPresent(legacyBak):
				removePath(legacyBak, ignoreErrors=True)
			target.rename(backup)
		try:
			staging.rename(target)
		except Exception:
			if backup is not None and pathPresent(backup) and not pathPresent(target):
				backup.rename(target)
			raise
		if backup is not None and pathPresent(backup):
			removePath(backup, ignoreErrors=True)
			if pathPresent(backup):
				removePath(backup, ignoreErrors=True)
	except Exception:
		if pathPresent(staging):
			removePath(staging, ignoreErrors=True)
		raise


@contextmanager
def pinsLock() -> Iterator[None]:
	with _exclusiveLock("pins.lock"):
		yield


@contextmanager
def diskJobsLock() -> Iterator[None]:
	with _exclusiveLock("disk.lock"):
		yield


@contextmanager
def cacheJobsLock() -> Iterator[None]:
	with _exclusiveLock("cache.lock"):
		yield


@contextmanager
def tryExclusiveLock(fileName: str) -> Iterator[bool]:
	config.ensureDataDir()
	lockPath: Path = config.dataDir() / fileName
	handle = lockPath.open("a+", encoding="utf-8")
	acquired: bool = False
	try:
		try:
			fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
			acquired = True
		except BlockingIOError:
			acquired = False
		yield acquired
	finally:
		if acquired:
			fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
		handle.close()


def indexIsRunning() -> bool:
	with tryExclusiveLock("index.lock") as acquired:
		return not acquired


@contextmanager
def _exclusiveLock(fileName: str) -> Iterator[None]:
	config.ensureDataDir()
	lockPath: Path = config.dataDir() / fileName
	handle = lockPath.open("a+", encoding="utf-8")
	try:
		fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
		try:
			yield
		finally:
			fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
	finally:
		handle.close()


def evictCache(
	maxBytes: int = DEFAULT_MAX_CACHE_BYTES,
	protect: Path | None = None,
	skip: Callable[[Path], bool] | None = None,
) -> None:
	root: Path = config.cacheDir()
	if not root.is_dir():
		return
	protected: Path | None = protect.resolve() if protect is not None else None
	files: list[tuple[float, int, Path]] = []
	total: int = 0
	for child in root.rglob("*"):
		if not child.is_file():
			continue
		info = child.stat()
		size: int = int(info.st_blocks) * 512
		total += size
		files.append((float(info.st_mtime), size, child))
	if total <= maxBytes:
		return
	files.sort(key=lambda item: item[0])
	for _mtime, size, path in files:
		if total <= maxBytes:
			return
		if protected is not None and path.resolve() == protected:
			continue
		if skip is not None and skip(path):
			continue
		if _evictionBlocked(path):
			continue
		try:
			path.unlink()
		except OSError:
			continue
		_dropChunkRow(root, path)
		total -= size


def _evictionBlocked(path: Path) -> bool:
	fd: int
	try:
		fd = os.open(str(path), os.O_RDONLY)
	except OSError:
		return True
	blocked: bool = True
	try:
		try:
			fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except OSError:
			return True
		blocked = False
		try:
			fcntl.flock(fd, fcntl.LOCK_UN)
		except OSError:
			return True
	finally:
		os.close(fd)
	return blocked


def _dropChunkRow(root: Path, path: Path) -> None:
	try:
		rel: str = path.resolve().relative_to(root.resolve()).as_posix()
	except ValueError:
		return
	from fastonedrive.chunks import deleteChunkRow

	deleteChunkRow(rel)
