import fnmatch
import shutil
from collections.abc import Callable
from pathlib import Path

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.disk import (
	assertFreeSpace,
	cacheJobsLock,
	isStagingRel,
	pathPresent,
	pinsLock,
	removePath,
	suppressPath,
	swapIntoPlace,
)
from fastonedrive.hydrate import adoptDownloadedFile, isCacheDirty, isDirtyCacheFile
from fastonedrive.index import (
	clearBaselineTree,
	getEntry,
	hasDescendants,
	isPinnedHereOrAbove,
	listDescendants,
	listPinned,
	setPinned,
	setPinnedTree,
	upsertPinned,
)
from fastonedrive.log import logError, logInfo
from fastonedrive.paths import remoteBasename, remoteItemPath, toRemotePath
from fastonedrive.rclone import (
	copyDir,
	copyToReplace,
	isRemoteDir,
	iterLsjson,
	remoteSizeBytes,
	remoteStat,
)
from fastonedrive.search import pinnedLocalPath

PIN_REFUSAL: str = "há alterações locais por enviar; nada foi mantido neste dispositivo"
UNPIN_REFUSAL: str = "há alterações locais por enviar; nada foi deixado de manter"


def pinRemote(source: str, onStats: Callable[[str], None] | None = None) -> str:
	itemPath: str = remoteItemPath(toRemotePath(source))
	if itemPath != "":
		_flushPendingOrRefuse(itemPath, PIN_REFUSAL)
	with pinsLock():
		if itemPath != "" and _treeNeedsUpload(itemPath):
			raise FastOneDriveError(PIN_REFUSAL)
		result: str = _pinRemote(source, onStats)
	_notifyPinChange(source)
	return result


def _notifyPinChange(source: str) -> None:
	"""O emblema muda logo (mantido / na nuvem), sem esperar que o Dolphin repinte."""
	from fastonedrive.notify import notifyDirChanged, notifyStateChanged

	try:
		itemPath: str = remoteItemPath(toRemotePath(source))
	except FastOneDriveError:
		return
	notifyStateChanged(itemPath)
	notifyDirChanged(itemPath)


def unpinRemote(source: str) -> str:
	itemPath: str = remoteItemPath(toRemotePath(source))
	if itemPath != "":
		_flushPendingOrRefuse(itemPath, UNPIN_REFUSAL)
	with pinsLock():
		if itemPath != "" and _treeNeedsUpload(itemPath):
			raise FastOneDriveError(UNPIN_REFUSAL)
		result: str = _unpinRemote(source)
	_notifyPinChange(source)
	return result


def _pinRemote(source: str, onStats: Callable[[str], None] | None) -> str:
	remotePath: str = toRemotePath(source)
	itemPath: str = remoteItemPath(remotePath)
	if itemPath == "":
		raise FastOneDriveError("não é possível manter a raiz do Drive localmente")
	name: str = remoteBasename(remotePath)
	pathBlocked: str | None = pinPathBlocked(itemPath)
	if pathBlocked is not None:
		raise FastOneDriveError(pathBlocked)
	isDir: bool
	entry = getEntry(itemPath)
	if entry is not None:
		isDir = bool(entry["isDir"])
	else:
		isDir = isRemoteDir(remotePath)
	size: int
	if isDir:
		size = remoteSizeBytes(remotePath, skipPinCopyFlags())
	else:
		size = remoteSizeBytes(remotePath, useFilters=False)
	root: Path = config.ensurePinDir().resolve()
	target: Path = pinnedLocalPath(itemPath)
	_assertInside(root, target)
	if not target.exists():
		clearBaselineTree(itemPath)
	with cacheJobsLock():
		assertFreeSpace(root, size)
		if isDir:
			_pinDirectory(
				root,
				remotePath,
				itemPath,
				target,
				size,
				onStats,
			)
		else:
			target.parent.mkdir(parents=True, exist_ok=True)
			assertFreeSpace(target.parent, size)
			suppressPath(itemPath)
			copyToReplace(remotePath, str(target), onStats)
			upsertPinned(itemPath, True, name, False, size)
	_baselineAfterPin(itemPath)
	return str(target)


def _unpinRemote(source: str) -> str:
	remotePath: str = toRemotePath(source)
	itemPath: str = remoteItemPath(remotePath)
	if itemPath == "":
		raise FastOneDriveError("não é possível deixar de manter a raiz do Drive")
	root: Path = config.ensurePinDir().resolve()
	target: Path = pinnedLocalPath(itemPath)
	_assertInside(root, target)
	entry = getEntry(itemPath)
	nested: bool = hasDescendants(itemPath)
	isDir: bool = (
		(entry is not None and bool(entry["isDir"]))
		or target.is_dir()
		or nested
	)
	cleared: int
	with cacheJobsLock():
		if isDir:
			priorPinned: list[str] = _pinnedDescendants(itemPath)
			cleared = setPinnedTree(itemPath, False)
			try:
				if target.is_dir():
					_removePinnedFiles(itemPath, target)
			except Exception as error:
				_restorePinnedStillPresent(priorPinned)
				raise FastOneDriveError(
					f"não foi possível remover a cópia local: {target}"
				) from error
		else:
			cleared = 1 if setPinned(itemPath, False) else 0
			try:
				if target.is_file() or target.is_symlink():
					target.unlink(missing_ok=True)
			except Exception as error:
				if pathPresent(target):
					setPinned(itemPath, True)
				raise FastOneDriveError(
					f"não foi possível remover a cópia local: {target}"
				) from error
		try:
			_pruneEmptyParents(root, target.parent)
		except OSError:
			pass
	logInfo(
		"unpin",
		extra={"path": itemPath, "isDir": isDir, "nested": nested, "cleared": cleared},
	)
	return str(target)


def freeLocalSpace(source: str) -> str:
	from fastonedrive.index import getEntry
	from fastonedrive.paths import remoteItemPath, toRemotePath
	from fastonedrive.search import cacheLocalPath

	itemPath: str = remoteItemPath(toRemotePath(source))
	if itemPath == "":
		raise FastOneDriveError("não é possível libertar a raiz")
	entry = getEntry(itemPath)
	if entry is not None and bool(entry["pinned"]):
		if _needsUpload(itemPath):
			_syncOrRefuse(itemPath)
		return unpinRemote(source)
	cached: Path = cacheLocalPath(itemPath)
	if cached.is_file() or cached.is_dir():
		if _cacheTreeDirty(cached):
			_syncOrRefuse(itemPath)
			if _cacheTreeDirty(cached):
				raise FastOneDriveError(
					"há alterações locais por enviar; o espaço não foi libertado"
				)
		from fastonedrive.chunks import deleteChunkTree

		if cached.is_dir():
			shutil.rmtree(cached)
		else:
			cached.unlink(missing_ok=True)
		deleteChunkTree(itemPath)
		_notifyPinChange(source)
		return str(cached)
	raise FastOneDriveError("nada local para libertar neste item")


def _baselineAfterPin(itemPath: str) -> None:
	try:
		entry = getEntry(itemPath)
		remote: str = config.remotePath(itemPath)
		if entry is None or not bool(entry["isDir"]):
			local: Path = pinnedLocalPath(itemPath)
			stat = remoteStat(remote)
			if stat is not None and local.is_file():
				adoptDownloadedFile(itemPath, local, stat)
			return
		for item in iterLsjson(remote, recursive=True, useHash=True):
			if bool(item.get("IsDir")):
				continue
			rel: str = str(item.get("Path") or item.get("Name") or "").replace("\\", "/").strip("/")
			if rel == "":
				continue
			full: str = f"{itemPath}/{rel}"
			child: Path = pinnedLocalPath(full)
			if child.is_file():
				adoptDownloadedFile(full, child, item)
	except FastOneDriveError as error:
		logError(str(error), extra={"path": itemPath, "step": "baseline"})


def _needsUpload(itemPath: str) -> bool:
	entry = getEntry(itemPath)
	if entry is not None:
		state: str = ""
		try:
			state = str(entry["syncState"] or "")
		except (IndexError, KeyError, TypeError):
			state = ""
		if state in ("dirty", "syncing", "conflict", "error"):
			return True
	return isCacheDirty(itemPath)


def _cacheTreeDirty(cached: Path) -> bool:
	if cached.is_file():
		return isDirtyCacheFile(cached)
	if not cached.is_dir():
		return False
	for child in cached.rglob("*"):
		if child.is_file() and isDirtyCacheFile(child):
			return True
	return False


def _syncOrRefuse(itemPath: str) -> None:
	from fastonedrive.syncpins import syncPins

	syncPins(itemPath=itemPath)
	if _needsUpload(itemPath):
		raise FastOneDriveError(
			"há alterações locais por enviar; o espaço não foi libertado"
		)


def storedPinFlag(itemPath: str) -> bool | None:
	if itemPath != "" and pinPathBlocked(itemPath) is not None:
		return False
	if itemPath != "" and isPinnedHereOrAbove(itemPath):
		return True
	return None


def shouldStorePinned(itemPath: str) -> bool:
	return storedPinFlag(itemPath) is True


def pinPathBlocked(itemPath: str) -> str | None:
	normalized: str = itemPath.strip("/")
	parts: list[str] = [part.lower() for part in normalized.split("/") if part != ""]
	pathLower: str = normalized.lower()
	for skip in config.skipPinSegments():
		rule: str = skip.strip().strip("/")
		if rule == "":
			continue
		if _skipRuleMatches(
			pathLower,
			parts,
			rule.lower(),
		):
			return f"não é permitido manter localmente '{skip}'"
	return None


def skipPinCopyFlags() -> list[str]:
	flags: list[str] = ["--ignore-case"]
	for skip in config.skipPinSegments():
		rule: str = skip.strip().strip("/")
		if rule == "":
			continue
		if "*" in rule or "?" in rule:
			pattern: str = rule
			if not pattern.startswith("/") and not pattern.startswith("*"):
				flags.extend(["--exclude", f"/{pattern}"])
				pattern = f"**/{pattern}"
			flags.extend(["--exclude", pattern])
			continue
		if "/" in rule:
			flags.extend(["--exclude", f"/{rule}/**"])
			flags.extend(["--exclude", f"/{rule}"])
			continue
		flags.extend(["--exclude", f"/{rule}/**"])
		flags.extend(["--exclude", f"/{rule}"])
		flags.extend(["--exclude", f"**/{rule}/**"])
		flags.extend(["--exclude", f"**/{rule}"])
	return flags


def _skipRuleMatches(pathLower: str, parts: list[str], rule: str) -> bool:
	if "*" in rule or "?" in rule:
		candidates: list[str] = [pathLower]
		built: str = ""
		for part in parts:
			built = part if built == "" else f"{built}/{part}"
			candidates.append(built)
		for candidate in candidates:
			if fnmatch.fnmatch(candidate, rule):
				return True
			if fnmatch.fnmatch(candidate, f"{rule}/**"):
				return True
		return False
	if "/" in rule:
		return pathLower == rule or pathLower.startswith(rule + "/")
	return rule in parts


def _pinDirectory(
	root: Path,
	remotePath: str,
	itemPath: str,
	target: Path,
	needBytes: int,
	onStats: Callable[[str], None] | None,
) -> None:
	staging: Path = _stagingDir(root, itemPath)
	if pathPresent(staging):
		removePath(staging)
	staging.mkdir(parents=True, exist_ok=True)
	try:
		assertFreeSpace(root, needBytes)
		copyDir(
			remotePath,
			str(staging),
			onStats,
			skipPinCopyFlags(),
		)
		swapIntoPlace(staging, target)
		upsertPinned(
			itemPath,
			True,
			Path(itemPath).name,
			True,
			0,
		)
	except Exception:
		if pathPresent(staging):
			removePath(staging, ignoreErrors=True)
		raise
	_pinCopiedTree(root, itemPath)


def _stagingDir(root: Path, itemPath: str) -> Path:
	slug: str = itemPath.replace("/", "__")
	return root / f".fod-staging-{slug}"


def _pinCopiedTree(root: Path, itemPath: str) -> None:
	base: Path = pinnedLocalPath(itemPath)
	upsertPinned(itemPath, True, Path(itemPath).name, True, 0)
	found: list[Path] = list(base.rglob("*"))
	for child in found:
		if not child.exists():
			continue
		rel: str = child.resolve().relative_to(root).as_posix()
		if isStagingRel(rel):
			_removePinnedPath(child)
			continue
		blocked: str | None = pinPathBlocked(rel)
		if blocked is not None:
			_removePinnedPath(child)
			continue
		isDir: bool = child.is_dir()
		size: int = child.stat().st_size if child.is_file() else 0
		upsertPinned(rel, True, child.name, isDir, size)


def _removePinnedPath(path: Path) -> None:
	if path.is_dir():
		shutil.rmtree(path, ignore_errors=True)
		return
	if path.is_file() or path.is_symlink():
		path.unlink(missing_ok=True)


def _pinnedDescendants(itemPath: str) -> list[str]:
	prefix: str = f"{itemPath}/"
	found: list[str] = []
	for row in listPinned():
		path: str = str(row["path"])
		if path == itemPath or path.startswith(prefix):
			found.append(path)
	return found


def _flushPendingOrRefuse(itemPath: str, refusal: str) -> None:
	if not _treeNeedsUpload(itemPath):
		return
	from fastonedrive.syncpins import syncPins

	syncPins(itemPath=itemPath)
	if _treeNeedsUpload(itemPath):
		raise FastOneDriveError(refusal)


def _treeNeedsUpload(itemPath: str) -> bool:
	from datetime import datetime, timedelta, timezone

	from fastonedrive.rclone import parseRemoteModTime
	from fastonedrive.search import cacheLocalPath

	for row in listDescendants(itemPath):
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


def _removePinnedFiles(itemPath: str, target: Path) -> None:
	if target.is_symlink() or target.is_file():
		target.unlink()
		return
	if not target.is_dir():
		return
	for child in list(target.iterdir()):
		childItem: str = f"{itemPath}/{child.name}" if itemPath != "" else child.name
		_removePinnedFiles(childItem, child)
	target.rmdir()


def _restorePinnedStillPresent(paths: list[str]) -> None:
	for path in paths:
		if pathPresent(pinnedLocalPath(path)):
			setPinned(path, True)


def _assertInside(root: Path, target: Path) -> None:
	try:
		target.relative_to(root)
	except ValueError as error:
		raise FastOneDriveError("caminho de pin inválido") from error


def _pruneEmptyParents(root: Path, start: Path) -> None:
	rootResolved: Path = root.resolve()
	current: Path = start.resolve()
	while True:
		try:
			current.relative_to(rootResolved)
		except ValueError:
			return
		if current == rootResolved:
			return
		if not current.is_dir():
			current = current.parent
			continue
		try:
			next(current.iterdir())
			return
		except StopIteration:
			parent: Path = current.parent
			current.rmdir()
			current = parent
