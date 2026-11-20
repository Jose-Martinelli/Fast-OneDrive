import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import copy2
from typing import Any

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.log import logError
from fastonedrive.activity import setActivity
from fastonedrive.disk import (
	assertFreeSpace,
	evictCache,
	isEphemeralName,
	isEphemeralPath,
	isStagingRel,
	pathPresent,
	pinsLock,
	tryExclusiveWriteGuard,
	writeGuardHeld,
	pruneJobArtifacts,
	suppressPath,
)
from fastonedrive.chunks import deleteChunkRow, fillHoles, isPartial
from fastonedrive.hydrate import (
	_dirtyCacheBeside,
	adoptDownloadedFile,
	isCacheDirty,
	isDirtyCacheFile,
)
from fastonedrive.index import (
	SYNC_DIRTY,
	SYNC_ERROR,
	SYNC_SYNCING,
	deleteIndexTree,
	clearItemBaseline,
	getEntry,
	listDirtyPaths,
	listPinned,
	markDirty,
	parentPath,
	setPinned,
	setSyncState,
	upsertEntry,
	upsertPinned,
)
from fastonedrive.notify import notifyDirChanged
from fastonedrive.pin import pinPathBlocked
from fastonedrive.rclone import (
	contentTokenFromStat,
	copyFromLocal,
	copyToReplace,
	deleteRemoteFile,
	hashLocalQuickXor,
	isContentToken,
	iterLsjson,
	parseRemoteModTime,
	quickXorEqual,
	remoteSizeBytes,
	remoteStat,
	sameToken,
	statSize,
	rmdirRemote,
	tokenKind,
	tokensDiffer,
)
from fastonedrive.search import cacheLocalPath, pinnedLocalPath

MTIME_SKEW: timedelta = timedelta(seconds=2)
# Acima disto, sem reflink, copiar para um instantâneo custa mais do que segurar o
# ficheiro durante o envio.
SNAPSHOT_COPY_MAX_BYTES: int = 64 * 1024 * 1024
FICLONE: int = 0x40049409
DOWNLOAD_MARK: str = "descarga"
_dirtyRedirects: set[str] = set()


def syncPins(
	onStats: Callable[[str], None] | None = None,
	itemPath: str | None = None,
) -> dict[str, int]:
	with pinsLock():
		return _syncPins(onStats, itemPath)


def _syncPins(
	onStats: Callable[[str], None] | None,
	itemPath: str | None,
) -> dict[str, int]:
	return _syncPinsLocked(onStats, itemPath)


def _syncPinsLocked(
	onStats: Callable[[str], None] | None,
	itemPath: str | None,
) -> dict[str, int]:
	target: str = (itemPath or "").strip().strip("/")
	if target == "":
		pruneJobArtifacts(config.ensureCacheDir(), includePartials=True, recursive=True)
		_pruneUploadSnapshots()
	else:
		# Envio de um só ficheiro (cada gravação): percorrer a cache inteira com rglob
		# pesava em cada Ctrl+S; basta a pasta do item.
		pruneJobArtifacts(
			cacheLocalPath(parentPath(target)) if "/" in target else config.ensureCacheDir(),
			includePartials=True,
		)
	stats: dict[str, int] = {
		"downloaded": 0,
		"uploaded": 0,
		"deletedLocal": 0,
		"deletedRemote": 0,
		"skipped": 0,
		"conflicts": 0,
		"errors": 0,
	}
	if target != "" and isEphemeralPath(target):
		return stats
	setActivity("syncing", target if target != "" else "pins")
	pendingDirs: list[str] = []
	try:
		if target != "":
			_syncOnePath(target, stats, onStats, pendingDirs)
			_syncDirtyCache(stats, onStats, target)
		else:
			_syncPinnedRows(stats, onStats, pendingDirs)
			_syncDirtyDirectories(stats)
			_dropOrphanRows(stats)
			_syncDirtyCache(stats, onStats, None)
		_flushPendingRmdirs(pendingDirs, stats)
		setActivity("idle", "")
	except Exception as error:
		setActivity("error", str(error))
		raise
	return stats


def _syncPinnedRows(
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
	pendingDirs: list[str],
) -> None:
	for row in listPinned():
		path: str = str(row["path"])
		if isEphemeralPath(path):
			continue
		blocked: str | None = pinPathBlocked(path)
		if blocked is not None:
			continue
		_syncOnePath(path, stats, onStats, pendingDirs)


def _syncOnePath(
	itemPath: str,
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
	pendingDirs: list[str],
) -> None:
	entry = getEntry(itemPath)
	if entry is not None and bool(entry["isDir"]):
		pinnedHere: bool = bool(entry["pinned"])
		state: str = str(entry["syncState"] or "")
		if state == SYNC_DIRTY and not _publishDirtyDirectory(itemPath, stats):
			return
		_syncDirtyUnder(itemPath, stats, onStats, pendingDirs)
		if not pinnedHere and not pinnedLocalPath(itemPath).is_dir():
			return
		_syncPinnedDirectory(itemPath, stats, onStats, pendingDirs)
		return
	_ensurePinnedCopy(itemPath, stats)
	kind: str
	local: Path | None
	kind, local = _intactLocal(itemPath, stats)
	if kind == "blocked":
		return
	remotePath: str = config.remotePath(itemPath)
	if entry is None and kind == "missing":
		# Temporário de editor (QSaveFile "nome.txt.XXXXXX") já renomeado: nada no índice
		# nem no disco. Antes custava um rclone stat (segundos) à frente do envio real.
		stats["skipped"] += 1
		return
	try:
		stat = quickStat(itemPath, remotePath)
	except OfflineError:
		_retryOffline(itemPath, stats)
		return
	except FastOneDriveError:
		stats["errors"] += 1
		setSyncState(itemPath, SYNC_ERROR, "stat remoto falhou")
		return
	remoteExists: bool = stat is not None
	if kind == "download":
		if remoteExists:
			_downloadRemote(
				itemPath,
				pinnedLocalPath(itemPath),
				stats,
				onStats,
			)
			return
		stats["skipped"] += 1
		return
	localExists: bool = local is not None and local.is_file()
	if localExists and remoteExists:
		assert stat is not None and local is not None
		_resolveBothExist(itemPath, local, stat, stats, onStats)
		return
	if remoteExists and not localExists:
		_resolveRemoteOnly(itemPath, stats, onStats, pendingDirs)
		return
	if localExists and not remoteExists:
		assert local is not None
		_resolveLocalOnly(itemPath, local, stats, onStats)
		return
	if _orphanLocalRow(itemPath, entry):
		# Criado aqui, nunca subiu, e a cópia local desapareceu: a linha só mostrava
		# um ficheiro fantasma de 0 bytes.
		deleteIndexTree(itemPath)
		stats["deletedLocal"] += 1
		return
	stats["skipped"] += 1


def _dropOrphanRows(stats: dict[str, int]) -> None:
	itemPath: str
	for itemPath in listDirtyPaths(""):
		if isEphemeralPath(itemPath):
			continue
		if _orphanLocalRow(itemPath, getEntry(itemPath)):
			deleteIndexTree(itemPath)
			stats["deletedLocal"] += 1


def _orphanLocalRow(itemPath: str, entry: object) -> bool:
	if entry is None:
		return False
	try:
		if bool(entry["isDir"]):  # type: ignore[index]
			return False
		for key in ("itemId", "etag", "baseEtag"):
			if str(entry[key] or "") != "":  # type: ignore[index]
				return False
	except (KeyError, IndexError, TypeError):
		return False
	if writeGuardHeld(itemPath):
		return False
	return not pinnedLocalPath(itemPath).exists() and not cacheLocalPath(itemPath).exists()


def _publishDirtyDirectory(itemPath: str, stats: dict[str, int]) -> bool:
	from fastonedrive import graphitems
	from fastonedrive.mutate import mkdirRemoteFast

	try:
		mkdirRemoteFast(itemPath)
		return True
	except graphitems.GraphUnreachable:
		_retryOffline(itemPath, stats)
		return False
	except FastOneDriveError as error:
		if not _remoteDirExists(itemPath):
			stats["errors"] += 1
			setSyncState(itemPath, SYNC_ERROR, str(error))
			return False
	setSyncState(itemPath, "")
	notifyDirChanged(itemPath)
	return True


OFFLINE_RETRY_SEC: float = 60.0


class OfflineError(FastOneDriveError):
	"""Sem rede: o item fica dirty e volta a tentar, sem emblema de erro."""


def _retryOffline(itemPath: str, stats: dict[str, int]) -> None:
	from fastonedrive.watcher import debounceSync

	stats["skipped"] += 1
	setSyncState(itemPath, SYNC_DIRTY, "sem rede")
	debounceSync(itemPath, OFFLINE_RETRY_SEC)


def _remoteDirExists(itemPath: str) -> bool:
	try:
		stat: dict[str, Any] | None = remoteStat(config.remotePath(itemPath))
	except FastOneDriveError:
		return False
	return stat is not None and bool(stat.get("IsDir"))


def _syncDirtyDirectories(stats: dict[str, int]) -> None:
	itemPath: str
	for itemPath in listDirtyPaths(""):
		entry = getEntry(itemPath)
		if entry is None or not bool(entry["isDir"]):
			continue
		if str(entry["syncState"] or "") != SYNC_DIRTY:
			continue
		_publishDirtyDirectory(itemPath, stats)


def _syncDirtyUnder(
	root: str,
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
	pendingDirs: list[str],
) -> None:
	itemPath: str
	for itemPath in listDirtyPaths(root):
		entry = getEntry(itemPath)
		if entry is None:
			continue
		if bool(entry["isDir"]):
			if str(entry["syncState"] or "") == SYNC_DIRTY:
				_publishDirtyDirectory(itemPath, stats)
			continue
		_syncOnePath(itemPath, stats, onStats, pendingDirs)


def _syncPinnedDirectory(
	itemPath: str,
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
	pendingDirs: list[str],
) -> None:
	target: Path = pinnedLocalPath(itemPath)
	if not target.is_dir():
		_syncMissingPinnedDirectory(
			itemPath,
			target,
			stats,
			onStats,
			pendingDirs,
		)
		return
	remotePath: str = config.remotePath(itemPath)
	try:
		_applyRemoteListing(
			itemPath,
			remotePath,
			stats,
			onStats,
			pendingDirs,
		)
	except FastOneDriveError:
		stats["errors"] += 1
		setSyncState(itemPath, SYNC_ERROR, "listagem remota falhou")


def _listedRemoteIsFile(remotePath: str, itemPath: str, items: list[dict[str, Any]]) -> bool:
	name: str = itemPath.rsplit("/", 1)[-1]
	if name == "" or len(items) != 1:
		return False
	item: dict[str, Any] = items[0]
	if bool(item.get("IsDir")) or str(item.get("Name") or "") != name:
		return False
	stat: dict[str, Any] | None = remoteStat(remotePath)
	return stat is not None and not bool(stat.get("IsDir"))


def _applyRemoteListing(
	itemPath: str,
	remotePath: str,
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
	pendingDirs: list[str],
) -> None:
	raw: list[dict[str, Any]] = list(
		iterLsjson(remotePath, recursive=False, maxDepth=1)
	)
	if _listedRemoteIsFile(remotePath, itemPath, raw):
		return
	seen: set[str] = set()
	for item in raw:
		name: str = str(item.get("Name") or "")
		if name == "" or isEphemeralName(name):
			continue
		child: str = f"{itemPath}/{name}"
		seen.add(child)
		if pinPathBlocked(child) is not None:
			continue
		isDir: bool = bool(item.get("IsDir"))
		_keepPinnedChild(child, name, isDir, int(item.get("Size") or 0))
		if isDir:
			pinnedLocalPath(child).mkdir(parents=True, exist_ok=True)
			_syncPinnedDirectory(child, stats, onStats, pendingDirs)
			continue
		_syncOnePath(child, stats, onStats, pendingDirs)
	localRoot: Path = pinnedLocalPath(itemPath)
	if not localRoot.is_dir():
		return
	for childPath in localRoot.iterdir():
		if childPath.name in ("", ".", "..") or isEphemeralName(childPath.name):
			continue
		rel: str = f"{itemPath}/{childPath.name}"
		if rel in seen:
			continue
		if childPath.is_file():
			_syncOnePath(rel, stats, onStats, pendingDirs)


def _syncMissingPinnedDirectory(
	itemPath: str,
	target: Path,
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
	pendingDirs: list[str],
) -> None:
	entry = getEntry(itemPath)
	baseEtag: str = str(entry["baseEtag"] or "") if entry is not None else ""
	if not isContentToken(baseEtag):
		target.mkdir(parents=True, exist_ok=True)
		_downloadPinnedChildren(itemPath, stats, onStats)
		return
	_resolveRemoteOnly(itemPath, stats, onStats, pendingDirs)


def _downloadPinnedChildren(
	itemPath: str,
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
) -> None:
	remotePath: str = config.remotePath(itemPath)
	try:
		raw: list[dict[str, Any]] = list(
			iterLsjson(remotePath, recursive=False, maxDepth=1)
		)
		if _listedRemoteIsFile(remotePath, itemPath, raw):
			return
		for item in raw:
			name: str = str(item.get("Name") or "")
			if name in ("", ".", "..") or isEphemeralName(name):
				continue
			child: str = f"{itemPath}/{name}"
			if child == itemPath or pinPathBlocked(child) is not None:
				continue
			if bool(item.get("IsDir")):
				pinnedLocalPath(child).mkdir(parents=True, exist_ok=True)
				upsertPinned(child, True, name, True, 0)
				_downloadPinnedChildren(child, stats, onStats)
				continue
			if _preferDirtyCache(child, stats, onStats):
				continue
			_downloadRemote(
				child,
				pinnedLocalPath(child),
				stats,
				onStats,
			)
	except FastOneDriveError:
		stats["errors"] += 1
		setSyncState(itemPath, SYNC_ERROR, "listagem remota falhou")


def _preferDirtyCache(
	itemPath: str,
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
) -> bool:
	if itemPath in _dirtyRedirects or not isCacheDirty(itemPath):
		return False
	if writeGuardHeld(itemPath):
		return True
	_dirtyRedirects.add(itemPath)
	try:
		promoted: tuple[str, Path | None] = _promoteDirtyCache(itemPath, stats)
		if promoted[0] == "blocked":
			return True
		if promoted[0] != "file":
			return False
		_syncOnePath(itemPath, stats, onStats, [])
		return True
	finally:
		_dirtyRedirects.discard(itemPath)


def _promoteDirtyCache(
	itemPath: str,
	stats: dict[str, int],
) -> tuple[str, Path | None]:
	with tryExclusiveWriteGuard(itemPath) as held:
		if not held:
			return "blocked", None
		cached: Path = cacheLocalPath(itemPath)
		if not cached.is_file() or not isCacheDirty(itemPath):
			return "skip", None
		if isPartial(itemPath) and not _ensureFilled(itemPath, cached):
			stats["errors"] += 1
			return "blocked", None
		if not cached.is_file():
			return "skip", None
		target: Path = pinnedLocalPath(itemPath)
		if target.is_dir():
			return "file", cached
		target.parent.mkdir(parents=True, exist_ok=True)
		sameFile: bool = False
		try:
			sameFile = cached.resolve() == target.resolve()
		except OSError:
			sameFile = False
		if not sameFile:
			copy2(cached, target)
			if not target.is_file():
				stats["errors"] += 1
				return "blocked", None
			cached.unlink(missing_ok=True)
		if not target.is_file():
			stats["errors"] += 1
			return "blocked", None
		return "file", target


def _mtMatchesLocal(local: Path, remoteToken: str) -> bool:
	body: str = remoteToken[3:]
	epochText: str
	sizeText: str
	epochText, sep, sizeText = body.partition(":")
	if sep == "" or epochText == "" or sizeText == "":
		return False
	try:
		remoteWhen: datetime = datetime.fromtimestamp(int(epochText), tz=timezone.utc)
		remoteSize: int = int(sizeText)
	except ValueError:
		return False
	localWhen: datetime = datetime.fromtimestamp(local.stat().st_mtime, tz=timezone.utc)
	localSize: int = int(local.stat().st_size)
	if localSize != remoteSize:
		return False
	return abs((localWhen - remoteWhen).total_seconds()) <= MTIME_SKEW.total_seconds()


def _baselineNeedsHash(baseEtag: str) -> bool:
	return baseEtag.startswith("migrated:") or baseEtag.startswith("mt:")


def _resolveHashedBaseline(
	itemPath: str,
	local: Path,
	stat: dict[str, object],
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
) -> None:
	if not _readyToHash(itemPath, local, stats):
		return
	localToken: str = ""
	try:
		localToken = hashLocalQuickXor(local)
	except FastOneDriveError:
		localToken = ""
	remoteToken: str = contentTokenFromStat(stat)
	matched: bool | None = quickXorEqual(localToken, remoteToken)
	if matched is True:
		adoptDownloadedFile(itemPath, local, stat)
		stats["skipped"] += 1
		return
	if matched is False and _replacedKnownVersion(itemPath, stat):
		# Substituir no explorador um ficheiro que nunca foi aberto aqui (sem base): se a
		# nuvem ainda é a versão que o índice conhecia, ninguém a mudou entretanto e a
		# versão nova sobe por cima, como no Windows. Antes, cada substituição gerava
		# uma cópia "(conflito local …)".
		_uploadLocal(itemPath, local, stats, onStats)
		return
	if matched is False:
		_writeConflict(
			itemPath,
			local,
			stat,
			stats,
			onStats,
		)
		return
	entry = getEntry(itemPath)
	baseEtag: str = str(entry["baseEtag"] or "") if entry is not None else ""
	if baseEtag == "" and tokenKind(remoteToken) == "mt":
		if _mtMatchesLocal(local, remoteToken):
			adoptDownloadedFile(itemPath, local, stat)
			stats["skipped"] += 1
			return
		_writeConflict(
			itemPath,
			local,
			stat,
			stats,
			onStats,
		)
		return
	if baseEtag == "":
		_writeConflict(
			itemPath,
			local,
			stat,
			stats,
			onStats,
		)
		return
	baseTime: datetime | None = None
	if entry is not None:
		baseTime = parseRemoteModTime(str(entry["baseModTime"] or ""))
	localTime: datetime = datetime.fromtimestamp(local.stat().st_mtime, tz=timezone.utc)
	remoteTime: datetime | None = parseRemoteModTime(str(stat.get("ModTime") or ""))
	localNewer: bool = baseTime is None or localTime > baseTime + MTIME_SKEW
	if localNewer or remoteTime is None:
		_writeConflict(
			itemPath,
			local,
			stat,
			stats,
			onStats,
		)
		return
	if abs((localTime - remoteTime).total_seconds()) <= MTIME_SKEW.total_seconds():
		stats["errors"] += 1
		setSyncState(itemPath, SYNC_ERROR, "não foi possível comparar o conteúdo")
		return
	_writeConflict(
		itemPath,
		local,
		stat,
		stats,
		onStats,
	)


def _replacedKnownVersion(itemPath: str, stat: dict[str, object]) -> bool:
	entry = getEntry(itemPath)
	if entry is None or str(entry["baseEtag"] or "") != "":
		return False
	if str(entry["syncState"] or "") not in (SYNC_DIRTY, SYNC_SYNCING):
		return False
	known: str = str(entry["etag"] or "")
	remote: str = contentTokenFromStat(stat)  # type: ignore[arg-type]
	if known == "" or remote == "":
		return False
	return not tokensDiffer(
		known,
		remote,
		parseRemoteModTime(str(entry["modTime"] or "")),
		parseRemoteModTime(str(stat.get("ModTime") or "")),
		int(entry["size"] or 0) or None,
		statSize(stat),
	)


def _ensurePinnedCopy(itemPath: str, stats: dict[str, int]) -> None:
	entry = getEntry(itemPath)
	if entry is None or not bool(entry["pinned"]) or bool(entry["isDir"]):
		return
	pinFile: Path = pinnedLocalPath(itemPath)
	if pathPresent(pinFile):
		return
	cached: Path = cacheLocalPath(itemPath)
	if cached.is_file() and not isPartial(itemPath) and not isCacheDirty(itemPath):
		copied: tuple[str, Path | None] | None = _materializePinFromCache(
			itemPath,
			pinFile,
			cached,
			True,
			stats,
		)
		if pinFile.is_file():
			return
		if copied is not None and copied[0] == "blocked":
			return
	if pathPresent(pinFile) or pathPresent(cached):
		return
	clearItemBaseline(itemPath)


def _keepPinnedChild(child: str, name: str, isDir: bool, size: int) -> None:
	if pinPathBlocked(child) is not None:
		return
	entry = getEntry(child)
	if entry is not None and bool(entry["pinned"]):
		if not isDir:
			_ensurePinnedCopy(child, {"errors": 0})
		return
	upsertPinned(child, True, name, isDir, size)
	if entry is None or isDir:
		return
	if pathPresent(pinnedLocalPath(child)):
		return
	clearItemBaseline(child)


def _baselineEquivalent(
	baseEtag: str,
	remoteEtag: str,
	baseTime: datetime | None,
	remoteTime: datetime | None,
	baseSize: int | None,
	remoteSize: int | None,
) -> bool:
	if baseEtag == "" or remoteEtag == "" or baseEtag.startswith("migrated:"):
		return False
	return not tokensDiffer(
		baseEtag,
		remoteEtag,
		baseTime,
		remoteTime,
		baseSize,
		remoteSize,
	)


def _deleteEquivalentRemote(
	itemPath: str,
	entry: object,
	stats: dict[str, int],
	pendingDirs: list[str],
) -> None:
	isDir: bool = False
	if entry is not None:
		try:
			isDir = bool(entry["isDir"])  # type: ignore[index]
		except (IndexError, KeyError, TypeError):
			isDir = False
	if isDir:
		pendingDirs.append(itemPath)
		return
	try:
		deleteRemoteFile(config.remotePath(itemPath))
	except FastOneDriveError as error:
		stats["errors"] += 1
		setSyncState(itemPath, SYNC_ERROR, str(error))
		return
	deleteIndexTree(itemPath)
	stats["deletedRemote"] += 1
	notifyDirChanged(parentPath(itemPath))


def _flushPendingRmdirs(pendingDirs: list[str], stats: dict[str, int]) -> None:
	ordered: list[str] = sorted(
		set(pendingDirs),
		key=lambda itemPath: (itemPath.count("/"), len(itemPath)),
		reverse=True,
	)
	itemPath: str
	for itemPath in ordered:
		_rmdirDeferred(itemPath, stats)


def _rmdirDeferred(itemPath: str, stats: dict[str, int]) -> None:
	remotePath: str = config.remotePath(itemPath)
	try:
		stat: dict[str, object] | None = remoteStat(remotePath)
	except FastOneDriveError as error:
		stats["errors"] += 1
		setSyncState(itemPath, SYNC_ERROR, str(error))
		return
	if stat is None:
		_dropRemovedDir(itemPath, stats)
		return
	try:
		rmdirRemote(remotePath)
	except FastOneDriveError:
		try:
			still: dict[str, object] | None = remoteStat(remotePath)
		except FastOneDriveError as error:
			stats["errors"] += 1
			setSyncState(itemPath, SYNC_ERROR, str(error))
			return
		if still is None:
			_dropRemovedDir(itemPath, stats)
			return
		stats["skipped"] += 1
		return
	_dropRemovedDir(itemPath, stats)


def _dropRemovedDir(itemPath: str, stats: dict[str, int]) -> None:
	deleteIndexTree(itemPath)
	stats["deletedRemote"] += 1
	notifyDirChanged(parentPath(itemPath))


def _resolveBothExist(
	itemPath: str,
	local: Path,
	stat: dict[str, object],
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
) -> None:
	entry = getEntry(itemPath)
	remoteTime: datetime | None = parseRemoteModTime(str(stat.get("ModTime") or ""))
	remoteEtag: str = contentTokenFromStat(stat)
	localTime: datetime = datetime.fromtimestamp(local.stat().st_mtime, tz=timezone.utc)
	baseTime: datetime | None = None
	baseEtag: str = ""
	if entry is not None:
		baseTime = parseRemoteModTime(str(entry["baseModTime"] or ""))
		baseEtag = str(entry["baseEtag"] or "")
	if baseEtag == "" or _baselineNeedsHash(baseEtag):
		_resolveHashedBaseline(
			itemPath,
			local,
			stat,
			stats,
			onStats,
		)
		return
	if not isContentToken(baseEtag):
		_adoptOrSend(
			itemPath,
			local,
			stat,
			remoteTime,
			localTime,
			entry,
			stats,
			onStats,
		)
		return
	localChanged: bool = _localChanged(localTime, baseTime, entry)
	remoteSize: int | None = statSize(stat)
	baseSize: int | None = int(entry["size"]) if entry is not None else None
	remoteChanged: bool = _remoteChanged(
		remoteEtag,
		baseEtag,
		remoteTime,
		baseTime,
		remoteSize,
		baseSize,
	)
	if localChanged and remoteChanged:
		_writeConflict(itemPath, local, stat, stats, onStats)
		return
	if localChanged:
		_uploadLocal(itemPath, local, stats, onStats)
		return
	if remoteChanged:
		_downloadRemote(itemPath, local, stats, onStats)
		return
	stats["skipped"] += 1


def _resolveRemoteOnly(
	itemPath: str,
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
	pendingDirs: list[str],
) -> None:
	entry = getEntry(itemPath)
	pinned: bool = entry is not None and bool(entry["pinned"])
	if not pinned:
		stats["skipped"] += 1
		return
	baseEtag: str = str(entry["baseEtag"] or "") if entry is not None else ""
	if baseEtag == "":
		target: Path = pinnedLocalPath(itemPath)
		_downloadRemote(itemPath, target, stats, onStats)
		return
	try:
		stat = remoteStat(config.remotePath(itemPath))
	except FastOneDriveError:
		stats["errors"] += 1
		setSyncState(itemPath, SYNC_ERROR, "stat remoto falhou")
		return
	remoteEtag: str = contentTokenFromStat(stat) if stat is not None else ""
	remoteTime: datetime | None = None
	remoteSize: int | None = None
	if stat is not None:
		remoteTime = parseRemoteModTime(str(stat.get("ModTime") or ""))
		remoteSize = statSize(stat)
	baseTime: datetime | None = None
	baseSize: int | None = None
	if entry is not None:
		baseTime = parseRemoteModTime(str(entry["baseModTime"] or ""))
		baseSize = int(entry["size"])
	if _baselineEquivalent(
		baseEtag,
		remoteEtag,
		baseTime,
		remoteTime,
		baseSize,
		remoteSize,
	):
		_deleteEquivalentRemote(itemPath, entry, stats, pendingDirs)
		return
	setPinned(itemPath, False)
	stats["skipped"] += 1


def _resolveLocalOnly(
	itemPath: str,
	local: Path,
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
) -> None:
	entry = getEntry(itemPath)
	baseEtag: str = str(entry["baseEtag"] or "") if entry is not None else ""
	if entry is None or baseEtag == "":
		_uploadLocal(itemPath, local, stats, onStats)
		return
	baseTime: datetime | None = parseRemoteModTime(str(entry["baseModTime"] or ""))
	localTime: datetime = datetime.fromtimestamp(local.stat().st_mtime, tz=timezone.utc)
	localChanged: bool = _localChanged(localTime, baseTime, entry)
	if localChanged:
		_uploadLocal(itemPath, local, stats, onStats)
		return
	_removeLocalCopies(itemPath, local)
	deleteIndexTree(itemPath)
	stats["deletedLocal"] += 1


def _isDownloadState(entry: object) -> bool:
	if entry is None:
		return False
	try:
		state: str = str(entry["syncState"] or "")  # type: ignore[index]
		error: str = str(entry["syncError"] or "")  # type: ignore[index]
	except (IndexError, KeyError, TypeError):
		return False
	return state in ("syncing", "error") and error.startswith(DOWNLOAD_MARK)


def _localChanged(
	localTime: datetime,
	baseTime: datetime | None,
	entry: object,
) -> bool:
	if entry is not None:
		try:
			state: str = str(entry["syncState"] or "")  # type: ignore[index]
		except (IndexError, KeyError, TypeError):
			state = ""
		if state in ("dirty", "syncing", "conflict", "error") and not _isDownloadState(entry):
			return True
	if baseTime is None:
		return True
	return localTime > baseTime + MTIME_SKEW


def _adoptOrSend(
	itemPath: str,
	local: Path,
	stat: dict[str, object],
	remoteTime: datetime | None,
	localTime: datetime,
	entry: object,
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
) -> None:
	state: str = ""
	if entry is not None:
		try:
			state = str(entry["syncState"] or "")  # type: ignore[index]
		except (IndexError, KeyError, TypeError):
			state = ""
	remoteSize: int | None = statSize(stat)
	localSize: int = int(local.stat().st_size)
	localToken: str = ""
	try:
		localToken = hashLocalQuickXor(local)
	except FastOneDriveError:
		localToken = ""
	remoteToken: str = contentTokenFromStat(stat)
	if _sameBytes(
		localToken,
		remoteToken,
		localTime,
		remoteTime,
		localSize,
		remoteSize,
	):
		adoptDownloadedFile(itemPath, local, stat)
		stats["skipped"] += 1
		return
	localEdited: bool = (
		state in ("dirty", "syncing", "conflict", "error") and not _isDownloadState(entry)
	) or (remoteTime is not None and localTime > remoteTime + MTIME_SKEW)
	if localEdited:
		_writeConflict(
			itemPath,
			local,
			stat,
			stats,
			onStats,
		)
		return
	_downloadRemote(
		itemPath,
		local,
		stats,
		onStats,
	)


def _sameBytes(
	localToken: str,
	remoteToken: str,
	localTime: datetime,
	remoteTime: datetime | None,
	localSize: int | None,
	remoteSize: int | None,
) -> bool:
	localKind: str = tokenKind(localToken)
	remoteKind: str = tokenKind(remoteToken)
	if localKind != "" and localKind == remoteKind:
		return sameToken(localToken, remoteToken)
	if remoteTime is None:
		return False
	if abs((localTime - remoteTime).total_seconds()) > MTIME_SKEW.total_seconds():
		return False
	if localSize is None or remoteSize is None:
		return False
	return localSize == remoteSize


def _remoteChanged(
	remoteEtag: str,
	baseEtag: str,
	remoteTime: datetime | None,
	baseTime: datetime | None,
	remoteSize: int | None,
	baseSize: int | None,
) -> bool:
	return tokensDiffer(
		baseEtag,
		remoteEtag,
		baseTime,
		remoteTime,
		baseSize,
		remoteSize,
	)


def _uploadLocal(
	itemPath: str,
	local: Path,
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
) -> None:
	if isEphemeralPath(itemPath) or isEphemeralName(local.name):
		return
	payload: Path = local
	pinExists: bool = pinnedLocalPath(itemPath).is_file()
	entry = getEntry(itemPath)
	rowPinned: bool = entry is not None and bool(entry["pinned"])
	if pinExists or rowPinned:
		promoted: tuple[str, Path | None] = _promoteDirtyCache(itemPath, stats)
		if promoted[0] == "blocked":
			stats["skipped"] += 1
			return
		if promoted[0] == "file" and promoted[1] is not None:
			payload = promoted[1]
	snapshot: Path | None = None
	signature: tuple[int, int, int] | None = None
	with tryExclusiveWriteGuard(itemPath) as held:
		if not held:
			stats["skipped"] += 1
			from fastonedrive.watcher import RETRY_SEC, debounceSync

			debounceSync(itemPath, RETRY_SEC)
			return
		if not _ensureFilled(itemPath, payload):
			stats["errors"] += 1
			return
		problem: str = unsafeUploadReason(itemPath, payload)
		if problem != "":
			logError(f"envio recusado: {problem}", extra={"path": itemPath})
			setSyncState(itemPath, SYNC_ERROR, problem)
			stats["errors"] += 1
			return
		setSyncState(itemPath, SYNC_SYNCING)
		setActivity("syncing", itemPath, "upload")
		# O envio vai de um instantâneo: o write guard exclusivo durante o rclone
		# bloqueava o open() de escrita do editor (Ctrl+S) durante todo o envio (~15 s).
		snapshot, signature = _uploadSnapshot(itemPath, payload)
		if snapshot is None:
			_sendAndAdopt(itemPath, payload, payload, None, stats, onStats)
			return
	try:
		_sendAndAdopt(itemPath, payload, snapshot, signature, stats, onStats)
	finally:
		snapshot.unlink(missing_ok=True)


def _pruneUploadSnapshots() -> None:
	import time

	root: Path = config.dataDir() / "upload-snapshots"
	if not root.is_dir():
		return
	cutoff: float = time.time() - 6 * 3600
	for child in root.iterdir():
		try:
			if child.is_file() and child.stat().st_mtime < cutoff and child.stat().st_ctime < cutoff:
				child.unlink(missing_ok=True)
		except OSError:
			continue


def _fileSignature(path: Path) -> tuple[int, int, int] | None:
	try:
		info: os.stat_result = path.stat()
	except OSError:
		return None
	return int(info.st_ino), int(info.st_size), int(info.st_mtime_ns)


def _uploadSnapshot(itemPath: str, payload: Path) -> tuple[Path | None, tuple[int, int, int] | None]:
	"""Cópia (reflink se o disco deixar) do ficheiro a enviar, com o mesmo mtime."""
	import fcntl
	import hashlib

	signature: tuple[int, int, int] | None = _fileSignature(payload)
	if signature is None:
		return None, None
	root: Path = config.dataDir() / "upload-snapshots"
	try:
		root.mkdir(parents=True, exist_ok=True)
	except OSError:
		return None, None
	digest: str = hashlib.sha256(itemPath.encode("utf-8")).hexdigest()
	target: Path = root / f"{digest}-{os.getpid()}"
	cloned: bool = False
	try:
		with payload.open("rb") as source, target.open("wb") as dest:
			try:
				fcntl.ioctl(dest.fileno(), FICLONE, source.fileno())
				cloned = True
			except OSError:
				cloned = False
		if not cloned:
			if signature[1] > SNAPSHOT_COPY_MAX_BYTES:
				target.unlink(missing_ok=True)
				return None, None
			copy2(payload, target)
		info: os.stat_result = payload.stat()
		os.utime(target, ns=(int(info.st_atime_ns), int(info.st_mtime_ns)))
	except OSError:
		target.unlink(missing_ok=True)
		return None, None
	return target, signature


def _sendAndAdopt(
	itemPath: str,
	payload: Path,
	source: Path,
	signature: tuple[int, int, int] | None,
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
) -> None:
	"""Envia source; adopta a versão enviada como base de payload.

	signature None = o write guard exclusivo está seguro por quem chama (caminho antigo).
	Senão, payload pode ter mudado durante o envio: só se adopta se está igual.
	"""
	try:
		stat = _sendFile(itemPath, source, onStats)
		modTime: str = ""
		size: int = int(source.stat().st_size)
		if stat is not None:
			modTime = str(stat.get("ModTime") or "")
			size = int(stat.get("Size") or size)
		if getEntry(itemPath) is None:
			upsertEntry(
				itemPath,
				payload.name,
				False,
				size,
				None,
				modTime,
				"",
				contentTokenFromStat(stat) if stat is not None else "",
				str(stat.get("ID") or "") if stat is not None else "",
			)
		if stat is None:
			setSyncState(itemPath, SYNC_ERROR, "stat após envio falhou")
			stats["errors"] += 1
			return
		from fastonedrive.index import setItemIdIfMissing

		setItemIdIfMissing(itemPath, str(stat.get("ID") or ""))
		if signature is None:
			adoptDownloadedFile(itemPath, payload, stat)
		else:
			_adoptIfUnchanged(itemPath, payload, signature, stat)
		stats["uploaded"] += 1
		notifyDirChanged(parentPath(itemPath))
	except OfflineError:
		_retryOffline(itemPath, stats)
	except FastOneDriveError as error:
		setSyncState(itemPath, SYNC_ERROR, str(error))
		stats["errors"] += 1


def _sendFile(
	itemPath: str,
	source: Path,
	onStats: Callable[[str], None] | None,
) -> dict[str, Any] | None:
	"""Ficheiro pequeno: PUT directo no Graph (< 1 s). Grande, ou o Graph recusou: rclone."""
	from fastonedrive import graphitems
	from fastonedrive.synccontrol import load

	remotePath: str = config.remotePath(itemPath)
	small: bool = int(source.stat().st_size) <= graphitems.SIMPLE_UPLOAD_MAX_BYTES
	# Com limite de banda configurado, o rclone respeita-o; o PUT directo não.
	if small and load().bwLimit == "":
		try:
			return graphitems.uploadSmallFile(itemPath, source)
		except graphitems.GraphUnreachable as error:
			raise OfflineError(str(error)) from error
		except FastOneDriveError as error:
			logError(f"envio Graph falhou, a usar rclone: {error}", extra={"path": itemPath})
	copyFromLocal(str(source), remotePath, onStats)
	return quickStat(itemPath, remotePath)


def _adoptIfUnchanged(
	itemPath: str,
	payload: Path,
	signature: tuple[int, int, int],
	stat: dict[str, Any],
) -> None:
	from fastonedrive.index import setBaseline

	with tryExclusiveWriteGuard(itemPath) as held:
		if held and _fileSignature(payload) == signature:
			adoptDownloadedFile(itemPath, payload, stat)
			return
	# Gravado outra vez durante o envio: a nuvem tem a versão anterior, que passa a
	# ser a base (o próximo envio não é conflito); a cópia local continua por enviar.
	token: str = contentTokenFromStat(stat)
	if token != "":
		setBaseline(itemPath, str(stat.get("ModTime") or ""), token, int(stat.get("Size") or 0), keepSyncState=True)
	markDirty(itemPath)
	from fastonedrive.watcher import RETRY_SEC, debounceSync

	debounceSync(itemPath, RETRY_SEC)


def quickStat(itemPath: str, remotePath: str) -> dict[str, Any] | None:
	"""Stat pelo Graph (~0,2 s) em vez de arrancar o rclone (vários segundos)."""
	from fastonedrive import graphitems

	entry = getEntry(itemPath)
	known: bool = entry is not None and str(entry["itemId"] or "") != ""
	try:
		return graphitems.graphStat(itemPath)[0]
	except graphitems.GraphItemGone:
		if not known:
			# Sem itemId o URL é por caminho: 404 quer dizer que não existe.
			return None
	except graphitems.GraphUnreachable as error:
		raise OfflineError(str(error)) from error
	except FastOneDriveError:
		pass
	return remoteStat(remotePath)


def _downloadBlocked(itemPath: str, allowDirty: bool) -> bool:
	if writeGuardHeld(itemPath):
		return True
	if allowDirty:
		return False
	entry = getEntry(itemPath)
	if entry is None:
		return False
	state: str = str(entry["syncState"] or "")
	if _isDownloadState(entry):
		return False
	return state in ("dirty", "syncing", "conflict", "error")


def _downloadRemote(
	itemPath: str,
	target: Path,
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
	allowDirty: bool = False,
	guardHeld: bool = False,
) -> None:
	if guardHeld:
		_downloadLocked(itemPath, target, stats, onStats, allowDirty)
		return
	if _downloadBlocked(itemPath, allowDirty):
		stats["skipped"] += 1
		return
	if _preferDirtyCache(itemPath, stats, onStats):
		return
	with tryExclusiveWriteGuard(itemPath) as held:
		if not held:
			stats["skipped"] += 1
			return
		_downloadLocked(itemPath, target, stats, onStats, allowDirty)


def _downloadLocked(
	itemPath: str,
	target: Path,
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
	allowDirty: bool,
) -> None:
	if not allowDirty and _downloadStateBlocked(itemPath):
		stats["skipped"] += 1
		return
	protectCache: bool = _dirtyCacheBeside(itemPath, target)
	if not protectCache:
		setSyncState(itemPath, SYNC_SYNCING, DOWNLOAD_MARK)
	setActivity("syncing", itemPath, "download")
	try:
		size: int = remoteSizeBytes(
			config.remotePath(itemPath),
			useFilters=False,
		)
		target.parent.mkdir(parents=True, exist_ok=True)
		assertFreeSpace(target.parent, size)
		suppressPath(itemPath)
		copyToReplace(config.remotePath(itemPath), str(target), onStats)
		stat = remoteStat(config.remotePath(itemPath))
		if stat is not None:
			size = int(stat.get("Size") or size)
		if target == pinnedLocalPath(itemPath) or getEntry(itemPath) is None:
			upsertPinned(itemPath, True, target.name, False, size)
		if stat is None:
			setSyncState(itemPath, SYNC_ERROR, f"{DOWNLOAD_MARK}: stat após descarga falhou")
			stats["errors"] += 1
			return
		adoptDownloadedFile(
			itemPath,
			target,
			stat,
			protectCache,
			True,
		)
		stats["downloaded"] += 1
		notifyDirChanged(parentPath(itemPath))
	except FastOneDriveError as error:
		setSyncState(itemPath, SYNC_ERROR, f"{DOWNLOAD_MARK}: {error}")
		stats["errors"] += 1


def _downloadStateBlocked(itemPath: str) -> bool:
	entry = getEntry(itemPath)
	if entry is None:
		return False
	state: str = str(entry["syncState"] or "")
	if _isDownloadState(entry):
		return False
	return state in ("dirty", "syncing", "conflict", "error")


CONFLICT_MARK: str = "(conflito local "
HOLLOW_MIN_BYTES: int = 1024 * 1024


def holeBytes(path: Path) -> int:
	"""Bytes em buracos (SEEK_HOLE/SEEK_DATA). 0 se o sistema de ficheiros não sabe."""
	try:
		size: int = int(path.stat().st_size)
		fd: int = os.open(str(path), os.O_RDONLY)
	except OSError:
		return 0
	holes: int = 0
	try:
		offset: int = 0
		while offset < size:
			try:
				hole: int = os.lseek(fd, offset, os.SEEK_HOLE)
			except OSError:
				return 0
			if hole >= size:
				break
			try:
				data: int = os.lseek(fd, hole, os.SEEK_DATA)
			except OSError:
				data = size
			holes += data - hole
			offset = data
	finally:
		os.close(fd)
	return holes


def unsafeUploadReason(itemPath: str, local: Path) -> str:
	"""Motivo para NÃO enviar este ficheiro por cima da nuvem, ou "".

	Protege contra dois casos vistos na prática: uma cópia a pedido que perdeu o registo
	dos blocos (ficheiro só com buracos/zeros) e um envio interrompido que deixou a
	cópia local vazia. Nos dois, enviar apagava o conteúdo real na nuvem.
	"""
	try:
		size: int = int(local.stat().st_size)
	except OSError:
		return "ficheiro local desapareceu"
	if size >= HOLLOW_MIN_BYTES and holeBytes(local) * 2 >= size:
		return "a cópia local está incompleta (buracos); não enviada para não estragar a nuvem"
	entry = getEntry(itemPath)
	if size == 0 and entry is not None:
		known: int = int(entry["size"] or 0)
		base: str = str(entry["baseEtag"] or "")
		if known > 0 and base == "":
			return "a cópia local ficou vazia depois de um envio interrompido; não enviada"
	return ""


def _writeConflict(
	itemPath: str,
	local: Path,
	stat: dict[str, object],
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
) -> None:
	if CONFLICT_MARK in local.name or CONFLICT_MARK in itemPath.rsplit("/", 1)[-1]:
		# Nunca um conflito de um conflito. A cópia de conflito é nossa: se o envio
		# anterior foi interrompido (mount off, reboot), a versão local ganha e sobe por
		# cima. Antes, cada interrupção gerava "(conflito local A) (conflito local B)"
		# e voltava a enviar o ficheiro inteiro.
		_uploadLocal(itemPath, local, stats, onStats)
		return
	if not _readyToHash(itemPath, local, stats):
		return
	hollow: str = unsafeUploadReason(itemPath, local)
	if hollow != "":
		# Uma cópia de conflito de um ficheiro estragado só multiplica o estrago.
		logError(f"conflito não criado: {hollow}", extra={"path": itemPath})
		setSyncState(itemPath, SYNC_ERROR, hollow)
		stats["errors"] += 1
		return
	with tryExclusiveWriteGuard(itemPath) as held:
		if not held:
			stats["skipped"] += 1
			return
		stamp: str = datetime.now().strftime("%Y-%m-%d %H%M%S")
		suffix: str = local.suffix
		stem: str = local.stem
		conflictName: str = f"{stem} (conflito local {stamp}){suffix}"
		conflictPath: Path = local.with_name(conflictName)
		number: int = 2
		while conflictPath.exists():
			conflictName = f"{stem} (conflito local {stamp}-{number}){suffix}"
			conflictPath = local.with_name(conflictName)
			number += 1
		copy2(local, conflictPath)
		conflictItem: str = f"{parentPath(itemPath)}/{conflictName}".lstrip("/")
		origin = getEntry(itemPath)
		upsertEntry(
			conflictItem,
			conflictName,
			False,
			int(conflictPath.stat().st_size),
			True if origin is not None and bool(origin["pinned"]) else None,
		)
		markDirty(conflictItem)
		_downloadRemote(
			itemPath,
			local,
			stats,
			onStats,
			True,
			True,
		)
	_uploadLocal(conflictItem, conflictPath, stats, onStats)
	stats["conflicts"] += 1


def _syncDirtyCache(
	stats: dict[str, int],
	onStats: Callable[[str], None] | None,
	onlyPath: str | None,
) -> None:
	root: Path = config.ensureCacheDir()
	if onlyPath:
		entry = getEntry(onlyPath)
		if entry is not None and bool(entry["isDir"]):
			return
		target: Path = cacheLocalPath(onlyPath)
		if target.is_file() and isCacheDirty(onlyPath):
			_syncOnePath(onlyPath, stats, onStats, [])
		return
	children: list[Path] = sorted(
		root.rglob("*"),
		key=lambda path: len(path.parts),
		reverse=True,
	)
	for child in children:
		if not child.is_file():
			continue
		if isEphemeralName(child.name):
			continue
		try:
			rel: str = child.resolve().relative_to(root.resolve()).as_posix()
		except ValueError:
			continue
		if rel in ("", ".") or isStagingRel(rel) or isEphemeralPath(rel):
			continue
		if not isCacheDirty(rel):
			continue
		_syncOnePath(rel, stats, onStats, [])
	evictCache(skip=isDirtyCacheFile)


def _materializePinFromCache(
	itemPath: str,
	pinFile: Path,
	cached: Path,
	keepOnDisk: bool,
	stats: dict[str, int],
) -> tuple[str, Path | None] | None:
	if not keepOnDisk or pinFile.is_dir() or not cached.is_file():
		return None
	if isPartial(itemPath) or isCacheDirty(itemPath):
		return None
	with tryExclusiveWriteGuard(itemPath) as held:
		if not held:
			return "blocked", None
		if pinFile.is_file():
			return "file", pinFile
		if isCacheDirty(itemPath):
			return None
		if not cached.is_file():
			return "blocked", None
		staging: Path = pinFile.with_name(f".fod-staging-{pinFile.name}")
		try:
			pinFile.parent.mkdir(parents=True, exist_ok=True)
			if pathPresent(staging):
				staging.unlink()
			copy2(cached, staging)
			staging.replace(pinFile)
		except OSError:
			try:
				if staging.is_file() or staging.is_symlink():
					staging.unlink(missing_ok=True)
			except OSError:
				pass
			stats["errors"] += 1
			return "blocked", None
	if pinFile.is_file():
		return "file", pinFile
	return None


def _intactLocal(itemPath: str, stats: dict[str, int]) -> tuple[str, Path | None]:
	entry = getEntry(itemPath)
	pinnedFlag: bool = entry is not None and bool(entry["pinned"])
	pinFile: Path = pinnedLocalPath(itemPath)
	keepOnDisk: bool = pinnedFlag or pinFile.is_file()
	if keepOnDisk and isCacheDirty(itemPath) and writeGuardHeld(itemPath):
		return "blocked", None
	if keepOnDisk:
		promoted: tuple[str, Path | None] = _promoteDirtyCache(itemPath, stats)
		if promoted[0] == "blocked":
			return "blocked", None
		if promoted[0] == "file" and promoted[1] is not None:
			return "file", promoted[1]
	if pinFile.is_file():
		return "file", pinFile
	cached: Path = cacheLocalPath(itemPath)
	materialized: tuple[str, Path | None] | None = _materializePinFromCache(
		itemPath,
		pinFile,
		cached,
		keepOnDisk,
		stats,
	)
	if materialized is not None:
		return materialized
	if not cached.is_file():
		return "missing", None
	if isPartial(itemPath):
		if keepOnDisk:
			return "download", None
		if isCacheDirty(itemPath):
			return "file", cached
		return "missing", None
	return "file", cached


def _readyToHash(itemPath: str, local: Path, stats: dict[str, int]) -> bool:
	if local != cacheLocalPath(itemPath) or not isPartial(itemPath):
		return True
	if _ensureFilled(itemPath, local):
		return True
	stats["errors"] += 1
	return False


def _ensureFilled(itemPath: str, local: Path) -> bool:
	if not isPartial(itemPath):
		return True
	wasDirty: bool = isCacheDirty(itemPath)
	if not local.is_file():
		if wasDirty:
			setSyncState(itemPath, SYNC_DIRTY, "ficheiro ainda tem buracos")
		return False
	target: Path = cacheLocalPath(itemPath)
	if not target.is_file():
		deleteChunkRow(itemPath)
		return True
	info: os.stat_result = target.stat()
	stamp: tuple[float, float] = (info.st_atime, info.st_mtime)
	fd: int = os.open(str(target), os.O_RDWR)
	try:
		try:
			fillHoles(
				itemPath,
				config.remotePath(itemPath),
				fd,
				[],
			)
		except FastOneDriveError as error:
			if wasDirty:
				setSyncState(itemPath, SYNC_DIRTY, str(error))
			return False
	finally:
		os.close(fd)
		if target.is_file():
			os.utime(target, stamp)
	if isPartial(itemPath):
		if wasDirty:
			setSyncState(itemPath, SYNC_DIRTY, "ficheiro ainda tem buracos")
		return False
	return True


def _removeLocalCopies(itemPath: str, local: Path) -> None:
	local.unlink(missing_ok=True)
	extra: Path
	for extra in (pinnedLocalPath(itemPath), cacheLocalPath(itemPath)):
		if extra == local:
			continue
		if extra.is_file() or extra.is_symlink():
			extra.unlink(missing_ok=True)


def scheduleItemSync(itemPath: str) -> None:
	if itemPath == "" or isEphemeralPath(itemPath):
		return
	markDirty(itemPath)
	from fastonedrive.watcher import debounceSync

	debounceSync(itemPath)
