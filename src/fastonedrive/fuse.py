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
	truncatePartial,
)
from fastonedrive.disk import (
	closeWriteGuard,
	isEphemeralName,
	isEphemeralPath,
	openWriteGuard,
	suppressPath,
)
from fastonedrive.hydrate import cleanCacheIsStale, ensureCacheBaseline, hydrateRemote
from fastonedrive.index import (
	SYNC_ERROR,
	deleteIndexTree,
	getEntry,
	hasDescendants,
	markDirty,
	parentPath,
	renameIndexPath,
	scheduleParentRefresh,
	setSyncState,
	storedUnixMode,
	upsertEntry,
)
from fastonedrive.log import logError, logInfo
from fastonedrive.mutate import mkdirRemoteFast, moveRemotePath, removeRemotePath
from fastonedrive.pendingops import OP_DELETE, OP_MOVE, OP_RMDIR
from fastonedrive.pin import shouldStorePinned, storedPinFlag
from fastonedrive.rangeread import fetchRange
from fastonedrive.readahead import Readahead
from fastonedrive.remotewatch import RemoteWatcher
from fastonedrive.rclone import parseRemoteModTime, rmdirRemote
from fastonedrive.search import cacheLocalPath, chmodItem, itemStatus, listChildren, pinnedLocalPath
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


STATE_XATTR: str = "user.fastonedrive.state"
# Leituras "de passagem" (o Dolphin a ver o ficheiro seleccionado): descritores acima
# deste número não têm ficheiro local; blocos de 256 KiB ficam só em memória.
STREAM_FH_BASE: int = 1 << 30
STREAM_BLOCK: int = 256 * 1024
STREAM_KEEP_BLOCKS: int = 16
STREAM_MAX_BYTES: int = 32 * 1024 * 1024
BROWSING_COMMS: frozenset[str] = frozenset({"dolphin", "plasmashell", "krunner", "kded6", "baloorunner"})
FUSE_ATTR_TIMEOUT: float = 2.0
FUSE_ENTRY_TIMEOUT: float = 2.0
FUSE_NEGATIVE_TIMEOUT: float = 1.0
# Apagar muitos ficheiros de seguida em ~/OneDrive apaga-os na nuvem. Como no Windows,
# avisa e aponta para a reciclagem do OneDrive.
SLOW_OP_SEC: float = 0.25
OFFLINE_RETRY_SEC: float = 60.0
MASS_DELETE_COUNT: int = 50
MASS_DELETE_WINDOW_SEC: float = 120.0


class FastOneDriveFS(Operations):
	_handles: dict[int, dict[str, Any]]
	_handleLock: threading.Lock
	_nextFh: int

	def __init__(self, mountPoint: Path | None = None, profileId: str = "") -> None:
		self._handles = {}
		self._handleLock = threading.Lock()
		self._nextFh = 1
		self._readahead = Readahead()
		self._mountPoint: Path = mountPoint if mountPoint is not None else config.mountPath()
		self._profileId: str = profileId
		self._remoteWatch: RemoteWatcher | None = None
		self._deleteTimes: list[float] = []
		self._deleteWarned: float = 0.0

	def __call__(self, op: str, *args: Any) -> Any:
		# Mede cada operação: as que passam de SLOW_OP_SEC ficam no log com o processo
		# que as pediu ("fuse lento"), para achar o que trava o Dolphin/editor.
		started: float = time.monotonic()
		try:
			return super().__call__(op, *args)
		finally:
			elapsed: float = time.monotonic() - started
			if elapsed >= SLOW_OP_SEC:
				try:
					process: str = _callerInfo()[0]
				except Exception:  # noqa: BLE001
					process = ""
				logInfo(
					"fuse lento",
					extra={
						"op": op,
						"path": str(args[0]) if len(args) > 0 else "",
						"ms": int(elapsed * 1000),
						"process": process,
					},
				)

	def init(self, path: str) -> None:
		from fastonedrive import sdnotify

		sdnotify.ready(f"montado em {self._mountPoint}")
		self._remoteWatch = RemoteWatcher(self._profileId, self._mountPoint)
		self._remoteWatch.start()

	def destroy(self, path: str) -> None:
		from fastonedrive import sdnotify

		sdnotify.stopping()
		if self._remoteWatch is not None:
			self._remoteWatch.stop()

	def _noteDelete(self) -> None:
		now: float = time.monotonic()
		with self._handleLock:
			self._deleteTimes = [stamp for stamp in self._deleteTimes if now - stamp < MASS_DELETE_WINDOW_SEC]
			self._deleteTimes.append(now)
			count: int = len(self._deleteTimes)
			if count < MASS_DELETE_COUNT or now - self._deleteWarned < MASS_DELETE_WINDOW_SEC:
				return
			self._deleteWarned = now
		threading.Thread(target=_warnMassDelete, args=(count,), daemon=True).start()

	def getattr(self, path: str, fh: int | None = None) -> dict[str, Any]:
		itemPath: str = _itemPath(path)
		if itemPath == "":
			return _statDir(int(time.time()))
		if _isTrashDir(itemPath.split("/", 1)[0]):
			# Invisível para o KIO: sem lixo local nesta pasta (ver mkdir).
			raise FuseOSError(errno.ENOENT)
		if fh is not None:
			handle: dict[str, Any] | None = self._handles.get(fh)
			if handle is not None and handle.get("stream"):
				return dict(handle["stat"])
			if handle is not None:
				return _statFromOs(os.fstat(int(handle["fd"])))
		local: Path | None = _existingLocal(itemPath)
		if local is not None:
			return _statFromOs(local.lstat())
		if isEphemeralPath(itemPath):
			raise FuseOSError(errno.ENOENT)
		entry = getEntry(itemPath)
		if entry is None or _localOnlyOrphan(entry):
			raise FuseOSError(errno.ENOENT)
		return _statFromIndex(entry)

	def readdir(self, path: str, fh: int) -> list[str]:
		itemPath: str = _itemPath(path)
		self._refreshListing(itemPath)
		names: set[str] = set()
		for row in listChildren(itemPath):
			name: str = str(row["name"])
			if name != "" and not isEphemeralName(name) and not _localOnlyOrphan(row):
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
			if (
				local is not None
				and local == cacheLocalPath(itemPath)
				and not _isThumbnailer()
				and not isEphemeralPath(itemPath)
				and _indexedOnCloud(getEntry(itemPath))
				and not self._indexConfirmsCache(itemPath)
			):
				# Só uma cópia de um item da nuvem pode estar velha. Sem linha (o .part do
				# Dolphin, temporários) isCacheDirty fazia um rclone stat e o
				# cleanCacheIsStale esperava o cache.lock: abrir o .part levava 8 s.
				if cleanCacheIsStale(itemPath):
					local = None
			if local is not None:
				if writable and local == cacheLocalPath(itemPath):
					ensureCacheBaseline(itemPath)
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
			browsing: str = "" if writable else _browsingMode()
			if browsing != "":
				# O próprio Dolphin lê ficheiros só por os seleccionar (painel de
				# informação, detecção do tipo, menu "Abrir com"). Como no Explorer, isso
				# não pode descarregar o ficheiro para o PC: lê da nuvem sem guardar.
				streamEntry = getEntry(itemPath)
				if streamEntry is None:
					raise FuseOSError(errno.ENOENT)
				if bool(streamEntry["isDir"]):
					raise FuseOSError(errno.EISDIR)
				return self._registerStream(
					itemPath,
					int(streamEntry["size"] or 0),
					streamEntry,
					headless=browsing == "ui",
				)
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
		if _isTrashDir(_itemPath(path).split("/", 1)[0]):
			raise FuseOSError(errno.EACCES)
		return self._create(path, mode, fi)

	def _create(self, path: str, mode: int, fi: object | None = None) -> int:
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
		dirtyHandle: bool = False
		filled: set[int] | None = None
		itemPath: str = _itemPath(path)
		with self._handleLock:
			handle: dict[str, Any] | None = self._handles.get(fh)
			if handle is not None and handle.get("stream"):
				streamHandle: dict[str, Any] = handle
			else:
				streamHandle = {}
			if handle is not None and not handle.get("stream"):
				partial = bool(handle.get("partial"))
				dirtyHandle = bool(handle.get("dirty")) or int(handle.get("guard") or -1) >= 0
				itemPath = str(handle.get("path") or itemPath)
				filled = handle.setdefault("filled", set())
		if streamHandle:
			return self._readStream(streamHandle, size, offset)
		if partial:
			fileSize: int = int(os.fstat(fh).st_size)
			remotePath: str = config.remotePath(itemPath)
			first: int = offset // CHUNK_SIZE
			last: int = max(first, (min(offset + size, fileSize) - 1) // CHUNK_SIZE)
			needed: range = range(first, last + 1)
			# Um bloco já cheio não volta a ter buracos neste handle: o kernel pede
			# 128 KiB de cada vez e cada pedido consultava o SQLite duas vezes.
			if filled is None or not all(index in filled for index in needed):
				try:
					ensureRange(
						itemPath,
						remotePath,
						fh,
						offset,
						size,
						fileSize,
					)
				except FastOneDriveError as error:
					logError(str(error), extra={"path": itemPath})
					raise FuseOSError(errno.EIO) from error
				if filled is not None:
					filled.update(needed)
			if not dirtyHandle:
				self._readahead.onRead(fh, itemPath, remotePath, offset, size, fileSize)
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
			try:
				truncatePartial(
					itemPath,
					config.remotePath(itemPath),
					fh,
					length,
				)
			except FastOneDriveError as error:
				logError(str(error), extra={"path": itemPath})
				raise FuseOSError(errno.EIO) from error
			with self._handleLock:
				stored: dict[str, Any] | None = self._handles.get(fh)
				if stored is not None:
					stored["partial"] = False
			return
		guardFd: int = -1
		if not isEphemeralPath(itemPath):
			guardFd = openWriteGuard(itemPath)
			markDirty(itemPath)
		try:
			local: Path | None = _existingLocal(itemPath)
			pinnedFile: Path = pinnedLocalPath(itemPath)
			if local is None and length > 0:
				if _isThumbnailer():
					raise FuseOSError(errno.EACCES)
				entry = getEntry(itemPath)
				if entry is None or bool(entry["isDir"]):
					raise FuseOSError(errno.ENOENT)
				try:
					local = prepareSparse(
						itemPath,
						int(entry["size"] or 0),
						str(entry["etag"] or ""),
					)
				except FastOneDriveError as error:
					logError(str(error), extra={"path": itemPath})
					raise FuseOSError(errno.EIO) from error
			if local is not None and isPartial(itemPath) and not pinnedFile.exists():
				partialFd: int = os.open(str(local), os.O_RDWR)
				try:
					truncatePartial(
						itemPath,
						config.remotePath(itemPath),
						partialFd,
						length,
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
		if fh >= STREAM_FH_BASE:
			return
		os.fsync(fh)

	def fsync(self, path: str, datasync: int, fh: int) -> None:
		if fh >= STREAM_FH_BASE:
			return
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
		if fh >= STREAM_FH_BASE:
			with self._handleLock:
				self._handles.pop(fh, None)
			return
		self._readahead.forget(fh)
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
		if _unlinkFast(itemPath):
			self._invalidate(parentPath(itemPath))
			self._noteDelete()
			return
		try:
			removeRemotePath(config.remotePath(itemPath), True, discardLocalEdits=True)
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
		self._noteDelete()

	def mkdir(self, path: str, mode: int) -> None:
		itemPath: str = _itemPath(path)
		if _isTrashDir(itemPath):
			# O "Mover para o lixo" do KIO criaria ~/OneDrive/.Trash-<uid> e moveria os
			# ficheiros para lá — na nuvem. Sem esta pasta, o Dolphin oferece apagar, e
			# o OneDrive guarda o apagado na sua Reciclagem (como no Windows).
			raise FuseOSError(errno.EACCES)
		ephemeral: bool = isEphemeralPath(itemPath)
		backing: Path = _backingPath(itemPath)
		try:
			backing.mkdir(parents=True, exist_ok=True)
			os.chmod(backing, mode)
		except OSError as error:
			raise FuseOSError(error.errno or errno.EIO) from error
		if not ephemeral:
			upsertEntry(
				itemPath,
				backing.name,
				True,
				0,
				storedPinFlag(itemPath),
			)
			markDirty(itemPath)
			_scheduleRemoteMkdir(itemPath)
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
		if self._rmdirFast(itemPath, local):
			return
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

	def _rmdirFast(self, itemPath: str, local: Path | None) -> bool:
		from fastonedrive import graphitems
		from fastonedrive.mutate import FolderNotEmpty, rmdirRemoteFast

		entry = getEntry(itemPath)
		if entry is None:
			return False
		neverSent: bool = str(entry["itemId"] or "") == "" and str(entry["syncState"] or "") == "dirty"
		if not neverSent:
			try:
				if not rmdirRemoteFast(itemPath):
					return False
			except FolderNotEmpty as error:
				raise FuseOSError(errno.ENOTEMPTY) from error
			except graphitems.GraphUnreachable:
				_queueOffline(OP_RMDIR, itemPath)
		# Pasta criada aqui que ainda não subiu: o mkdir em segundo plano vê que já
		# não é querida (_mkdirStillWanted) e não a cria.
		if local is not None and local.is_dir():
			try:
				local.rmdir()
			except OSError as error:
				raise FuseOSError(error.errno or errno.ENOTEMPTY) from error
		deleteIndexTree(itemPath)
		self._invalidate(parentPath(itemPath))
		return True

	def rename(self, old: str, new: str) -> None:
		srcPath: str = _itemPath(old)
		destPath: str = _itemPath(new)
		if srcPath == destPath:
			return
		if _isTrashDir(destPath.split("/", 1)[0]):
			raise FuseOSError(errno.EACCES)
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
			if self._renameCloudFast(srcPath, destPath):
				return
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

	def _renameCloudFast(self, srcPath: str, destPath: str) -> bool:
		from fastonedrive import graphitems
		from fastonedrive.mutate import moveRemoteFast

		try:
			moved: bool = moveRemoteFast(srcPath, destPath)
		except graphitems.GraphUnreachable:
			moved = _renameOffline(srcPath, destPath)
		if not moved:
			return False
		self._invalidate(parentPath(srcPath))
		if parentPath(srcPath) != parentPath(destPath):
			self._invalidate(parentPath(destPath))
		return True

	def _renameAtomicSave(self, srcPath: str, destPath: str, srcLocal: Path | None, destLocal: Path) -> None:
		# O editor grava um temporário e renomeia por cima: a base é a versão que estava
		# no disco antes da troca.
		ensureCacheBaseline(destPath)
		_replaceLocal(srcLocal, destLocal)
		deleteChunkRow(destPath)
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
		if (
			srcLocal is not None
			and srcLocal.is_file()
			and _indexedOnCloud(getEntry(destPath))
		):
			self._renameAtomicSave(srcPath, destPath, srcLocal, destLocal)
			if entry is not None:
				deleteIndexTree(srcPath)
			return
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

	def getxattr(self, path: str, name: str, position: int = 0) -> bytes:
		# Estado para o plugin de emblemas do Dolphin e para scripts:
		#   getfattr -n user.fastonedrive.state ~/OneDrive/ficheiro
		# Não aparece no listxattr, para o KIO e o cp -a não copiarem o atributo
		# para fora do OneDrive.
		if name != STATE_XATTR:
			raise FuseOSError(errno.ENODATA)
		itemPath: str = _itemPath(path)
		try:
			state: str = itemStatus(itemPath)
		except Exception as error:  # noqa: BLE001 — o emblema nunca deve derrubar o FUSE
			logError(f"xattr de estado falhou: {error}", extra={"path": itemPath})
			raise FuseOSError(errno.EIO) from error
		if state == "":
			raise FuseOSError(errno.ENODATA)
		return state.encode("utf-8")

	def listxattr(self, path: str) -> list[str]:
		return []

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

	def _registerStream(self, itemPath: str, size: int, entry: Any, headless: bool = False) -> int:
		with self._handleLock:
			fh: int = STREAM_FH_BASE + self._nextFh
			self._nextFh += 1
			self._handles[fh] = {
				"path": itemPath,
				"stream": True,
				"headless": headless,
				"size": size,
				"stat": _statFromIndex(entry),
				"blocks": {},
				"fetched": 0,
			}
			return fh

	def _readStream(self, handle: dict[str, Any], size: int, offset: int) -> bytes:
		if handle.get("headless"):
			# Thread da interface do Dolphin (detecção do tipo ao clicar com o botão
			# direito): sem conteúdo, o tipo sai da extensão, como no Explorer. Ir à nuvem
			# aqui congelava a janela 1–2 s.
			return b""
		itemPath: str = str(handle["path"])
		total: int = int(handle["size"])
		end: int = min(offset + size, total)
		if offset >= end:
			return b""
		blocks: dict[int, bytes] = handle["blocks"]
		out: bytearray = bytearray()
		position: int = offset
		while position < end:
			index: int = position // STREAM_BLOCK
			data: bytes | None = blocks.get(index)
			if data is None:
				if int(handle["fetched"]) >= STREAM_MAX_BYTES:
					# Um "só a ver" que já leu muito: parar aqui em vez de puxar o ficheiro.
					raise FuseOSError(errno.EIO)
				start: int = index * STREAM_BLOCK
				count: int = min(STREAM_BLOCK, total - start)
				try:
					data = fetchRange(itemPath, config.remotePath(itemPath), start, count)
				except FastOneDriveError as error:
					logError(str(error), extra={"path": itemPath})
					raise FuseOSError(errno.EIO) from error
				handle["fetched"] = int(handle["fetched"]) + len(data)
				if len(blocks) >= STREAM_KEEP_BLOCKS:
					blocks.pop(next(iter(blocks)))
				blocks[index] = data
			inner: int = position - index * STREAM_BLOCK
			take: int = min(len(data) - inner, end - position)
			if take <= 0:
				break
			out.extend(data[inner : inner + take])
			position += take
		return bytes(out)

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
				# create/truncate já marcaram dirty no índice.
				"marked": dirty and not isEphemeralPath(itemPath),
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
		needsMark: bool = True
		with self._handleLock:
			handle: dict[str, Any] | None = self._handles.get(fh)
			if handle is not None:
				handle["dirty"] = True
				needsGuard = int(handle.get("guard") or -1) < 0
				# Cada write() fazia um commit no SQLite e um busctl: um ficheiro de 1 GiB
				# eram mil commits. Basta marcar uma vez por handle.
				needsMark = not bool(handle.get("marked"))
				handle["marked"] = True
		if needsGuard:
			guardFd: int = openWriteGuard(itemPath)
			with self._handleLock:
				stored: dict[str, Any] | None = self._handles.get(fh)
				if stored is None or int(stored.get("guard") or -1) >= 0:
					closeWriteGuard(guardFd)
				else:
					stored["guard"] = guardFd
		if needsMark:
			markDirty(itemPath)

	def _indexConfirmsCache(self, itemPath: str) -> bool:
		"""Com o poll do delta em dia, o índice já diz se a nuvem mudou: se a versão da
		nuvem é a base da cópia local, abrir não precisa de perguntar à nuvem (cada
		abertura no editor custava um stat remoto sob o cacheJobsLock)."""
		if self._remoteWatch is None or not self._remoteWatch.healthy():
			return False
		entry = getEntry(itemPath)
		if entry is None:
			return False
		from fastonedrive.rclone import sameToken

		base: str = str(entry["baseEtag"] or "")
		return base != "" and sameToken(base, str(entry["etag"] or ""))

	def _refreshListing(self, itemPath: str) -> None:
		# Com o poll do delta a funcionar, o índice já está fresco (≤ 1 min): um
		# lsjson por pasta só pesava em travessias recursivas (find, busca do Dolphin).
		if self._remoteWatch is not None and self._remoteWatch.healthy():
			return
		scheduleParentRefresh(itemPath)

	def _invalidate(self, itemPath: str) -> None:
		from fastonedrive.notify import notifyLocalDirsChanged

		# Sem invalidateListing: o índice já tem a mudança local. Limpar o TTL fazia o
		# relistar do Dolphin (pedido pelo FilesAdded abaixo) lançar logo um
		# refresh-parent + lsjson por cada criar/renomear, sobretudo com o poll em pausa.
		mount: Path = config.mountPath()
		folder: str = str(mount / itemPath) if itemPath != "" else str(mount)
		notifyLocalDirsChanged([folder])


def runFuse(mountPoint: Path | None = None) -> None:
	target: Path = mountPoint if mountPoint is not None else config.mountPath()
	target.mkdir(parents=True, exist_ok=True)
	config.ensurePinDir()
	config.ensureCacheDir()
	startLocalWatch()
	logInfo("fuse start", extra={"mount": str(target)})
	profile: config.Profile | None = config.activeProfile()
	from fastonedrive.notify import useAsyncSignals

	useAsyncSignals()
	FUSE(
		FastOneDriveFS(target, profile.id if profile is not None else ""),
		str(target),
		foreground=True,
		fsname="fastonedrive",
		subtype="fastonedrive",
		# O índice é a fonte de verdade dos metadados: o kernel pode guardá-los uns
		# segundos em vez de chamar getattr/lookup em cada stat do Dolphin.
		attr_timeout=FUSE_ATTR_TIMEOUT,
		entry_timeout=FUSE_ENTRY_TIMEOUT,
		negative_timeout=FUSE_NEGATIVE_TIMEOUT,
		big_writes=True,
		hard_remove=True,
		max_write=1024 * 1024,
	)


def _isTrashDir(itemPath: str) -> bool:
	if "/" in itemPath:
		return False
	return itemPath == ".Trash" or (itemPath.startswith(".Trash-") and itemPath[7:].isdigit())


def _warnMassDelete(count: int) -> None:
	from fastonedrive.notify import notify

	notify(
		"OneDrive: muitos ficheiros apagados",
		f"Apagou {count} ficheiros do OneDrive nos últimos minutos. Também saíram da nuvem; "
		"pode recuperá-los na Reciclagem do OneDrive (onedrive.live.com → Reciclagem) durante 30 dias.",
	)


def _scheduleRemoteMkdir(itemPath: str) -> None:
	threading.Thread(
		target=_remoteMkdirWorker,
		args=(itemPath,),
		daemon=True,
		name=f"fod-mkdir:{itemPath}",
	).start()


def _mkdirStillWanted(itemPath: str) -> bool:
	entry = getEntry(itemPath)
	if entry is None or not bool(entry["isDir"]):
		return False
	return _existingLocal(itemPath) is not None


def _remoteMkdirWorker(itemPath: str) -> None:
	if not _mkdirStillWanted(itemPath):
		return
	from fastonedrive.synccontrol import pauseReason

	if pauseReason() != "":
		# Em pausa nada sobe (como no Windows): a pasta fica dirty e entra no mesmo
		# lote que os ficheiros quando a pausa acabar.
		from fastonedrive.watcher import debounceSync

		debounceSync(itemPath)
		return
	from fastonedrive import graphitems

	try:
		mkdirRemoteFast(itemPath)
	except graphitems.GraphUnreachable:
		# Sem rede: fica dirty (emblema "a sincronizar") e tenta de novo daqui a 1 min.
		from fastonedrive.watcher import debounceSync

		debounceSync(itemPath, OFFLINE_RETRY_SEC)
		return
	except FastOneDriveError as error:
		logError(str(error), extra={"path": itemPath, "step": "mkdir"})
		if _mkdirStillWanted(itemPath):
			setSyncState(itemPath, SYNC_ERROR, str(error))
		return
	except OSError as error:
		logError(str(error), extra={"path": itemPath, "step": "mkdir"})
		if _mkdirStillWanted(itemPath):
			setSyncState(itemPath, SYNC_ERROR, str(error))
		return
	if not _mkdirStillWanted(itemPath):
		try:
			rmdirRemote(config.remotePath(itemPath))
		except FastOneDriveError:
			pass


def _itemPath(path: str) -> str:
	return path.strip("/")


def _queueOffline(op: str, itemPath: str, dest: str = "") -> None:
	"""Sem rede: a operação fica na fila (pendingops) com o URL do item por id."""
	from fastonedrive import graphitems
	from fastonedrive.pendingops import enqueue

	enqueue(op, itemPath, graphitems.itemUrl(itemPath), dest)


def _renameOffline(srcPath: str, destPath: str) -> bool:
	from fastonedrive.disk import cacheJobsLock
	from fastonedrive.mutate import _moveLocalCopies

	if getEntry(destPath) is not None:
		# Substituir um item que existe: sem rede não dá para decidir com segurança.
		return False
	_queueOffline(OP_MOVE, srcPath, destPath)
	with cacheJobsLock():
		renameIndexPath(srcPath, destPath)
		_moveLocalCopies(srcPath, destPath)
	return True


def _unlinkFast(itemPath: str) -> bool:
	"""Apagar sem arrancar o rclone (o removeRemotePath fazia stat + deletefile, 14–21 s,
	com o cache.lock seguro, e tudo o resto que abre ficheiros esperava por ele).

	- Ficheiro criado aqui e nunca enviado: só disco e índice, sem rede.
	- Item com itemId: DELETE no Graph (~0,3 s) e depois disco e índice.
	Devolve False para o caminho antigo (envio a meio, pasta, Graph recusou).
	"""
	from fastonedrive import graphitems
	from fastonedrive.disk import cacheJobsLock, removePath
	from fastonedrive.notify import notifyDirChanged

	entry = getEntry(itemPath)
	if entry is None or bool(entry["isDir"]):
		return False
	state: str = str(entry["syncState"] or "")
	if state == "syncing":
		return False
	localOnly: bool = (
		str(entry["itemId"] or "") == "" and str(entry["baseEtag"] or "") == "" and str(entry["etag"] or "") == ""
	)
	if not localOnly:
		if str(entry["itemId"] or "") == "":
			return False
		try:
			graphitems.deleteItem(itemPath)
		except graphitems.GraphUnreachable:
			_queueOffline(OP_DELETE, itemPath)
		except FastOneDriveError as error:
			logError(f"DELETE Graph falhou, a usar rclone: {error}", extra={"path": itemPath})
			return False
	with cacheJobsLock():
		for stored in (pinnedLocalPath(itemPath), cacheLocalPath(itemPath)):
			if stored.is_file() or stored.is_symlink():
				removePath(stored, ignoreErrors=True)
		deleteChunkRow(itemPath)
		deleteIndexTree(itemPath)
	notifyDirChanged(itemPath)
	return True


def _localOnlyOrphan(entry: Any) -> bool:
	"""Linha de um ficheiro criado aqui que nunca subiu e cuja cópia local já não
	existe (rename/apagar interrompido). Mostrá-la dava um ficheiro de 0 bytes que
	não abre; o sync-pins apaga a linha."""
	try:
		if bool(entry["isDir"]):
			return False
		if str(entry["itemId"] or "") != "" or str(entry["etag"] or "") != "" or str(entry["baseEtag"] or "") != "":
			return False
		itemPath: str = str(entry["path"])
	except (KeyError, IndexError, TypeError):
		return False
	return _existingLocal(itemPath) is None


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


def _callerInfo() -> tuple[str, str]:
	"""(nome do processo, nome da thread) de quem fez o pedido ao FUSE.

	O FUSE dá o id da thread. O Dolphin faz leituras em threads auxiliares (detecção do
	tipo, plugins do menu de contexto), por isso conta o processo, não só a thread.
	"""
	try:
		_uid, _gid, tid = fuse_get_context()
		base: Path = Path(f"/proc/{int(tid)}")
		thread: str = (base / "comm").read_text(encoding="utf-8").strip()
		process: str = thread
		for line in (base / "status").read_text(encoding="utf-8").splitlines():
			if line.startswith("Tgid:"):
				tgid: int = int(line.split()[1])
				if tgid != int(tid):
					process = Path(f"/proc/{tgid}/comm").read_text(encoding="utf-8").strip()
				break
		return process, thread
	except Exception:  # noqa: BLE001
		return "", ""


def _browsingMode() -> str:
	""""" (leitura normal), "ui" (thread da interface) ou "stream" (outra thread)."""
	process, thread = _callerInfo()
	if process not in BROWSING_COMMS:
		return ""
	# Copiar/colar no Dolphin corre o worker "file" do KIO numa thread do próprio Dolphin
	# (KIO::WorkerThread): essa leitura é uma cópia a sério, não um "só a ver".
	if thread.startswith("KIO::"):
		return ""
	return "ui" if thread == process else "stream"


def _isBrowsingProcess() -> bool:
	return _browsingMode() != ""


def _isThumbnailer() -> bool:
	try:
		_uid, _gid, pid = fuse_get_context()
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
