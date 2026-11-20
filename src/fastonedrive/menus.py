import os
import shutil
import subprocess
from pathlib import Path

from fastonedrive import FastOneDriveError
from fastonedrive import config

APP_ICON_NAME: str = "fastonedrive"
_ICON_SIZES: tuple[str, ...] = ("16", "22", "24", "32", "48", "64", "128", "256", "512")


def repoRoot() -> Path:
	return Path(__file__).resolve().parent.parent.parent


def repoBinPath() -> Path:
	return repoRoot() / "bin" / "fastonedrive"


def repoIconSource() -> Path:
	return repoRoot() / "icon.png"


def repoHicolorIconsDir() -> Path:
	return repoRoot() / "icons" / "hicolor"


def userHicolorIconsDir() -> Path:
	return Path.home() / ".local" / "share" / "icons" / "hicolor"


def appIconPath() -> Path | None:
	"""Melhor PNG disponível para a app (tema local, sistema ou repo)."""
	candidates: list[Path] = []
	for size in ("256", "128", "512", "64", "48"):
		candidates.append(userHicolorIconsDir() / f"{size}x{size}" / "apps" / f"{APP_ICON_NAME}.png")
		candidates.append(Path("/usr/share/icons/hicolor") / f"{size}x{size}" / "apps" / f"{APP_ICON_NAME}.png")
		candidates.append(repoHicolorIconsDir() / f"{size}x{size}" / "apps" / f"{APP_ICON_NAME}.png")
	candidates.append(repoIconSource())
	for path in candidates:
		if path.is_file():
			return path
	return None


def installAppIcons() -> Path:
	sourceRoot: Path = repoHicolorIconsDir()
	if not sourceRoot.is_dir():
		raise FastOneDriveError(f"ícones não encontrados: {sourceRoot}")
	destRoot: Path = userHicolorIconsDir()
	installed: int = 0
	for size in _ICON_SIZES:
		src: Path = sourceRoot / f"{size}x{size}" / "apps" / f"{APP_ICON_NAME}.png"
		if not src.is_file():
			continue
		dest: Path = destRoot / f"{size}x{size}" / "apps" / f"{APP_ICON_NAME}.png"
		dest.parent.mkdir(parents=True, exist_ok=True)
		shutil.copy2(src, dest)
		installed += 1
	if installed == 0:
		raise FastOneDriveError(f"nenhum ícone {APP_ICON_NAME}.png em {sourceRoot}")
	_updateIconCache(destRoot.parent)
	return destRoot


def cliWrapperPath() -> Path:
	return Path.home() / ".local" / "bin" / "fastonedrive"


def installedCliPath() -> Path | None:
	wrapper: Path = cliWrapperPath()
	if wrapper.is_file():
		return wrapper
	system: Path = Path("/usr/bin/fastonedrive")
	if system.is_file():
		return system
	return None


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
	return repoRoot() / "dolphin" / "servicemenu.py"


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
	return repoRoot() / "dolphin" / "fast-onedrive-settings.desktop"


def autostartDir() -> Path:
	return Path.home() / ".config" / "autostart"


def autostartDesktopPath() -> Path:
	return autostartDir() / "fast-onedrive.desktop"


def repoAutostartDesktop() -> Path:
	return repoRoot() / "dolphin" / "fast-onedrive-autostart.desktop"


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
	return repoRoot() / "dolphin" / "fast-onedrive-open.desktop"


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
	template: Path = repoRoot() / "dolphin" / "fast-onedrive.desktop"
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
	installAppIcons()
	installSettingsDesktop(binary)
	installAutostartDesktop(binary)
	installSchemeHandler(binary)
	_rebuildSycoca()
	return path


def rebuildSycoca() -> None:
	_rebuildSycoca()


def _updateIconCache(iconsDir: Path) -> None:
	binary: str | None = shutil.which("gtk-update-icon-cache")
	if binary is None:
		return
	subprocess.run(
		[binary, "-f", "-t", str(iconsDir / "hicolor")],
		check=False,
		capture_output=True,
	)


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
