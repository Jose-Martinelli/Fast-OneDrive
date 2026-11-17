import mimetypes
import os
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from shutil import copy2

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.disk import (
	assertFreeSpace,
	cacheJobsLock,
	isEphemeralPath,
	pathPresent,
	removePath,
)
from fastonedrive.index import (
	deleteIndexTree,
	getEntry,
	hasDescendants,
	listDescendants,
	markDirty,
	parentPath,
	renameIndexPath,
	setSyncState,
	upsertEntry,
)
from fastonedrive.pin import shouldStorePinned, storedPinFlag
from fastonedrive.notify import notifyDirChanged
from fastonedrive.paths import remoteBasename, remoteItemPath, toRemotePath
from fastonedrive.hydrate import adoptDownloadedFile, isCacheDirty
from fastonedrive.rclone import (
	contentTokenFromStat,
	copyDir,
	copyFromLocal,
	deleteRemoteFile,
	isRemoteDir,
	iterLsjson,
	mkdirRemote,
	moveRemote,
	purgeRemote,
	remoteSizeBytes,
	remoteStat,
)
from fastonedrive.search import cacheLocalPath, pinnedLocalPath


def _refuseIfExists(destRemote: str, overwrite: bool) -> None:
	if overwrite:
		return
	stat = remoteStat(destRemote)
	if stat is not None:
		raise FastOneDriveError(f"já existe: {destRemote}")


def _refuseDestination(destRemote: str, itemPath: str, overwrite: bool) -> None:
	if itemPath == "":
		_refuseIfExists(destRemote, overwrite)
		return
	if not overwrite and _destinationDirty(itemPath):
		raise FastOneDriveError("o destino tem alterações locais por enviar")
	_refuseIfExists(destRemote, overwrite)
	if not overwrite and _localCopyPresent(itemPath):
		raise FastOneDriveError(f"já existe: {destRemote}")


def _sourceIsDestCopy(source: Path, itemPath: str) -> bool:
	if itemPath == "":
		return False
	try:
		resolved: Path = source.expanduser().resolve()
	except OSError:
		return False
	root: Path
	for root in (pinnedLocalPath(itemPath), cacheLocalPath(itemPath)):
		try:
			rootResolved: Path = root.resolve()
		except OSError:
			continue
		if resolved == rootResolved:
			return True
		try:
			resolved.relative_to(rootResolved)
		except ValueError:
			continue
		return True
	return False


def remoteExists(dest: str) -> dict[str, bool]:
	destRemote: str = toRemotePath(dest)
	itemPath: str = remoteItemPath(destRemote)
	if itemPath == "":
		return {"exists": True, "isDir": True}
	return _localKind(itemPath)


def _localKind(itemPath: str) -> dict[str, bool]:
	entry = getEntry(itemPath)
	if entry is None:
		from fastonedrive.search import adoptLocalOrphan

		entry = adoptLocalOrphan(itemPath)
	if entry is not None:
		return {"exists": True, "isDir": bool(entry["isDir"])}
	candidate: Path
	for candidate in (pinnedLocalPath(itemPath), cacheLocalPath(itemPath)):
		if candidate.exists():
			return {
				"exists": True,
				"isDir": candidate.is_dir() and not candidate.is_symlink(),
			}
	if hasDescendants(itemPath):
		return {"exists": True, "isDir": True}
	return {"exists": False, "isDir": False}


def _assertSafeItemPath(itemPath: str) -> None:
	if itemPath == "":
		raise FastOneDriveError("path vazio")
	parts: list[str] = itemPath.split("/")
	if any(part in ("", ".", "..") for part in parts):
		raise FastOneDriveError("path inválido")


def stageLocalWrite(localPath: str, dest: str, overwrite: bool = False) -> str:
	source: Path = Path(localPath).expanduser()
	if not source.is_file() and not source.is_dir():
		raise FastOneDriveError(f"origem local inválida: {localPath}")
	destRemote: str = toRemotePath(dest)
	itemPath: str = remoteItemPath(destRemote)
	_assertSafeItemPath(itemPath)
	if isEphemeralPath(itemPath):
		return destRemote
	sourceIsDir: bool = source.is_dir() and not source.is_symlink()
	_refuseLocal(itemPath, sourceIsDir, overwrite)
	backing: Path = _localBacking(itemPath)
	assertFreeSpace(backing, _treeBytes(source))
	staged: list[str]
	if sourceIsDir:
		staged = _stageDirectory(source, itemPath)
	else:
		_stageFile(source, itemPath)
		staged = [itemPath]
	notifyDirChanged(itemPath)
	_scheduleStaged(staged)
	return destRemote


def _refuseLocal(itemPath: str, sourceIsDir: bool, overwrite: bool) -> None:
	kind: dict[str, bool] = _localKind(itemPath)
	if not bool(kind["exists"]):
		return
	if bool(kind["isDir"]) != sourceIsDir:
		raise FastOneDriveError("o destino e a origem não são do mesmo tipo")
	if not overwrite:
		raise FastOneDriveError(f"já existe: {itemPath}")


def _localBacking(itemPath: str) -> Path:
	if shouldStorePinned(itemPath):
		return pinnedLocalPath(itemPath)
	return cacheLocalPath(itemPath)


def _treeBytes(source: Path) -> int:
	if source.is_symlink() or source.is_file():
		return int(source.stat().st_size)
	total: int = 0
	child: Path
	for child in source.rglob("*"):
		if child.is_symlink() or not child.is_file():
			continue
		total += int(child.stat().st_size)
	return total


def _stageDirectory(source: Path, itemPath: str) -> list[str]:
	staged: list[str] = []
	_stageDirectoryNode(itemPath)
	staged.append(itemPath)
	child: Path
	for child in source.rglob("*"):
		if child.is_symlink():
			continue
		rel: str = child.relative_to(source).as_posix()
		if rel == "" or isEphemeralPath(rel):
			continue
		full: str = f"{itemPath}/{rel}"
		if child.is_dir():
			_stageDirectoryNode(full)
			staged.append(full)
			continue
		if not child.is_file():
			continue
		_stageFile(child, full)
		staged.append(full)
	return staged


def _stageDirectoryNode(itemPath: str) -> None:
	target: Path = _localBacking(itemPath)
	upsertEntry(
		itemPath,
		target.name,
		True,
		0,
		shouldStorePinned(itemPath),
	)
	markDirty(itemPath)
	target.mkdir(parents=True, exist_ok=True)


def _stageFile(source: Path, itemPath: str) -> None:
	target: Path = _localBacking(itemPath)
	target.parent.mkdir(parents=True, exist_ok=True)
	_indexStagedFile(itemPath, None)
	if not _sameFile(source, target):
		copy2(source, target)
	_dropOtherCopy(itemPath, target)
	_indexStagedFile(itemPath, target)


def _indexStagedFile(itemPath: str, target: Path | None) -> None:
	name: str = itemPath.rsplit("/", 1)[-1]
	size: int = 0
	modTime: str = ""
	if target is not None and target.is_file():
		info: os.stat_result = target.stat()
		size = int(info.st_size)
		modTime = _isoModTime(float(info.st_mtime))
	upsertEntry(
		itemPath,
		name,
		False,
		size,
		shouldStorePinned(itemPath),
		modTime,
		_mimeType(name),
	)
	markDirty(itemPath)


def _dropOtherCopy(itemPath: str, keep: Path) -> None:
	other: Path
	for other in (pinnedLocalPath(itemPath), cacheLocalPath(itemPath)):
		if not pathPresent(other):
			continue
		try:
			if other.resolve() == keep.resolve():
				continue
		except OSError:
			continue
		removePath(other)


def _isoModTime(mtime: float) -> str:
	stamp: datetime = datetime.fromtimestamp(mtime, tz=timezone.utc)
	return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mimeType(name: str) -> str:
	guessed: str | None
	guessed, _encoding = mimetypes.guess_type(name)
	if guessed is None:
		return ""
	return guessed


def _scheduleStaged(paths: list[str]) -> None:
	from fastonedrive.log import logError

	if len(paths) == 0:
		return
	try:
		binary: str = config.cliBin()
	except FastOneDriveError as error:
		logError(str(error), extra={"step": "sync-detach"})
		return
	profile: config.Profile | None = config.activeProfile()
	command: list[str] = [binary]
	if profile is not None:
		command.extend(["--profile", profile.id])
	command.append("sync-pins")
	command.extend(["--path", paths[0]])
	try:
		subprocess.Popen(
			command,
			stdin=subprocess.DEVNULL,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			start_new_session=True,
			close_fds=True,
		)
	except OSError as error:
		logError(str(error), extra={"step": "sync-detach"})


def putLocalToRemote(
	localPath: str,
	dest: str,
	onStats: Callable[[str], None] | None = None,
	overwrite: bool = False,
) -> str:
	source: Path = Path(localPath).expanduser()
	if not source.is_file() and not source.is_dir():
		raise FastOneDriveError(f"origem local inválida: {localPath}")
	destRemote: str = toRemotePath(dest)
	itemPath: str = remoteItemPath(destRemote)
	_assertSafeItemPath(itemPath)
	if isEphemeralPath(itemPath):
		return destRemote
	with cacheJobsLock():
		conflict: str = ""
		if _sourceIsDestCopy(source, itemPath):
			_refuseIfExists(destRemote, overwrite)
		else:
			_refuseDestination(
				destRemote,
				itemPath,
				overwrite,
			)
			if overwrite:
				conflict = _preserveDirtyDestination(itemPath)
		try:
			if source.is_dir():
				mkdirRemote(destRemote)
				copyDir(str(source), destRemote, onStats)
			else:
				copyFromLocal(str(source), destRemote, onStats)
		except Exception:
			_undoPreserve(itemPath, conflict)
			raise
		_finishPreserved(conflict)
		if source.is_dir():
			_retireReplacedCaches(itemPath, source)
			indexed: list[tuple[str, dict[str, Any]]] = _indexRemoteTree(itemPath)
			copied: list[str] = _mirrorPinnedLocal(itemPath, source, True)
			_adoptMirroredFiles(copied, indexed)
			notifyDirChanged(itemPath)
			return destRemote
		size: int = int(source.stat().st_size)
		_retireReplacedCaches(itemPath, source)
		_mirrorPinnedLocal(itemPath, source, False)
		_indexAfterWrite(
			itemPath,
			False,
			size,
		)
	notifyDirChanged(itemPath)
	return destRemote


def mkdirRemotePath(dest: str) -> str:
	destRemote: str = toRemotePath(dest)
	itemPath: str = remoteItemPath(destRemote)
	_assertSafeItemPath(itemPath)
	with cacheJobsLock():
		mkdirRemote(destRemote)
		_indexAfterWrite(itemPath, True, 0)
		if shouldStorePinned(itemPath):
			target: Path = pinnedLocalPath(itemPath)
			target.mkdir(parents=True, exist_ok=True)
			upsertEntry(
				itemPath,
				remoteBasename(destRemote),
				True,
				0,
				True,
			)
	notifyDirChanged(itemPath)
	return destRemote


def removeRemotePath(source: str, expectFile: bool | None = None) -> str:
	remotePath: str = toRemotePath(source)
	itemPath: str = remoteItemPath(remotePath)
	_assertSafeItemPath(itemPath)
	with cacheJobsLock():
		if _pendingLocalEdit(itemPath):
			raise FastOneDriveError("há alterações locais por enviar")
		stat = remoteStat(remotePath)
		if stat is None:
			if _localCopyPresent(itemPath):
				raise FastOneDriveError("não encontrado na nuvem; a cópia local foi mantida")
			deleteIndexTree(itemPath)
			notifyDirChanged(itemPath)
			return remotePath
		remoteIsDir: bool = bool(stat.get("IsDir"))
		if expectFile is True and remoteIsDir:
			raise FastOneDriveError("o item é uma pasta; apagamento como ficheiro recusado")
		if expectFile is False and not remoteIsDir:
			raise FastOneDriveError("o item é um ficheiro; apagamento como pasta recusado")
		if remoteIsDir:
			purgeRemote(remotePath)
		else:
			deleteRemoteFile(remotePath)
		_removeLocalCopies(itemPath)
		if _localCopyPresent(itemPath):
			raise FastOneDriveError("não foi possível apagar a cópia local")
		deleteIndexTree(itemPath)
	notifyDirChanged(itemPath)
	return remotePath


def _ownedRemote(raw: str) -> tuple[str, str, str]:
	from fastonedrive.profiles import profileIdFromText

	profileId: str = profileIdFromText(raw) or ""
	if profileId != "":
		config.activateProfile(profileId)
	remote: str = toRemotePath(raw)
	return profileId, remote, remoteItemPath(remote)


def moveRemotePath(source: str, dest: str, overwrite: bool = False) -> str:
	sourceId: str
	sourceRemote: str
	oldPath: str
	destId: str
	destRemote: str
	newPath: str
	sourceId, sourceRemote, oldPath = _ownedRemote(source)
	destId, destRemote, newPath = _ownedRemote(dest)
	_assertSafeItemPath(oldPath)
	_assertSafeItemPath(newPath)
	if oldPath == newPath and sourceId == destId:
		return destRemote
	if sourceId == destId and newPath.startswith(f"{oldPath}/"):
		raise FastOneDriveError("não é possível mover para dentro de si")
	if sourceId != destId and sourceId != "" and destId != "":
		return _moveAcrossProfiles(
			sourceId,
			destId,
			sourceRemote,
			destRemote,
			oldPath,
			newPath,
			overwrite,
		)
	with cacheJobsLock():
		_refuseDestination(
			destRemote,
			newPath,
			overwrite,
		)
		conflict: str = _preserveDirtyDestination(newPath) if overwrite else ""
		try:
			moveRemote(sourceRemote, destRemote)
		except Exception:
			_undoPreserve(newPath, conflict)
			raise
		_finishPreserved(conflict)
		try:
			renameIndexPath(oldPath, newPath)
		except FastOneDriveError:
			_indexAfterWrite(
				newPath,
				isRemoteDir(destRemote),
				0 if isRemoteDir(destRemote) else remoteSizeBytes(destRemote, useFilters=False),
			)
			deleteIndexTree(oldPath)
		_moveLocalCopies(oldPath, newPath)
	notifyDirChanged(newPath)
	notifyDirChanged(oldPath)
	return destRemote


def _moveAcrossProfiles(
	sourceId: str,
	destId: str,
	sourceRemote: str,
	destRemote: str,
	oldPath: str,
	newPath: str,
	overwrite: bool,
) -> str:
	with cacheJobsLock():
		config.activateProfile(destId)
		_refuseDestination(
			destRemote,
			newPath,
			overwrite,
		)
		conflict: str = _preserveDirtyDestination(newPath) if overwrite else ""
		config.activateProfile(sourceId)
		if _pendingLocalEdit(oldPath):
			config.activateProfile(destId)
			_undoPreserve(newPath, conflict)
			raise FastOneDriveError("há alterações locais por enviar")
		config.activateProfile(destId)
		try:
			moveRemote(sourceRemote, destRemote)
		except Exception:
			_undoPreserve(newPath, conflict)
			raise
		_finishPreserved(conflict)
		config.activateProfile(sourceId)
		_removeLocalCopies(oldPath)
		if not _localCopyPresent(oldPath):
			try:
				deleteIndexTree(oldPath)
			except FastOneDriveError:
				pass
		notifyDirChanged(oldPath)
		config.activateProfile(destId)
		_indexAfterWrite(
			newPath,
			isRemoteDir(destRemote),
			0 if isRemoteDir(destRemote) else remoteSizeBytes(destRemote, useFilters=False),
		)
		notifyDirChanged(newPath)
	return destRemote


def _pendingLocalEdit(itemPath: str) -> bool:
	from datetime import datetime, timedelta, timezone

	from fastonedrive.rclone import parseRemoteModTime

	rows = listDescendants(itemPath)
	if len(rows) == 0:
		if isCacheDirty(itemPath):
			return True
		return pathPresent(pinnedLocalPath(itemPath))
	for row in rows:
		state: str = str(row["syncState"] or "")
		if state in ("dirty", "syncing", "conflict", "error"):
			return True
		if bool(row["isDir"]):
			continue
		path: str = str(row["path"])
		if isCacheDirty(path):
			return True
		if str(row["baseEtag"] or "") == "":
			if pathPresent(pinnedLocalPath(path)) or pathPresent(cacheLocalPath(path)):
				return True
		base = parseRemoteModTime(str(row["baseModTime"] or ""))
		if base is None:
			continue
		local: Path
		for local in (pinnedLocalPath(path), cacheLocalPath(path)):
			if not local.is_file():
				continue
			localTime: datetime = datetime.fromtimestamp(local.stat().st_mtime, tz=timezone.utc)
			if localTime > base + timedelta(seconds=2):
				return True
	return False


def _localCopyPresent(itemPath: str) -> bool:
	if pathPresent(pinnedLocalPath(itemPath)) or pathPresent(cacheLocalPath(itemPath)):
		return True
	row: object
	for row in listDescendants(itemPath):
		path: str = str(row["path"])  # type: ignore[index]
		if path == itemPath:
			continue
		if pathPresent(pinnedLocalPath(path)) or pathPresent(cacheLocalPath(path)):
			return True
	return False


def _preserveDirtyDestination(itemPath: str) -> str:
	if itemPath == "" or not _destinationDirty(itemPath) or not _localCopyPresent(itemPath):
		return ""
	from datetime import datetime

	from fastonedrive.index import parentPath
	from fastonedrive.search import relocateStoredFiles

	parent: str = parentPath(itemPath)
	name: str = itemPath.rsplit("/", 1)[-1]
	dot: int = name.rfind(".")
	stem: str = name[:dot] if dot > 0 else name
	suffix: str = name[dot:] if dot > 0 else ""
	stamp: str = datetime.now().strftime("%Y-%m-%d %H%M%S")
	number: int = 1
	conflict: str = ""
	while conflict == "":
		extra: str = "" if number == 1 else f"-{number}"
		conflictName: str = f"{stem} (conflito local {stamp}{extra}){suffix}"
		full: str = f"{parent}/{conflictName}" if parent != "" else conflictName
		if (
			getEntry(full) is None
			and not pathPresent(pinnedLocalPath(full))
			and not pathPresent(cacheLocalPath(full))
		):
			conflict = full
			break
		number += 1
	hadRows: bool = len(listDescendants(itemPath)) > 0
	relocateStoredFiles(itemPath, conflict)
	if hadRows:
		try:
			renameIndexPath(itemPath, conflict)
		except FastOneDriveError:
			try:
				relocateStoredFiles(conflict, itemPath)
			except FastOneDriveError:
				pass
			raise
	else:
		_indexShelvedCopy(conflict)
	return conflict


def _finishPreserved(conflict: str) -> None:
	if conflict == "":
		return
	from fastonedrive.index import detachLocalIdentity
	from fastonedrive.syncpins import scheduleItemSync

	if getEntry(conflict) is not None:
		detachLocalIdentity(conflict)
	scheduled: bool = False
	for row in listDescendants(conflict):
		if bool(row["isDir"]):
			continue
		scheduleItemSync(str(row["path"]))
		scheduled = True
	if not scheduled:
		scheduleItemSync(conflict)


def _undoPreserve(itemPath: str, conflict: str) -> None:
	if conflict == "" or conflict == itemPath:
		return
	from fastonedrive.search import relocateStoredFiles

	try:
		if getEntry(conflict) is not None and getEntry(itemPath) is None:
			renameIndexPath(conflict, itemPath)
	except FastOneDriveError:
		pass
	try:
		relocateStoredFiles(conflict, itemPath)
	except FastOneDriveError:
		pass


def _indexShelvedCopy(itemPath: str) -> None:
	name: str = itemPath.rsplit("/", 1)[-1]
	root: Path
	pinnedFlag: bool | None
	for root, pinnedFlag in (
		(pinnedLocalPath(itemPath), True),
		(cacheLocalPath(itemPath), None),
	):
		if not pathPresent(root):
			continue
		if root.is_file() or root.is_symlink():
			upsertEntry(
				itemPath,
				name,
				False,
				int(root.stat().st_size),
				pinnedFlag,
			)
			return
		upsertEntry(
			itemPath,
			name,
			True,
			0,
			pinnedFlag,
		)
		child: Path
		for child in root.rglob("*"):
			if not child.is_file() and not child.is_dir():
				continue
			rel: str = child.relative_to(root).as_posix()
			full: str = f"{itemPath}/{rel}"
			upsertEntry(
				full,
				child.name,
				child.is_dir(),
				0 if child.is_dir() else int(child.stat().st_size),
				pinnedFlag,
			)
		return


def _destinationDirty(itemPath: str) -> bool:
	from datetime import datetime, timedelta, timezone

	from fastonedrive.hydrate import isCacheDirty
	from fastonedrive.rclone import parseRemoteModTime

	rows = listDescendants(itemPath)
	if len(rows) == 0:
		return (
			isCacheDirty(itemPath)
			or pathPresent(pinnedLocalPath(itemPath))
			or pathPresent(cacheLocalPath(itemPath))
		)
	for row in rows:
		state: str = str(row["syncState"] or "")
		if state in ("dirty", "syncing", "conflict", "error"):
			return True
		if bool(row["isDir"]):
			continue
		path: str = str(row["path"])
		if str(row["baseEtag"] or "") == "":
			if pathPresent(pinnedLocalPath(path)) or pathPresent(cacheLocalPath(path)):
				return True
		if isCacheDirty(path):
			return True
		base = parseRemoteModTime(str(row["baseModTime"] or ""))
		if base is None:
			continue
		local: Path
		for local in (pinnedLocalPath(path), cacheLocalPath(path)):
			if not local.is_file():
				continue
			localTime: datetime = datetime.fromtimestamp(local.stat().st_mtime, tz=timezone.utc)
			if localTime > base + timedelta(seconds=2):
				return True
	return False


def _indexRemoteTree(itemPath: str) -> list[tuple[str, dict[str, Any]]]:
	_indexAfterWrite(itemPath, True, 0)
	files: list[tuple[str, dict[str, Any]]] = []
	for item in iterLsjson(config.remotePath(itemPath), useHash=True):
		rel: str = str(item.get("Path") or item.get("Name") or "")
		if rel in ("", ".", "/"):
			continue
		full: str = f"{itemPath}/{rel}"
		pinned: bool | None = storedPinFlag(full)
		isDir: bool = bool(item.get("IsDir"))
		upsertEntry(
			full,
			Path(full).name,
			isDir,
			int(item.get("Size") or 0),
			pinned,
			str(item.get("ModTime") or ""),
			str(item.get("MimeType") or ""),
			contentTokenFromStat(item),
			str(item.get("ID") or ""),
		)
		if not isDir:
			files.append((full, item))
	return files


def _adoptMirroredFiles(copied: list[str], indexed: list[tuple[str, dict[str, Any]]]) -> None:
	stats: dict[str, dict[str, Any]] = {path: item for path, item in indexed}
	full: str
	for full in copied:
		item: dict[str, Any] | None = stats.get(full)
		pinFile: Path = pinnedLocalPath(full)
		if item is None or not pinFile.is_file():
			continue
		adoptDownloadedFile(full, pinFile, item)


def _indexAfterWrite(itemPath: str, isDir: bool, size: int) -> None:
	name: str = itemPath.rsplit("/", 1)[-1]
	stat = remoteStat(config.remotePath(itemPath))
	modTime: str = ""
	mimeType: str = ""
	if stat is not None:
		if size <= 0:
			size = int(stat.get("Size") or 0)
		modTime = str(stat.get("ModTime") or "")
		mimeType = str(stat.get("MimeType") or "")
		if bool(stat.get("IsDir")):
			isDir = True
	pinned: bool | None = storedPinFlag(itemPath)
	token: str = contentTokenFromStat(stat) if stat is not None else ""
	itemId: str = str(stat.get("ID") or "") if stat is not None else ""
	upsertEntry(
		itemPath,
		name,
		isDir,
		size,
		pinned,
		modTime,
		mimeType,
		token,
		itemId,
	)
	if stat is not None and not isDir:
		pinnedFile: Path = pinnedLocalPath(itemPath)
		if not shouldStorePinned(itemPath) and (pinnedFile.is_file() or pinnedFile.is_symlink()):
			pinnedFile.unlink(missing_ok=True)
		adoptDownloadedFile(itemPath, pinnedFile, stat)


def _retireReplacedCaches(itemPath: str, source: Path) -> None:
	if not source.is_dir():
		_dropReplacedCacheFile(itemPath, source)
		_clearPathDirty(itemPath)
		return
	child: Path
	for child in source.rglob("*"):
		if not child.is_file():
			continue
		rel: str = child.relative_to(source).as_posix()
		full: str = f"{itemPath}/{rel}"
		_dropReplacedCacheFile(full, child)
		_clearPathDirty(full)


def _clearPathDirty(itemPath: str) -> None:
	entry = getEntry(itemPath)
	if entry is None:
		return
	state: str = str(entry["syncState"] or "")
	if state in ("dirty", "syncing", "conflict", "error"):
		setSyncState(itemPath, "")


def _dropReplacedCacheFile(itemPath: str, source: Path) -> None:
	cached: Path = cacheLocalPath(itemPath)
	if not cached.is_file() and not cached.is_symlink():
		return
	try:
		if source.resolve() == cached.resolve():
			return
	except OSError:
		return
	from fastonedrive.chunks import deleteChunkRow

	cached.unlink(missing_ok=True)
	deleteChunkRow(itemPath)


def _sameFile(source: Path, dest: Path) -> bool:
	try:
		return source.resolve() == dest.resolve()
	except OSError:
		return False


def _mirrorPinnedLocal(itemPath: str, source: Path, isDir: bool) -> list[str]:
	if not shouldStorePinned(itemPath):
		return []
	target: Path = pinnedLocalPath(itemPath)
	target.parent.mkdir(parents=True, exist_ok=True)
	if not isDir:
		if target.is_dir() and not target.is_symlink():
			removePath(target)
		if not _sameFile(source, target):
			copy2(source, target)
		upsertEntry(
			itemPath,
			itemPath.rsplit("/", 1)[-1],
			False,
			int(target.stat().st_size),
			True,
		)
		return [itemPath]
	if target.is_file() or target.is_symlink():
		target.unlink(missing_ok=True)
	target.mkdir(parents=True, exist_ok=True)
	copied: list[str] = []
	child: Path
	for child in source.rglob("*"):
		if not child.is_file() and not child.is_dir():
			continue
		rel: str = child.relative_to(source).as_posix()
		if rel == "" or isEphemeralPath(rel):
			continue
		full: str = f"{itemPath}/{rel}"
		if not shouldStorePinned(full):
			continue
		dest: Path = pinnedLocalPath(full)
		if child.is_dir():
			if dest.is_file() or dest.is_symlink():
				dest.unlink(missing_ok=True)
			dest.mkdir(parents=True, exist_ok=True)
			continue
		if dest.is_dir() and not dest.is_symlink():
			removePath(dest)
		dest.parent.mkdir(parents=True, exist_ok=True)
		if not _sameFile(child, dest):
			copy2(child, dest)
		if dest.is_file():
			copied.append(full)
	return copied


def _removeLocalCopies(itemPath: str) -> None:
	for rootFn in (pinnedLocalPath, cacheLocalPath):
		target: Path = rootFn(itemPath)
		if pathPresent(target):
			removePath(target, ignoreErrors=True)


def _moveLocalCopies(oldPath: str, newPath: str) -> None:
	for rootFn in (pinnedLocalPath, cacheLocalPath):
		source: Path = rootFn(oldPath)
		dest: Path = rootFn(newPath)
		if not pathPresent(source):
			if pathPresent(dest):
				removePath(dest, ignoreErrors=True)
			continue
		dest.parent.mkdir(parents=True, exist_ok=True)
		if pathPresent(dest):
			removePath(dest, ignoreErrors=True)
		source.rename(dest)
