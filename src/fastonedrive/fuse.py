import errno
import fcntl
import os
import shutil
import stat
import threading
import time
from pathlib import Path
from typing import Any

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive._fusepy import FUSE, FuseOSError, Operations, fuse_get_context
from fastonedrive.chunks import (
	CHUNK_SIZE,
	chunkLocks,
	deleteChunkRow,
	ensureRange,
	fillHoles,
	isPartial,
	noteWrite,
	prepareSparse,
)
from fastonedrive.disk import (
	closeWriteGuard,
	isEphemeralName,
	isEphemeralPath,
	openWriteGuard,
	suppressPath,
)
from fastonedrive.hydrate import cleanCacheIsStale, hydrateRemote
from fastonedrive.index import (
	deleteIndexTree,
	getEntry,
	hasDescendants,
	invalidateListing,
	markDirty,
	parentPath,
	renameIndexPath,
	scheduleParentRefresh,
	storedUnixMode,
	upsertEntry,
)
from fastonedrive.log import logError, logInfo
from fastonedrive.mutate import mkdirRemotePath, moveRemotePath, removeRemotePath
from fastonedrive.pin import shouldStorePinned, storedPinFlag
from fastonedrive.rclone import parseRemoteModTime, rmdirRemote
from fastonedrive.search import cacheLocalPath, chmodItem, listChildren, pinnedLocalPath
from fastonedrive.syncpins import scheduleItemSync
from fastonedrive.watcher import startLocalWatch

THUMB_COMMS: frozenset[str] = frozenset(
	{
		"baloo_file",
		"baloo_file_extr",
		"tumblerd",
		"ffmpegthumbs",
		"kio_thumbnail",
		"gdk-pixbuf-thumb",
		"evince-thumbnail",
		"gsd-thumbnailer",
		"kf6-thumbnail",
		"tracker-miner-fs",
		"tracker-miner-fs-3",
	}
)


class FastOneDriveFS(Operations):
	_handles: dict[int, dict[str, Any]]
	_handleLock: threading.Lock
	_nextFh: int

	def __init__(self) -> None:
		self._handles = {}
		self._handleLock = threading.Lock()
		self._nextFh = 1

	def getattr(self, path: str, fh: int | None = None) -> dict[str, Any]:
		itemPath: str = _itemPath(path)
		if itemPath == "":
			return _statDir(int(time.time()))
		if fh is not None:
			handle: dict[str, Any] | None = self._handles.get(fh)
			if handle is not None:
				return _statFromOs(os.fstat(int(handle["fd"])))
		local: Path | None = _existingLocal(itemPath)
		if local is not None:
			return _statFromOs(local.lstat())
		if isEphemeralPath(itemPath):
			raise FuseOSError(errno.ENOENT)
		entry = getEntry(itemPath)
		if entry is None:
			raise FuseOSError(errno.ENOENT)
		return _statFromIndex(entry)

	def readdir(self, path: str, fh: int) -> list[str]:
		itemPath: str = _itemPath(path)
		self._refreshListing(itemPath)
		names: set[str] = set()
		for row in listChildren(itemPath):
			name: str = str(row["name"])
			if name != "" and not isEphemeralName(name):
				names.add(name)
		for rootFn in (pinnedLocalPath, cacheLocalPath):
			folder: Path
			if itemPath == "":
				folder = config.pinDir() if rootFn is pinnedLocalPath else config.cacheDir()
			else:
				folder = rootFn(itemPath)
			if not folder.is_dir():
				continue
			try:
				for child in folder.iterdir():
					if child.name in ("", ".", "..") or isEphemeralName(child.name):
						continue
					names.add(child.name)
			except OSError:
				continue
		return [".", "..", *sorted(names)]

	def open(self, path: str, flags: int) -> int:
		itemPath: str = _itemPath(path)
		writable: bool = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_TRUNC))
		guardFd: int = -1
		if writable and not isEphemeralPath(itemPath):
			guardFd = openWriteGuard(itemPath)
		try:
			local: Path | None = _completeLocal(itemPath)
			if local is not None and local == cacheLocalPath(itemPath) and not _isThumbnailer():
				if cleanCacheIsStale(itemPath):
					local = None
			if local is not None:
				fd: int = os.open(str(local), flags)
				_holdShared(fd)
				if writable:
					truncated: bool = (flags & os.O_TRUNC) != 0
					if truncated:
						os.ftruncate(fd, 0)
					return self._openForWrite(
						itemPath,
						fd,
						False,
						guardFd,
						truncated,
					)
				return self._registerHandle(itemPath, fd, False, False)
			if isEphemeralPath(itemPath):
				raise FuseOSError(errno.ENOENT)
			if _isThumbnailer() and not writable:
				raise FuseOSError(errno.EACCES)
			entry = getEntry(itemPath)
			if entry is None:
				raise FuseOSError(errno.ENOENT)
			if bool(entry["isDir"]):
				raise FuseOSError(errno.EISDIR)
			remoteSize: int = int(entry["size"] or 0)
			useSparse: bool = (not writable) or remoteSize > config.openMaxBytes()
			if useSparse:
				try:
					local = prepareSparse(
						itemPath,
						remoteSize,
						str(entry["etag"] or ""),
					)
				except FastOneDriveError as error:
					logError(str(error), extra={"path": itemPath})
					raise FuseOSError(errno.EIO) from error
				sparseFd: int = os.open(str(local), os.O_RDWR)
				_holdShared(sparseFd)
				sparseTruncated: bool = (flags & os.O_TRUNC) != 0
				if sparseTruncated:
					os.ftruncate(sparseFd, 0)
					deleteChunkRow(itemPath)
				if writable:
					return self._openForWrite(
						itemPath,
						sparseFd,
						not sparseTruncated,
						guardFd,
						sparseTruncated,
					)
				return self._registerHandle(itemPath, sparseFd, False, True)
			try:
				local = hydrateRemote(config.remotePath(itemPath))
			except FastOneDriveError as error:
				logError(str(error), extra={"path": itemPath})
				raise FuseOSError(errno.EIO) from error
			fullFd: int = os.open(str(local), flags)
			_holdShared(fullFd)
			if writable:
				fullTruncated: bool = (flags & os.O_TRUNC) != 0
				if fullTruncated:
					os.ftruncate(fullFd, 0)
				return self._openForWrite(
					itemPath,
					fullFd,
					False,
					guardFd,
					fullTruncated,
				)
			return self._registerHandle(itemPath, fullFd, False, False)
		except Exception:
			closeWriteGuard(guardFd)
			raise

	def create(self, path: str, mode: int, fi: object | None = None) -> int:
		itemPath: str = _itemPath(path)
		backing: Path = _backingPath(itemPath)
		backing.parent.mkdir(parents=True, exist_ok=True)
		guardFd: int = -1
		existed: bool = getEntry(itemPath) is not None
		try:
			if not isEphemeralPath(itemPath):
				guardFd = openWriteGuard(itemPath)
				upsertEntry(
					itemPath,
					backing.name,
					False,
					0,
					storedPinFlag(itemPath),
				)
				markDirty(itemPath)
			fd: int = os.open(str(backing), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
			self._invalidate(parentPath(itemPath))
			return self._registerHandle(itemPath, fd, True, False, guardFd)
		except Exception:
			closeWriteGuard(guardFd)
			if not existed and not backing.exists() and not hasDescendants(itemPath):
				try:
					deleteIndexTree(itemPath)
				except FastOneDriveError as error:
					logError(str(error), extra={"path": itemPath, "step": "create"})
			raise

	def read(self, path: str, size: int, offset: int, fh: int) -> bytes:
		partial: bool = False
		itemPath: str = _itemPath(path)
		with self._handleLock:
			handle: dict[str, Any] | None = self._handles.get(fh)
			if handle is not None:
				partial = bool(handle.get("partial"))
				itemPath = str(handle.get("path") or itemPath)
		if partial:
			fileSize: int = int(os.fstat(fh).st_size)
			try:
				ensureRange(
					itemPath,
					config.remotePath(itemPath),
					fh,
					offset,
					size,
					fileSize,
				)
			except FastOneDriveError as error:
				logError(str(error), extra={"path": itemPath})
				raise FuseOSError(errno.EIO) from error
		return os.pread(fh, size, offset)

	def write(self, path: str, data: bytes, offset: int, fh: int) -> int:
		partial: bool = False
		itemPath: str = _itemPath(path)
		self._noteWriteIntent(itemPath, fh)
		with self._handleLock:
			handle: dict[str, Any] | None = self._handles.get(fh)
			if handle is not None:
				partial = bool(handle.get("partial"))
				itemPath = str(handle.get("path") or itemPath)
		if partial and len(data) > 0:
			first: int = offset // CHUNK_SIZE
			last: int = (offset + len(data) - 1) // CHUNK_SIZE
			with chunkLocks(itemPath, first, last):
				written: int = os.pwrite(fh, data, offset)
				noteWrite(itemPath, offset, written)
		else:
			written = os.pwrite(fh, data, offset)
		with self._handleLock:
			stored: dict[str, Any] | None = self._handles.get(fh)
			if stored is not None:
				stored["dirty"] = True
				if stored.get("partial"):
					ranges: object = stored.get("writes")
					if isinstance(ranges, list):
						ranges.append((offset, written))
		return written

	def truncate(self, path: str, length: int, fh: int | None = None) -> None:
		itemPath: str = _itemPath(path)
		if fh is not None:
			self._noteWriteIntent(itemPath, fh)
			os.ftruncate(fh, length)
			return
		guardFd: int = -1
		if not isEphemeralPath(itemPath):
			guardFd = openWriteGuard(itemPath)
			markDirty(itemPath)
		try:
			local: Path | None = _existingLocal(itemPath)
			pinnedFile: Path = pinnedLocalPath(itemPath)
			if local is not None and isPartial(itemPath) and not pinnedFile.exists():
				partialFd: int = os.open(str(local), os.O_RDWR)
				try:
					os.ftruncate(partialFd, length)
					fillHoles(
						itemPath,
						config.remotePath(itemPath),
						partialFd,
						[],
					)
				except FastOneDriveError as error:
					logError(str(error), extra={"path": itemPath})
					raise FuseOSError(errno.EIO) from error
				finally:
					os.close(partialFd)
			else:
				if local is None:
					if _isThumbnailer():
						raise FuseOSError(errno.EACCES)
					local = _backingPath(itemPath)
					local.parent.mkdir(parents=True, exist_ok=True)
					local.touch()
				os.truncate(str(local), length)
		finally:
			closeWriteGuard(guardFd)
		if isEphemeralPath(itemPath) or isPartial(itemPath):
			return
		scheduleItemSync(itemPath)

	def flush(self, path: str, fh: int) -> None:
		os.fsync(fh)

	def fsync(self, path: str, datasync: int, fh: int) -> None:
		if datasync:
			os.fdatasync(fh)
		else:
			os.fsync(fh)

	def release(self, path: str, fh: int) -> None:
		itemPath: str = _itemPath(path)
		dirty: bool = False
		partial: bool = False
		guardFd: int = -1
		writes: list[tuple[int, int]] = []
		with self._handleLock:
			handle: dict[str, Any] | None = self._handles.pop(fh, None)
			if handle is not None:
				dirty = bool(handle.get("dirty"))
				partial = bool(handle.get("partial"))
				itemPath = str(handle.get("path") or itemPath)
				guardRaw: object = handle.get("guard")
				if isinstance(guardRaw, int):
					guardFd = guardRaw
				rawWrites: object = handle.get("writes")
				if isinstance(rawWrites, list):
					writes = [(int(item[0]), int(item[1])) for item in rawWrites]
		fillError: FastOneDriveError | None = None
		stillPartial: bool = False
		readOnlyPartial: bool = partial and not dirty and not isEphemeralPath(itemPath)
		try:
			if readOnlyPartial:
				suppressPath(itemPath)
			if partial and dirty and not isEphemeralPath(itemPath):
				try:
					fillHoles(
						itemPath,
						config.remotePath(itemPath),
						fh,
						writes,
					)
				except FastOneDriveError as error:
					logError(str(error), extra={"path": itemPath})
					fillError = error
					dirty = True
				stillPartial = isPartial(itemPath)
			if stillPartial:
				suppressPath(itemPath)
			try:
				os.close(fh)
			except OSError:
				pass
		finally:
			closeWriteGuard(guardFd)
		if fillError is not None:
			if not isEphemeralPath(itemPath):
				markDirty(itemPath)
			raise FuseOSError(errno.EIO) from fillError
		if readOnlyPartial:
			return
		if stillPartial and not isEphemeralPath(itemPath):
			markDirty(itemPath)
			return
		if dirty and not isEphemeralPath(itemPath):
			scheduleItemSync(itemPath)

	def unlink(self, path: str) -> None:
		itemPath: str = _itemPath(path)
		if isEphemeralPath(itemPath):
			local: Path | None = _existingLocal(itemPath)
			if local is not None and local.is_file():
				local.unlink(missing_ok=True)
			self._invalidate(parentPath(itemPath))
			return
		try:
			removeRemotePath(config.remotePath(itemPath), True)
		except FastOneDriveError as error:
			text: str = str(error).lower()
			if "not found" not in text and "não encontrado" not in text:
				raise FuseOSError(errno.EIO) from error
			stored: Path
			for stored in (pinnedLocalPath(itemPath), cacheLocalPath(itemPath)):
				if not stored.is_file() and not stored.is_symlink():
					continue
				try:
					stored.unlink()
				except OSError as unlinkError:
					raise FuseOSError(unlinkError.errno or errno.EIO) from unlinkError
			if pinnedLocalPath(itemPath).exists() or cacheLocalPath(itemPath).exists():
				raise FuseOSError(errno.EIO) from error
			deleteIndexTree(itemPath)
		self._invalidate(parentPath(itemPath))

	def mkdir(self, path: str, mode: int) -> None:
		itemPath: str = _itemPath(path)
		ephemeral: bool = isEphemeralPath(itemPath)
		if not ephemeral:
			try:
				mkdirRemotePath(config.remotePath(itemPath))
			except FastOneDriveError as error:
				raise FuseOSError(errno.EIO) from error
		backing: Path = _backingPath(itemPath)
		try:
			backing.mkdir(parents=True, exist_ok=True)
			os.chmod(backing, mode)
		except OSError as error:
			if backing.is_dir():
				try:
					backing.rmdir()
				except OSError:
					pass
			raise FuseOSError(error.errno or errno.EIO) from error
		self._invalidate(parentPath(itemPath))

	def rmdir(self, path: str) -> None:
		itemPath: str = _itemPath(path)
		if itemPath == "":
			raise FuseOSError(errno.EPERM)
		local: Path | None = _existingLocal(itemPath)
		if local is not None and local.is_dir() and _dirHasChild(local):
			raise FuseOSError(errno.ENOTEMPTY)
		if isEphemeralPath(itemPath):
			if local is not None and local.is_dir():
				try:
					local.rmdir()
				except OSError as error:
					raise FuseOSError(error.errno or errno.ENOTEMPTY) from error
			self._invalidate(parentPath(itemPath))
			return
		if hasDescendants(itemPath):
			raise FuseOSError(errno.ENOTEMPTY)
		removedLocal: Path | None = None
		if local is not None and local.is_dir():
			try:
				local.rmdir()
			except OSError as error:
				raise FuseOSError(error.errno or errno.ENOTEMPTY) from error
			removedLocal = local
		try:
			rmdirRemote(config.remotePath(itemPath))
		except FastOneDriveError as error:
			text: str = str(error).lower()
			missing: bool = "not found" in text or "não encontrado" in text
			if not missing and removedLocal is not None:
				removedLocal.mkdir(parents=True, exist_ok=True)
			if missing:
				pass
			elif "not empty" in text or "não está vazia" in text:
				raise FuseOSError(errno.ENOTEMPTY) from error
			else:
				raise FuseOSError(errno.EIO) from error
		try:
			deleteIndexTree(itemPath)
		except FastOneDriveError:
			pass
		self._invalidate(parentPath(itemPath))

	def rename(self, old: str, new: str) -> None:
		srcPath: str = _itemPath(old)
		destPath: str = _itemPath(new)
		if srcPath == destPath:
			return
		srcLocal: Path | None = _existingLocal(srcPath)
		destLocal: Path = _backingPath(destPath)
		srcIsTemp: bool = isEphemeralPath(srcPath)
		destIsTemp: bool = isEphemeralPath(destPath)
		if srcIsTemp and not destIsTemp:
			self._renameAtomicSave(srcPath, destPath, srcLocal, destLocal)
			return
		if srcIsTemp and destIsTemp:
			_replaceLocal(srcLocal, destLocal)
			self._invalidate(parentPath(destPath))
			return
		if destIsTemp:
			_linkTempAlias(srcLocal, destLocal)
			self._invalidate(parentPath(destPath))
			return
		entry = getEntry(srcPath)
		if _indexedOnCloud(entry):
			try:
				moveRemotePath(
					config.remotePath(srcPath),
					config.remotePath(destPath),
					getEntry(destPath) is not None,
				)
			except FastOneDriveError as error:
				raise FuseOSError(errno.EIO) from error
			self._invalidate(parentPath(srcPath))
			self._invalidate(parentPath(destPath))
			return
		self._renameUntracked(srcPath, destPath, srcLocal, destLocal, entry)

	def _renameAtomicSave(self, srcPath: str, destPath: str, srcLocal: Path | None, destLocal: Path) -> None:
		_replaceLocal(srcLocal, destLocal)
		if getEntry(destPath) is None and destLocal.is_file():
			upsertEntry(
				destPath,
				destLocal.name,
				False,
				int(destLocal.stat().st_size),
				storedPinFlag(destPath),
			)
		markDirty(destPath)
		scheduleItemSync(destPath)
		self._invalidate(parentPath(destPath))
		if parentPath(srcPath) != parentPath(destPath):
			self._invalidate(parentPath(srcPath))

	def _renameUntracked(
		self,
		srcPath: str,
		destPath: str,
		srcLocal: Path | None,
		destLocal: Path,
		entry: object,
	) -> None:
		_replaceLocal(srcLocal, destLocal)
		if entry is not None:
			try:
				renameIndexPath(srcPath, destPath)
			except FastOneDriveError as error:
				raise FuseOSError(errno.EIO) from error
		elif destLocal.is_file():
			upsertEntry(
				destPath,
				destLocal.name,
				False,
				int(destLocal.stat().st_size),
				storedPinFlag(destPath),
			)
		markDirty(destPath)
		scheduleItemSync(destPath)
		self._invalidate(parentPath(srcPath))
		self._invalidate(parentPath(destPath))

	def chmod(self, path: str, mode: int) -> None:
		itemPath: str = _itemPath(path)
		try:
			chmodItem(itemPath, mode)
		except FastOneDriveError:
			local: Path | None = _existingLocal(itemPath)
			if local is None:
				return
			os.chmod(local, mode)

	def statfs(self, path: str) -> dict[str, int]:
		st = os.statvfs(config.cacheDir())
		return {
			"f_bsize": int(st.f_bsize),
			"f_frsize": int(st.f_frsize),
			"f_blocks": int(st.f_blocks),
			"f_bfree": int(st.f_bfree),
			"f_bavail": int(st.f_bavail),
			"f_files": int(st.f_files),
			"f_ffree": int(st.f_ffree),
			"f_favail": int(st.f_favail),
			"f_flag": int(st.f_flag),
			"f_namemax": int(st.f_namemax),
		}

	def _registerHandle(
		self,
		itemPath: str,
		fd: int,
		dirty: bool,
		partial: bool = False,
		guardFd: int = -1,
	) -> int:
		with self._handleLock:
			fh: int = fd
			self._handles[fh] = {
				"path": itemPath,
				"fd": fd,
				"dirty": dirty,
				"partial": partial,
				"guard": guardFd,
				"writes": [],
			}
			return fh

	def _openForWrite(
		self,
		itemPath: str,
		fd: int,
		partial: bool,
		guardFd: int = -1,
		truncated: bool = False,
	) -> int:
		if not isEphemeralPath(itemPath):
			if guardFd < 0:
				guardFd = openWriteGuard(itemPath)
			if truncated:
				markDirty(itemPath)
		return self._registerHandle(itemPath, fd, truncated, partial, guardFd)

	def _noteWriteIntent(self, itemPath: str, fh: int) -> None:
		if isEphemeralPath(itemPath):
			return
		needsGuard: bool = False
		with self._handleLock:
			handle: dict[str, Any] | None = self._handles.get(fh)
			if handle is not None:
				handle["dirty"] = True
				needsGuard = int(handle.get("guard") or -1) < 0
		if needsGuard:
			guardFd: int = openWriteGuard(itemPath)
			with self._handleLock:
				stored: dict[str, Any] | None = self._handles.get(fh)
				if stored is None or int(stored.get("guard") or -1) >= 0:
					closeWriteGuard(guardFd)
				else:
					stored["guard"] = guardFd
		markDirty(itemPath)

	def _refreshListing(self, itemPath: str) -> None:
		scheduleParentRefresh(itemPath)

	def _invalidate(self, itemPath: str) -> None:
		invalidateListing(itemPath)


def runFuse(mountPoint: Path | None = None) -> None:
	target: Path = mountPoint if mountPoint is not None else config.mountPath()
	target.mkdir(parents=True, exist_ok=True)
	config.ensurePinDir()
	config.ensureCacheDir()
	startLocalWatch()
	logInfo("fuse start", extra={"mount": str(target)})
	FUSE(
		FastOneDriveFS(),
		str(target),
		foreground=True,
		fsname="fastonedrive",
		subtype="fastonedrive",
	)


def _itemPath(path: str) -> str:
	return path.strip("/")


def _indexedOnCloud(entry: object) -> bool:
	if entry is None:
		return False
	try:
		if str(entry["itemId"] or "") != "":  # type: ignore[index]
			return True
		if str(entry["baseEtag"] or "") != "":  # type: ignore[index]
			return True
	except (KeyError, IndexError, TypeError):
		return False
	return False


def _dirHasChild(folder: Path) -> bool:
	try:
		for child in folder.iterdir():
			if child.name not in ("", ".", ".."):
				return True
	except OSError:
		return True
	return False


def _replaceLocal(srcLocal: Path | None, destLocal: Path) -> None:
	if srcLocal is None:
		return
	destLocal.parent.mkdir(parents=True, exist_ok=True)
	if destLocal.exists() and destLocal != srcLocal and destLocal.is_dir():
		raise FuseOSError(errno.EISDIR)
	srcLocal.replace(destLocal)


def _linkTempAlias(srcLocal: Path | None, destLocal: Path) -> None:
	if srcLocal is None or not srcLocal.exists():
		return
	destLocal.parent.mkdir(parents=True, exist_ok=True)
	if destLocal.exists() and destLocal != srcLocal:
		if destLocal.is_dir():
			raise FuseOSError(errno.EISDIR)
		destLocal.unlink()
	try:
		os.link(srcLocal, destLocal)
	except OSError:
		shutil.copy2(srcLocal, destLocal)


def _completeLocal(itemPath: str) -> Path | None:
	pinned: Path = pinnedLocalPath(itemPath)
	if pinned.exists():
		return pinned
	cached: Path = cacheLocalPath(itemPath)
	if cached.is_file() and not isPartial(itemPath):
		return cached
	return None


def _holdShared(fd: int) -> None:
	try:
		fcntl.flock(fd, fcntl.LOCK_SH)
	except OSError:
		return


def _existingLocal(itemPath: str) -> Path | None:
	pinned: Path = pinnedLocalPath(itemPath)
	if pinned.exists():
		return pinned
	cached: Path = cacheLocalPath(itemPath)
	if cached.exists():
		return cached
	return None


def _backingPath(itemPath: str) -> Path:
	if shouldStorePinned(itemPath):
		return pinnedLocalPath(itemPath)
	return cacheLocalPath(itemPath)


def _statFromOs(info: os.stat_result) -> dict[str, Any]:
	return {
		"st_mode": info.st_mode,
		"st_nlink": info.st_nlink,
		"st_size": info.st_size,
		"st_ctime": int(info.st_ctime),
		"st_mtime": int(info.st_mtime),
		"st_atime": int(info.st_atime),
		"st_uid": info.st_uid,
		"st_gid": info.st_gid,
	}


def _statDir(mtime: int) -> dict[str, Any]:
	return {
		"st_mode": stat.S_IFDIR | 0o755,
		"st_nlink": 2,
		"st_size": 0,
		"st_ctime": mtime,
		"st_mtime": mtime,
		"st_atime": mtime,
		"st_uid": os.getuid(),
		"st_gid": os.getgid(),
	}


def _statFromIndex(entry: Any) -> dict[str, Any]:
	isDir: bool = bool(entry["isDir"])
	mtime: int = int(time.time())
	parsed = parseRemoteModTime(str(entry["modTime"] or ""))
	if parsed is not None:
		mtime = int(parsed.timestamp())
	modeBits: int = 0o755 if isDir else 0o644
	stored: int | None = storedUnixMode(entry)
	if stored is not None:
		modeBits = stored
	mode: int = (stat.S_IFDIR | modeBits) if isDir else (stat.S_IFREG | modeBits)
	return {
		"st_mode": mode,
		"st_nlink": 2 if isDir else 1,
		"st_size": 0 if isDir else int(entry["size"] or 0),
		"st_ctime": mtime,
		"st_mtime": mtime,
		"st_atime": mtime,
		"st_uid": os.getuid(),
		"st_gid": os.getgid(),
	}


def _isThumbnailer() -> bool:
	try:
		pid, _, _ = fuse_get_context()
	except Exception:
		return False
	comm: str = ""
	try:
		comm = Path(f"/proc/{int(pid)}/comm").read_text(encoding="utf-8").strip()
	except OSError:
		return False
	if comm in THUMB_COMMS:
		return True
	lower: str = comm.lower()
	if "thumb" in lower or lower.startswith("baloo") or lower.startswith("tracker-miner"):
		return True
	if lower != "kioworker" and not lower.startswith("kioworker"):
		return False
	try:
		raw: bytes = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
	except OSError:
		return False
	command: str = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").lower()
	return "thumbnail" in command or "preview" in command
