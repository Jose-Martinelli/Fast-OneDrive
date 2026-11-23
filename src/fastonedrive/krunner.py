import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.menus import repoBinPath, writeInstalledText
from fastonedrive.hydrate import hydrateRemote
from fastonedrive.mount import fuseItemPath, fuseMountReady
from fastonedrive.notify import notify
from fastonedrive.search import pinnedLocalPath, rowToDict, search

KRUNNER_IFACE: str = "org.kde.krunner1"
MATCH_TYPE_POSSIBLE: int = 50
ACTION_COPY: str = "copy"
ACTION_PIN: str = "pin"


def runKrunner() -> None:
	try:
		from dbus.mainloop.glib import DBusGMainLoop
		import dbus
		import dbus.service
		from gi.repository import GLib
	except ImportError as error:
		raise FastOneDriveError(
			"python-dbus e PyGObject são necessários para o KRunner"
		) from error

	class FastOneDriveRunner(dbus.service.Object):
		activationToken: str

		def __init__(self, bus: dbus.SessionBus, path: str) -> None:
			self.activationToken = ""
			super().__init__(bus, path)

		@dbus.service.method(KRUNNER_IFACE, in_signature="s", out_signature="a(sssida{sv})")
		def Match(self, query: str) -> list[Any]:
			return matchQuery(str(query), dbus)

		@dbus.service.method(KRUNNER_IFACE, in_signature="", out_signature="a(sss)")
		def Actions(self) -> list[tuple[str, str, str]]:
			return [
				(ACTION_COPY, "Copiar", "cloud-download"),
				(ACTION_PIN, "Manter localmente", "pin"),
			]

		@dbus.service.method(KRUNNER_IFACE, in_signature="ss", out_signature="")
		def Run(self, matchId: str, actionId: str) -> None:
			runMatch(str(matchId), str(actionId), self.activationToken)

		@dbus.service.method(KRUNNER_IFACE, in_signature="", out_signature="")
		def Teardown(self) -> None:
			self.activationToken = ""

		@dbus.service.method(KRUNNER_IFACE, in_signature="s", out_signature="")
		def SetActivationToken(self, token: str) -> None:
			self.activationToken = str(token)

	DBusGMainLoop(set_as_default=True)
	bus = dbus.SessionBus()
	busName = dbus.service.BusName(config.KRUNNER_BUS_NAME, bus)
	runner = FastOneDriveRunner(bus, config.KRUNNER_OBJECT_PATH)
	GLib.MainLoop().run()
	_ = (busName, runner)


def matchQuery(query: str, dbusMod: Any | None = None) -> list[Any]:
	needle: str = query.strip()
	if len(needle) < config.KRUNNER_MIN_LETTERS:
		return []
	matches: list[Any] = []
	lower: str = needle.lower()
	for profile in config.listProfiles():
		try:
			config.activateProfile(profile.id)
			rows = search(needle, config.KRUNNER_MATCH_LIMIT)
		except FastOneDriveError:
			continue
		for row in rows:
			matches.append(_matchFromRow(row, lower, dbusMod, profile))
	matches.sort(key=lambda item: float(item[4]), reverse=True)
	return matches[: config.KRUNNER_MATCH_LIMIT]


def runMatch(matchId: str, actionId: str, activationToken: str = "") -> None:
	profileId: str
	itemPath: str
	profileId, itemPath = _splitMatchId(matchId)
	if itemPath == "" and profileId == "":
		return
	if profileId != "":
		config.activateProfile(profileId)
	else:
		config.ensureActiveProfile()
	remote: str = config.remotePath(itemPath)
	if actionId == ACTION_COPY:
		_spawnCli(["copy", "--notify", "--pick-dest", remote])
		return
	if actionId == ACTION_PIN:
		_spawnCli(["pin", "--notify", remote])
		return
	try:
		openIndexedPath(itemPath, activationToken)
	except FastOneDriveError as error:
		notify("Fast-OneDrive", str(error))


def openIndexedPath(itemPath: str, activationToken: str = "") -> None:
	fusePath: Path | None = fuseItemPath(itemPath)
	if itemPath == "":
		if fusePath is not None:
			_openLocal(fusePath, activationToken, "dolphin")
			return
		_openDolphinUrl(_profileRootUrl(), activationToken)
		return
	entry = _entry(itemPath)
	if entry is None:
		raise FastOneDriveError(f"item não encontrado no índice: {itemPath}")
	data: dict[str, Any] = rowToDict(entry)
	if fusePath is not None:
		_openLocal(fusePath, activationToken, "dolphin" if bool(data["isDir"]) else "xdg-open")
		return
	if bool(data["pinned"]) and bool(data["isDir"]):
		local: Path = pinnedLocalPath(itemPath)
		if local.exists():
			_openLocal(local, activationToken, "dolphin")
			return
	if bool(data["isDir"]):
		_openKioItem(itemPath, activationToken)
		return
	localPath: str = str(data.get("localPath") or "")
	if localPath != "":
		_openLocal(Path(localPath), activationToken, "xdg-open")
		return
	size: int = int(data["size"])
	if size > config.openMaxBytes():
		if not fuseMountReady():
			raise FastOneDriveError(
				"Activa o FUSE do Fast-OneDrive para abrir este ficheiro."
			)
		_openLocal(config.mountPath() / itemPath, activationToken, "xdg-open")
		return
	hydrated: Path = hydrateRemote(config.remotePath(itemPath))
	_openLocal(hydrated, activationToken, "xdg-open")


def installRunner() -> Path:
	config.ensureUserConfig()
	binary: Path = repoBinPath()
	if not binary.is_file():
		raise FastOneDriveError(f"CLI não encontrado: {binary}")
	pluginDir: Path = Path.home() / ".local" / "share" / "krunner" / "dbusplugins"
	pluginDir.mkdir(parents=True, exist_ok=True)
	template: Path = (
		Path(__file__).resolve().parent.parent.parent
		/ "krunner"
		/ "plasma-runner-fastonedrive.desktop"
	)
	if not template.is_file():
		raise FastOneDriveError(f"template KRunner não encontrado: {template}")
	pluginPath: Path = pluginDir / "plasma-runner-fastonedrive.desktop"
	writeInstalledText(
		pluginPath,
		template.read_text(encoding="utf-8").replace("@BIN@", str(binary)),
		0o644,
	)
	wrapperDir: Path = Path.home() / ".local" / "bin"
	wrapperDir.mkdir(parents=True, exist_ok=True)
	wrapper: Path = wrapperDir / "fastonedrive-krunner"
	writeInstalledText(
		wrapper,
		f"#!/bin/sh\nexec \"{binary}\" krunner\n",
		0o755,
	)
	dbusDir: Path = Path.home() / ".local" / "share" / "dbus-1" / "services"
	dbusDir.mkdir(parents=True, exist_ok=True)
	dbusPath: Path = dbusDir / f"{config.KRUNNER_BUS_NAME}.service"
	writeInstalledText(
		dbusPath,
		"[D-BUS Service]\n"
		f"Name={config.KRUNNER_BUS_NAME}\n"
		f"Exec={wrapper}\n",
		0o644,
	)
	_rebuildSycoca()
	return pluginPath


def _splitMatchId(matchId: str) -> tuple[str, str]:
	if "\t" not in matchId:
		return "", matchId
	profileId: str
	itemPath: str
	profileId, itemPath = matchId.split("\t", 1)
	return profileId, itemPath


def _profileRootUrl() -> str:
	profile: config.Profile | None = config.activeProfile()
	if profile is None:
		return f"{config.KIO_SCHEME}:/"
	return f"{config.KIO_SCHEME}:/p/{profile.id}/"


def _kioItemUrl(itemPath: str) -> str:
	quoted: str = quote(itemPath, safe="/")
	profile: config.Profile | None = config.activeProfile()
	if profile is None:
		return f"{config.KIO_SCHEME}:/item/{quoted}"
	return f"{config.KIO_SCHEME}:/p/{profile.id}/item/{quoted}"


def _matchFromRow(
	row: Any,
	lowerQuery: str,
	dbusMod: Any | None,
	profile: config.Profile,
) -> Any:
	itemPath: str = str(row["path"])
	matchId: str = f"{profile.id}\t{itemPath}"
	name: str = str(row["name"])
	isDir: bool = bool(row["isDir"])
	size: int = int(row["size"])
	large: bool = (not isDir) and size > config.openMaxBytes()
	icon: str = "folder" if isDir else "text-x-generic"
	relevance: float = 1.0 if name.lower().startswith(lowerQuery) else 0.7
	subtext: str = f"{profile.name} — {config.remotePath(itemPath)}"
	if bool(row["pinned"]):
		subtext = f"Mantido localmente — {subtext}"
	actions: list[str] = [ACTION_COPY, ACTION_PIN] if large else []
	props: dict[str, Any] = {
		"subtext": subtext,
		"category": "OneDrive",
		"actions": actions,
	}
	if dbusMod is None:
		return (matchId, name, icon, MATCH_TYPE_POSSIBLE, relevance, props)
	dbusActions = dbusMod.Array(
		[dbusMod.String(item) for item in actions],
		signature="s",
	)
	dbusProps = dbusMod.Dictionary(
		{
			dbusMod.String("subtext"): dbusMod.String(subtext),
			dbusMod.String("category"): dbusMod.String("OneDrive"),
			dbusMod.String("actions"): dbusActions,
		},
		signature="sv",
	)
	return (
		dbusMod.String(matchId),
		dbusMod.String(name),
		dbusMod.String(icon),
		dbusMod.Int32(MATCH_TYPE_POSSIBLE),
		dbusMod.Double(relevance),
		dbusProps,
	)


def _entry(itemPath: str) -> Any:
	from fastonedrive.index import getEntry
	return getEntry(itemPath)


def _openKioItem(itemPath: str, activationToken: str) -> None:
	_openDolphinUrl(_kioItemUrl(itemPath), activationToken)


def _openDolphinUrl(url: str, activationToken: str) -> None:
	env: dict[str, str] = os.environ.copy()
	pluginRoot: str = str(Path.home() / ".local" / "lib" / "qt6" / "plugins")
	current: str = env.get("QT_PLUGIN_PATH", "")
	if pluginRoot not in current.split(":"):
		env["QT_PLUGIN_PATH"] = pluginRoot if current == "" else f"{pluginRoot}:{current}"
	if activationToken != "":
		env["XDG_ACTIVATION_TOKEN"] = activationToken
	binary: str | None = shutil.which("dolphin")
	if binary is None:
		raise FastOneDriveError("dolphin não encontrado")
	subprocess.Popen(
		[binary, url],
		start_new_session=True,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		env=env,
	)


def _spawnCli(args: list[str]) -> None:
	binary: Path = repoBinPath()
	subprocess.Popen(
		[str(binary), *args],
		start_new_session=True,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)


def _openLocal(path: Path, activationToken: str, program: str) -> None:
	env: dict[str, str] = os.environ.copy()
	if activationToken != "":
		env["XDG_ACTIVATION_TOKEN"] = activationToken
	binary: str | None = shutil.which(program)
	if binary is None:
		raise FastOneDriveError(f"{program} não encontrado")
	subprocess.Popen(
		[binary, str(path)],
		start_new_session=True,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		env=env,
	)


def _rebuildSycoca() -> None:
	binary: str | None = shutil.which("kbuildsycoca6")
	if binary is None:
		binary = shutil.which("kbuildsycoca5")
	if binary is None:
		return
	subprocess.run(
		[binary, "--noincremental"],
		check=False,
		capture_output=True,
	)
