import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

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
	opened: subprocess.CompletedProcess[bytes] = subprocess.run(
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
	showFuseInDolphin()
	_buildDolphinPlugins()
	_excludeFromBaloo()
	rebuildSycoca()
	if str(_qtPluginRoot()) not in os.environ.get("QT_PLUGIN_PATH", "").split(":"):
		# Instalação em ~/.local: o Dolphin só vê os plugins (emblemas, submenu) com o
		# QT_PLUGIN_PATH novo, que a sessão Plasma lê ao entrar. O pacote instala em
		# /usr e não precisa disto.
		print(
			"Aviso: termine a sessão e volte a entrar (ou reinicie) para o Dolphin carregar "
			"os emblemas e o submenu OneDrive."
		)
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


def _buildDolphinPlugins() -> None:
	"""Emblemas e submenu "OneDrive" para ~/OneDrive (precisa de cmake e kf6-kio).

	Não é fatal: sem estes plugins a pasta continua a funcionar, só sem emblemas.
	"""
	from fastonedrive.log import logError

	source: Path = Path(__file__).resolve().parent.parent.parent / "dolphin-plugin"
	if not (source / "CMakeLists.txt").is_file():
		return
	prefix: Path = Path.home() / ".local"
	make: str | None = shutil.which("make")
	cmake: str | None = shutil.which("cmake")
	errors: list[str] = []
	# Primeiro o Makefile (o mesmo caminho que já compila o worker KIO nesta máquina);
	# se falhar, o cmake.
	if make is not None and (source / "Makefile").is_file():
		try:
			_buildPluginsWithMake(make, source, prefix)
			print(f"Plugins do Dolphin instalados em {prefix / 'lib' / 'qt6' / 'plugins' / 'kf6'}")
			return
		except FastOneDriveError as error:
			errors.append(f"make: {error}")
	if cmake is not None:
		try:
			_buildWithCmake(cmake, source, prefix)
			print(f"Plugins do Dolphin instalados em {prefix / 'lib' / 'qt6' / 'plugins' / 'kf6'}")
			return
		except FastOneDriveError as error:
			errors.append(f"cmake: {error}")
	if len(errors) == 0:
		errors.append("nem make nem cmake encontrados")
	detail: str = "\n".join(errors)
	logError(f"plugins do Dolphin não compilaram:\n{detail}")
	print(f"Aviso: plugins do Dolphin não compilaram (emblemas e submenu OneDrive):\n{detail}")


def _buildPluginsWithMake(make: str, source: Path, prefix: Path) -> None:
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
			f"PLUGINROOT={prefix / 'lib' / 'qt6' / 'plugins' / 'kf6'}",
			"install",
		],
		check=False,
		capture_output=True,
		text=True,
		encoding="utf-8",
		env=env,
		timeout=300,
	)
	if result.returncode != 0:
		raise FastOneDriveError(_buildErrorSummary(result.stderr or result.stdout or "", "make", result.returncode))


def _excludeFromBaloo() -> None:
	"""O Baloo a indexar ~/OneDrive leria o FUSE (e descarregaria ficheiros)."""
	balooctl: str | None = shutil.which("balooctl6") or shutil.which("balooctl")
	if balooctl is None:
		return
	mounts: list[Path] = [Path(profile.mountPath).expanduser() for profile in config.listProfiles()]
	if len(mounts) == 0:
		mounts.append(config.mountPath())
	for mount in mounts:
		subprocess.run(
			[balooctl, "config", "add", "excludeFolders", str(mount)],
			check=False,
			capture_output=True,
			timeout=20,
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
		raise FastOneDriveError(
			_buildErrorSummary((result.stderr or "") + "\n" + (result.stdout or ""), "cmake", result.returncode)
		)


def _buildErrorSummary(output: str, tool: str, code: int) -> str:
	"""As linhas que explicam a falha (CMake Error / error:), não só a última da pilha."""
	lines: list[str] = [line.rstrip() for line in output.splitlines() if line.strip() != ""]
	if len(lines) == 0:
		return f"{tool} saiu com código {code}"
	picked: list[str] = []
	for index, line in enumerate(lines):
		lowered: str = line.lower()
		if "cmake error" in lowered or " error:" in lowered or lowered.startswith("error") or "fatal error" in lowered:
			picked.extend(lines[index : index + 4])
	if len(picked) == 0:
		picked = lines[-6:]
	return "\n".join(dict.fromkeys(picked))[:2000]


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


def showFuseInDolphin() -> None:
	"""Um só lugar no Dolphin: a pasta FUSE de cada perfil (~/OneDrive), como no Windows.

	Desfaz o que versões anteriores faziam (esconder a pasta em ~/.hidden e trocar os
	marcadores file:// por fastonedrive:/…). O Local de cada perfil aponta para a pasta
	FUSE quando o mount está ligado; sem mount, continua em fastonedrive:/p/<id>/.
	A busca continua no protocolo (fastonedrive:/search/…), no KRunner e na janela.
	"""
	profiles: list[config.Profile] = config.listProfiles()
	mounts: list[Path] = [Path(profile.mountPath).expanduser() for profile in profiles]
	if len(mounts) == 0:
		mounts.append(config.mountPath())
	for mount in mounts:
		_unhideMountInHome(mount)
	places: Path = Path.home() / ".local" / "share" / "user-places.xbel"
	if not places.is_file():
		return
	for profile in profiles:
		upsertProfilePlace(profile)
	_rewriteKioItemPlaces(places)


def placeHref(profile: config.Profile) -> str:
	if profile.mountEnabled:
		return Path(profile.mountPath).expanduser().resolve(strict=False).as_uri()
	return f"{config.KIO_SCHEME}:/p/{profile.id}/"


def _placeHrefs(profileId: str, mountPath: str) -> set[str]:
	hrefs: set[str] = {f"{config.KIO_SCHEME}:/p/{profileId}"}
	if mountPath != "":
		mount: Path = Path(mountPath).expanduser().resolve(strict=False)
		hrefs.add(mount.as_uri())
	return hrefs


def upsertProfilePlace(profile: config.Profile) -> None:
	href: str = placeHref(profile)
	known: set[str] = _placeHrefs(profile.id, profile.mountPath)
	placeId: str = f"FastOneDrivePlace-{profile.id}"
	path: Path = Path.home() / ".local" / "share" / "user-places.xbel"
	if not path.is_file():
		raise FastOneDriveError(f"Locais do Dolphin não encontrados: {path}")
	ET.register_namespace("bookmark", _BOOKMARK_NS)
	ET.register_namespace("kdepriv", "http://www.kde.org/kdepriv")
	ET.register_namespace("mime", "http://www.freedesktop.org/standards/shared-mime-info")
	tree = ET.parse(path)
	root: ET.Element | None = tree.getroot()
	if root is None:
		raise FastOneDriveError("user-places.xbel sem elemento xbel")
	updated: bool = False
	for bookmark in list(root.findall("bookmark")):
		current: str = bookmark.get("href") or ""
		bookmarkId: str = _bookmarkId(bookmark)
		legacyRoot: bool = profile.legacy and _isLegacyPlace(current, bookmarkId)
		if current.rstrip("/") in known or bookmarkId == placeId or legacyRoot:
			if updated:
				# Um só Local por perfil: o marcador antigo do protocolo e o da pasta
				# FUSE fundem-se no primeiro.
				root.remove(bookmark)
				continue
			bookmark.set("href", href)
			_setBookmarkTitle(bookmark, profile.name)
			_setBookmarkId(bookmark, placeId)
			_setBookmarkIcon(bookmark, "fastonedrive")
			updated = True
	if not updated:
		root.append(_newBookmark(href, profile.name, placeId))
	_writePlaces(path, root)


def _setBookmarkIcon(bookmark: ET.Element, iconName: str) -> None:
	info: ET.Element | None = bookmark.find("info")
	if info is None:
		return
	for metadata in info.findall("metadata"):
		found: ET.Element | None = metadata.find(_ICON_TAG)
		if found is not None:
			found.set("name", iconName)
			return


def removeProfilePlace(profileId: str) -> None:
	path: Path = Path.home() / ".local" / "share" / "user-places.xbel"
	if not path.is_file():
		return
	href: str = f"{config.KIO_SCHEME}:/p/{profileId}/"
	placeId: str = f"FastOneDrivePlace-{profileId}"
	ET.register_namespace("bookmark", _BOOKMARK_NS)
	ET.register_namespace("kdepriv", "http://www.kde.org/kdepriv")
	ET.register_namespace("mime", "http://www.freedesktop.org/standards/shared-mime-info")
	tree = ET.parse(path)
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


def _unhideMountInHome(mountPath: Path) -> None:
	mount: Path = mountPath.expanduser().resolve(strict=False)
	home: Path = config.homeDir().resolve()
	if mount.parent != home:
		return
	hiddenPath: Path = home / ".hidden"
	if not hiddenPath.is_file():
		return
	lines: list[str] = hiddenPath.read_text(encoding="utf-8").splitlines()
	kept: list[str] = [line for line in lines if line.strip() != mount.name]
	if len(kept) == len(lines):
		return
	if len(kept) == 0:
		hiddenPath.unlink(missing_ok=True)
		return
	_replaceText(hiddenPath, "\n".join(kept) + "\n")


def _rewriteKioItemPlaces(path: Path) -> None:
	"""Marcadores fastonedrive:/…/item/X voltam a file://<pasta do perfil>/X.

	Também tira a entrada da montagem FUSE em Dispositivos: a pasta já está nos Locais.
	"""
	ET.register_namespace("bookmark", _BOOKMARK_NS)
	ET.register_namespace("kdepriv", "http://www.kde.org/kdepriv")
	ET.register_namespace("mime", "http://www.freedesktop.org/standards/shared-mime-info")
	tree = ET.parse(path)
	root: ET.Element | None = tree.getroot()
	if root is None:
		return
	mounts: dict[str, Path] = {}
	legacyMount: Path | None = None
	for profile in config.listProfiles():
		if not profile.mountEnabled:
			continue
		mount: Path = Path(profile.mountPath).expanduser().resolve(strict=False)
		mounts[profile.id] = mount
		if profile.legacy:
			legacyMount = mount
	changed: bool = False
	for child in list(root):
		if child.tag == "separator":
			for mount in mounts.values():
				if _isFuseDeviceSeparator(child, str(mount)):
					root.remove(child)
					changed = True
					break
			continue
		if child.tag != "bookmark":
			continue
		href: str = child.get("href") or ""
		local: Path | None = _fileForKioItem(href, mounts, legacyMount)
		if local is None:
			continue
		child.set("href", local.as_uri())
		changed = True
	if changed:
		_writePlaces(path, root)


def _fileForKioItem(href: str, mounts: dict[str, Path], legacyMount: Path | None) -> Path | None:
	prefix: str = f"{config.KIO_SCHEME}:"
	if not href.startswith(prefix):
		return None
	body: str = unquote(href[len(prefix):]).lstrip("/")
	mount: Path | None = legacyMount
	if body.startswith("p/"):
		parts: list[str] = body.split("/", 2)
		if len(parts) < 2:
			return None
		mount = mounts.get(parts[1])
		body = parts[2] if len(parts) > 2 else ""
	if mount is None or not body.startswith("item/"):
		return None
	relative: str = body[len("item/"):].strip("/")
	if relative == "":
		return mount
	return mount / relative


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
		'    <bookmark:icon name="fastonedrive"/>\n'
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
	ET.SubElement(freedesktop, _ICON_TAG, {"name": "fastonedrive"})
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
