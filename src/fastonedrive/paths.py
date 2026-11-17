from pathlib import Path
from urllib.parse import unquote, urlparse

from fastonedrive import FastOneDriveError
from fastonedrive import config


def toRemotePath(raw: str) -> str:
	text: str = raw.strip()
	if text == "":
		raise FastOneDriveError("caminho vazio")
	if isRemotePath(text):
		return _normalizeRemotePath(text)
	localPath: Path = _toLocalPath(text)
	return _localToRemote(localPath)


def remoteItemPath(remotePath: str) -> str:
	normalized: str = _normalizeRemotePath(remotePath)
	prefix: str = config.remoteRoot()
	if not normalized.startswith(prefix):
		raise FastOneDriveError(f"remote não suportado: {remotePath}")
	return normalized[len(prefix):].lstrip("/")


def remoteBasename(remotePath: str) -> str:
	itemPath: str = remoteItemPath(remotePath)
	if itemPath == "":
		return config.remoteName()
	return itemPath.rsplit("/", 1)[-1]


def isUnderMount(localPath: Path) -> bool:
	roots: list[Path] = [config.mountPath()]
	profile: config.Profile
	for profile in config.listProfiles():
		roots.append(Path(profile.mountPath).expanduser())
	seen: set[str] = set()
	root: Path
	for root in roots:
		key: str = str(root)
		if key in seen:
			continue
		seen.add(key)
		if _relativeToRoot(localPath, root) is not None:
			return True
	return False


def overlayItemPath(localPath: Path) -> str | None:
	for root in (config.pinDir(), config.cacheDir()):
		relative: str | None = _relativeToRoot(localPath, root)
		if relative is not None:
			return relative
	return None


def isDriveSource(raw: str) -> bool:
	text: str = raw.strip()
	if text == "":
		return False
	try:
		itemPath: str = remoteItemPath(toRemotePath(text))
	except FastOneDriveError:
		return False
	return itemPath != ""


def isRemotePath(text: str) -> bool:
	if _profileForRemoteText(text) is not None:
		return True
	if text.startswith(config.remoteRoot()):
		return True
	parsed = urlparse(text)
	return parsed.scheme in (config.remoteName(), config.KIO_SCHEME)


def _profileForRemoteText(text: str) -> config.Profile | None:
	body: str = text.strip()
	if body == "":
		return None
	chosen: config.Profile | None = None
	chosenLen: int = -1
	profile: config.Profile
	for profile in config.listProfiles():
		remote: str = profile.remoteName.strip().rstrip(":")
		if remote == "":
			continue
		if not body.startswith(f"{remote}:"):
			continue
		if len(remote) <= chosenLen:
			continue
		chosen = profile
		chosenLen = len(remote)
	return chosen


def _normalizeRemotePath(text: str) -> str:
	parsed = urlparse(text)
	if parsed.scheme == config.KIO_SCHEME:
		return _normalizeKioPath(_kioRawPath(text))
	matched: config.Profile | None = _profileForRemoteText(text)
	if matched is not None:
		config.activateProfile(matched.id)
	name: str = config.remoteName()
	body: str
	if text.startswith(f"{name}://"):
		body = text[len(f"{name}://"):]
	elif text.startswith(f"{name}:"):
		body = text[len(f"{name}:"):]
	else:
		body = unquote(parsed.path)
	body = body.lstrip("/")
	if body == "":
		return config.remoteRoot()
	return config.remotePath(body)


def _kioRawPath(text: str) -> str:
	prefix: str = f"{config.KIO_SCHEME}:"
	body: str = text
	if text.startswith(f"{prefix}//"):
		body = text[len(prefix) + 2 :]
	elif text.startswith(prefix):
		body = text[len(prefix) :]
	return unquote(body)


def _normalizeKioPath(path: str) -> str:
	from fastonedrive.profiles import splitProfilePath

	profileId: str
	itemPath: str
	profileId, itemPath = splitProfilePath(path)
	if profileId != "":
		config.activateProfile(profileId)
	else:
		legacy: config.Profile | None = config.legacyProfile()
		if legacy is not None:
			config.activateProfile(legacy.id)
		elif config.activeProfile() is None:
			profiles: list[config.Profile] = config.listProfiles()
			if len(profiles) == 1:
				config.activateProfile(profiles[0].id)
	item: str = itemPath
	if item.startswith("/item/"):
		item = item[len("/item/"):]
	elif item == "/item" or item == "/":
		raise FastOneDriveError(f"URL KIO não aponta para um item: {path}")
	else:
		item = item.lstrip("/")
	item = item.lstrip("/")
	if item == "" or item == "search" or item.startswith("search/") or item == "LEIA-ME.txt":
		raise FastOneDriveError(f"URL KIO não aponta para um item: {path}")
	return config.remotePath(item)


def _toLocalPath(text: str) -> Path:
	if text.startswith("file:"):
		parsed = urlparse(text)
		if parsed.path == "":
			raise FastOneDriveError(f"URI inválida: {text}")
		return Path(unquote(parsed.path))
	return Path(text).expanduser()


def _localToRemote(localPath: Path) -> str:
	relative: str | None = _relativeToRoot(localPath, config.mountPath())
	if relative is None:
		relative = overlayItemPath(localPath)
	if relative is None:
		from fastonedrive.profiles import profileIdFromText

		found: str | None = profileIdFromText(str(localPath))
		if found is not None:
			config.activateProfile(found)
			relative = _relativeToRoot(localPath, config.mountPath())
			if relative is None:
				relative = overlayItemPath(localPath)
	if relative is None:
		resolved: Path = localPath.expanduser().resolve(strict=False)
		if _isKioTemp(resolved):
			raise FastOneDriveError(
				f"o Dolphin passou um ficheiro temporário em vez da URL Fast-OneDrive: {localPath}"
			)
		raise FastOneDriveError(
			f"caminho não está no mount {config.mountPath()} nem em pinned/cache: {localPath}"
		)
	if relative == "":
		return config.remoteRoot()
	return config.remotePath(relative)


def _relativeToRoot(localPath: Path, rootDir: Path) -> str | None:
	try:
		relative: Path = localPath.expanduser().resolve(strict=False).relative_to(
			rootDir.resolve()
		)
	except ValueError:
		return None
	relativePosix: str = relative.as_posix()
	if relativePosix == ".":
		return ""
	return relativePosix


def _isKioTemp(path: Path) -> bool:
	parts: tuple[str, ...] = path.parts
	return "kioexec" in parts or any(part.startswith(".kio-") or part.startswith("kio") for part in parts)
