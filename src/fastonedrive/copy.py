from collections.abc import Callable
from pathlib import Path

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.disk import (
	assertFreeSpace,
	cacheJobsLock,
	cleanupJobArtifacts,
	pruneJobArtifacts,
	pathPresent,
	removePath,
	swapIntoPlace,
)
from fastonedrive.index import getEntry
from fastonedrive.paths import isUnderMount, remoteBasename, remoteItemPath, toRemotePath
from fastonedrive.search import cacheLocalPath, pinnedLocalPath
from fastonedrive.rclone import (
	copyDir,
	copyRemoteToRemote,
	copyToReplace,
	isRemoteDir,
	remoteSizeBytes,
)


def _dropReplacedLocal(itemPath: str) -> None:
	if itemPath == "":
		return
	from fastonedrive.mutate import _destinationDirty

	if _destinationDirty(itemPath):
		return
	target: Path
	for target in (pinnedLocalPath(itemPath), cacheLocalPath(itemPath)):
		if pathPresent(target):
			removePath(target, ignoreErrors=True)


def copyDriveToDrive(
	source: str,
	dest: str,
	onStats: Callable[[str], None] | None = None,
	overwrite: bool = False,
) -> str:
	sourceRemote: str = toRemotePath(source)
	sourceItem: str = remoteItemPath(sourceRemote)
	# O perfil da origem tem de continuar activo ao escolher a cópia local;
	# toRemotePath(dest) activa o destino e o pin/cache lidos seriam os errados.
	local: Path | None = _intactLocalFile(sourceItem)
	localPath: str | None = str(local) if local is not None else None
	destRemote: str = toRemotePath(dest)
	if sourceRemote == destRemote:
		return destRemote
	destItem: str = remoteItemPath(destRemote)
	if localPath is not None:
		from fastonedrive.mutate import stageLocalWrite

		return stageLocalWrite(localPath, destRemote, overwrite)
	from fastonedrive.index import parentPath, refreshParentListing
	from fastonedrive.mutate import (
		_finishPreserved,
		_preserveDirtyDestination,
		_refuseDestination,
		_undoPreserve,
	)

	_refuseDestination(destRemote, destItem, overwrite)
	conflict: str = ""
	with cacheJobsLock():
		if overwrite:
			conflict = _preserveDirtyDestination(destItem)
	try:
		copyRemoteToRemote(sourceRemote, destRemote, onStats)
	except Exception:
		with cacheJobsLock():
			_undoPreserve(destItem, conflict)
		raise
	with cacheJobsLock():
		_finishPreserved(conflict)
		_dropReplacedLocal(destItem)
	refreshParentListing(parentPath(destItem))
	return destRemote


def _intactLocalFile(itemPath: str) -> Path | None:
	if itemPath == "":
		return None
	from fastonedrive.chunks import isPartial
	from fastonedrive.hydrate import isCacheDirty

	cached: Path = cacheLocalPath(itemPath)
	pinned: Path = pinnedLocalPath(itemPath)
	entry = getEntry(itemPath)
	if entry is not None and bool(entry["isDir"]):
		return None
	cacheOk: bool = cached.is_file() and not cached.is_symlink() and not isPartial(itemPath)
	pinOk: bool = pinned.is_file() and not pinned.is_symlink()
	cacheDirty: bool = False
	if cacheOk and entry is not None:
		cacheDirty = isCacheDirty(itemPath)
	elif cacheOk:
		cacheDirty = True
	local: Path | None = None
	if cacheOk and pinOk and cacheDirty:
		if cached.stat().st_mtime >= pinned.stat().st_mtime:
			local = cached
		else:
			local = pinned
	elif cacheOk and cacheDirty:
		local = cached
	elif pinOk:
		local = pinned
	elif cacheOk:
		local = cached
	if local is None:
		return None
	if entry is None:
		return local
	state: str = str(entry["syncState"] or "")
	if state not in ("dirty", "syncing", "conflict", "error") and not cacheDirty:
		if int(local.stat().st_size) != int(entry["size"]):
			return None
	return local


def copyRemoteToLocal(
	source: str,
	destDir: str,
	onStats: Callable[[str], None] | None = None,
) -> str:
	return _copyRemoteToLocal(source, destDir, onStats)


def _copyRemoteToLocal(
	source: str,
	destDir: str,
	onStats: Callable[[str], None] | None,
) -> str:
	if destDir.strip() == "":
		raise FastOneDriveError("indica a pasta de destino da cópia")
	remotePath: str = toRemotePath(source)
	dest: Path = Path(destDir).expanduser()
	if isUnderMount(dest):
		raise FastOneDriveError(
			f"destino não pode ser o mount FUSE {config.mountPath()}"
		)
	itemPath: str = remoteItemPath(remotePath)
	entry = getEntry(itemPath)
	isDir: bool
	if entry is not None:
		isDir = bool(entry["isDir"])
	else:
		isDir = isRemoteDir(remotePath)
	name: str = remoteBasename(remotePath)
	target: Path
	if dest.exists() and dest.is_dir():
		target = dest / name
	else:
		target = dest
		target.parent.mkdir(parents=True, exist_ok=True)
	if isDir and target.exists() and not target.is_dir():
		raise FastOneDriveError(f"destino é um ficheiro: {target}")
	pruneJobArtifacts(target.parent, includePartials=True, recursive=False)
	cleanupJobArtifacts(target.parent, target.name)
	size: int = remoteSizeBytes(remotePath, useFilters=isDir)
	spaceDest: Path = target if target.exists() else target.parent
	assertFreeSpace(spaceDest, size)
	if isDir:
		_copyDirectory(
			remotePath,
			target,
			size,
			onStats,
		)
		return str(target)
	if target.exists() and target.is_dir():
		raise FastOneDriveError(f"destino é uma pasta: {target}")
	copyToReplace(remotePath, str(target), onStats)
	return str(target)


def _copyDirectory(
	remotePath: str,
	target: Path,
	needBytes: int,
	onStats: Callable[[str], None] | None,
) -> None:
	staging: Path = target.parent / f".fod-staging-{target.name}"
	if pathPresent(staging):
		removePath(staging)
	staging.mkdir(parents=True, exist_ok=True)
	try:
		assertFreeSpace(target.parent, needBytes)
		copyDir(remotePath, str(staging), onStats)
		swapIntoPlace(staging, target)
	except Exception:
		if pathPresent(staging):
			removePath(staging, ignoreErrors=True)
		raise
