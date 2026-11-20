import filecmp
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.disk import (
	assertFreeSpace,
	cacheJobsLock,
	evictCache,
	localFileBusy,
	pruneJobArtifacts,
	suppressPath,
	writeGuardHeld,
)
from fastonedrive.chunks import deleteChunkRow, fillHoles, isPartial
from fastonedrive.index import (
	clearItemBaseline,
	getEntry,
	parentPath,
	setBaseline,
	setSyncState,
	updateFileMeta,
)
from fastonedrive.graphitems import fastRemoteStat
from fastonedrive.log import logError
from fastonedrive.notify import notifyDirChanged
from fastonedrive.paths import remoteItemPath, toRemotePath
from fastonedrive.rclone import (
	contentTokenFromStat,
	copyToReplace,
	hashLocalQuickXor,
	isContentToken,
	parseRemoteModTime,
	quickXorEqual,
	remoteSizeBytes,
	remoteStat,
	tokenKind,
)
from fastonedrive.search import applyStoredMode, cacheLocalPath, pinnedLocalPath

MTIME_SKEW: timedelta = timedelta(seconds=2)


def hydrateRemote(source: str, onStats: Callable[[str], None] | None = None) -> Path:
	with cacheJobsLock():
		return _hydrateRemote(source, onStats)


def _hydrateRemote(source: str, onStats: Callable[[str], None] | None) -> Path:
	remotePath: str = toRemotePath(source)
	itemPath: str = remoteItemPath(remotePath)
	if itemPath == "":
		raise FastOneDriveError("não é possível hidratar a raiz do Drive")
	entry = getEntry(itemPath)
	if entry is not None and bool(entry["isDir"]):
		raise FastOneDriveError("não é possível hidratar uma pasta")
	pinned: Path = pinnedLocalPath(itemPath)
	if pinned.is_file():
		return pinned
	target: Path = cacheLocalPath(itemPath)
	if target.is_file() and (_cacheBlocked(entry) or isCacheDirty(itemPath)):
		if isPartial(itemPath):
			_fillDirtyPartial(itemPath, remotePath, target)
		_markCacheWritable(itemPath, target)
		return target
	size: int = 0
	remoteTime: datetime | None = None
	stat: dict[str, Any] | None = None
	try:
		stat = fastRemoteStat(remotePath)
	except FastOneDriveError:
		stat = None
	if stat is not None:
		size = int(stat.get("Size") or 0)
		remoteTime = parseRemoteModTime(str(stat.get("ModTime") or ""))
	elif entry is not None:
		size = int(entry["size"] or 0)
		remoteTime = parseRemoteModTime(str(entry["modTime"] or ""))
	if size <= 0:
		size = remoteSizeBytes(remotePath, useFilters=False)
	remoteToken: str = ""
	indexToken: str = ""
	if stat is not None:
		remoteToken = contentTokenFromStat(stat)
	if entry is not None:
		try:
			indexToken = str(entry["baseEtag"] or "")
		except (IndexError, KeyError, TypeError):
			indexToken = ""
	if isPartial(itemPath):
		if writeGuardHeld(itemPath) or (target.is_file() and localFileBusy(target)):
			if target.is_file():
				_fillDirtyPartial(itemPath, remotePath, target)
				_markCacheWritable(itemPath, target)
				return target
		else:
			target.unlink(missing_ok=True)
			deleteChunkRow(itemPath)
	if _cacheIsFresh(
		target,
		size,
		remoteTime,
		remoteToken,
		indexToken,
	):
		_markCacheWritable(itemPath, target)
		if entry is None or not isContentToken(str(entry["baseEtag"] or "")):
			fresh: dict[str, Any] | None = stat if isinstance(stat, dict) else None
			if fresh is None:
				fresh = fastRemoteStat(remotePath)
			if fresh is not None:
				adoptDownloadedFile(itemPath, target, fresh)
		return target
	config.ensureCacheDir()
	pruneJobArtifacts(config.cacheDir(), includePartials=True, recursive=True)
	target.parent.mkdir(parents=True, exist_ok=True)
	try:
		assertFreeSpace(target.parent, size)
	except FastOneDriveError:
		evictCache(
			protect=target if target.is_file() else None,
			skip=isDirtyCacheFile,
		)
		try:
			assertFreeSpace(target.parent, size)
		except FastOneDriveError:
			if target.is_file():
				target.unlink()
			evictCache(skip=isDirtyCacheFile)
			assertFreeSpace(target.parent, size)
	suppressPath(itemPath)
	freshStat: dict[str, Any] | None = None
	try:
		# HTTP directo ao Graph: sem arrancar o rclone (vários segundos por processo).
		from fastonedrive.graphitems import downloadToFile, graphStat

		freshStat, url = graphStat(itemPath)
		downloadToFile(itemPath, target, url)
	except FastOneDriveError as error:
		logError(f"download Graph falhou, a usar rclone: {error}", extra={"path": itemPath})
		copyToReplace(remotePath, str(target), onStats)
		freshStat = fastRemoteStat(remotePath)
	_markCacheWritable(itemPath, target)
	if freshStat is not None:
		adoptDownloadedFile(itemPath, target, freshStat)
	evictCache(protect=target, skip=isDirtyCacheFile)
	notifyDirChanged(parentPath(itemPath))
	from fastonedrive.notify import notifyStateChanged

	notifyStateChanged(itemPath)
	return target


def ensureCacheBaseline(itemPath: str) -> None:
	"""Antes de editar uma cópia em cache sem linha de base: se o conteúdo é igual ao
	da nuvem (QuickXor), adoptar a versão da nuvem como base. Ficheiros abertos a pedido
	antes da 0.2.1 ficaram sem base e cada edição virava conflito."""
	entry = getEntry(itemPath)
	if entry is None or bool(entry["pinned"]) or bool(entry["isDir"]):
		return
	if str(entry["baseEtag"] or "") != "" or isPartial(itemPath):
		return
	# O QuickXor abaixo decide se o conteúdo é o da nuvem; o mtime não chega
	# (isCacheDirty olha só para a data).
	if str(entry["syncState"] or "") in ("dirty", "syncing", "conflict", "error"):
		return
	remoteToken: str = str(entry["etag"] or "")
	if tokenKind(remoteToken) != "qxh":
		return
	target: Path = cacheLocalPath(itemPath)
	if not target.is_file() or target.stat().st_size > config.openMaxBytes():
		return
	try:
		local: str = hashLocalQuickXor(target)
	except FastOneDriveError:
		return
	if quickXorEqual(local, remoteToken) is True:
		setBaseline(itemPath, str(entry["modTime"] or ""), remoteToken, keepSyncState=True)


def _cacheBlocked(entry: object) -> bool:
	if entry is None:
		return False
	try:
		state: str = str(entry["syncState"] or "")  # type: ignore[index]
	except (IndexError, KeyError, TypeError):
		return False
	return state in ("dirty", "syncing", "conflict", "error")


def _fillDirtyPartial(itemPath: str, remotePath: str, target: Path) -> None:
	fd: int = os.open(str(target), os.O_RDWR)
	try:
		fillHoles(
			itemPath,
			remotePath,
			fd,
			[],
		)
	finally:
		os.close(fd)


def _cacheIsFresh(target: Path, size: int, remoteTime: datetime | None, remoteToken: str, indexToken: str) -> bool:
	if not target.is_file() or target.stat().st_size != size:
		return False
	agrees: bool | None = _contentAgrees(target, remoteToken, indexToken)
	if agrees is False:
		return False
	if agrees is True:
		return True
	if remoteTime is None:
		return False
	localTime: datetime = datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc)
	return localTime >= remoteTime - MTIME_SKEW


def _contentAgrees(target: Path, remoteToken: str, indexToken: str) -> bool | None:
	kind: str = tokenKind(remoteToken)
	if kind not in ("qxh", "sha1"):
		return None
	stored: str = indexToken
	if tokenKind(stored) != kind:
		if kind != "qxh" or not target.is_file() or target.stat().st_size > config.openMaxBytes():
			return None
		try:
			stored = hashLocalQuickXor(target)
		except FastOneDriveError:
			return None
		if tokenKind(stored) != "qxh":
			return None
	if kind == "qxh":
		same: bool | None = quickXorEqual(stored, remoteToken)
		if same is None:
			return stored == remoteToken
		return same
	return stored == remoteToken


def cleanCacheIsStale(itemPath: str) -> bool:
	if itemPath == "" or isPartial(itemPath):
		return False
	pinned: Path = pinnedLocalPath(itemPath)
	if pinned.is_file():
		return False
	target: Path = cacheLocalPath(itemPath)
	if not target.is_file() or isCacheDirty(itemPath):
		return False
	with cacheJobsLock():
		if isPartial(itemPath) or pinned.is_file() or not target.is_file() or isCacheDirty(itemPath):
			return False
		return _dropIfStale(itemPath, target)


def _dropIfStale(itemPath: str, target: Path) -> bool:
	remotePath: str = config.remotePath(itemPath)
	try:
		stat: dict[str, Any] | None = fastRemoteStat(remotePath)
	except FastOneDriveError:
		return False
	if stat is None:
		return False
	size: int = int(stat.get("Size") or 0)
	remoteTime: datetime | None = parseRemoteModTime(str(stat.get("ModTime") or ""))
	if size <= 0:
		try:
			size = remoteSizeBytes(remotePath, useFilters=False)
		except FastOneDriveError:
			return False
	if size <= 0:
		return False
	entry = getEntry(itemPath)
	if entry is None:
		return False
	indexToken: str = ""
	try:
		indexToken = str(entry["baseEtag"] or "")
	except (IndexError, KeyError, TypeError):
		indexToken = ""
	remoteToken: str = contentTokenFromStat(stat)
	if _cacheIsFresh(
		target,
		size,
		remoteTime,
		remoteToken,
		indexToken,
	):
		return False
	indexSize: int = 0
	try:
		indexSize = int(entry["size"] or 0)
	except (IndexError, KeyError, TypeError, ValueError):
		indexSize = 0
	remoteLarge: bool = size > config.openMaxBytes()
	if not remoteLarge and indexSize <= config.openMaxBytes():
		return True
	etag: str = remoteToken
	if etag == "":
		try:
			etag = str(entry["etag"] or "")
		except (IndexError, KeyError, TypeError):
			etag = ""
	modTime: str = str(stat.get("ModTime") or "")
	shrunk: bool = target.is_file() and target.stat().st_size > size
	if (remoteLarge or shrunk) and (
		writeGuardHeld(itemPath) or (target.is_file() and localFileBusy(target))
	):
		return False
	if remoteLarge or shrunk:
		target.unlink(missing_ok=True)
		deleteChunkRow(itemPath)
		clearItemBaseline(itemPath)
	updateFileMeta(
		itemPath,
		size,
		modTime,
		etag,
	)
	notifyDirChanged(parentPath(itemPath))
	return True


def adoptDownloadedFile(
	itemPath: str,
	local: Path,
	stat: dict[str, Any],
	protectDirtyCache: bool | None = None,
	guardHeld: bool = False,
) -> None:
	cached: Path = cacheLocalPath(itemPath)
	if protectDirtyCache is None:
		protectDirtyCache = _dirtyCacheBeside(itemPath, local)
	when: datetime | None = parseRemoteModTime(str(stat.get("ModTime") or ""))
	stamp: float | None = when.timestamp() if when is not None else None
	paths: list[Path] = [local]
	current = getEntry(itemPath)
	uploading: bool = (
		not guardHeld and current is not None and str(current["syncState"] or "") == "syncing"
	)
	for extra in (pinnedLocalPath(itemPath), cached):
		if extra in paths:
			continue
		if protectDirtyCache and extra == cached:
			continue
		if not extra.is_file() or _sameFileOnDisk(extra, local):
			continue
		if _sameContent(extra, local):
			paths.append(extra)
		elif extra == cached and not uploading and (guardHeld or not writeGuardHeld(itemPath)):
			if localFileBusy(cached):
				continue
			cached.unlink(missing_ok=True)
			deleteChunkRow(itemPath)
	if stamp is not None:
		for path in paths:
			if path.is_file():
				os.utime(path, (stamp, stamp))
	token: str = contentTokenFromStat(stat)
	if token == "":
		return
	pinFile: Path = pinnedLocalPath(itemPath)
	row = getEntry(itemPath)
	if row is not None and bool(row["pinned"]) and not pinFile.is_file():
		clearItemBaseline(itemPath)
		setSyncState(itemPath, "synced")
		return
	size: int = int(stat.get("Size") or 0)
	if size <= 0 and local.is_file():
		size = int(local.stat().st_size)
	setBaseline(
		itemPath,
		str(stat.get("ModTime") or ""),
		token,
		size,
		protectDirtyCache,
	)


def _sameFileOnDisk(first: Path, second: Path) -> bool:
	try:
		return first.resolve() == second.resolve()
	except OSError:
		return False


def _sameContent(first: Path, second: Path) -> bool:
	try:
		if first.stat().st_size != second.stat().st_size:
			return False
		return filecmp.cmp(first, second, shallow=False)
	except OSError:
		return False


def _dirtyCacheBeside(itemPath: str, local: Path) -> bool:
	cached: Path = cacheLocalPath(itemPath)
	if not cached.is_file():
		return False
	try:
		if cached.resolve() == local.resolve():
			return False
	except OSError:
		return False
	entry = getEntry(itemPath)
	state: str = ""
	if entry is not None:
		try:
			state = str(entry["syncState"] or "")
		except (IndexError, KeyError, TypeError):
			state = ""
	if state == "syncing":
		return False
	if state in ("dirty", "conflict"):
		return True
	return isCacheDirty(itemPath)


def _markCacheWritable(itemPath: str, target: Path) -> None:
	if not target.is_file():
		raise FastOneDriveError(f"cache inválido: {target}")
	applyStoredMode(itemPath, target)


def isCacheDirty(itemPath: str) -> bool:
	local: Path = cacheLocalPath(itemPath)
	if not local.is_file():
		return False
	entry = getEntry(itemPath)
	if entry is not None:
		state: str = ""
		try:
			state = str(entry["syncState"] or "")
		except (IndexError, KeyError):
			state = ""
		if state in ("dirty", "syncing", "conflict", "error"):
			return True
		baseTime: datetime | None = parseRemoteModTime(str(entry["baseModTime"] or ""))
		if baseTime is None:
			baseTime = parseRemoteModTime(str(entry["modTime"] or ""))
		localTime: datetime = datetime.fromtimestamp(
			local.stat().st_mtime,
			tz=timezone.utc,
		)
		if baseTime is None:
			return True
		return localTime > baseTime + MTIME_SKEW
	stat = remoteStat(config.remotePath(itemPath))
	remoteTime: datetime | None = None
	if stat is not None:
		remoteTime = parseRemoteModTime(str(stat.get("ModTime") or ""))
	localTime = datetime.fromtimestamp(local.stat().st_mtime, tz=timezone.utc)
	if remoteTime is None:
		return True
	return localTime > remoteTime + MTIME_SKEW


def isDirtyCacheFile(path: Path) -> bool:
	root: Path = config.cacheDir().resolve()
	try:
		rel: str = path.resolve().relative_to(root).as_posix()
	except ValueError:
		return False
	if rel == "." or rel == "":
		return False
	return isCacheDirty(rel)
