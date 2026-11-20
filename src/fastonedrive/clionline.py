"""Comandos do menu "OneDrive" do Dolphin e da bandeja que falam com o Graph ou
controlam a sincronização: ver online, histórico de versões, pausa e limites."""

import argparse
import shutil
import subprocess
import sys
import time
from datetime import datetime
from typing import Any

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.notify import notify

PAUSE_CHOICES: dict[str, float] = {"2h": 2 * 3600.0, "8h": 8 * 3600.0, "24h": 24 * 3600.0}


def addOnlineCommands(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
	webParser: argparse.ArgumentParser = sub.add_parser(
		"open-web",
		help="Abrir o item no OneDrive na web (Ver online)",
	)
	webParser.add_argument("source", help="Caminho em ~/OneDrive, onedrive:PATH ou fastonedrive:/item/…")
	webParser.add_argument("--notify", action="store_true")
	webParser.add_argument("--print", action="store_true", help="Só escrever o URL")
	webParser.set_defaults(handler=_cmdOpenWeb)

	versionsParser: argparse.ArgumentParser = sub.add_parser(
		"versions",
		help="Histórico de versões de um ficheiro (listar ou restaurar)",
	)
	versionsParser.add_argument("source", help="Caminho em ~/OneDrive, onedrive:PATH ou fastonedrive:/item/…")
	versionsParser.add_argument("--notify", action="store_true", help="Escolher a versão numa janela (kdialog)")
	versionsParser.add_argument("--restore", default="", help="Id da versão a restaurar")
	versionsParser.set_defaults(handler=_cmdVersions)

	pauseParser: argparse.ArgumentParser = sub.add_parser(
		"pause",
		help="Pausar a sincronização de fundo (2h, 8h ou 24h)",
	)
	pauseParser.add_argument("duration", choices=sorted(PAUSE_CHOICES), nargs="?", default="2h")
	pauseParser.set_defaults(handler=_cmdPause)

	resumeParser: argparse.ArgumentParser = sub.add_parser(
		"resume",
		help="Retomar a sincronização",
	)
	resumeParser.set_defaults(handler=_cmdResume)

	doctorParser: argparse.ArgumentParser = sub.add_parser(
		"doctor",
		help="Diagnosticar a integração (mount, estado, plugins do Dolphin, serviços)",
	)
	doctorParser.add_argument("--perf", action="store_true", help="Medir Graph, leitura por intervalos, rclone e SQLite")
	doctorParser.set_defaults(handler=_cmdDoctor)

	recycleParser: argparse.ArgumentParser = sub.add_parser(
		"open-recycle",
		help="Abrir a Reciclagem do OneDrive no browser (ficheiros apagados, 30 dias)",
	)
	recycleParser.set_defaults(handler=_cmdOpenRecycle)

	discardParser: argparse.ArgumentParser = sub.add_parser(
		"discard-local",
		help="Descartar a cópia local e o estado de sync de itens (não mexe na nuvem)",
	)
	discardParser.add_argument("sources", nargs="+", help="Caminho em ~/OneDrive ou onedrive:PATH")
	discardParser.set_defaults(handler=_cmdDiscardLocal)

	folderParser: argparse.ArgumentParser = sub.add_parser(
		"open-folder",
		help="Abrir a pasta OneDrive no Dolphin (com um item seleccionado, opcional)",
	)
	folderParser.add_argument("item", nargs="?", default="", help="Caminho do item no OneDrive")
	folderParser.set_defaults(handler=_cmdOpenFolder)

	controlParser: argparse.ArgumentParser = sub.add_parser(
		"sync-control",
		help="Mostrar ou mudar limite de banda, pausa em rede limitada e intervalo do poll",
	)
	controlParser.add_argument("--bwlimit", default=None, help="Ex.: 10M, 10M:2M (envio:descarga), off")
	controlParser.add_argument("--pause-on-metered", choices=["on", "off"], default=None)
	controlParser.add_argument("--poll-seconds", type=int, default=None)
	controlParser.set_defaults(handler=_cmdSyncControl)


def _itemPathFrom(source: str) -> str:
	from fastonedrive.paths import remoteItemPath, toRemotePath

	return remoteItemPath(toRemotePath(source))


def _openUrl(url: str) -> None:
	opener: str | None = shutil.which("xdg-open") or shutil.which("kde-open")
	if opener is None:
		raise FastOneDriveError("xdg-open não encontrado")
	subprocess.Popen(
		[opener, url],
		start_new_session=True,
		stdin=subprocess.DEVNULL,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)


def _cmdOpenWeb(args: argparse.Namespace) -> int:
	from fastonedrive.graphitems import itemWebUrl

	try:
		url: str = itemWebUrl(_itemPathFrom(str(args.source)))
	except FastOneDriveError as error:
		if bool(args.notify):
			notify("OneDrive", f"Não foi possível abrir online: {error}")
		raise
	print(url)
	if not bool(args.print):
		_openUrl(url)
	return 0


def _versionLabel(version: dict[str, Any]) -> str:
	stamp: str = str(version.get("lastModifiedDateTime") or "")
	when: str = stamp
	try:
		when = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%d/%m/%Y %H:%M")
	except ValueError:
		pass
	size: int = int(version.get("size") or 0)
	who: str = ""
	user: Any = (version.get("lastModifiedBy") or {}).get("user") if isinstance(version.get("lastModifiedBy"), dict) else None
	if isinstance(user, dict):
		who = str(user.get("displayName") or "")
	label: str = f"{when}  ·  {_humanSize(size)}"
	if who != "":
		label += f"  ·  {who}"
	return label


def _humanSize(size: int) -> str:
	value: float = float(size)
	for unit in ("B", "KB", "MB", "GB"):
		if value < 1024 or unit == "GB":
			return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
		value /= 1024
	return f"{size} B"


def _cmdVersions(args: argparse.Namespace) -> int:
	from fastonedrive.graphitems import listVersions, restoreVersion

	itemPath: str = _itemPathFrom(str(args.source))
	if itemPath == "":
		raise FastOneDriveError("escolha um ficheiro")
	restoreId: str = str(args.restore or "")
	if restoreId == "" and bool(args.notify):
		restoreId = _pickVersion(itemPath, listVersions(itemPath))
		if restoreId == "":
			return 0
	if restoreId != "":
		restoreVersion(itemPath, restoreId)
		_afterRestore(itemPath)
		if bool(args.notify):
			notify("OneDrive", f"Versão restaurada: {itemPath.rsplit('/', 1)[-1]}")
		print(f"restaurada a versão {restoreId}")
		return 0
	for version in listVersions(itemPath):
		print(f"{version.get('id')}\t{_versionLabel(version)}")
	return 0


def _pickVersion(itemPath: str, versions: list[dict[str, Any]]) -> str:
	kdialog: str | None = shutil.which("kdialog")
	if kdialog is None:
		raise FastOneDriveError("kdialog não encontrado")
	name: str = itemPath.rsplit("/", 1)[-1]
	if len(versions) <= 1:
		subprocess.run(
			[kdialog, "--title", "Histórico de versões", "--msgbox", f"{name} só tem a versão actual."],
			check=False,
		)
		return ""
	menu: list[str] = []
	for index, version in enumerate(versions):
		label: str = _versionLabel(version)
		if index == 0:
			label += "  (actual)"
		menu.extend([str(version.get("id") or ""), label])
	result: subprocess.CompletedProcess[str] = subprocess.run(
		[
			kdialog,
			"--title",
			f"Histórico de versões — {name}",
			"--menu",
			"Escolha a versão a restaurar (a actual fica no histórico):",
			*menu,
		],
		check=False,
		capture_output=True,
		text=True,
	)
	chosen: str = result.stdout.strip()
	if result.returncode != 0 or chosen == "" or chosen == str(versions[0].get("id") or ""):
		return ""
	confirm: subprocess.CompletedProcess[bytes] = subprocess.run(
		[kdialog, "--title", "Restaurar versão", "--yesno", f"Restaurar esta versão de {name}?"],
		check=False,
	)
	return chosen if confirm.returncode == 0 else ""


def _afterRestore(itemPath: str) -> None:
	"""O conteúdo mudou na nuvem: o índice e a cópia local apanham-no no próximo ciclo."""
	from fastonedrive.index import parentPath, refreshParentListing
	from fastonedrive.notify import notifyDirChanged

	try:
		refreshParentListing(parentPath(itemPath))
	except FastOneDriveError:
		pass
	notifyDirChanged(itemPath)


def _cmdPause(args: argparse.Namespace) -> int:
	from fastonedrive.synccontrol import pauseFor

	control = pauseFor(PAUSE_CHOICES[str(args.duration)])
	until: str = time.strftime("%H:%M", time.localtime(control.pausedUntil))
	message: str = f"Sincronização em pausa até às {until}"
	notify("OneDrive", message)
	print(message)
	return 0


def _cmdResume(args: argparse.Namespace) -> int:
	from fastonedrive.synccontrol import resume

	resume()
	notify("OneDrive", "Sincronização retomada")
	print("sincronização retomada")
	_kickSync()
	return 0


def _kickSync() -> None:
	try:
		binary: str = config.cliBin()
	except FastOneDriveError:
		return
	subprocess.Popen(
		[binary, "sync-pins"],
		start_new_session=True,
		stdin=subprocess.DEVNULL,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)


def recycleBinUrl() -> str:
	from fastonedrive.graph import _remoteField

	driveType: str = ""
	try:
		driveType = _remoteField("drive_type").lower()
	except FastOneDriveError:
		driveType = ""
	if driveType in ("business", "documentlibrary"):
		from fastonedrive.graphitems import itemWebUrl

		root: str = itemWebUrl("")
		# https://empresa-my.sharepoint.com/personal/x/Documents → …/personal/x/_layouts/15/RecycleBin.aspx
		base: str = root.split("/Documents", 1)[0] if "/Documents" in root else root.rstrip("/")
		return base + "/_layouts/15/RecycleBin.aspx"
	return "https://onedrive.live.com/?v=recyclebin"


def _cmdOpenRecycle(args: argparse.Namespace) -> int:
	url: str = recycleBinUrl()
	print(url)
	_openUrl(url)
	return 0


def _cmdDiscardLocal(args: argparse.Namespace) -> int:
	"""Para recuperar de uma cópia local estragada: apaga-a do disco, limpa a linha de
	base e o estado (erro, a sincronizar…) e volta a listar a pasta a partir da nuvem."""
	from fastonedrive.chunks import deleteChunkRow
	from fastonedrive.index import clearItemBaseline, getEntry, parentPath, refreshParentListing, setPinned, setSyncState
	from fastonedrive.notify import notifyDirChanged

	from fastonedrive.search import cacheLocalPath, pinnedLocalPath

	for source in [str(item) for item in args.sources]:
		itemPath: str = _itemPathFrom(source)
		if itemPath == "":
			raise FastOneDriveError("não é possível descartar a raiz")
		entry = getEntry(itemPath)
		if entry is not None and bool(entry["isDir"]):
			raise FastOneDriveError(f"{itemPath} é uma pasta; indique ficheiros")
		for local in (cacheLocalPath(itemPath), pinnedLocalPath(itemPath)):
			if local.is_file() or local.is_symlink():
				local.unlink()
		deleteChunkRow(itemPath)
		if entry is not None:
			if bool(entry["pinned"]):
				setPinned(itemPath, False)
			clearItemBaseline(itemPath)
			setSyncState(itemPath, "", "")
		try:
			from fastonedrive.graphitems import fastRemoteStat
			from fastonedrive.index import deleteIndexTree

			if fastRemoteStat(config.remotePath(itemPath)) is None:
				# Já não existe na nuvem e não tem cópia local: a linha sai do índice.
				deleteIndexTree(itemPath)
			else:
				refreshParentListing(parentPath(itemPath))
		except FastOneDriveError:
			pass
		notifyDirChanged(itemPath)
		print(f"descartado localmente: {itemPath}")
	return 0


def _cmdOpenFolder(args: argparse.Namespace) -> int:
	from pathlib import Path

	mount: Path = config.mountPath()
	item: str = str(args.item or "").strip().strip("/")
	target: Path = mount / item if item != "" else mount
	dolphin: str | None = shutil.which("dolphin")
	command: list[str]
	if dolphin is not None and item != "":
		command = [dolphin, "--select", str(target)]
	elif dolphin is not None:
		command = [dolphin, str(target)]
	else:
		_openUrl(target.as_uri())
		return 0
	subprocess.Popen(
		command,
		start_new_session=True,
		stdin=subprocess.DEVNULL,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	return 0


def _mountTypes() -> dict[str, str]:
	"""Pasta montada → tipo, lido de /proc/self/mountinfo (como o plugin do Dolphin faz)."""
	found: dict[str, str] = {}
	try:
		lines: list[str] = open("/proc/self/mountinfo", encoding="utf-8").read().splitlines()
	except OSError:
		return found
	for line in lines:
		head, sep, tail = line.partition(" - ")
		if sep == "":
			continue
		fields: list[str] = head.split(" ")
		if len(fields) < 5:
			continue
		point: str = fields[4].encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")
		found[point] = tail.split(" ")[0]
	return found


def _cmdDoctor(args: argparse.Namespace) -> int:
	import os
	from pathlib import Path

	problems: int = 0

	def report(ok: bool, label: str, detail: str = "") -> None:
		nonlocal problems
		if not ok:
			problems += 1
		mark: str = "ok " if ok else "ERRO"
		print(f"[{mark}] {label}" + (f": {detail}" if detail != "" else ""))

	mounts: dict[str, str] = _mountTypes()
	profiles: list[config.Profile] = config.listProfiles()
	report(len(profiles) > 0, "perfis", ", ".join(f"{p.id} → {p.mountPath}" for p in profiles) or "nenhum")
	for profile in profiles:
		mount: Path = Path(profile.mountPath).expanduser()
		resolved: str = str(mount.resolve(strict=False))
		kind: str = mounts.get(resolved, mounts.get(str(mount), ""))
		unit: str = f"fast-onedrive-mount@{profile.id}.service"
		active: subprocess.CompletedProcess[str] = subprocess.run(
			["systemctl", "--user", "is-active", unit], check=False, capture_output=True, text=True
		)
		report(active.stdout.strip() == "active", f"{profile.id}: serviço {unit}", active.stdout.strip() or "?")
		unitFile: Path = Path.home() / ".config" / "systemd" / "user" / unit
		unitText: str = unitFile.read_text(encoding="utf-8") if unitFile.is_file() else ""
		current: bool = "Type=notify" in unitText and "KillMode=process" in unitText
		report(current, f"{profile.id}: unidade actual do mount", "" if current else "faça mount off && mount on")
		report(kind == "fuse.fastonedrive", f"{profile.id}: montagem {resolved}", kind or "não montada")
		if kind == "":
			continue
		try:
			state: bytes = os.getxattr(resolved, "user.fastonedrive.state")
			report(True, f"{profile.id}: estado (xattr) da raiz", state.decode("utf-8", errors="replace"))
		except OSError as error:
			import errno as errnoModule

			if error.errno == errnoModule.ENODATA:
				report(True, f"{profile.id}: estado (xattr) da raiz", "sem pendentes")
			else:
				report(False, f"{profile.id}: estado (xattr)", f"{error.strerror} — o FUSE em execução é antigo? (mount off && mount on)")
		sample: Path | None = None
		try:
			for child in mount.iterdir():
				if child.is_file():
					sample = child
					break
		except OSError:
			sample = None
		if sample is not None:
			try:
				value: bytes = os.getxattr(str(sample), "user.fastonedrive.state")
				report(True, f"{profile.id}: estado de {sample.name}", value.decode("utf-8", errors="replace"))
			except OSError as error:
				report(False, f"{profile.id}: estado de {sample.name}", error.strerror or str(error))
	root: Path = Path.home() / ".local" / "lib" / "qt6" / "plugins" / "kf6"
	for relative in ("overlayicon/fastonedriveoverlay.so", "kfileitemaction/fastonedriveactions.so", "kio/kio_fastonedrive.so"):
		report((root / relative).is_file(), f"plugin {relative}")
	pluginPath: str = os.environ.get("QT_PLUGIN_PATH", "")
	report(str(root.parent) in pluginPath.split(":"), "QT_PLUGIN_PATH inclui ~/.local/lib/qt6/plugins", pluginPath or "vazio (sessão iniciada antes do install-kio? termine a sessão)")
	menurc: Path = Path.home() / ".config" / "kservicemenurc"
	disabled: bool = menurc.is_file() and "fastonedriveactions=false" in menurc.read_text(encoding="utf-8", errors="replace")
	report(not disabled, "submenu OneDrive activo em Configurar Dolphin → Menu de contexto")
	report(shutil.which("busctl") is not None, "busctl (sinais de estado para os emblemas)")
	from fastonedrive.synccontrol import pauseReason

	reason: str = pauseReason()
	report(True, "sincronização", reason or "activa")
	if bool(getattr(args, "perf", False)):
		for profile in profiles:
			problems += _doctorPerf(profile)
	print("Tudo certo." if problems == 0 else f"{problems} problema(s).")
	return 0 if problems == 0 else 1


def _doctorPerf(profile: config.Profile) -> int:
	import sqlite3

	from fastonedrive.disk import indexIsRunning
	from fastonedrive.index import connectDb

	config.activateProfile(profile.id)
	problems: int = 0

	def timed(label: str, action: Any, slowAfter: float) -> Any:
		nonlocal problems
		started: float = time.monotonic()
		try:
			result: Any = action()
		except Exception as error:  # noqa: BLE001 — diagnóstico mostra tudo
			problems += 1
			print(f"[ERRO] {profile.id}: {label}: {type(error).__name__}: {error}")
			return None
		spent: float = time.monotonic() - started
		mark: str = "ok " if spent <= slowAfter else "LENTO"
		if spent > slowAfter:
			problems += 1
		print(f"[{mark}] {profile.id}: {label}: {spent:.2f} s")
		return result

	running: bool = indexIsRunning()
	print(f"[{'info' if not running else 'AVISO'}] {profile.id}: indexação a correr agora: {'sim' if running else 'não'}")

	def lockWait() -> None:
		connection: sqlite3.Connection = connectDb()
		try:
			connection.execute("BEGIN IMMEDIATE")
			connection.rollback()
		finally:
			connection.close()

	timed("esperar pelo cadeado de escrita do SQLite", lockWait, 1.0)
	connection: sqlite3.Connection = connectDb()
	try:
		row = connection.execute(
			"SELECT path, size FROM files WHERE isDir = 0 AND size BETWEEN ? AND ? "
			"AND syncState = '' ORDER BY size DESC LIMIT 1",
			(8 * 1024 * 1024, 2 * 1024 * 1024 * 1024),
		).fetchone()
	finally:
		connection.close()
	if row is None:
		print(f"[info] {profile.id}: sem ficheiro ≥ 8 MiB no índice para medir a leitura")
		return problems
	itemPath: str = str(row[0])
	print(f"[info] {profile.id}: ficheiro de teste: {itemPath} ({int(row[1]) // (1024 * 1024)} MiB)")
	from fastonedrive.graph import accessToken
	from fastonedrive.graphitems import forgetDownloadUrl, graphStat
	from fastonedrive.rangeread import _graphRange
	from fastonedrive.rclone import remoteStat

	timed("token Graph", accessToken, 3.0)
	forgetDownloadUrl(itemPath)
	timed("metadados pelo Graph (graphStat)", lambda: graphStat(itemPath), 2.0)
	chunk: int = 4 * 1024 * 1024
	started: float = time.monotonic()
	data: bytes | None = timed("ler 4 MiB por HTTP Range", lambda: _graphRange(itemPath, 0, chunk), 5.0)
	if data is not None:
		rate: float = len(data) / max(0.001, time.monotonic() - started) / (1024 * 1024)
		print(f"[info] {profile.id}: velocidade de um bloco: {rate:.1f} MiB/s")
	timed("metadados pelo rclone (lsjson --stat)", lambda: remoteStat(config.remotePath(itemPath)), 8.0)
	from fastonedrive.log import readLogTail

	tail: str = readLogTail(400)
	for marker in ("Graph indisponível", "leitura Graph falhou", "leitura HTTP falhou", "download Graph falhou", "database is locked"):
		count: int = tail.count(marker)
		if count > 0:
			problems += 1
			print(f"[AVISO] {profile.id}: log recente tem {count}× «{marker}»")
	return problems


def _cmdSyncControl(args: argparse.Namespace) -> int:
	from fastonedrive.synccontrol import MIN_POLL_SECONDS, load, pauseReason, save

	control = load()
	changed: bool = False
	if args.bwlimit is not None:
		control.bwLimit = str(args.bwlimit)
		changed = True
	if args.pause_on_metered is not None:
		control.pauseOnMetered = str(args.pause_on_metered) == "on"
		changed = True
	if args.poll_seconds is not None:
		control.remotePollSeconds = max(MIN_POLL_SECONDS, int(args.poll_seconds))
		changed = True
	if changed:
		save(control)
		control = load()
	reason: str = pauseReason(control)
	print(f"limite de banda:\t{control.bwLimit or 'sem limite'}")
	print(f"pausa em rede limitada:\t{'sim' if control.pauseOnMetered else 'não'}")
	print(f"poll da nuvem:\t{control.remotePollSeconds} s")
	print(f"estado:\t{reason or 'a sincronizar'}")
	return 0


if __name__ == "__main__":
	sys.exit(0)
