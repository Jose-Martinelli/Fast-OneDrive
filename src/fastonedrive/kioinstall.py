import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import ParseResult, quote, unquote, urlparse

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.menus import (
	installCliWrapper,
	installSchemeHandler,
	rebuildSycoca,
	writeInstalledText,
)

_BOOKMARK_NS: str = "http://www.freedesktop.org/standards/desktop-bookmarks"
_ICON_TAG: str = f"{{{_BOOKMARK_NS}}}icon"
_KDE_OWNER: str = "http://www.kde.org"


def runDolphinSearch() -> str:
	binary: str | None = shutil.which("kdialog")
	if binary is None:
		raise FastOneDriveError("kdialog não encontrado (necessário para a busca no Dolphin)")
	result: subprocess.CompletedProcess[str] = subprocess.run(
		[
			binary,
			"--title",
			"Fast-OneDrive",
			"--inputbox",
			"Buscar no OneDrive:",
		],
		check=False,
		capture_output=True,
		text=True,
		encoding="utf-8",
	)
	query: str = result.stdout.strip()
	if result.returncode != 0 or query == "":
		raise FastOneDriveError("busca cancelada")
	profile: config.Profile | None = config.activeProfile()
	prefix: str = config.KIO_SCHEME + ":"
	if profile is not None:
		prefix = f"{config.KIO_SCHEME}:/p/{profile.id}"
	url: str = f"{prefix}/search/{quote(query, safe='')}"
	dolphin: str | None = shutil.which("dolphin")
	if dolphin is None:
		raise FastOneDriveError("dolphin não encontrado")
	opened: subprocess.CompletedProcess[str] = subprocess.run(
		[dolphin, url],
		check=False,
		env=_pluginEnv(),
	)
	if opened.returncode != 0:
		raise FastOneDriveError(f"dolphin saiu com código {opened.returncode}")
	return url


def installKio() -> Path:
	config.ensureUserConfig()
	binary: Path = installCliWrapper()
	installSchemeHandler(binary)
	pluginPath: Path = _buildAndInstallWorker()
	_installPluginEnv()
	_installPlace()
	hideFuseFromDolphin()
	rebuildSycoca()
	return pluginPath


def _qtPluginRoot() -> Path:
	return Path.home() / ".local" / "lib" / "qt6" / "plugins"


def _quoteEnvValue(value: str) -> str:
	escaped: str = value.replace("\\", "\\\\").replace('"', '\\"')
	return f'"{escaped}"'


def _unquoteEnvValue(value: str) -> str:
	if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
		return value[1:-1]
	return value


def _earlierQtPluginPath() -> str:
	directory: Path = Path.home() / ".config" / "environment.d"
	if not directory.is_dir():
		return ""
	chosen: str = ""
	ourName: str = "90-fastonedrive.conf"
	for conf in sorted(directory.glob("*.conf")):
		if conf.name >= ourName:
			continue
		try:
			text: str = conf.read_text(encoding="utf-8")
		except OSError:
			continue
		for line in text.splitlines():
			stripped: str = line.strip()
			key: str = "QT_PLUGIN_PATH="
			if stripped == "" or stripped.startswith("#") or not stripped.startswith(key):
				continue
			chosen = _unquoteEnvValue(stripped[len(key):].strip())
	return chosen


def _systemdPluginPath(pluginRoot: Path) -> str:
	root: str = str(pluginRoot)
	parts: list[str] = [part for part in _earlierQtPluginPath().split(":") if part != "" and part != root]
	parts.insert(0, root)
	return ":".join(parts)


def _replaceText(path: Path, body: str) -> None:
	tmp: Path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
	try:
		tmp.write_text(body, encoding="utf-8")
		os.replace(tmp, path)
	except Exception:
		tmp.unlink(missing_ok=True)
		raise


def _pluginEnv() -> dict[str, str]:
	env: dict[str, str] = os.environ.copy()
	pluginRoot: str = str(_qtPluginRoot())
	current: str = env.get("QT_PLUGIN_PATH", "")
	if pluginRoot not in current.split(":"):
		env["QT_PLUGIN_PATH"] = pluginRoot if current == "" else f"{pluginRoot}:{current}"
	env.setdefault("FASTONEDRIVE_BIN", str(Path.home() / ".local" / "bin" / "fastonedrive"))
	return env


def _installPluginEnv() -> None:
	pluginRoot: Path = _qtPluginRoot()
	script: Path = Path.home() / ".config" / "plasma-workspace" / "env" / "fastonedrive.sh"
	writeInstalledText(
		script,
		"#!/bin/sh\n"
		f'export QT_PLUGIN_PATH="{pluginRoot}'
		'${QT_PLUGIN_PATH:+:$QT_PLUGIN_PATH}"\n'
		'export FASTONEDRIVE_BIN="${FASTONEDRIVE_BIN:-$HOME/.local/bin/fastonedrive}"\n',
		0o755,
	)
	conf: Path = Path.home() / ".config" / "environment.d" / "90-fastonedrive.conf"
	writeInstalledText(
		conf,
		f"QT_PLUGIN_PATH={_quoteEnvValue(_systemdPluginPath(pluginRoot))}\n",
		0o644,
	)


def _buildAndInstallWorker() -> Path:
	source: Path = Path(__file__).resolve().parent.parent.parent / "kio"
	if not (source / "CMakeLists.txt").is_file() and not (source / "Makefile").is_file():
		raise FastOneDriveError(f"fonte do worker KIO não encontrada: {source}")
	prefix: Path = Path.home() / ".local"
	cmake: str | None = shutil.which("cmake")
	make: str | None = shutil.which("make")
	if (source / "Makefile").is_file() and make is not None:
		_buildWithMake(make, source, prefix)
	elif cmake is not None:
		_buildWithCmake(cmake, source, prefix)
	else:
		raise FastOneDriveError(
			"nem cmake nem make encontrados; instale cmake (pacman -S cmake extra-cmake-modules)"
		)
	pluginPath: Path | None = _findPlugin(prefix)
	if pluginPath is None:
		raise FastOneDriveError("plugin KIO instalado, mas kio_fastonedrive.so não foi encontrado")
	return pluginPath


def _buildWithCmake(cmake: str, source: Path, prefix: Path) -> None:
	build: Path = source / "build"
	build.mkdir(parents=True, exist_ok=True)
	_runCmake(
		[
			cmake,
			"-S",
			str(source),
			"-B",
			str(build),
			f"-DCMAKE_INSTALL_PREFIX={prefix}",
			"-DCMAKE_BUILD_TYPE=Release",
		]
	)
	_runCmake([cmake, "--build", str(build), "-j"])
	_runCmake([cmake, "--install", str(build)])


def _buildWithMake(make: str, source: Path, prefix: Path) -> None:
	pluginDir: Path = prefix / "lib" / "qt6" / "plugins" / "kf6" / "kio"
	env: dict[str, str] = os.environ.copy()
	env["PATH"] = "/usr/bin:/bin:" + env.get("PATH", "")
	env["CXX"] = "/usr/bin/g++"
	env["MOC"] = "/usr/lib/qt6/moc"
	result: subprocess.CompletedProcess[str] = subprocess.run(
		[
			make,
			"-C",
			str(source),
			f"PREFIX={prefix}",
			f"PLUGINDIR={pluginDir}",
			"install",
		],
		check=False,
		capture_output=True,
		text=True,
		encoding="utf-8",
		env=env,
	)
	if result.returncode != 0:
		detail: str = (result.stderr or result.stdout or "").strip()
		if detail == "":
			raise FastOneDriveError(f"make saiu com código {result.returncode}")
		raise FastOneDriveError(detail.splitlines()[-1])


def _runCmake(command: list[str]) -> None:
	result: subprocess.CompletedProcess[str]
	try:
		result = subprocess.run(
			command,
			check=False,
			capture_output=True,
			text=True,
			encoding="utf-8",
			timeout=120,
		)
	except subprocess.TimeoutExpired as error:
		raise FastOneDriveError("cmake excedeu o tempo") from error
	if result.returncode != 0:
		detail: str = (result.stderr or result.stdout or "").strip()
		if detail == "":
			raise FastOneDriveError(f"cmake saiu com código {result.returncode}")
		raise FastOneDriveError(detail.splitlines()[-1])


def _findPlugin(prefix: Path) -> Path | None:
	matches: list[Path] = list(prefix.glob("**/kf6/kio/*fastonedrive.so"))
	if not matches:
		return None
	return matches[0]


def _installPlace() -> None:
	profiles: list[config.Profile] = config.listProfiles()
	if len(profiles) == 0:
		_insertPlaceBookmark("fastonedrive:/", "OneDrive", "FastOneDrivePlace")
		return
	for profile in profiles:
		upsertProfilePlace(profile)


def hideFuseFromDolphin() -> None:
	profiles: list[config.Profile] = config.listProfiles()
	if len(profiles) == 0:
		_hideMountInHome(config.mountPath())
	else:
		for profile in profiles:
			_hideMountInHome(Path(profile.mountPath))
	places: Path = Path.home() / ".local" / "share" / "user-places.xbel"
	if places.is_file():
		_rewriteFusePlaces(places)


def upsertProfilePlace(profile: config.Profile) -> None:
	href: str = f"{config.KIO_SCHEME}:/p/{profile.id}/"
	placeId: str = f"FastOneDrivePlace-{profile.id}"
	path: Path = Path.home() / ".local" / "share" / "user-places.xbel"
	if not path.is_file():
		raise FastOneDriveError(f"Locais do Dolphin não encontrados: {path}")
	ET.register_namespace("bookmark", _BOOKMARK_NS)
	ET.register_namespace("kdepriv", "http://www.kde.org/kdepriv")
	ET.register_namespace("mime", "http://www.freedesktop.org/standards/shared-mime-info")
	tree: ET.ElementTree = ET.parse(path)
	root: ET.Element | None = tree.getroot()
	if root is None:
		raise FastOneDriveError("user-places.xbel sem elemento xbel")
	updated: bool = False
	for bookmark in list(root.findall("bookmark")):
		current: str = bookmark.get("href") or ""
		bookmarkId: str = _bookmarkId(bookmark)
		legacyRoot: bool = profile.legacy and _isLegacyPlace(current, bookmarkId)
		if current.rstrip("/") == href.rstrip("/") or bookmarkId == placeId or legacyRoot:
			bookmark.set("href", href)
			_setBookmarkTitle(bookmark, profile.name)
			_setBookmarkId(bookmark, placeId)
			updated = True
	if not updated:
		root.append(_newBookmark(href, profile.name, placeId))
	_writePlaces(path, root)


def removeProfilePlace(profileId: str) -> None:
	path: Path = Path.home() / ".local" / "share" / "user-places.xbel"
	if not path.is_file():
		return
	href: str = f"{config.KIO_SCHEME}:/p/{profileId}/"
	placeId: str = f"FastOneDrivePlace-{profileId}"
	ET.register_namespace("bookmark", _BOOKMARK_NS)
	ET.register_namespace("kdepriv", "http://www.kde.org/kdepriv")
	ET.register_namespace("mime", "http://www.freedesktop.org/standards/shared-mime-info")
	tree: ET.ElementTree = ET.parse(path)
	root: ET.Element | None = tree.getroot()
	if root is None:
		return
	changed: bool = False
	for bookmark in list(root.findall("bookmark")):
		current: str = bookmark.get("href") or ""
		if current.rstrip("/") == href.rstrip("/") or _bookmarkId(bookmark) == placeId:
			root.remove(bookmark)
			changed = True
	if changed:
		_writePlaces(path, root)


def _hideMountInHome(mountPath: Path) -> None:
	mount: Path = mountPath.expanduser().resolve(strict=False)
	home: Path = config.homeDir().resolve()
	if mount.parent != home:
		return
	name: str = mount.name
	hiddenPath: Path = home / ".hidden"
	lines: list[str] = []
	if hiddenPath.is_file():
		lines = hiddenPath.read_text(encoding="utf-8").splitlines()
	names: list[str] = [line.strip() for line in lines]
	if name in names:
		return
	lines.append(name)
	_replaceText(hiddenPath, "\n".join(lines) + "\n")


def _rewriteFusePlaces(path: Path) -> None:
	ET.register_namespace("bookmark", _BOOKMARK_NS)
	ET.register_namespace("kdepriv", "http://www.kde.org/kdepriv")
	ET.register_namespace("mime", "http://www.freedesktop.org/standards/shared-mime-info")
	tree: ET.ElementTree = ET.parse(path)
	root: ET.Element | None = tree.getroot()
	if root is None:
		return
	mounts: list[tuple[Path, str]] = []
	for profile in config.listProfiles():
		mounts.append(
			(
				Path(profile.mountPath).expanduser().resolve(strict=False),
				profile.id,
			)
		)
	if len(mounts) == 0:
		mounts.append((config.mountPath().expanduser().resolve(strict=False), ""))
	existing: set[str] = set()
	for bookmark in root.findall("bookmark"):
		href: str = bookmark.get("href") or ""
		if href.startswith(f"{config.KIO_SCHEME}:"):
			existing.add(href)
	changed: bool = False
	for child in list(root):
		if child.tag == "bookmark":
			bestMount: Path | None = None
			bestId: str = ""
			bestLen: int = -1
			href: str = child.get("href") or ""
			parsed: ParseResult = urlparse(href)
			if parsed.scheme == "file":
				local: Path = Path(unquote(parsed.path)).resolve(strict=False)
				for mount, profileId in mounts:
					try:
						local.relative_to(mount)
					except ValueError:
						continue
					length: int = len(mount.parts)
					if length > bestLen:
						bestLen = length
						bestMount = mount
						bestId = profileId
			if bestMount is None:
				continue
			if _rewriteFuseBookmark(
				child,
				root,
				bestMount,
				existing,
				bestId,
			):
				changed = True
		elif child.tag == "separator":
			for mount, _profileId in mounts:
				if _isFuseDeviceSeparator(child, str(mount)):
					root.remove(child)
					changed = True
					break
	if changed:
		_writePlaces(path, root)


def _rewriteFuseBookmark(
	bookmark: ET.Element,
	root: ET.Element,
	mount: Path,
	existing: set[str],
	profileId: str,
) -> bool:
	href: str = bookmark.get("href") or ""
	kioHref: str | None = _kioHrefForFuseFile(href, mount, profileId)
	if kioHref is None:
		return False
	if kioHref in existing:
		root.remove(bookmark)
		return True
	bookmark.set("href", kioHref)
	existing.add(kioHref)
	icon: ET.Element | None = None
	info: ET.Element | None = bookmark.find("info")
	if info is not None:
		for metadata in info.findall("metadata"):
			found: ET.Element | None = metadata.find(_ICON_TAG)
			if found is not None:
				icon = found
				break
	if icon is not None:
		icon.set("name", "folder-cloud")
	return True


def _isFuseDeviceSeparator(separator: ET.Element, mountText: str) -> bool:
	info: ET.Element | None = separator.find("info")
	if info is None:
		return False
	for metadata in info.findall("metadata"):
		if metadata.get("owner") != _KDE_OWNER:
			continue
		udi: ET.Element | None = metadata.find("UDI")
		text: str = (udi.text or "") if udi is not None else ""
		if mountText in text:
			return True
	return False


def _kioHrefForFuseFile(href: str, mount: Path, profileId: str) -> str | None:
	parsed: ParseResult = urlparse(href)
	if parsed.scheme != "file":
		return None
	local: Path = Path(unquote(parsed.path)).resolve(strict=False)
	try:
		relative: Path = local.relative_to(mount)
	except ValueError:
		return None
	posix: str = relative.as_posix()
	prefix: str = config.KIO_SCHEME + ":"
	if profileId != "":
		prefix = f"{config.KIO_SCHEME}:/p/{profileId}"
	if posix == ".":
		return f"{prefix}/"
	return f"{prefix}/item/{quote(posix, safe='/')}"


def _insertPlaceBookmark(href: str, title: str, placeId: str) -> None:
	path: Path = Path.home() / ".local" / "share" / "user-places.xbel"
	if not path.is_file():
		raise FastOneDriveError(f"Locais do Dolphin não encontrados: {path}")
	text: str = path.read_text(encoding="utf-8")
	if f'href="{href}"' in text:
		return
	snippet: str = (
		f' <bookmark href="{href}">\n'
		f"  <title>{title}</title>\n"
		"  <info>\n"
		'   <metadata owner="http://freedesktop.org">\n'
		'    <bookmark:icon name="folder-cloud"/>\n'
		"   </metadata>\n"
		'   <metadata owner="http://www.kde.org">\n'
		f"    <ID>{placeId}</ID>\n"
		"    <IsHidden>false</IsHidden>\n"
		"   </metadata>\n"
		"  </info>\n"
		" </bookmark>\n"
	)
	marker: str = "</xbel>"
	index: int = text.rfind(marker)
	if index < 0:
		raise FastOneDriveError("user-places.xbel sem elemento xbel")
	_replaceText(path, text[:index] + snippet + text[index:])


def _newBookmark(href: str, title: str, placeId: str) -> ET.Element:
	bookmark: ET.Element = ET.Element("bookmark", {"href": href})
	titleNode: ET.Element = ET.SubElement(bookmark, "title")
	titleNode.text = title
	info: ET.Element = ET.SubElement(bookmark, "info")
	freedesktop: ET.Element = ET.SubElement(
		info,
		"metadata",
		{"owner": "http://freedesktop.org"},
	)
	ET.SubElement(freedesktop, _ICON_TAG, {"name": "folder-cloud"})
	kde: ET.Element = ET.SubElement(info, "metadata", {"owner": _KDE_OWNER})
	idNode: ET.Element = ET.SubElement(kde, "ID")
	idNode.text = placeId
	hidden: ET.Element = ET.SubElement(kde, "IsHidden")
	hidden.text = "false"
	return bookmark


def _bookmarkId(bookmark: ET.Element) -> str:
	info: ET.Element | None = bookmark.find("info")
	if info is None:
		return ""
	for metadata in info.findall("metadata"):
		if metadata.get("owner") != _KDE_OWNER:
			continue
		found: ET.Element | None = metadata.find("ID")
		if found is not None and found.text:
			return found.text
	return ""


def _setBookmarkId(bookmark: ET.Element, placeId: str) -> None:
	info: ET.Element | None = bookmark.find("info")
	if info is None:
		info = ET.SubElement(bookmark, "info")
	for metadata in info.findall("metadata"):
		if metadata.get("owner") != _KDE_OWNER:
			continue
		found: ET.Element | None = metadata.find("ID")
		if found is None:
			found = ET.SubElement(metadata, "ID")
		found.text = placeId
		return
	metadata = ET.SubElement(info, "metadata", {"owner": _KDE_OWNER})
	found = ET.SubElement(metadata, "ID")
	found.text = placeId


def _setBookmarkTitle(bookmark: ET.Element, title: str) -> None:
	found: ET.Element | None = bookmark.find("title")
	if found is None:
		found = ET.Element("title")
		bookmark.insert(0, found)
	found.text = title


def _isLegacyPlace(href: str, bookmarkId: str) -> bool:
	if bookmarkId == "FastOneDrivePlace":
		return True
	parsed = urlparse(href)
	if parsed.scheme != config.KIO_SCHEME:
		return False
	return parsed.path.strip("/") == ""


def _writePlaces(path: Path, root: ET.Element) -> None:
	ET.indent(root, space=" ")
	body: str = ET.tostring(root, encoding="unicode")
	text: str = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xbel>\n' + body + "\n"
	tmp: Path = path.with_name(f".{path.name}.tmp")
	try:
		tmp.write_text(text, encoding="utf-8")
		os.replace(tmp, path)
	except Exception:
		tmp.unlink(missing_ok=True)
		raise
