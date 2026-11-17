import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.log import logError

_MIGRATING: bool = False
_LEGACY_NAMES: tuple[str, ...] = (
	"index.db",
	"index.db-wal",
	"index.db-shm",
	"pinned",
	"cache",
	"index-status.json",
	"activity.json",
	"listing-ttl.json",
	"index-job.log",
	"listing-ttl.lock",
	"index.lock",
	"disk.lock",
	"cache.lock",
	"pins.lock",
)
_SINGLETON_MOUNT_UNIT: str = "fast-onedrive-mount.service"


def migrateLegacyIfNeeded() -> None:
	global _MIGRATING
	if _MIGRATING or config.profilesKeyPresent():
		return
	if not _hasLegacyInstall():
		return
	_MIGRATING = True
	try:
		with config.rcloneConfigLock():
			if config.profilesKeyPresent():
				return
			remote: str = _legacyRemoteName()
			profileId: str = remote if config.isProfileId(remote) else allocateProfileId(remote)
			display: str = "OneDrive" if remote == "onedrive" else remote
			mount: Path = config.homeDir() / "OneDrive"
			enabled: bool = bool(config.rcloneFlagValues().get("mountEnabled", True))
			profile: config.Profile = config.Profile(
				profileId,
				display,
				remote,
				config.pathDisplay(mount),
				enabled,
				True,
			)
			_stopSingletonMount(mount)
			_moveLegacyFiles(profile)
			config.replaceProfiles([profile])
			try:
				_rewriteLegacyPlace(profile)
			except FastOneDriveError as error:
				logError(str(error), extra={"profile": profile.id, "step": "place"})
			if enabled:
				_enableMigratedMount(profile)
	finally:
		_MIGRATING = False


def allocateProfileId(displayName: str) -> str:
	base: str = slugify(displayName)
	if base == "":
		base = "onedrive"
	taken: set[str] = {profile.id for profile in config.listProfiles()}
	if base not in taken:
		return base
	number: int = 2
	while f"{base}-{number}" in taken:
		number += 1
	return f"{base}-{number}"


def slugify(displayName: str) -> str:
	text: str = displayName.strip().lower()
	chars: list[str] = []
	pendingDash: bool = False
	for char in text:
		if char.isascii() and char.isalnum():
			if pendingDash and len(chars) > 0:
				chars.append("-")
			pendingDash = False
			chars.append(char)
		else:
			pendingDash = True
	return "".join(chars).strip("-")


def defaultMountPath(displayName: str) -> Path:
	home: Path = config.homeDir()
	primary: Path = home / "OneDrive"
	if not mountPathTaken(primary):
		return primary
	label: str = displayName.strip() or "OneDrive"
	candidate: Path = home / f"OneDrive {label}"
	if not mountPathTaken(candidate):
		return candidate
	suffix: int = 2
	while mountPathTaken(home / f"OneDrive {label} {suffix}"):
		suffix += 1
	return home / f"OneDrive {label} {suffix}"


def mountPathTaken(path: Path) -> bool:
	resolved: Path = path.expanduser().resolve(strict=False)
	for profile in config.listProfiles():
		other: Path = Path(profile.mountPath).expanduser().resolve(strict=False)
		if other == resolved:
			return True
	return False


def profileIdFromText(text: str) -> str | None:
	raw: str = text.strip()
	if raw == "":
		return None
	if raw.startswith("fastonedrive:") or raw.startswith("file:"):
		return _profileFromUrl(raw)
	remoteId: str | None = _profileFromRemote(raw)
	if remoteId is not None:
		return remoteId
	return _profileFromLocal(Path(raw))


def removeProfile(profileId: str, discardLocal: bool = False) -> None:
	profile: config.Profile | None = config.profileById(profileId)
	if profile is None:
		raise FastOneDriveError(f"perfil desconhecido: {profileId}")
	if not discardLocal and _profileHasPending(profile):
		raise FastOneDriveError(
			"há alterações locais por enviar; a remoção do perfil foi recusada"
		)
	from fastonedrive.mount import disableProfileMount

	disableProfileMount(profile)
	from fastonedrive.kioinstall import removeProfilePlace

	removeProfilePlace(profile.id)
	state: Path = config.profileStateDir(profile)
	if state.is_dir():
		shutil.rmtree(state)
	mount: Path = Path(profile.mountPath).expanduser()
	try:
		mount.rmdir()
	except OSError:
		pass
	removeRemoteSection(profile.remoteName)
	remaining: list[config.Profile] = [
		item for item in config.listProfiles() if item.id != profile.id
	]
	config.replaceProfiles(remaining)
	active: config.Profile | None = config.activeProfile()
	if active is not None and active.id == profile.id:
		config.clearActiveProfile()


def writeRemoteToken(remoteName: str, token: str) -> None:
	name: str = remoteName.strip()
	if name == "" or "\n" in token or "\r" in token:
		raise FastOneDriveError("token inválido")
	path: Path = config.rcloneConfigPath()
	path.parent.mkdir(parents=True, exist_ok=True)
	header: str = f"[{name}]"
	with config.rcloneConfigLock():
		lines: list[str] = []
		if path.is_file():
			lines = path.read_text(encoding="utf-8").splitlines()
		updated: list[str] = _upsertTokenSection(lines, header, token)
		body: str = "\n".join(updated)
		if body != "" and not body.endswith("\n"):
			body += "\n"
		_replaceConfigText(path, body)


def _upsertTokenSection(lines: list[str], header: str, token: str) -> list[str]:
	start: int | None = None
	end: int = len(lines)
	index: int
	for index, line in enumerate(lines):
		stripped: str = line.strip()
		if not (stripped.startswith("[") and stripped.endswith("]")):
			continue
		if start is None and stripped == header:
			start = index
			continue
		if start is not None:
			end = index
			break
	tokenLine: str = f"token = {token}"
	typeLine: str = "type = onedrive"
	if start is None:
		block: list[str] = [header, typeLine, tokenLine]
		if len(lines) > 0 and lines[-1].strip() != "":
			return [*lines, "", *block]
		return [*lines, *block]
	body: list[str] = lines[start + 1 : end]
	foundType: bool = False
	foundToken: bool = False
	rebuilt: list[str] = []
	item: str
	for item in body:
		key: str = ""
		stripped = item.strip()
		if "=" in stripped:
			key = stripped.split("=", 1)[0].strip().lower()
		if key == "type":
			rebuilt.append(typeLine)
			foundType = True
			continue
		if key == "token":
			rebuilt.append(tokenLine)
			foundToken = True
			continue
		rebuilt.append(item)
	if not foundType:
		rebuilt.insert(0, typeLine)
	if not foundToken:
		rebuilt.append(tokenLine)
	return [*lines[: start + 1], *rebuilt, *lines[end:]]


def removeRemoteSection(remoteName: str) -> None:
	path: Path = config.rcloneConfigPath()
	if not path.is_file():
		return
	name: str = remoteName.strip()
	if name == "":
		return
	from fastonedrive.rclone import rcloneBin

	with config.rcloneConfigLock():
		result: subprocess.CompletedProcess[str] = subprocess.run(
			[rcloneBin(), "--config", str(path), "config", "delete", name],
			stdin=subprocess.DEVNULL,
			capture_output=True,
			text=True,
			encoding="utf-8",
		)
		if result.returncode == 0:
			if path.is_file():
				path.chmod(0o600)
			return
		detail: str = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
		if "not found" in detail or "couldn't find" in detail or "doesn't exist" in detail:
			return
		_stripRemoteSection(path, name)


def _stripRemoteSection(path: Path, remoteName: str) -> None:
	header: str = f"[{remoteName.strip()}]"
	lines: list[str] = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.is_file() else []
	kept: list[str] = []
	skipping: bool = False
	for line in lines:
		stripped: str = line.strip()
		if stripped.startswith("[") and stripped.endswith("]"):
			skipping = stripped == header
		if skipping:
			continue
		kept.append(line)
	_replaceConfigText(path, "".join(kept))


def _replaceConfigText(path: Path, body: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	fd: int
	tmpName: str
	fd, tmpName = tempfile.mkstemp(dir=str(path.parent), prefix=".rclone.conf.")
	tmpPath: Path = Path(tmpName)
	try:
		with os.fdopen(fd, "w", encoding="utf-8") as handle:
			handle.write(body)
			handle.flush()
			os.fsync(handle.fileno())
		os.chmod(tmpPath, 0o600)
		os.replace(tmpPath, path)
	except Exception:
		tmpPath.unlink(missing_ok=True)
		raise
	path.chmod(0o600)


def _profileHasPending(profile: config.Profile) -> bool:
	db: Path = config.profileStateDir(profile) / "index.db"
	if not db.is_file():
		return False
	connection: sqlite3.Connection = sqlite3.connect(db)
	connection.row_factory = sqlite3.Row
	try:
		rows: list[sqlite3.Row]
		try:
			rows = list(
				connection.execute(
					"SELECT path, isDir, syncState, baseModTime FROM files"
				)
			)
		except sqlite3.OperationalError:
			return False
		root: Path = config.profileStateDir(profile)
		return any(_rowHasPending(row, root) for row in rows)
	finally:
		connection.close()


def _rowHasPending(row: sqlite3.Row, root: Path) -> bool:
	from fastonedrive.rclone import parseRemoteModTime

	state: str = str(row["syncState"] or "")
	if state in ("dirty", "syncing", "conflict", "error"):
		return True
	if int(row["isDir"] or 0) == 1:
		return False
	itemPath: str = str(row["path"] or "")
	if itemPath == "" or itemPath.startswith("/") or ".." in itemPath.split("/"):
		return False
	base: datetime | None = parseRemoteModTime(str(row["baseModTime"] or ""))
	local: Path
	for local in (root / "pinned" / itemPath, root / "cache" / itemPath):
		if not local.is_file():
			continue
		if base is None:
			return True
		localTime: datetime = datetime.fromtimestamp(local.stat().st_mtime, tz=timezone.utc)
		if localTime > base + timedelta(seconds=2):
			return True
	return False


def remoteSectionStatus(remoteName: str) -> dict[str, str] | None:
	path: Path = config.rcloneConfigPath()
	if not path.is_file():
		return None
	header: str = f"[{remoteName.strip()}]"
	inside: bool = False
	found: bool = False
	status: dict[str, str] = {}
	for line in path.read_text(encoding="utf-8").splitlines():
		stripped: str = line.strip()
		if stripped.startswith("[") and stripped.endswith("]"):
			if inside:
				break
			inside = stripped == header
			found = found or inside
			continue
		if not inside or stripped == "" or stripped.startswith("#") or "=" not in stripped:
			continue
		key: str
		value: str
		key, value = stripped.split("=", 1)
		name: str = key.strip()
		if name == "token":
			status["hasToken"] = "1" if value.strip() != "" else ""
		elif name in ("drive_id", "drive_type"):
			status[name] = value.strip()
	if not found:
		return None
	return status


def _hasLegacyInstall() -> bool:
	root: Path = config.stateRoot()
	if (root / "index.db").exists() or (root / "pinned").exists() or (root / "cache").exists():
		return True
	path: Path = config.configPath()
	if not path.is_file():
		return False
	text: str = path.read_text(encoding="utf-8")
	return "remoteName" in text and "[[profiles]]" not in text and "profiles =" not in text


def _legacyRemoteName() -> str:
	remote: str = config.remoteName().strip().rstrip(":")
	if remote == "":
		return config.REMOTE_NAME
	return remote


def _stopSingletonMount(mount: Path) -> None:
	subprocess.run(
		["systemctl", "--user", "disable", "--now", _SINGLETON_MOUNT_UNIT],
		check=False,
		capture_output=True,
		text=True,
		encoding="utf-8",
	)
	subprocess.run(
		["fusermount3", "-uz", str(mount)],
		check=False,
		capture_output=True,
		text=True,
		encoding="utf-8",
	)


def _moveLegacyFiles(profile: config.Profile) -> None:
	dest: Path = config.profileStateDir(profile)
	dest.mkdir(parents=True, exist_ok=True)
	root: Path = config.stateRoot()
	for name in _LEGACY_NAMES:
		source: Path = root / name
		if not source.exists() and not source.is_symlink():
			continue
		target: Path = dest / name
		if target.exists():
			continue
		shutil.move(str(source), str(target))


def _rewriteLegacyPlace(profile: config.Profile) -> None:
	from fastonedrive.kioinstall import upsertProfilePlace

	upsertProfilePlace(profile)


def _enableMigratedMount(profile: config.Profile) -> None:
	from fastonedrive.mount import enableProfileMount

	try:
		enableProfileMount(profile)
	except FastOneDriveError as error:
		logError(str(error), extra={"profile": profile.id, "step": "mount"})


def _profileFromUrl(text: str) -> str | None:
	parsed = urlparse(text)
	if parsed.scheme == "file":
		return _profileFromLocal(Path(unquote(parsed.path)))
	if parsed.scheme != config.KIO_SCHEME:
		return None
	profileId: str
	profileId, _rest = splitProfilePath(unquote(parsed.path))
	if profileId != "":
		return profileId
	legacy: config.Profile | None = config.legacyProfile()
	if legacy is not None:
		return legacy.id
	profiles: list[config.Profile] = config.listProfiles()
	if len(profiles) == 1:
		return profiles[0].id
	return None


def splitProfilePath(path: str) -> tuple[str, str]:
	item: str = path if path.startswith("/") else f"/{path}"
	parts: list[str] = item.split("/")
	if len(parts) >= 3 and parts[1] == "p" and config.isProfileId(parts[2]):
		rest: str = "/" + "/".join(parts[3:])
		if rest == "/":
			return parts[2], "/"
		return parts[2], rest
	return "", item


def _profileFromRemote(text: str) -> str | None:
	if "://" in text or "/" in text.split(":", 1)[0]:
		return None
	if ":" not in text:
		return None
	name: str = text.split(":", 1)[0]
	if name in (config.KIO_SCHEME, "file", "http", "https"):
		return None
	if not re.fullmatch(r"[A-Za-z0-9_.+-]+", name):
		return None
	for profile in config.listProfiles():
		if profile.remoteName == name:
			return profile.id
	return None


def _profileFromLocal(path: Path) -> str | None:
	resolved: Path = path.expanduser().resolve(strict=False)
	bestId: str = ""
	bestLen: int = -1
	for profile in config.listProfiles():
		roots: tuple[Path, ...] = (
			Path(profile.mountPath).expanduser(),
			config.profileStateDir(profile) / "pinned",
			config.profileStateDir(profile) / "cache",
		)
		for root in roots:
			rootResolved: Path = root.expanduser().resolve(strict=False)
			try:
				resolved.relative_to(rootResolved)
			except ValueError:
				continue
			length: int = len(rootResolved.parts)
			if length > bestLen:
				bestLen = length
				bestId = profile.id
	if bestId == "":
		return None
	return bestId
