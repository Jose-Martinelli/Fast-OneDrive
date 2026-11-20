import fcntl
import os
import re
import shlex
import shutil
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastonedrive import FastOneDriveError

REMOTE_NAME: str = "onedrive"
RCLONE_BIN: str = "/usr/bin/rclone"
MULTI_THREAD_STREAMS: int = 8
MULTI_THREAD_CUTOFF: str = "64M"
INDEX_BATCH_SIZE: int = 1000
SEARCH_DEFAULT_LIMIT: int = 50
SEARCH_KIO_LIMIT: int = 200
KIO_SCHEME: str = "fastonedrive"
DEFAULT_OPEN_MAX_BYTES: int = 50 * 1024 * 1024
KRUNNER_MIN_LETTERS: int = 3
KRUNNER_MATCH_LIMIT: int = 15
PIN_SKIP_SEGMENTS: tuple[str, ...] = ("roms", "backups")
MB_BYTES: int = 1024 * 1024
KRUNNER_BUS_NAME: str = "org.fastonedrive.krunner"
KRUNNER_OBJECT_PATH: str = "/runner"
FORBIDDEN_RC_ADDR: str = "127.0.0.1:5572"
DEFAULT_VFS_CACHE_MODE: str = "full"
DEFAULT_VFS_CACHE_MAX_SIZE: str = "150G"
DEFAULT_VFS_CACHE_MAX_AGE: str = "9999h"
DEFAULT_VFS_WRITE_BACK: str = "5s"
DEFAULT_DIR_CACHE_TIME: str = "9999h"
DEFAULT_POLL_INTERVAL: str = "15s"
DEFAULT_RCLONE_EXCLUDE: tuple[str, ...] = ("/Cofre Pessoal/**",)
VFS_CACHE_MODES: tuple[str, ...] = ("off", "minimal", "writes", "full")
_VALUE_FLAGS: frozenset[str] = frozenset(
	{
		"--vfs-cache-mode",
		"--vfs-cache-max-size",
		"--vfs-cache-max-age",
		"--vfs-write-back",
		"--dir-cache-time",
		"--poll-interval",
		"--config",
		"--rc-addr",
	}
)


_PROFILE_ID_RE: re.Pattern[str] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_activeProfileId: str = ""


class Profile:
	id: str
	name: str
	remoteName: str
	mountPath: str
	mountEnabled: bool
	legacy: bool

	def __init__(
		self,
		profileId: str,
		name: str,
		remoteNameValue: str,
		mountPathValue: str,
		mountEnabledValue: bool,
		legacyValue: bool,
	) -> None:
		self.id = profileId
		self.name = name
		self.remoteName = remoteNameValue
		self.mountPath = mountPathValue
		self.mountEnabled = mountEnabledValue
		self.legacy = legacyValue


def isProfileId(value: str) -> bool:
	return _PROFILE_ID_RE.fullmatch(value.strip()) is not None


def profilesKeyPresent() -> bool:
	return "profiles" in _userConfig()


def listProfiles() -> list[Profile]:
	raw: object = _userConfig().get("profiles")
	if not isinstance(raw, list):
		return []
	profiles: list[Profile] = []
	for item in raw:
		if not isinstance(item, dict):
			continue
		profileId: str = str(item.get("id") or "").strip()
		if not isProfileId(profileId):
			continue
		remote: str = str(item.get("remoteName") or profileId).strip().rstrip(":")
		if remote == "":
			remote = profileId
		mount: str = str(item.get("mountPath") or str(homeDir() / "OneDrive")).strip()
		if mount == "":
			mount = str(homeDir() / "OneDrive")
		profiles.append(
			Profile(
				profileId,
				str(item.get("name") or profileId),
				remote,
				mount,
				bool(item.get("mountEnabled", True)),
				bool(item.get("legacy", False)),
			)
		)
	return profiles


def profileById(profileId: str) -> Profile | None:
	wanted: str = profileId.strip()
	for profile in listProfiles():
		if profile.id == wanted:
			return profile
	return None


def legacyProfile() -> Profile | None:
	for profile in listProfiles():
		if profile.legacy:
			return profile
	return None


def activateProfile(profileId: str) -> Profile:
	global _activeProfileId
	found: Profile | None = profileById(profileId)
	if found is None:
		raise FastOneDriveError(f"perfil desconhecido: {profileId}")
	_activeProfileId = found.id
	return found


def clearActiveProfile() -> None:
	global _activeProfileId
	_activeProfileId = ""


def activeProfile() -> Profile | None:
	if _activeProfileId == "":
		return None
	return profileById(_activeProfileId)


def ensureActiveProfile() -> Profile:
	current: Profile | None = activeProfile()
	if current is not None:
		return current
	profiles: list[Profile] = listProfiles()
	if len(profiles) == 0:
		raise FastOneDriveError(
			"Ainda não há um perfil OneDrive. Abre o Fast-OneDrive e cria um."
		)
	chosen: Profile = profiles[0]
	for profile in profiles:
		if profile.legacy:
			chosen = profile
			break
	return activateProfile(chosen.id)


def storedRemoteName() -> str:
	raw: str = str(_userConfig().get("remoteName") or REMOTE_NAME).strip().rstrip(":")
	if raw == "":
		return REMOTE_NAME
	return raw


def remoteName() -> str:
	profile: Profile | None = activeProfile()
	if profile is not None and profile.remoteName != "":
		return profile.remoteName
	return storedRemoteName()


def remoteRoot() -> str:
	return f"{remoteName()}:"


def remotePath(itemPath: str) -> str:
	body: str = itemPath.strip().lstrip("/")
	if body == "":
		return remoteRoot()
	return f"{remoteName()}:{body}"


def systemRcloneConfigPath() -> Path:
	return homeDir() / ".config" / "rclone" / "rclone.conf"


def defaultRcloneConfigPath() -> Path:
	return configDir() / "rclone.conf"


def rcloneConfigPath() -> Path:
	raw: str = str(_userConfig().get("rcloneConfig") or str(defaultRcloneConfigPath()))
	return Path(raw).expanduser()


@contextmanager
def rcloneConfigLock() -> Iterator[None]:
	path: Path = configDir() / "rclone.conf.lock"
	path.parent.mkdir(parents=True, exist_ok=True)
	handle = path.open("a+", encoding="utf-8")
	try:
		fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
		try:
			yield
		finally:
			fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
	finally:
		handle.close()


def homeDir() -> Path:
	return Path.home()


def mountPath() -> Path:
	profile: Profile | None = activeProfile()
	if profile is None:
		return homeDir() / "OneDrive"
	return Path(profile.mountPath).expanduser()


def stateRoot() -> Path:
	return homeDir() / ".local" / "share" / "fast-onedrive"


def profileStateDir(profile: Profile) -> Path:
	return stateRoot() / "profiles" / profile.id


def dataDir() -> Path:
	profile: Profile | None = activeProfile()
	if profile is None:
		return stateRoot()
	return profileStateDir(profile)


def configDir() -> Path:
	return homeDir() / ".config" / "fast-onedrive"


def configPath() -> Path:
	return configDir() / "config.toml"


def dbPath() -> Path:
	return dataDir() / "index.db"


def pinDir() -> Path:
	return dataDir() / "pinned"


def cacheDir() -> Path:
	return dataDir() / "cache"


def logPath() -> Path:
	return stateRoot() / "fastonedrive.log"


def indexJobLogPath() -> Path:
	return dataDir() / "index-job.log"


def indexStatusPath() -> Path:
	return dataDir() / "index-status.json"


def activityPath() -> Path:
	return dataDir() / "activity.json"


READDIR_TTL_SEC: float = 15.0


def cliBin() -> str:
	local: Path = homeDir() / ".local" / "bin" / "fastonedrive"
	if local.is_file():
		return str(local)
	system: Path = Path("/usr/bin/fastonedrive")
	if system.is_file():
		return str(system)
	found: str | None = shutil.which("fastonedrive")
	if found is not None:
		return found
	raise FastOneDriveError("CLI fastonedrive não encontrado em ~/.local/bin nem em /usr/bin")


def defaultCopyDest() -> Path:
	return homeDir() / "Downloads"


def ensureDataDir() -> Path:
	path: Path = dataDir()
	path.mkdir(parents=True, exist_ok=True)
	return path


def ensurePinDir() -> Path:
	path: Path = pinDir()
	path.mkdir(parents=True, exist_ok=True)
	return path


def ensureCacheDir() -> Path:
	path: Path = cacheDir()
	path.mkdir(parents=True, exist_ok=True)
	return path


def ensureUserConfig() -> Path:
	configDir().mkdir(parents=True, exist_ok=True)
	path: Path = configPath()
	if not path.is_file():
		_writeUserConfig(
			path,
			"~/Downloads",
			DEFAULT_OPEN_MAX_BYTES,
			list(PIN_SKIP_SEGMENTS),
			"~/.config/fast-onedrive/rclone.conf",
			REMOTE_NAME,
			defaultRcloneFlagValues(),
			[],
		)
	ensureRcloneConfig()
	from fastonedrive.profiles import migrateLegacyIfNeeded

	migrateLegacyIfNeeded()
	return path


def ensureRcloneConfig() -> Path:
	configDir().mkdir(parents=True, exist_ok=True)
	dest: Path = rcloneConfigPath()
	dest.parent.mkdir(parents=True, exist_ok=True)
	if dest.is_file():
		return dest
	source: Path = systemRcloneConfigPath()
	if source.is_file() and source.resolve() != dest.resolve():
		shutil.copy2(source, dest)
		dest.chmod(0o600)
		return dest
	if not dest.is_file():
		dest.write_text("", encoding="utf-8")
		dest.chmod(0o600)
	return dest


def importSystemRcloneConfig() -> Path:
	source: Path = systemRcloneConfigPath()
	if not source.is_file():
		raise FastOneDriveError(f"rclone.conf do sistema não encontrado: {source}")
	dest: Path = rcloneConfigPath()
	dest.parent.mkdir(parents=True, exist_ok=True)
	if source.resolve() == dest.resolve():
		return dest
	from fastonedrive.profiles import _replaceConfigText

	from fastonedrive.secretconf import plainConfigForEdit

	incoming: str = source.read_text(encoding="utf-8")
	with plainConfigForEdit(dest), rcloneConfigLock():
		current: str = dest.read_text(encoding="utf-8") if dest.is_file() else ""
		_replaceConfigText(dest, _mergeMissingSections(current, incoming))
	return dest


def _mergeMissingSections(current: str, incoming: str) -> str:
	present: set[str] = set(_rcloneSections(current))
	added: list[str] = [
		block for header, block in _rcloneSections(incoming).items() if header not in present
	]
	if len(added) == 0:
		return current
	merged: str = current
	if merged != "" and not merged.endswith("\n"):
		merged += "\n"
	if merged != "" and not merged.endswith("\n\n"):
		merged += "\n"
	return merged + "\n".join(block.rstrip("\n") + "\n" for block in added)


def _rcloneSections(text: str) -> dict[str, str]:
	sections: dict[str, str] = {}
	header: str = ""
	lines: list[str] = []
	for line in text.splitlines(keepends=True):
		stripped: str = line.strip()
		if stripped.startswith("[") and stripped.endswith("]"):
			if header != "":
				sections[header] = "".join(lines)
			header = stripped[1:-1].strip()
			lines = [line]
			continue
		if header != "":
			lines.append(line)
	if header != "":
		sections[header] = "".join(lines)
	return sections


def saveUserConfig(
	copyDest: str,
	openMaxBytesValue: int,
	skipPin: list[str],
	rcloneConfig: str,
	remoteNameValue: str,
	rcloneFlags: dict[str, Any] | None = None,
	profiles: list[Profile] | None = None,
) -> Path:
	dest: str = copyDest.strip()
	if dest == "":
		dest = str(defaultCopyDest())
	limitOpen: int = int(openMaxBytesValue)
	if limitOpen <= 0:
		limitOpen = DEFAULT_OPEN_MAX_BYTES
	conf: str = rcloneConfig.strip()
	if conf == "":
		conf = str(defaultRcloneConfigPath())
	remote: str = remoteNameValue.strip().rstrip(":")
	if remote == "":
		remote = REMOTE_NAME
	path: Path = ensureUserConfig()
	flags: dict[str, Any] = rcloneFlagValues() if rcloneFlags is None else dict(rcloneFlags)
	storedProfiles: list[Profile] = listProfiles() if profiles is None else profiles
	_writeUserConfig(
		path,
		dest,
		limitOpen,
		skipPin,
		conf,
		remote,
		flags,
		storedProfiles,
	)
	ensureRcloneConfig()
	return path


def replaceProfiles(profiles: list[Profile]) -> Path:
	loaded: dict[str, Any] = _userConfig()
	flags: dict[str, Any] = rcloneFlagValues()
	skip: list[str] = list(skipPinSegments())
	path: Path = configPath()
	path.parent.mkdir(parents=True, exist_ok=True)
	_writeUserConfig(
		path,
		str(loaded.get("copyDest") or str(defaultCopyDest())),
		int(loaded.get("openMaxBytes") or DEFAULT_OPEN_MAX_BYTES),
		skip,
		str(loaded.get("rcloneConfig") or str(defaultRcloneConfigPath())),
		str(loaded.get("remoteName") or REMOTE_NAME),
		flags,
		profiles,
	)
	return path


def replaceProfile(profile: Profile) -> Path:
	profiles: list[Profile] = []
	replaced: bool = False
	current: Profile
	for current in listProfiles():
		if current.id == profile.id:
			profiles.append(profile)
			replaced = True
		else:
			profiles.append(current)
	if not replaced:
		profiles.append(profile)
	return replaceProfiles(profiles)


def copyDestPath() -> Path:
	raw: str = str(_userConfig().get("copyDest") or str(defaultCopyDest()))
	return Path(raw).expanduser()


def openMaxBytes() -> int:
	value: Any = _userConfig().get("openMaxBytes", DEFAULT_OPEN_MAX_BYTES)
	try:
		return int(value)
	except (TypeError, ValueError):
		return DEFAULT_OPEN_MAX_BYTES


def skipPinSegments() -> tuple[str, ...]:
	loaded: dict[str, Any] = _userConfig()
	if "skipPin" not in loaded:
		return PIN_SKIP_SEGMENTS
	value: object = loaded["skipPin"]
	raw: list[str]
	if isinstance(value, str):
		raw = [part.strip() for part in value.split(",")]
	elif isinstance(value, list):
		raw = [str(item) for item in value]
	else:
		return PIN_SKIP_SEGMENTS
	return tuple(_normalizeSkipPin(raw))


def pathDisplay(path: Path) -> str:
	expanded: Path = path.expanduser()
	home: Path = homeDir().resolve()
	try:
		relative: Path = expanded.resolve(strict=False).relative_to(home)
	except ValueError:
		return str(expanded)
	posix: str = relative.as_posix()
	if posix == ".":
		return "~"
	return f"~/{posix}"


def copyDestDisplay() -> str:
	return pathDisplay(copyDestPath())


def rcloneConfigDisplay() -> str:
	return pathDisplay(rcloneConfigPath())


def defaultRcloneFlagValues() -> dict[str, Any]:
	return {
		"vfsCacheMode": DEFAULT_VFS_CACHE_MODE,
		"vfsCacheMaxSize": DEFAULT_VFS_CACHE_MAX_SIZE,
		"vfsCacheMaxAge": DEFAULT_VFS_CACHE_MAX_AGE,
		"vfsWriteBack": DEFAULT_VFS_WRITE_BACK,
		"dirCacheTime": DEFAULT_DIR_CACHE_TIME,
		"pollInterval": DEFAULT_POLL_INTERVAL,
		"vfsFastFingerprint": True,
		"rcloneExclude": list(DEFAULT_RCLONE_EXCLUDE),
		"rcloneExtra": "",
		"mountEnabled": True,
	}


def rcloneFlagValues() -> dict[str, Any]:
	values: dict[str, Any] = defaultRcloneFlagValues()
	loaded: dict[str, Any] = _userConfig()
	mode: str = str(loaded.get("vfsCacheMode") or values["vfsCacheMode"]).strip()
	if mode in VFS_CACHE_MODES:
		values["vfsCacheMode"] = mode
	values["vfsCacheMaxSize"] = str(loaded.get("vfsCacheMaxSize") or values["vfsCacheMaxSize"]).strip()
	values["vfsCacheMaxAge"] = str(loaded.get("vfsCacheMaxAge") or values["vfsCacheMaxAge"]).strip()
	values["vfsWriteBack"] = str(loaded.get("vfsWriteBack") or values["vfsWriteBack"]).strip()
	values["dirCacheTime"] = str(loaded.get("dirCacheTime") or values["dirCacheTime"]).strip()
	values["pollInterval"] = str(loaded.get("pollInterval") or values["pollInterval"]).strip()
	if "vfsFastFingerprint" in loaded:
		values["vfsFastFingerprint"] = bool(loaded["vfsFastFingerprint"])
	if "rcloneExclude" in loaded:
		values["rcloneExclude"] = _normalizeExclude(loaded["rcloneExclude"])
	values["rcloneExtra"] = str(loaded.get("rcloneExtra") or "")
	if "mountEnabled" in loaded:
		values["mountEnabled"] = bool(loaded["mountEnabled"])
	return values


def rcloneExclude() -> list[str]:
	return list(rcloneFlagValues()["rcloneExclude"])


def pathIsExcluded(itemPath: str) -> bool:
	relative: str = itemPath.strip("/")
	for pattern in _listingExcludePatterns():
		if _excludeMatches(relative, pattern):
			return True
	return False


def _listingExcludePatterns() -> list[str]:
	patterns: list[str] = list(rcloneExclude())
	try:
		tokens: list[str] = _sanitizeUserFlags(_extraFlagTokens(), allowVfs=False)
	except FastOneDriveError:
		return patterns
	index: int = 0
	while index < len(tokens):
		token: str = tokens[index]
		if token == "--exclude" and index + 1 < len(tokens):
			patterns.append(tokens[index + 1])
			index += 2
			continue
		if token.startswith("--exclude="):
			patterns.append(token.split("=", 1)[1])
		index += 1
	return patterns


def _excludeMatches(path: str, pattern: str) -> bool:
	text: str = pattern.strip()
	if text == "":
		return False
	anchored: bool = text.startswith("/")
	body: str = text[1:] if anchored else text
	body = body.strip("/")
	if body == "":
		return True
	if anchored:
		return _globMatch(path, body)
	if _globMatch(path, body):
		return True
	parts: list[str] = path.split("/")
	index: int
	for index in range(len(parts)):
		suffix: str = "/".join(parts[index:])
		if _globMatch(suffix, body):
			return True
	return False


def _globMatch(path: str, pattern: str) -> bool:
	if pattern.endswith("/**") and "*" not in pattern[:-3] and "?" not in pattern[:-3]:
		prefix: str = pattern[:-3].strip("/")
		if prefix == "":
			return True
		return path == prefix or path.startswith(f"{prefix}/")
	if (
		pattern.endswith("/*")
		and "**" not in pattern
		and "*" not in pattern[:-2]
		and "?" not in pattern[:-2]
	):
		prefix = pattern[:-2].strip("/")
		if prefix == "":
			return "/" not in path
		rest: str = path[len(prefix) + 1 :] if path.startswith(f"{prefix}/") else ""
		return path.startswith(f"{prefix}/") and "/" not in rest
	if "*" not in pattern and "?" not in pattern:
		return path == pattern.strip("/")
	return re.fullmatch(_globToRegex(pattern), path) is not None


def _globToRegex(pattern: str) -> str:
	regex: list[str] = []
	index: int = 0
	while index < len(pattern):
		if pattern.startswith("**/", index):
			regex.append("(?:.*/)?")
			index += 3
			continue
		if pattern.startswith("**", index):
			regex.append(".*")
			index += 2
			continue
		char: str = pattern[index]
		if char == "*":
			regex.append("[^/]*")
		elif char == "?":
			regex.append("[^/]")
		else:
			regex.append(re.escape(char))
		index += 1
	return "".join(regex)


def mountEnabled() -> bool:
	return bool(rcloneFlagValues()["mountEnabled"])


def jobFilterFlags() -> list[str]:
	flags: list[str] = []
	for pattern in rcloneExclude():
		flags.extend(["--exclude", pattern])
	flags.extend(_sanitizeUserFlags(_extraFlagTokens(), allowVfs=False))
	_assertSafeFlags(flags)
	return flags


def bytesToMb(value: int) -> int:
	return max(0, int(value) // MB_BYTES)


def mbToBytes(value: int) -> int:
	return max(0, int(value) * MB_BYTES)


def _userConfig() -> dict[str, Any]:
	path: Path = configPath()
	if not path.is_file():
		return {}
	with path.open("rb") as handle:
		loaded: dict[str, Any] = tomllib.load(handle)
	return loaded


def _normalizeSkipPin(values: list[str]) -> list[str]:
	seen: set[str] = set()
	result: list[str] = []
	for item in values:
		text: str = item.strip()
		lower: str = text.lower()
		if lower.startswith("onedrive:"):
			text = text[9:].lstrip()
		text = text.strip("/")
		if text == "":
			continue
		key: str = text.lower()
		if key in seen:
			continue
		seen.add(key)
		result.append(text)
	return result


def _escapeTomlString(value: str) -> str:
	escaped: str = (
		value.replace("\\", "\\\\")
		.replace('"', '\\"')
		.replace("\n", "\\n")
		.replace("\t", "\\t")
	)
	return f'"{escaped}"'


def _writeUserConfig(
	path: Path,
	copyDest: str,
	openMaxBytesValue: int,
	skipPin: list[str],
	rcloneConfig: str,
	remoteNameValue: str,
	rcloneFlags: dict[str, Any],
	profiles: list[Profile] | None = None,
) -> None:
	segments: list[str] = _normalizeSkipPin(skipPin)
	items: str = ", ".join(_escapeTomlString(item) for item in segments)
	values: dict[str, Any] = {**defaultRcloneFlagValues(), **rcloneFlags}
	excludeItems: str = ", ".join(
		_escapeTomlString(item) for item in _normalizeExclude(values.get("rcloneExclude"))
	)
	mode: str = str(values["vfsCacheMode"]).strip()
	if mode not in VFS_CACHE_MODES:
		mode = DEFAULT_VFS_CACHE_MODE
	text: str = (
		f"copyDest = {_escapeTomlString(copyDest)}\n"
		f"openMaxBytes = {int(openMaxBytesValue)}\n"
		f"skipPin = [{items}]\n"
		f"rcloneConfig = {_escapeTomlString(rcloneConfig)}\n"
		f"remoteName = {_escapeTomlString(remoteNameValue)}\n"
		f"vfsCacheMode = {_escapeTomlString(mode)}\n"
		f"vfsCacheMaxSize = {_escapeTomlString(str(values['vfsCacheMaxSize']).strip())}\n"
		f"vfsCacheMaxAge = {_escapeTomlString(str(values['vfsCacheMaxAge']).strip())}\n"
		f"vfsWriteBack = {_escapeTomlString(str(values['vfsWriteBack']).strip())}\n"
		f"dirCacheTime = {_escapeTomlString(str(values['dirCacheTime']).strip())}\n"
		f"pollInterval = {_escapeTomlString(str(values['pollInterval']).strip())}\n"
		f"vfsFastFingerprint = {_tomlBool(bool(values['vfsFastFingerprint']))}\n"
		f"rcloneExclude = [{excludeItems}]\n"
		f"rcloneExtra = {_escapeTomlString(str(values['rcloneExtra']))}\n"
		f"mountEnabled = {_tomlBool(bool(values['mountEnabled']))}\n"
		f"\n{_profilesBlock(listProfiles() if profiles is None else profiles)}"
	)
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp: Path = path.with_name(f".{path.name}.tmp")
	try:
		tmp.write_text(text, encoding="utf-8")
		os.replace(tmp, path)
	except Exception:
		tmp.unlink(missing_ok=True)
		raise


def _profilesBlock(profiles: list[Profile]) -> str:
	if len(profiles) == 0:
		return "profiles = []\n"
	blocks: list[str] = []
	for profile in profiles:
		blocks.append(
			"[[profiles]]\n"
			f"id = {_escapeTomlString(profile.id)}\n"
			f"name = {_escapeTomlString(profile.name)}\n"
			f"remoteName = {_escapeTomlString(profile.remoteName)}\n"
			f"mountPath = {_escapeTomlString(profile.mountPath)}\n"
			f"mountEnabled = {_tomlBool(profile.mountEnabled)}\n"
			f"legacy = {_tomlBool(profile.legacy)}\n"
		)
	return "\n".join(blocks)


def _tomlBool(value: bool) -> str:
	if value:
		return "true"
	return "false"


def _normalizeExclude(value: object) -> list[str]:
	raw: list[str]
	if isinstance(value, str):
		raw = [part.strip() for part in value.split(",")]
	elif isinstance(value, list):
		raw = [str(item).strip() for item in value]
	else:
		raw = list(DEFAULT_RCLONE_EXCLUDE)
	result: list[str] = []
	seen: set[str] = set()
	for item in raw:
		if item == "" or item in seen:
			continue
		seen.add(item)
		result.append(item)
	return result


def _extraFlagTokens() -> list[str]:
	raw: str = str(rcloneFlagValues()["rcloneExtra"] or "").strip()
	if raw == "":
		return []
	try:
		return shlex.split(raw)
	except ValueError as error:
		raise FastOneDriveError("flags extras do rclone inválidas") from error


def _sanitizeUserFlags(parts: list[str], allowVfs: bool) -> list[str]:
	result: list[str] = []
	skipValue: bool = False
	for part in parts:
		if skipValue:
			skipValue = False
			if not part.startswith("-"):
				continue
		if FORBIDDEN_RC_ADDR in part or part.endswith(":5572"):
			raise FastOneDriveError("a porta 5572 é o mount antigo; não usar")
		if part.startswith("--rc") or part == "--config" or part.startswith("--config="):
			skipValue = "=" not in part
			continue
		if (not allowVfs) and (
			part.startswith("--vfs-")
			or part in ("--dir-cache-time", "--poll-interval")
		):
			skipValue = "=" not in part
			continue
		result.append(part)
	return result


def _assertSafeFlags(flags: list[str], allowRc: bool = False) -> None:
	for flag in flags:
		if FORBIDDEN_RC_ADDR in flag or flag.endswith(":5572"):
			raise FastOneDriveError("a porta 5572 é o mount antigo; não usar")
		if (not allowRc) and flag.startswith("--rc"):
			raise FastOneDriveError("flags RC não são permitidas neste processo")
