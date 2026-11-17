import argparse
import json
import sys
from collections.abc import Callable
from typing import Any, Sequence

from fastonedrive import FastOneDriveError, __version__
from fastonedrive import config
from fastonedrive.copy import copyDriveToDrive, copyRemoteToLocal
from fastonedrive.hydrate import hydrateRemote
from fastonedrive.index import indexProgress, indexStats, rebuildIndex, runParentRefresh
from fastonedrive.kioinstall import installKio, runDolphinSearch
from fastonedrive.krunner import installRunner, openIndexedPath, runKrunner
from fastonedrive.log import LOG_TAIL_LINES, logError, logException, logInfo, readLogTail
from fastonedrive.menus import autostartDesktopPath, installMenus, pickDestDir, settingsDesktopPath
from fastonedrive.mutate import (
	mkdirRemotePath,
	moveRemotePath,
	putLocalToRemote,
	remoteExists,
	removeRemotePath,
	stageLocalWrite,
)
from fastonedrive.notify import NotifyJob, notify, runNotified
from fastonedrive.paths import isDriveSource, isRemotePath, remoteItemPath, toRemotePath
from fastonedrive.pin import freeLocalSpace, pinRemote, unpinRemote
from fastonedrive.rclone import (
	armParentDeathSignal,
	linkRemote,
	parseRcloneTransfer,
	rcloneBin,
	rcloneVersion,
	remoteAbout,
)
from fastonedrive.search import chmodItem, itemToDict, listEntries, rowToDict, search
from fastonedrive.settings import runSettingsWindow
from fastonedrive.syncpins import syncPins


def main() -> None:
	raise SystemExit(run(sys.argv[1:]))


def run(argv: Sequence[str] | None = None) -> int:
	raw: list[str] = _endDashPositionals(list(sys.argv[1:] if argv is None else argv))
	parser: argparse.ArgumentParser = _buildParser()
	args: argparse.Namespace = parser.parse_args(raw)
	command: str = str(getattr(args, "command", "") or "")
	if command != "refresh-parent":
		armParentDeathSignal()
	if command != "log":
		logInfo(f"cli {command}", extra={"argv": raw})
	try:
		_prepareProfile(args)
		handler = args.handler
		return int(handler(args))
	except FastOneDriveError as error:
		logError(str(error), extra={"command": command, "argv": raw})
		print(error, file=sys.stderr)
		return 1
	except Exception as error:
		logException(error, extra={"command": command, "argv": raw})
		print(error, file=sys.stderr)
		return 1


def _buildParser() -> argparse.ArgumentParser:
	parser: argparse.ArgumentParser = argparse.ArgumentParser(
		prog="fastonedrive",
		description="Índice, busca e cópia OneDrive via rclone (jobs sem FUSE)",
	)
	parser.add_argument(
		"--version",
		action="version",
		version=f"%(prog)s {__version__}",
	)
	parser.add_argument(
		"--profile",
		default="",
		help="Id do perfil OneDrive",
	)
	sub: argparse._SubParsersAction[argparse.ArgumentParser] = parser.add_subparsers(
		dest="command",
		required=True,
	)

	indexParser: argparse.ArgumentParser = sub.add_parser(
		"index",
		help="Indexar o Drive (incremental, em lotes; a pasta Places não espera)",
	)
	indexParser.add_argument(
		"path",
		nargs="?",
		default=None,
		help="Remote rclone (omissão: o remote das definições)",
	)
	indexParser.set_defaults(handler=_cmdIndex)

	searchParser: argparse.ArgumentParser = sub.add_parser(
		"search",
		help="Buscar nome ou caminho no índice",
	)
	searchParser.add_argument("query", help="Trecho do nome ou caminho")
	searchParser.add_argument(
		"--limit",
		type=int,
		default=config.SEARCH_DEFAULT_LIMIT,
		help="Máximo de resultados (0 = todos)",
	)
	searchParser.add_argument(
		"--json",
		action="store_true",
		help="Saída JSON para o worker KIO",
	)
	searchParser.set_defaults(handler=_cmdSearch)

	itemParser: argparse.ArgumentParser = sub.add_parser(
		"item",
		help="Metadados de um path do índice (JSON)",
	)
	itemParser.add_argument("path", help="path no índice, onedrive:PATH ou fastonedrive:/item/…")
	itemParser.set_defaults(handler=_cmdItem)

	childrenParser: argparse.ArgumentParser = sub.add_parser(
		"children",
		help="Listar filhos da pasta (JSON; remote um nível + upsert)",
	)
	childrenParser.add_argument(
		"parent",
		nargs="?",
		default="",
		help="path pai (omissão: raiz do índice)",
	)
	childrenParser.set_defaults(handler=_cmdChildren)

	refreshParentParser: argparse.ArgumentParser = sub.add_parser(
		"refresh-parent",
		help="Actualizar uma pasta do índice a partir da nuvem",
	)
	refreshParentParser.add_argument(
		"parent",
		nargs="?",
		default="",
		help="path pai (omissão: raiz do índice)",
	)
	refreshParentParser.set_defaults(handler=_cmdRefreshParent)

	dolphinSearchParser: argparse.ArgumentParser = sub.add_parser(
		"dolphin-search",
		help="Pedir termo (kdialog) e abrir a busca no Dolphin",
	)
	dolphinSearchParser.set_defaults(handler=_cmdDolphinSearch)

	installKioParser: argparse.ArgumentParser = sub.add_parser(
		"install-kio",
		help="Compilar e instalar o worker KIO + Local no Dolphin",
	)
	installKioParser.set_defaults(handler=_cmdInstallKio)

	copyParser: argparse.ArgumentParser = sub.add_parser(
		"copy",
		help="Copiar do remote para uma pasta local (rclone multi-thread)",
	)
	copyParser.add_argument(
		"paths",
		nargs="+",
		help="origem(s) e, opcionalmente, pasta destino no fim",
	)
	copyParser.add_argument(
		"--dest",
		default=None,
		help="Pasta local (sem isto, pede com kdialog)",
	)
	copyParser.add_argument(
		"--notify",
		action="store_true",
		help="Notificações Plasma de progresso/erro",
	)
	copyParser.add_argument(
		"--kio-progress",
		action="store_true",
		help="Progresso linha-a-linha para o widget do Dolphin",
	)
	copyParser.add_argument(
		"--pick-dest",
		action="store_true",
		help="Pedir pasta de destino com kdialog",
	)
	copyParser.add_argument("--overwrite", action="store_true")
	copyParser.set_defaults(handler=_cmdCopy)

	pinParser: argparse.ArgumentParser = sub.add_parser(
		"pin",
		help="Manter localmente (pasta pinned/ + flag SQLite)",
	)
	pinParser.add_argument("sources", nargs="+", help="onedrive:PATH ou ~/OneDrive/PATH")
	pinParser.add_argument("--notify", action="store_true")
	pinParser.set_defaults(handler=_cmdPin)

	unpinParser: argparse.ArgumentParser = sub.add_parser(
		"unpin",
		help="Deixar de manter localmente e apagar a cópia em pinned/",
	)
	unpinParser.add_argument("sources", nargs="+", help="onedrive:PATH ou ~/OneDrive/PATH")
	unpinParser.add_argument("--notify", action="store_true")
	unpinParser.set_defaults(handler=_cmdUnpin)

	installParser: argparse.ArgumentParser = sub.add_parser(
		"install-menus",
		help="Instalar menus de contexto do Dolphin",
	)
	installParser.set_defaults(handler=_cmdInstallMenus)

	installRunnerParser: argparse.ArgumentParser = sub.add_parser(
		"install-runner",
		help="Instalar o plugin KRunner (Alt+F2)",
	)
	installRunnerParser.set_defaults(handler=_cmdInstallRunner)

	krunnerParser: argparse.ArgumentParser = sub.add_parser(
		"krunner",
		help="Serviço D-Bus do KRunner (ativação automática)",
	)
	krunnerParser.set_defaults(handler=_cmdKrunner)

	statusParser: argparse.ArgumentParser = sub.add_parser(
		"status",
		help="Mostrar índice, rclone e caminhos",
	)
	statusParser.set_defaults(handler=_cmdStatus)

	logParser: argparse.ArgumentParser = sub.add_parser(
		"log",
		help="Mostrar as últimas linhas do log de erros",
	)
	logParser.add_argument(
		"--limit",
		type=int,
		default=LOG_TAIL_LINES,
		help=f"Linhas a mostrar (omissão: {LOG_TAIL_LINES})",
	)
	logParser.set_defaults(handler=_cmdLog)

	settingsParser: argparse.ArgumentParser = sub.add_parser(
		"settings",
		help="Abrir a janela de definições",
	)
	settingsParser.add_argument(
		"--tray",
		action="store_true",
		help="Iniciar na bandeja (sem janela)",
	)
	settingsParser.set_defaults(handler=_cmdSettings)

	hydrateParser: argparse.ArgumentParser = sub.add_parser(
		"hydrate",
		help="Descarregar para cache/ e devolver o caminho local (não mantém)",
	)
	hydrateParser.add_argument("source", help="onedrive:PATH ou ~/OneDrive/PATH")
	hydrateParser.add_argument("--notify", action="store_true")
	hydrateParser.add_argument(
		"--kio-progress",
		action="store_true",
		help="Progresso linha-a-linha para o widget do Dolphin",
	)
	hydrateParser.set_defaults(handler=_cmdHydrate)

	openUrlParser: argparse.ArgumentParser = sub.add_parser(
		"open-url",
		help="Abrir um URL fastonedrive: ou um caminho do índice",
	)
	openUrlParser.add_argument("url", help="fastonedrive:/item/… ou caminho no índice")
	openUrlParser.set_defaults(handler=_cmdOpenUrl)

	syncParser: argparse.ArgumentParser = sub.add_parser(
		"sync-pins",
		help="Sincronizar pins (nuvem ↔ pinned/) e enviar cache editado",
	)
	syncParser.add_argument("--notify", action="store_true")
	syncParser.add_argument(
		"--path",
		action="append",
		default=None,
		help="Sincronizar só este item (repetível; onedrive:PATH ou path do índice)",
	)
	syncParser.set_defaults(handler=_cmdSyncPins)

	fuseParser: argparse.ArgumentParser = sub.add_parser(
		"fuse",
		help="Montar ~/OneDrive com o FUSE do Fast-OneDrive (primeiro plano)",
	)
	fuseParser.set_defaults(handler=_cmdFuse)

	linkParser: argparse.ArgumentParser = sub.add_parser(
		"link",
		help="Criar ligação de partilha (rclone link)",
	)
	linkParser.add_argument("sources", nargs="+", help="onedrive:PATH ou fastonedrive:/item/…")
	linkParser.add_argument("--notify", action="store_true")
	linkParser.set_defaults(handler=_cmdLink)

	freeParser: argparse.ArgumentParser = sub.add_parser(
		"free-space",
		help="Libertar espaço local (unpin ou apagar cache; não apaga a nuvem)",
	)
	freeParser.add_argument("sources", nargs="+", help="onedrive:PATH ou fastonedrive:/item/…")
	freeParser.add_argument("--notify", action="store_true")
	freeParser.set_defaults(handler=_cmdFreeSpace)

	aboutParser: argparse.ArgumentParser = sub.add_parser(
		"about",
		help="Quota OneDrive (rclone about)",
	)
	aboutParser.set_defaults(handler=_cmdAbout)

	putParser: argparse.ArgumentParser = sub.add_parser(
		"put",
		help="Enviar um ficheiro ou pasta local para o OneDrive",
	)
	putParser.add_argument("local", help="Caminho local")
	putParser.add_argument("dest", help="onedrive:PATH ou fastonedrive:/item/…")
	putParser.add_argument("--notify", action="store_true")
	putParser.add_argument("--kio-progress", action="store_true")
	putParser.add_argument("--overwrite", action="store_true")
	putParser.set_defaults(handler=_cmdPut)

	mkdirParser: argparse.ArgumentParser = sub.add_parser(
		"mkdir",
		help="Criar uma pasta no OneDrive",
	)
	mkdirParser.add_argument("dest", help="onedrive:PATH ou fastonedrive:/item/…")
	mkdirParser.add_argument("--notify", action="store_true")
	mkdirParser.set_defaults(handler=_cmdMkdir)

	rmParser: argparse.ArgumentParser = sub.add_parser(
		"rm",
		help="Apagar no OneDrive (nuvem + índice; não é unpin)",
	)
	rmParser.add_argument("paths", nargs="+", help="onedrive:PATH ou fastonedrive:/item/…")
	rmParser.add_argument("--notify", action="store_true")
	rmParser.add_argument("--file", dest="rmFile", action="store_true")
	rmParser.add_argument("--dir", dest="rmDir", action="store_true")
	rmParser.set_defaults(handler=_cmdRm)

	mvParser: argparse.ArgumentParser = sub.add_parser(
		"mv",
		help="Mover ou renomear no OneDrive",
	)
	mvParser.add_argument("source", help="origem")
	mvParser.add_argument("dest", help="destino")
	mvParser.add_argument("--notify", action="store_true")
	mvParser.add_argument("--overwrite", action="store_true")
	mvParser.set_defaults(handler=_cmdMv)

	chmodParser: argparse.ArgumentParser = sub.add_parser(
		"chmod",
		help="Gravar o modo Unix local (não sobe para a nuvem)",
	)
	chmodParser.add_argument("mode", help="Modo octal, por exemplo 0644")
	chmodParser.add_argument("path", help="onedrive:PATH ou fastonedrive:/item/…")
	chmodParser.set_defaults(handler=_cmdChmod)

	existsParser: argparse.ArgumentParser = sub.add_parser(
		"exists",
		help="JSON se o path existe no remote (para o worker KIO)",
	)
	existsParser.add_argument("path", help="onedrive:PATH ou fastonedrive:/item/…")
	existsParser.set_defaults(handler=_cmdExists)

	menuIfParser: argparse.ArgumentParser = sub.add_parser(
		"menu-if",
		help="Sair 0 só se todos os caminhos forem do Drive (menus Dolphin)",
	)
	menuIfParser.add_argument("paths", nargs="*", help="URLs ou caminhos")
	menuIfParser.set_defaults(handler=_cmdMenuIf)

	mountParser: argparse.ArgumentParser = sub.add_parser(
		"mount",
		help="Ligar ou desligar o FUSE ~/OneDrive do Fast-OneDrive",
	)
	mountParser.add_argument("action", choices=["on", "off", "status"])
	mountParser.set_defaults(handler=_cmdMount)

	return parser


def _endDashPositionals(raw: list[str]) -> list[str]:
	cursor: int = 0
	while cursor < len(raw):
		token: str = raw[cursor]
		if token in ("--help", "-h", "--version"):
			return raw
		if token == "--profile":
			cursor += 2
			continue
		if token.startswith("--profile="):
			cursor += 1
			continue
		break
	if cursor >= len(raw):
		return raw
	command: str = raw[cursor]
	if command == "search":
		return _endSearchQuery(raw, cursor)
	if command in ("item", "children", "refresh-parent"):
		return _endRawPath(raw, cursor)
	return raw


def _endSearchQuery(raw: list[str], commandAt: int) -> list[str]:
	if "--" in raw[commandAt + 1:]:
		return raw
	cursor: int = commandAt + 1
	while cursor < len(raw):
		token: str = raw[cursor]
		if token in ("--json", "--help", "-h"):
			cursor += 1
			continue
		if token == "--limit":
			cursor += 2
			continue
		if token.startswith("--limit="):
			cursor += 1
			continue
		if token.startswith("-"):
			return [*raw[:cursor], "--", *raw[cursor:]]
		return raw
	return raw


def _endRawPath(raw: list[str], commandAt: int) -> list[str]:
	if "--" in raw[commandAt + 1:]:
		return raw
	cursor: int = commandAt + 1
	while cursor < len(raw):
		token: str = raw[cursor]
		if token in ("--help", "-h"):
			return raw
		if token.startswith("-"):
			return [*raw[:cursor], "--", *raw[cursor:]]
		return raw
	return raw


def _prepareProfile(args: argparse.Namespace) -> None:
	from fastonedrive.profiles import profileIdFromText

	config.ensureUserConfig()
	explicit: str = str(getattr(args, "profile", "") or "").strip()
	command: str = str(getattr(args, "command", "") or "")
	inferred: str = ""
	for text in _argumentTexts(args):
		found: str | None = profileIdFromText(text)
		if found is None:
			continue
		if inferred == "":
			inferred = found
			continue
		if inferred != found:
			if command in ("copy", "mv") and explicit == "":
				return
			raise FastOneDriveError("os caminhos pertencem a perfis diferentes")
	if explicit != "" and inferred != "" and explicit != inferred:
		raise FastOneDriveError("o perfil indicado não corresponde ao caminho")
	chosen: str = explicit or inferred
	if chosen != "":
		config.activateProfile(chosen)
		return
	if command == "index" and getattr(args, "path", None) is None:
		return
	if command == "sync-pins" and not getattr(args, "path", None):
		return
	if command in (
		"log",
		"install-kio",
		"install-menus",
		"install-runner",
		"krunner",
		"mount",
		"status",
	):
		return
	if len(config.listProfiles()) == 0 and command == "settings":
		return
	config.ensureActiveProfile()


def _argumentTexts(args: argparse.Namespace) -> list[str]:
	texts: list[str] = []
	for key, value in vars(args).items():
		if key in ("handler", "command", "profile"):
			continue
		if isinstance(value, str):
			texts.append(value)
		elif isinstance(value, list):
			for item in value:
				if isinstance(item, str):
					texts.append(item)
	return texts


def _cmdIndex(args: argparse.Namespace) -> int:
	if args.path is None and config.activeProfile() is None:
		return _indexEveryProfile()
	config.ensureActiveProfile()
	root: str
	if args.path is None:
		root = config.remoteRoot()
	else:
		root = toRemotePath(str(args.path))
	label: str = config.ensureActiveProfile().name
	print(f"Indexando {label} ({root}) …", file=sys.stderr)
	count: int = rebuildIndex(root)
	if count < 0:
		print("Indexação já em curso em segundo plano.", file=sys.stderr)
		return 0
	print(f"Índice atualizado: {count} itens em {config.dbPath()}")
	return 0


def _indexEveryProfile() -> int:
	profiles: list[config.Profile] = config.listProfiles()
	if len(profiles) == 0:
		print("Nenhum perfil OneDrive.", file=sys.stderr)
		return 0
	failed: int = 0
	for profile in profiles:
		try:
			config.activateProfile(profile.id)
			root: str = config.remoteRoot()
			print(f"Indexando {profile.name} ({root}) …", file=sys.stderr)
			count: int = rebuildIndex(root)
			if count < 0:
				print(f"{profile.name}: indexação já em curso.", file=sys.stderr)
				continue
			print(f"{profile.name}: {count} itens em {config.dbPath()}")
		except Exception as error:
			failed += 1
			logError(str(error), extra={"command": "index", "profile": profile.id})
			print(f"{profile.name}: {error}", file=sys.stderr)
	return 1 if failed else 0


def _cmdSearch(args: argparse.Namespace) -> int:
	rows = search(str(args.query), int(args.limit))
	if bool(args.json):
		print(json.dumps([rowToDict(row) for row in rows], ensure_ascii=False))
		return 0
	if not rows:
		print("Nenhum resultado.")
		return 0
	for row in rows:
		kind: str = "pasta" if int(row["isDir"]) == 1 else "arquivo"
		size: str = _formatSize(int(row["size"])) if int(row["isDir"]) == 0 else "-"
		print(f"{kind}\t{size}\t{row['name']}\t{config.remotePath(str(row['path']))}")
	return 0


def _cmdItem(args: argparse.Namespace) -> int:
	print(json.dumps(itemToDict(_itemPathArg(str(args.path))), ensure_ascii=False))
	return 0


def _cmdChildren(args: argparse.Namespace) -> int:
	parent: str = str(args.parent).strip()
	if parent != "":
		parent = _itemPathArg(parent)
	print(json.dumps(listEntries(parent), ensure_ascii=False))
	return 0


def _cmdRefreshParent(args: argparse.Namespace) -> int:
	parent: str = str(args.parent).strip()
	if parent != "":
		parent = _itemPathArg(parent)
	runParentRefresh(parent)
	return 0


def _itemPathArg(raw: str) -> str:
	text: str = raw.strip()
	if text.startswith(config.remoteRoot()) or text.startswith(f"{config.KIO_SCHEME}:"):
		return remoteItemPath(toRemotePath(text))
	return text.lstrip("/")


def _cmdDolphinSearch(args: argparse.Namespace) -> int:
	url: str = runDolphinSearch()
	print(url)
	return 0


def _cmdInstallKio(args: argparse.Namespace) -> int:
	path: str = str(installKio())
	print(f"Worker KIO instalado em {path}")
	print("Local OneDrive na barra do Dolphin. Reabra o Dolphin.")
	print("Busca: barra de endereço fastonedrive:/search/comemora")
	print("ou: ./bin/fastonedrive dolphin-search")
	return 0


def _cmdCopy(args: argparse.Namespace) -> int:
	paths: list[str] = [str(item) for item in args.paths]
	dest: str | None
	sources: list[str]
	sources, dest = _splitCopyPaths(
		paths,
		str(args.dest) if args.dest is not None else None,
		bool(args.pick_dest),
	)
	if dest is None or dest.strip() == "":
		if bool(getattr(args, "kio_progress", False)):
			raise FastOneDriveError("indica a pasta de destino da cópia")
		dest = pickDestDir()
	destPath: str = dest
	overwrite: bool = bool(getattr(args, "overwrite", False))
	onDrive: bool = _pathIsDrive(destPath)
	if bool(getattr(args, "kio_progress", False)):
		for source in sources:
			written: str = _copySource(
				source,
				destPath,
				onDrive,
				overwrite,
				_kioProgressCallback(),
			)
			print(f"Copiado para {written}", flush=True)
		return 0
	if bool(args.notify):
		for source in sources:
			written = runNotified(
				"Fast-OneDrive",
				f"Copiando {source}…",
				lambda job, src=source, destDir=destPath, drive=onDrive, replace=overwrite: _copyNotified(
					src,
					destDir,
					drive,
					replace,
					job,
				),
			)
			print(f"Copiado para {written}")
		return 0
	for source in sources:
		written = _copySource(
			source,
			destPath,
			onDrive,
			overwrite,
			None,
		)
		print(f"Copiado para {written}")
	return 0


def _splitCopyPaths(
	paths: list[str],
	destFlag: str | None,
	pickDest: bool,
) -> tuple[list[str], str | None]:
	if pickDest:
		return paths, pickDestDir()
	if destFlag is not None:
		return paths, destFlag
	if len(paths) >= 2:
		last: str = paths[-1]
		if _pathIsDrive(last) or (
			not last.startswith(config.remoteRoot())
			and (last.startswith("/") or last.startswith("~"))
		):
			return paths[:-1], last
	return paths, None


def _pathIsDrive(text: str) -> bool:
	raw: str = text.strip()
	if raw.startswith("fastonedrive:"):
		return True
	return isRemotePath(raw)


def _copyNotified(source: str, dest: str, onDrive: bool, overwrite: bool, job: NotifyJob) -> str:
	written: str = _copySource(
		source,
		dest,
		onDrive,
		overwrite,
		job.statsCallback(),
	)
	job.success(f"Copiado para {written}")
	return written


def _copySource(source: str, dest: str, onDrive: bool, overwrite: bool, onStats: Callable[[str], None] | None) -> str:
	if onDrive:
		return copyDriveToDrive(
			source,
			dest,
			onStats,
			overwrite,
		)
	return copyRemoteToLocal(source, dest, onStats)


def _kioProgressCallback() -> Callable[[str], None]:
	def onStats(line: str) -> None:
		parsed: tuple[int, int, int] | None = parseRcloneTransfer(line)
		if parsed is None:
			return
		bytesDone: int
		total: int
		speed: int
		bytesDone, total, speed = parsed
		print(f"FODPROGRESS {bytesDone} {total} {speed}", flush=True)

	return onStats


def _cmdPin(args: argparse.Namespace) -> int:
	sources: list[str] = [str(item) for item in args.sources]
	if bool(args.notify):
		for source in sources:
			written: str = runNotified(
				"Fast-OneDrive",
				f"A manter localmente {source}…",
				lambda job, src=source: _pinOne(src, job),
			)
			print(f"Mantido em {written}")
		return 0
	for source in sources:
		written = pinRemote(source)
		print(f"Mantido em {written}")
	return 0


def _pinOne(source: str, job: NotifyJob) -> str:
	written: str = pinRemote(source, job.statsCallback())
	job.success(f"Mantido em {written}")
	return written


def _cmdUnpin(args: argparse.Namespace) -> int:
	sources: list[str] = [str(item) for item in args.sources]
	failed: int = 0
	for source in sources:
		if _isKioHelpOrSearch(source):
			continue
		try:
			removed: str = unpinRemote(source)
		except FastOneDriveError as error:
			failed += 1
			logError(str(error), extra={"command": "unpin", "source": source})
			if bool(args.notify):
				NotifyJob("Fast-OneDrive", str(error)).error(str(error))
			print(error, file=sys.stderr)
			continue
		if bool(args.notify):
			NotifyJob("Fast-OneDrive", f"Deixou de manter: {removed}").success(
				f"Deixou de manter: {removed}"
			)
		print(f"Deixou de manter: {removed}")
	return 1 if failed else 0


def _isKioHelpOrSearch(source: str) -> bool:
	text: str = source.strip()
	if not text.startswith(f"{config.KIO_SCHEME}:"):
		return False
	try:
		toRemotePath(text)
	except FastOneDriveError:
		return True
	return False


def _cmdInstallMenus(args: argparse.Namespace) -> int:
	path: str = str(installMenus())
	print(f"Menu instalado em {path}")
	print(f"Definições: {settingsDesktopPath()}")
	print(f"Bandeja (autostart): {autostartDesktopPath()}")
	print("Feche o Dolphin (kquitapp6 dolphin) e abra de novo para o menu novo valer.")
	return 0


def _cmdInstallRunner(args: argparse.Namespace) -> int:
	path: str = str(installRunner())
	print(f"KRunner instalado em {path}")
	print("Se Alt+F2 não listar resultados: kquitapp6 krunner")
	return 0


def _cmdKrunner(args: argparse.Namespace) -> int:
	runKrunner()
	return 0


def _cmdSettings(args: argparse.Namespace) -> int:
	return runSettingsWindow(bool(args.tray))


def _cmdHydrate(args: argparse.Namespace) -> int:
	source: str = str(args.source)
	if bool(args.notify):
		written: str = runNotified(
			"Fast-OneDrive",
			f"A abrir {source}…",
			lambda job, src=source: _hydrateOne(src, job),
		)
		print(written)
		return 0
	onStats: Callable[[str], None] | None = None
	if bool(getattr(args, "kio_progress", False)):
		onStats = _kioProgressCallback()
	written = str(hydrateRemote(source, onStats))
	print(written, flush=True)
	return 0


def _cmdOpenUrl(args: argparse.Namespace) -> int:
	try:
		openIndexedPath(_itemPathFromArgument(str(args.url)))
	except FastOneDriveError as error:
		notify("Fast-OneDrive", str(error))
		raise
	return 0


def _itemPathFromArgument(raw: str) -> str:
	from fastonedrive.paths import _kioRawPath
	from fastonedrive.profiles import splitProfilePath

	text: str = raw.strip()
	if text.startswith("fastonedrive:"):
		profileId: str
		item: str
		profileId, item = splitProfilePath(_kioRawPath(text))
		if profileId != "":
			config.activateProfile(profileId)
		if item.startswith("/item/"):
			return item[len("/item/"):]
		if item in ("", "/"):
			return ""
		if item.startswith("/"):
			return item[1:]
		return item
	return remoteItemPath(toRemotePath(text))


def _hydrateOne(source: str, job: NotifyJob) -> str:
	written: str = str(hydrateRemote(source, job.statsCallback()))
	job.success(f"Disponível em {written}")
	return written


def _cmdSyncPins(args: argparse.Namespace) -> int:
	paths: list[str] = _syncPathArgs(args)
	if len(paths) > 0:
		config.ensureActiveProfile()
	elif config.activeProfile() is None:
		return _syncEveryProfile(bool(args.notify))
	if bool(args.notify):
		summary: str = runNotified(
			"Fast-OneDrive",
			"A sincronizar o que é mantido localmente…",
			lambda job, itemPaths=paths: _syncPinsJob(job, itemPaths),
		)
		print(summary)
		return 0
	counts: dict[str, int] = _syncPinPaths(paths, None)
	summary = _formatSyncCounts(counts)
	print(summary)
	return 0


def _syncPathArgs(args: argparse.Namespace) -> list[str]:
	raw: object = getattr(args, "path", None)
	if raw is None:
		return []
	items: list[str]
	if isinstance(raw, str):
		items = [raw]
	else:
		items = [str(item) for item in raw]
	paths: list[str] = []
	text: str
	for text in items:
		cleaned: str = text.strip()
		if cleaned == "":
			continue
		paths.append(_itemPathArg(cleaned))
	return paths


def _syncPinPaths(
	paths: list[str],
	onStats: Callable[[str], None] | None,
) -> dict[str, int]:
	if len(paths) == 0:
		return syncPins(onStats, None)
	totals: dict[str, int] = {
		"downloaded": 0,
		"uploaded": 0,
		"deletedLocal": 0,
		"deletedRemote": 0,
		"skipped": 0,
		"conflicts": 0,
		"errors": 0,
	}
	itemPath: str
	for itemPath in paths:
		counts: dict[str, int] = syncPins(onStats, itemPath)
		key: str
		for key in totals:
			totals[key] += int(counts.get(key, 0))
	return totals


def _syncEveryProfile(notifyUser: bool) -> int:
	profiles: list[config.Profile] = config.listProfiles()
	if len(profiles) == 0:
		print("Nenhum perfil OneDrive.", file=sys.stderr)
		return 0

	def work(job: NotifyJob | None) -> tuple[str, bool]:
		totals: dict[str, int] = {
			"downloaded": 0,
			"uploaded": 0,
			"deletedLocal": 0,
			"deletedRemote": 0,
			"skipped": 0,
			"conflicts": 0,
			"errors": 0,
		}
		failed: list[str] = []
		for profile in profiles:
			try:
				config.activateProfile(profile.id)
				counts: dict[str, int] = syncPins(
					None if job is None else job.statsCallback(),
					None,
				)
				for key in totals:
					totals[key] += int(counts.get(key, 0))
			except Exception as error:
				failed.append(f"{profile.name}: {error}")
				logError(str(error), extra={"command": "sync-pins", "profile": profile.id})
		summary: str = _formatSyncCounts(totals)
		if len(failed) > 0:
			summary = f"{summary}; falhas: {'; '.join(failed)}"
		if job is not None:
			if len(failed) > 0:
				job.error(summary)
			else:
				job.success(summary)
		return summary, len(failed) > 0

	if notifyUser:
		summary: str
		failedRun: bool
		summary, failedRun = runNotified(
			"Fast-OneDrive",
			"A sincronizar os perfis…",
			lambda job: work(job),
		)
		print(summary)
		return 1 if failedRun else 0
	summary, failedRun = work(None)
	print(summary)
	return 1 if failedRun else 0


def _syncPinsJob(job: NotifyJob, paths: list[str]) -> str:
	counts: dict[str, int] = _syncPinPaths(paths, job.statsCallback())
	summary: str = _formatSyncCounts(counts)
	job.success(summary)
	return summary


def _formatSyncCounts(counts: dict[str, int]) -> str:
	return (
		f"baixados {counts['downloaded']}, "
		f"enviados {counts['uploaded']}, "
		f"apagados local {counts['deletedLocal']}, "
		f"apagados nuvem {counts['deletedRemote']}, "
		f"iguais {counts['skipped']}, "
		f"conflitos {counts.get('conflicts', 0)}, "
		f"erros {counts.get('errors', 0)}"
	)


def _cmdFuse(args: argparse.Namespace) -> int:
	from fastonedrive.fuse import runFuse

	runFuse()
	return 0


def _cmdLink(args: argparse.Namespace) -> int:
	for source in [str(item) for item in args.sources]:
		remote: str = toRemotePath(source)
		if bool(args.notify):
			url: str = runNotified(
				"Fast-OneDrive",
				"A criar ligação…",
				lambda job, src=remote: _linkOne(src, job),
			)
			print(url)
			continue
		print(linkRemote(remote))
	return 0


def _linkOne(remote: str, job: NotifyJob) -> str:
	url: str = linkRemote(remote)
	job.success(url)
	return url


def _cmdFreeSpace(args: argparse.Namespace) -> int:
	for source in [str(item) for item in args.sources]:
		if bool(args.notify):
			written: str = runNotified(
				"Fast-OneDrive",
				"A libertar espaço local…",
				lambda job, src=source: _freeOne(src, job),
			)
			print(written)
			continue
		print(freeLocalSpace(source))
	return 0


def _freeOne(source: str, job: NotifyJob) -> str:
	written: str = freeLocalSpace(source)
	job.success(f"Libertado: {written}")
	return written


def _cmdAbout(args: argparse.Namespace) -> int:
	payload: dict[str, Any] = remoteAbout()
	print(json.dumps(payload, ensure_ascii=False))
	return 0


def _cmdPut(args: argparse.Namespace) -> int:
	local: str = str(args.local)
	dest: str = str(args.dest)
	overwrite: bool = bool(args.overwrite)
	if bool(getattr(args, "kio_progress", False)):
		written: str = stageLocalWrite(local, dest, overwrite)
		print(written, flush=True)
		return 0
	if bool(args.notify):
		written = runNotified(
			"Fast-OneDrive",
			f"A enviar {local}…",
			lambda job, src=local, dst=dest, ow=overwrite: _putOne(src, dst, job, ow),
		)
		print(written)
		return 0
	written = putLocalToRemote(local, dest, overwrite=overwrite)
	print(written)
	return 0


def _putOne(local: str, dest: str, job: NotifyJob, overwrite: bool) -> str:
	written: str = putLocalToRemote(
		local,
		dest,
		job.statsCallback(),
		overwrite,
	)
	job.success(f"Enviado para {written}")
	return written


def _cmdMkdir(args: argparse.Namespace) -> int:
	dest: str = str(args.dest)
	if bool(args.notify):
		written: str = runNotified(
			"Fast-OneDrive",
			f"A criar pasta {dest}…",
			lambda job, dst=dest: _mkdirOne(dst, job),
		)
		print(written)
		return 0
	written = mkdirRemotePath(dest)
	print(written)
	return 0


def _mkdirOne(dest: str, job: NotifyJob) -> str:
	written: str = mkdirRemotePath(dest)
	job.success(f"Pasta criada: {written}")
	return written


def _rmExpectFile(args: argparse.Namespace) -> bool | None:
	asFile: bool = bool(args.rmFile)
	asDir: bool = bool(args.rmDir)
	if asFile and asDir:
		raise FastOneDriveError("indique só --file ou --dir")
	if asFile:
		return True
	if asDir:
		return False
	return None


def _cmdRm(args: argparse.Namespace) -> int:
	paths: list[str] = [str(item) for item in args.paths]
	expectFile: bool | None = _rmExpectFile(args)
	for source in paths:
		if _isKioHelpOrSearch(source):
			continue
		if bool(args.notify):
			removed: str = runNotified(
				"Fast-OneDrive",
				f"A apagar {source}…",
				lambda job, src=source, kind=expectFile: _rmOne(src, job, kind),
			)
			print(removed)
			continue
		removed = removeRemotePath(source, expectFile)
		print(removed)
	return 0


def _rmOne(source: str, job: NotifyJob, expectFile: bool | None) -> str:
	removed: str = removeRemotePath(source, expectFile)
	job.success(f"Apagado: {removed}")
	return removed


def _cmdMv(args: argparse.Namespace) -> int:
	source: str = str(args.source)
	dest: str = str(args.dest)
	overwrite: bool = bool(args.overwrite)
	if bool(args.notify):
		written: str = runNotified(
			"Fast-OneDrive",
			f"A mover {source}…",
			lambda job, src=source, dst=dest, ow=overwrite: _mvOne(src, dst, job, ow),
		)
		print(written)
		return 0
	written = moveRemotePath(source, dest, overwrite)
	print(written)
	return 0


def _mvOne(source: str, dest: str, job: NotifyJob, overwrite: bool) -> str:
	written: str = moveRemotePath(source, dest, overwrite)
	job.success(f"Movido para {written}")
	return written


def _cmdChmod(args: argparse.Namespace) -> int:
	modeText: str = str(args.mode).strip().lower()
	if modeText.startswith("0o"):
		modeText = modeText[2:]
	elif modeText.startswith("0"):
		modeText = modeText[1:]
	try:
		mode: int = int(modeText, 8)
	except ValueError as error:
		raise FastOneDriveError(f"modo inválido: {args.mode}") from error
	itemPath: str = remoteItemPath(toRemotePath(str(args.path)))
	chmodItem(itemPath, mode)
	return 0


def _cmdExists(args: argparse.Namespace) -> int:
	print(json.dumps(remoteExists(str(args.path)), ensure_ascii=False))
	return 0


def _cmdMenuIf(args: argparse.Namespace) -> int:
	paths: list[str] = [str(item) for item in args.paths]
	if not paths:
		return 1
	for source in paths:
		if not isDriveSource(source):
			return 1
	return 0


def _cmdMount(args: argparse.Namespace) -> int:
	from fastonedrive.mount import fastMountActive, legacyMountActive, setMountEnabled
	action: str = str(args.action)
	if action == "status":
		legacy: str = "activo" if legacyMountActive() else "desligado"
		print(f"rclone-onedrive:\t{legacy}")
		profiles: list[config.Profile] = config.listProfiles()
		if len(profiles) == 0:
			fast: str = "activo" if fastMountActive() else "parado"
			print(f"pasta:\t{config.mountPath()}")
			print(f"Fast-OneDrive:\t{fast}")
		for profile in profiles:
			config.activateProfile(profile.id)
			state: str = "activo" if fastMountActive() else "parado"
			print(f"{profile.name}:\t{config.mountPath()} ({state})")
		print("FUSE:\tpróprio (sem RC 5573)")
		return 0
	setMountEnabled(action == "on")
	print(f"mount Fast-OneDrive: {action}")
	return 0


def _cmdStatus(args: argparse.Namespace) -> int:
	from fastonedrive.mount import fastMountActive, legacyMountActive
	config.ensureUserConfig()
	stats: dict[str, object] = indexStats()
	legacy: str = "activo" if legacyMountActive() else "desligado"
	fast: str = "activo" if fastMountActive() else "parado"
	print(f"rclone:\t{rcloneBin()} ({rcloneVersion()})")
	print(f"rclone.conf:\t{config.rcloneConfigPath()}")
	print(f"remote:\t{config.remoteRoot()}")
	print(f"mount:\t{config.mountPath()} (Fast-OneDrive {fast}; antigo {legacy})")
	print("FUSE:\tpróprio (getattr SQLite; sem RC 5573)")
	print(f"índice:\t{config.dbPath()}")
	print(f"pins:\t{config.pinDir()}")
	print(f"cache:\t{config.cacheDir()}")
	print(f"log:\t{config.logPath()}")
	print(f"config:\t{config.configPath()}")
	progress: dict[str, Any] = indexProgress()
	stateLabels: dict[str, str] = {
		"running": "em curso",
		"idle": "parado",
		"done": "concluída",
		"error": "falhou",
		"empty": "ainda sem índice",
	}
	state: str = str(progress["state"])
	print(f"indexação:\t{stateLabels.get(state, state)}")
	modeLabels: dict[str, str] = {
		"incremental": "incremental (Graph)",
		"completo": "completo (Graph)",
		"rclone": "completo (rclone)",
	}
	mode: str = str(progress.get("mode") or "")
	if mode != "":
		print(f"modo:\t{modeLabels.get(mode, mode)}")
	print(f"ciclo:\t{progress['counted']} itens ({progress['root']})")
	if progress["error"]:
		print(f"erro:\t{progress['error']}")
	if not bool(stats["exists"]):
		print("itens:\tíndice ainda não criado (abra o Local OneDrive ou rode: fastonedrive index)")
		return 0
	print(f"itens:\t{stats['files']} arquivos, {stats['dirs']} pastas")
	print(f"atualizado:\t{stats['updatedAt'] or '-'}")
	return 0


def _cmdLog(args: argparse.Namespace) -> int:
	limit: int = int(args.limit)
	text: str = readLogTail(limit)
	path: str = str(config.logPath())
	if text == "":
		print(f"Log vazio: {path}")
		return 0
	print(text)
	return 0


def _formatSize(size: int) -> str:
	value: float = float(size)
	units: tuple[str, ...] = ("B", "KiB", "MiB", "GiB", "TiB")
	index: int = 0
	while value >= 1024 and index < len(units) - 1:
		value /= 1024
		index += 1
	if index == 0:
		return f"{int(value)} {units[index]}"
	return f"{value:.1f} {units[index]}"
