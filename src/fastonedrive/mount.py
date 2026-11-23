import os
import subprocess
from pathlib import Path

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.menus import installedCliPath, pythonBin, repoBinPath

LEGACY_MOUNT_UNIT: str = "rclone-onedrive.service"
FAST_MOUNT_UNIT: str = "fast-onedrive-mount.service"


def legacyMountActive() -> bool:
	return _systemctlActive(LEGACY_MOUNT_UNIT)


def profileUnitName(profileId: str) -> str:
	return f"fast-onedrive-mount@{profileId}.service"


def fastMountActive() -> bool:
	profile: config.Profile | None = config.activeProfile()
	if profile is not None:
		return _systemctlActive(profileUnitName(profile.id))
	return _systemctlActive(FAST_MOUNT_UNIT)


def fuseMountReady() -> bool:
	target: str = str(config.mountPath())
	try:
		text: str = Path("/proc/mounts").read_text(encoding="utf-8")
	except OSError:
		return False
	line: str
	for line in text.splitlines():
		parts: list[str] = line.split()
		if len(parts) < 3:
			continue
		mounted: str = _unescapeMountField(parts[1])
		if mounted == target and parts[2].startswith("fuse"):
			return True
	return False


def fuseItemPath(itemPath: str) -> Path | None:
	"""Caminho do item na pasta FUSE (~/OneDrive) do perfil activo, se o mount estiver pronto.

	É o local "oficial" para o utilizador: abrir, localizar ou copiar por aqui mantém
	as edições no OneDrive. fastonedrive:/… e pinned/cache ficam só como recurso.
	"""
	if not fuseMountReady():
		return None
	mount: Path = config.mountPath()
	return mount / itemPath if itemPath != "" else mount


def applyMount() -> None:
	config.ensureUserConfig()
	config.ensureRcloneConfig()
	stopLegacyMount()
	profile: config.Profile | None = config.activeProfile()
	if profile is not None:
		applyProfileMount(profile)
		return
	for item in config.listProfiles():
		applyProfileMount(item)


def stopLegacyMount() -> None:
	_systemctl(["stop", LEGACY_MOUNT_UNIT], check=False)
	_systemctl(["disable", LEGACY_MOUNT_UNIT], check=False)


def setMountEnabled(enabled: bool) -> Path:
	profiles: list[config.Profile] = config.listProfiles()
	if len(profiles) == 0:
		raise FastOneDriveError("Ainda não há um perfil OneDrive.")
	active: config.Profile | None = config.activeProfile()
	chosen: list[config.Profile] = profiles if active is None else [active]
	for profile in chosen:
		previous: bool = profile.mountEnabled
		profile.mountEnabled = enabled
		config.replaceProfile(profile)
		try:
			applyProfileMount(profile)
		except Exception:
			if enabled and not _systemctlActive(profileUnitName(profile.id)):
				profile.mountEnabled = previous
				config.replaceProfile(profile)
			raise
	return config.configPath()


def applyProfileMount(profile: config.Profile) -> None:
	from fastonedrive.kioinstall import showFuseInDolphin

	stopLegacyMount()
	if not profile.mountEnabled:
		disableProfileMount(profile)
		showFuseInDolphin()
		return
	enableProfileMount(profile)


def enableProfileMount(profile: config.Profile) -> None:
	from fastonedrive.kioinstall import _excludeFromBaloo, showFuseInDolphin

	_writeProfileUnit(profile)
	_systemctl(["daemon-reload"])
	_systemctl(["enable", "--now", profileUnitName(profile.id)])
	showFuseInDolphin()
	# Instalado pelo pacote não há install-kio: sem isto o Baloo percorria a pasta FUSE.
	try:
		_excludeFromBaloo()
	except (OSError, subprocess.SubprocessError):
		pass


def disableProfileMount(profile: config.Profile) -> None:
	unit: str = profileUnitName(profile.id)
	_systemctl(["disable", "--now", unit], check=False)
	mount: Path = Path(profile.mountPath).expanduser()
	subprocess.run(
		["fusermount3", "-uz", str(mount)],
		check=False,
		capture_output=True,
		text=True,
		encoding="utf-8",
	)


def _unescapeMountField(field: str) -> str:
	chars: list[str] = []
	index: int = 0
	while index < len(field):
		if field[index] == "\\" and index + 3 < len(field):
			digits: str = field[index + 1 : index + 4]
			if len(digits) == 3 and all(char in "01234567" for char in digits):
				chars.append(chr(int(digits, 8)))
				index += 4
				continue
		chars.append(field[index])
		index += 1
	return "".join(chars)


def fuseCommand(profileId: str = "") -> list[str]:
	wrapper: Path | None = installedCliPath()
	command: list[str]
	if wrapper is not None:
		command = [str(wrapper), "fuse"]
	else:
		binary: Path = repoBinPath()
		if binary.is_file():
			command = [pythonBin(), str(binary), "fuse"]
		else:
			command = [pythonBin(), "-m", "fastonedrive", "fuse"]
	if profileId == "" or len(command) == 0:
		return command
	return [*command[:-1], "--profile", profileId, command[-1]]


def _writeProfileUnit(profile: config.Profile) -> Path:
	path: Path = Path.home() / ".config" / "systemd" / "user" / profileUnitName(profile.id)
	path.parent.mkdir(parents=True, exist_ok=True)
	mount: Path = Path(profile.mountPath).expanduser()
	mount.mkdir(parents=True, exist_ok=True)
	start: str = " ".join(_shellArg(part) for part in fuseCommand(profile.id))
	homeMount: Path = config.homeDir() / "OneDrive"
	conflicts: str = ""
	if mount.resolve(strict=False) == homeMount.resolve(strict=False):
		conflicts = f"Conflicts={LEGACY_MOUNT_UNIT}\n"
	text: str = (
		"[Unit]\n"
		f"Description=Montagem {profile.name} (Fast-OneDrive FUSE)\n"
		"After=network-online.target\n"
		"Wants=network-online.target\n"
		f"{conflicts}"
		"\n"
		"[Service]\n"
		# O FUSE avisa o systemd (sd_notify READY=1) quando está montado.
		"Type=notify\n"
		"NotifyAccess=all\n"
		"TimeoutStartSec=90\n"
		# Parar o mount não mata um envio ou índice que o FUSE lançou: um envio
		# interrompido deixava o ficheiro sem linha de base e gerava conflitos.
		"KillMode=process\n"
		f"ExecStartPre=-/usr/bin/fusermount3 -uz {_shellArg(str(mount))}\n"
		f"ExecStart={start}\n"
		f"ExecStop=/usr/bin/fusermount3 -uz {_shellArg(str(mount))}\n"
		"Restart=on-failure\n"
		"RestartSec=10\n"
		"\n"
		"[Install]\n"
		"WantedBy=default.target\n"
	)
	tmp: Path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
	try:
		tmp.write_text(text, encoding="utf-8")
		os.replace(tmp, path)
	except Exception:
		tmp.unlink(missing_ok=True)
		raise
	return path


def _shellArg(value: str) -> str:
	if value == "":
		return '""'
	if any(char in value for char in ' \t\n"\'\\'):
		escaped: str = value.replace("'", "'\\''")
		return f"'{escaped}'"
	return value


def _systemctl(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
	result: subprocess.CompletedProcess[str] = subprocess.run(
		["systemctl", "--user", *args],
		check=False,
		capture_output=True,
		text=True,
		encoding="utf-8",
	)
	if check and result.returncode != 0:
		detail: str = (result.stderr or result.stdout or "").strip()
		if detail == "":
			raise FastOneDriveError(f"systemctl {' '.join(args)} saiu com código {result.returncode}")
		raise FastOneDriveError(detail.splitlines()[-1])
	return result


def _systemctlActive(unit: str) -> bool:
	result: subprocess.CompletedProcess[str] = _systemctl(["is-active", unit], check=False)
	return result.stdout.strip() == "active"
