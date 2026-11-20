import mimetypes
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.disk import isEphemeralPath
from fastonedrive.index import (
	FILE_SELECT,
	connectDb,
	forgetFileDescendants,
	getEntry,
	hasDescendants,
	indexHasRows,
	ensureFolderListed,
	listingCompleted,
	markDirty,
	parentPath,
	refreshParentListing,
	scheduleParentRefresh,
	setUnixMode,
	startBackgroundIndex,
	storedUnixMode,
	upsertEntry,
)
from fastonedrive.log import logError
from fastonedrive.rclone import contentTokenFromStat, remoteStat


def _cacheIsPartial(itemPath: str) -> bool:
	from fastonedrive.chunks import isPartial

	return isPartial(itemPath)


def _cacheIsDirty(itemPath: str) -> bool:
	from fastonedrive.hydrate import isCacheDirty

	return isCacheDirty(itemPath)


def search(query: str, limit: int = config.SEARCH_DEFAULT_LIMIT) -> list[sqlite3.Row]:
	needle: str = query.strip()
	if needle == "":
		raise FastOneDriveError("busca vazia")
	from fastonedrive.searchindex import SearchQuery, runSearch

	if SearchQuery(needle).isEmpty():
		raise FastOneDriveError("busca vazia")
	connection: sqlite3.Connection = connectDb()
	try:
		return runSearch(connection, needle, FILE_SELECT, limit)
	finally:
		connection.close()


def relocateStoredFiles(oldPath: str, newPath: str) -> None:
	if oldPath == "" or oldPath == newPath:
		return
	if newPath.startswith(f"{oldPath}/"):
		raise FastOneDriveError("não é possível mover para dentro de si")
	from fastonedrive.disk import pathPresent, removePath

	rootFn: Callable[[str], Path]
	for rootFn in (pinnedLocalPath, cacheLocalPath):
		source: Path = rootFn(oldPath)
		dest: Path = rootFn(newPath)
		if not pathPresent(source):
			continue
		try:
			if source.resolve() == dest.resolve():
				continue
		except OSError:
			pass
		dest.parent.mkdir(parents=True, exist_ok=True)
		if pathPresent(dest):
			removePath(dest)
		source.rename(dest)


def pinnedLocalPath(itemPath: str) -> Path:
	return _underDataRoot(config.pinDir(), itemPath)


def cacheLocalPath(itemPath: str) -> Path:
	return _underDataRoot(config.cacheDir(), itemPath)


def localDataPath(itemPath: str) -> Path | None:
	pinned: Path = pinnedLocalPath(itemPath)
	if pinned.exists():
		return pinned
	cached: Path = cacheLocalPath(itemPath)
	if cached.exists():
		return cached
	return None


def chmodItem(itemPath: str, mode: int) -> None:
	setUnixMode(itemPath, mode)
	stored: int = int(mode) & 0o777
	local: Path
	for local in (pinnedLocalPath(itemPath), cacheLocalPath(itemPath)):
		if local.is_file() or local.is_dir():
			os.chmod(local, stored)


def applyStoredMode(itemPath: str, target: Path) -> None:
	mode: int = 0o755 if target.is_dir() else 0o644
	entry = getEntry(itemPath)
	if entry is not None:
		stored: int | None = storedUnixMode(entry)
		if stored is not None:
			mode = stored
	target.chmod(mode)


def listedAccess(isDir: bool, localPath: str, stored: int | None) -> int:
	if stored is not None:
		return stored
	if localPath != "":
		try:
			return os.stat(localPath).st_mode & 0o777
		except OSError:
			pass
	return 0o755 if isDir else 0o644


def _underDataRoot(rootDir: Path, itemPath: str) -> Path:
	root: Path = rootDir.resolve()
	target: Path = (root / itemPath).resolve()
	if not target.is_relative_to(root):
		return root / "__invalid__"
	return target


def _statusFields(row: sqlite3.Row) -> tuple[str, bool, str, str]:
	"""Devolve (localPath, cachedCopy, syncState, status) de uma linha do índice.

	status: conflict | error | syncing | pinned | cached | cloud. É o mesmo valor que o
	worker KIO mostra como emblema e que o FUSE expõe em ``user.fastonedrive.state``.
	"""
	isDir: bool = bool(row["isDir"])
	pinned: bool = bool(row["pinned"])
	itemPath: str = str(row["path"])
	localPath: str = ""
	cachedCopy: bool = False
	if not isDir:
		cached: Path = cacheLocalPath(itemPath)
		if cached.is_file() and not _cacheIsPartial(itemPath):
			cachedCopy = True
			if _cacheIsDirty(itemPath):
				localPath = str(cached)
	if localPath == "" and pinned and not isDir:
		pinnedFile: Path = pinnedLocalPath(itemPath)
		if pinnedFile.is_file():
			localPath = str(pinnedFile)
	syncState: str = ""
	try:
		syncState = str(row["syncState"] or "")
	except (IndexError, KeyError):
		syncState = ""
	status: str
	if syncState == "conflict":
		status = "conflict"
	elif syncState == "error":
		status = "error"
	elif syncState in ("dirty", "syncing"):
		status = "syncing"
	elif pinned and (isDir or localPath != ""):
		status = "pinned"
	elif localPath != "" or cachedCopy:
		status = "cached"
	else:
		status = "cloud"
	return localPath, cachedCopy, syncState, status


def itemStatus(itemPath: str) -> str:
	"""Estado de um item para emblemas fora do KIO (xattr do FUSE).

	Pasta: o pior estado de um descendente (conflito > erro > a sincronizar) ganha ao
	estado da própria pasta, como no Explorer do Windows.
	"""
	from fastonedrive.chunks import isPartial

	if itemPath == "":
		return _worstDescendantState("")
	row: sqlite3.Row | None = getEntry(itemPath)
	if row is None:
		local: Path = pinnedLocalPath(itemPath)
		if local.exists() or cacheLocalPath(itemPath).exists():
			return "syncing"
		return ""
	status: str = _statusFields(row)[3]
	if not bool(row["isDir"]):
		if status == "cloud" and isPartial(itemPath):
			return "partial"
		return status
	if status in ("conflict", "error", "syncing"):
		return status
	worst: str = _worstDescendantState(itemPath)
	return worst if worst != "" else status


def _worstDescendantState(itemPath: str) -> str:
	connection: sqlite3.Connection = connectDb()
	try:
		low: str = f"{itemPath}/" if itemPath != "" else ""
		high: str = f"{itemPath}0" if itemPath != "" else "\U0010ffff"
		found: set[str] = {
			str(row[0])
			for row in connection.execute(
				"SELECT DISTINCT syncState FROM files WHERE path >= ? AND path < ? AND syncState IN "
				"('conflict', 'error', 'dirty', 'syncing')",
				(low, high),
			)
		}
	finally:
		connection.close()
	if "conflict" in found:
		return "conflict"
	if "error" in found:
		return "error"
	if "dirty" in found or "syncing" in found:
		return "syncing"
	return ""


def rowToDict(row: sqlite3.Row) -> dict[str, Any]:
	isDir: bool = bool(row["isDir"])
	size: int = int(row["size"])
	pinned: bool = bool(row["pinned"])
	localPath: str
	syncState: str
	status: str
	localPath, _cached, syncState, status = _statusFields(row)
	storedMode: int | None = storedUnixMode(row)
	return {
		"path": str(row["path"]),
		"name": str(row["name"]),
		"isDir": isDir,
		"size": size,
		"mimeType": str(row["mimeType"] or ""),
		"modTime": str(row["modTime"] or ""),
		"pinned": pinned,
		"localPath": localPath,
		"status": status,
		"syncState": syncState,
		"tooLargeToOpen": (not isDir) and localPath == "" and size > config.openMaxBytes(),
		"access": listedAccess(isDir, localPath, storedMode),
		**_fuseFields(
			str(row["path"]),
			isDir,
			(not isDir) and localPath == "" and size > config.openMaxBytes(),
		),
	}


def _fuseFields(itemPath: str, isDir: bool, tooLarge: bool) -> dict[str, Any]:
	if isDir or not tooLarge:
		return {"fusePath": "", "fuseReady": False}
	from fastonedrive.mount import fuseMountReady

	return {
		"fusePath": str(config.mountPath() / itemPath),
		"fuseReady": fuseMountReady(),
	}


def _parentIsKnown(itemPath: str) -> bool:
	parent: str = parentPath(itemPath)
	if parent == "":
		return indexHasRows()
	if getEntry(parent) is not None:
		return True
	return hasDescendants(parent)


def adoptLocalOrphan(itemPath: str) -> sqlite3.Row | None:
	if itemPath == "" or isEphemeralPath(itemPath) or _cacheIsPartial(itemPath):
		return None
	local: Path | None = localDataPath(itemPath)
	if local is None or local.is_symlink():
		return None
	if not local.is_file() and not local.is_dir():
		return None
	isDir: bool = local.is_dir()
	info: os.stat_result = local.stat()
	name: str = itemPath.rsplit("/", 1)[-1]
	onPin: bool = False
	try:
		onPin = local.resolve() == pinnedLocalPath(itemPath).resolve()
	except OSError:
		onPin = False
	mimeType: str = "inode/directory" if isDir else ""
	if not isDir:
		guessed: str | None
		guessed, _encoding = mimetypes.guess_type(name)
		mimeType = guessed or ""
	stamp: datetime = datetime.fromtimestamp(info.st_mtime, tz=timezone.utc)
	upsertEntry(
		itemPath,
		name,
		isDir,
		0 if isDir else int(info.st_size),
		onPin,
		"" if isDir else stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
		mimeType,
	)
	markDirty(itemPath)
	from fastonedrive.notify import notifyDirChanged

	notifyDirChanged(itemPath)
	return getEntry(itemPath)


def _dictFromLocal(itemPath: str, local: Path) -> dict[str, Any]:
	isDir: bool = local.is_dir() and not local.is_symlink()
	info: os.stat_result = local.stat()
	size: int = 0 if isDir else int(info.st_size)
	name: str = itemPath.rsplit("/", 1)[-1] if itemPath != "" else itemPath
	onPin: bool = False
	try:
		onPin = local.resolve() == pinnedLocalPath(itemPath).resolve()
	except OSError:
		onPin = False
	localPath: str = "" if isDir else str(local)
	mimeType: str = "inode/directory" if isDir else ""
	if not isDir:
		guessed: str | None
		guessed, _encoding = mimetypes.guess_type(name)
		mimeType = guessed or ""
	stamp: datetime = datetime.fromtimestamp(info.st_mtime, tz=timezone.utc)
	return {
		"path": itemPath,
		"name": name,
		"isDir": isDir,
		"size": size,
		"mimeType": mimeType,
		"modTime": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
		"pinned": onPin,
		"localPath": localPath,
		"status": "pinned" if onPin else "cached",
		"syncState": "",
		"tooLargeToOpen": (not isDir) and localPath == "" and size > config.openMaxBytes(),
		"access": listedAccess(isDir, localPath, None),
		**_fuseFields(
			itemPath,
			isDir,
			(not isDir) and localPath == "" and size > config.openMaxBytes(),
		),
	}


def itemToDict(itemPath: str) -> dict[str, Any]:
	if isEphemeralPath(itemPath):
		raise FastOneDriveError(f"item não encontrado no índice: {itemPath}")
	row = getEntry(itemPath)
	if row is None:
		row = adoptLocalOrphan(itemPath)
	if row is None:
		local: Path | None = localDataPath(itemPath)
		if local is not None:
			return _dictFromLocal(itemPath, local)
		if hasDescendants(itemPath):
			return _syntheticDir(itemPath)
		if _parentIsKnown(itemPath):
			parent: str = parentPath(itemPath)
			if not listingCompleted(parent):
				ensureFolderListed(parent)
			row = getEntry(itemPath)
			if row is None and hasDescendants(itemPath):
				return _syntheticDir(itemPath)
			if row is None:
				raise FastOneDriveError(f"item não encontrado no índice: {itemPath}")
		else:
			row = _statRemoteIntoIndex(itemPath)
	if row is not None:
		data: dict[str, Any] = rowToDict(row)
		if not bool(data["isDir"]) and hasDescendants(itemPath):
			data["isDir"] = True
			data["mimeType"] = "inode/directory"
			data["tooLargeToOpen"] = False
			if storedUnixMode(row) is None:
				data["access"] = 0o755
		return data
	if hasDescendants(itemPath):
		return _syntheticDir(itemPath)
	raise FastOneDriveError(f"item não encontrado no índice: {itemPath}")


def _syntheticDir(itemPath: str) -> dict[str, Any]:
	name: str = itemPath.rsplit("/", 1)[-1] if itemPath != "" else itemPath
	return {
		"path": itemPath,
		"name": name,
		"isDir": True,
		"size": 0,
		"mimeType": "inode/directory",
		"modTime": "",
		"pinned": False,
		"localPath": "",
		"status": "cloud",
		"syncState": "",
		"tooLargeToOpen": False,
		"access": 0o755,
	}


def listChildren(parentPath: str) -> list[sqlite3.Row]:
	connection: sqlite3.Connection = connectDb()
	try:
		return list(
			connection.execute(
				"""
				SELECT """ + FILE_SELECT + """
				FROM files
				WHERE parent = ?
				ORDER BY isDir DESC, name COLLATE NOCASE
				""",
				(parentPath,),
			)
		)
	finally:
		connection.close()


def listEntries(parentPath: str) -> list[dict[str, Any]]:
	parent: str = parentPath.strip("/")
	if parent != "":
		entry = getEntry(parent)
		if entry is not None and not bool(entry["isDir"]):
			forgetFileDescendants(parent)
			if not hasDescendants(parent):
				return []
	rows: list[sqlite3.Row] = listChildren(parent)
	known: bool = parent == "" or getEntry(parent) is not None or len(rows) > 0
	if parent == "" and len(rows) == 0:
		known = indexHasRows()
	if known:
		if parent != "" and len(rows) == 0 and not listingCompleted(parent):
			try:
				ensureFolderListed(parent)
			except FastOneDriveError as error:
				logError(
					f"listagem remota falhou, a usar o índice: {error}",
					extra={"parent": parent},
				)
			return [rowToDict(row) for row in listChildren(parent)]
		scheduleParentRefresh(parent)
		return [rowToDict(row) for row in rows]
	startBackgroundIndex()
	try:
		refreshParentListing(parent)
	except FastOneDriveError as error:
		logError(
			f"listagem remota falhou, a usar o índice: {error}",
			extra={"parent": parent},
		)
	return [rowToDict(row) for row in listChildren(parent)]


def _statRemoteIntoIndex(itemPath: str) -> sqlite3.Row | None:
	if itemPath == "":
		return None
	stat: dict[str, Any] | None
	try:
		stat = remoteStat(config.remotePath(itemPath))
	except FastOneDriveError:
		return None
	if stat is None:
		return None
	name: str = str(stat.get("Name") or itemPath.rsplit("/", 1)[-1])
	if name == "":
		return None
	upsertEntry(
		itemPath,
		name,
		bool(stat.get("IsDir")),
		int(stat.get("Size") or 0),
		modTime=str(stat.get("ModTime") or ""),
		mimeType=str(stat.get("MimeType") or ""),
		etag=contentTokenFromStat(stat),
		itemId=str(stat.get("ID") or ""),
	)
	return getEntry(itemPath)


def _escapeLike(text: str) -> str:
	return (
		text.replace("\\", "\\\\")
		.replace("%", "\\%")
		.replace("_", "\\_")
	)
