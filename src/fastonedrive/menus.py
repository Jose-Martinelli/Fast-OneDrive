import os
import shutil
import subprocess
from pathlib import Path

from fastonedrive import FastOneDriveError
from fastonedrive import config


def repoBinPath() -> Path:
	return Path(__file__).resolve().parent.parent.parent / "bin" / "fastonedrive"


def cliWrapperPath() -> Path:
	return Path.home() / ".local" / "bin" / "fastonedrive"


def pythonBin() -> str:
	for candidate in ("/usr/bin/python3.14", "/usr/bin/python3"):
		if Path(candidate).is_file():
			return candidate
	found: str | None = shutil.which("python3")
	if found is None:
		raise FastOneDriveError("python3 não encontrado")
	return found


def serviceMenuHelperPath() -> Path:
	return Path.home() / ".local" / "share" / "fast-onedrive" / "servicemenu.py"


def repoServiceMenuHelper() -> Path:
	return Path(__file__).resolve().parent.parent.parent / "dolphin" / "servicemenu.py"


def writeInstalledText(path: Path, body: str, mode: int) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp: Path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
	try:
		tmp.write_text(body, encoding="utf-8")
		tmp.chmod(mode)
		os.replace(tmp, path)
	except Exception:
		tmp.unlink(missing_ok=True)
		raise


def installCliWrapper() -> Path:
	binary: Path = repoBinPath()
	if not binary.is_file():
		raise FastOneDriveError(f"CLI não encontrado: {binary}")
	wrapper: Path = cliWrapperPath()
	writeInstalledText(
		wrapper,
		f"#!/bin/sh\nexec \"{binary}\" \"$@\"\n",
		0o755,
	)
	return wrapper


def installServiceMenuHelper() -> Path:
	source: Path = repoServiceMenuHelper()
	if not source.is_file():
		raise FastOneDriveError(f"launcher do menu não encontrado: {source}")
	dest: Path = serviceMenuHelperPath()
	writeInstalledText(
		dest,
		source.read_text(encoding="utf-8"),
		0o755,
	)
	return dest


def serviceMenuDir() -> Path:
	return Path.home() / ".local" / "share" / "kio" / "servicemenus"


def serviceMenuPath() -> Path:
	return serviceMenuDir() / "fast-onedrive.desktop"


def applicationsDir() -> Path:
	return Path.home() / ".local" / "share" / "applications"


def settingsDesktopPath() -> Path:
	return applicationsDir() / "fast-onedrive.desktop"


def repoSettingsDesktop() -> Path:
	return Path(__file__).resolve().parent.parent.parent / "dolphin" / "fast-onedrive-settings.desktop"


def autostartDir() -> Path:
	return Path.home() / ".config" / "autostart"


def autostartDesktopPath() -> Path:
	return autostartDir() / "fast-onedrive.desktop"


def repoAutostartDesktop() -> Path:
	return Path(__file__).resolve().parent.parent.parent / "dolphin" / "fast-onedrive-autostart.desktop"


def pickDestDir() -> str:
	binary: str | None = shutil.which("kdialog")
	if binary is None:
		raise FastOneDriveError("kdialog não encontrado (necessário para Copiar para…)")
	result: subprocess.CompletedProcess[str] = subprocess.run(
		[
			binary,
			"--getexistingdirectory",
			str(config.copyDestPath()),
		],
		check=False,
		capture_output=True,
		text=True,
		encoding="utf-8",
	)
	chosen: str = result.stdout.strip()
	if result.returncode != 0 or chosen == "":
		raise FastOneDriveError("cópia cancelada")
	return chosen


def installSettingsDesktop(binary: Path) -> Path:
	template: Path = repoSettingsDesktop()
	if not template.is_file():
		raise FastOneDriveError(f"template de definições não encontrado: {template}")
	path: Path = settingsDesktopPath()
	writeInstalledText(
		path,
		template.read_text(encoding="utf-8").replace("@BIN@", str(binary)),
		0o755,
	)
	return path


def installAutostartDesktop(binary: Path) -> Path:
	template: Path = repoAutostartDesktop()
	if not template.is_file():
		raise FastOneDriveError(f"template de autostart não encontrado: {template}")
	path: Path = autostartDesktopPath()
	writeInstalledText(
		path,
		template.read_text(encoding="utf-8").replace("@BIN@", str(binary)),
		0o644,
	)
	return path


def schemeHandlerPath() -> Path:
	return applicationsDir() / "fastonedrive-open.desktop"


def schemeHandlerTemplate() -> Path:
	return Path(__file__).resolve().parent.parent.parent / "dolphin" / "fast-onedrive-open.desktop"


def installSchemeHandler(binary: Path) -> Path:
	template: Path = schemeHandlerTemplate()
	if not template.is_file():
		raise FastOneDriveError(f"template de abertura não encontrado: {template}")
	path: Path = schemeHandlerPath()
	writeInstalledText(
		path,
		template.read_text(encoding="utf-8").replace("@BIN@", str(binary)),
		0o644,
	)
	mime: str | None = shutil.which("xdg-mime")
	if mime is not None:
		subprocess.run(
			[mime, "default", path.name, "x-scheme-handler/fastonedrive"],
			check=False,
			capture_output=True,
		)
	return path


def installMenus() -> Path:
	config.ensureUserConfig()
	binary: Path = installCliWrapper()
	helper: Path = installServiceMenuHelper()
	template: Path = Path(__file__).resolve().parent.parent.parent / "dolphin" / "fast-onedrive.desktop"
	if not template.is_file():
		raise FastOneDriveError(f"template de menu não encontrado: {template}")
	text: str = (
		template.read_text(encoding="utf-8")
		.replace("@PYTHON@", pythonBin())
		.replace("@LAUNCHER@", str(helper))
	)
	path: Path = serviceMenuPath()
	writeInstalledText(
		path,
		text,
		0o755,
	)
	installSettingsDesktop(binary)
	installAutostartDesktop(binary)
	installSchemeHandler(binary)
	_rebuildSycoca()
	return path


def rebuildSycoca() -> None:
	_rebuildSycoca()


def _rebuildSycoca() -> None:
	binary: str | None = shutil.which("kbuildsycoca6")
	if binary is None:
		binary = shutil.which("kbuildsycoca5")
	if binary is None:
		return
	env: dict[str, str] = os.environ.copy()
	pluginRoot: str = str(Path.home() / ".local" / "lib" / "qt6" / "plugins")
	current: str = env.get("QT_PLUGIN_PATH", "")
	if pluginRoot not in current.split(":"):
		env["QT_PLUGIN_PATH"] = pluginRoot if current == "" else f"{pluginRoot}:{current}"
	subprocess.run(
		[binary, "--noincremental"],
		check=False,
		capture_output=True,
		env=env,
	)
