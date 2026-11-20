import queue
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar
from urllib.parse import quote

from fastonedrive import FastOneDriveError

T = TypeVar("T")
_QURL_PATH_ENCODE: frozenset[str] = frozenset("\"#%<>?[\\]^`{|}")


def notify(title: str, body: str, replaceId: int | None = None) -> int | None:
	binary: str | None = shutil.which("notify-send")
	if binary is None:
		return replaceId
	command: list[str] = [
		binary,
		"--app-name=Fast-OneDrive",
		"--icon=cloud-download",
		"--hint=string:desktop-entry:fastonedrive",
		"--expire-time=8000",
	]
	if replaceId is not None:
		command.append(f"--replace-id={replaceId}")
	command.extend(["--print-id", title, body])
	result: subprocess.CompletedProcess[str] = subprocess.run(
		command,
		check=False,
		capture_output=True,
		text=True,
		encoding="utf-8",
	)
	if result.returncode != 0:
		fallback: list[str] = [
			binary,
			"--app-name=Fast-OneDrive",
			title,
			body,
		]
		subprocess.run(fallback, check=False, capture_output=True)
		return replaceId
	text: str = result.stdout.strip()
	if text.isdigit():
		return int(text)
	return replaceId


class NotifyJob:
	title: str
	replaceId: int | None

	def __init__(self, title: str, startBody: str) -> None:
		self.title = title
		self.replaceId = notify(title, startBody)

	def progress(self, body: str) -> None:
		self.replaceId = notify(self.title, body, self.replaceId)

	def statsCallback(self) -> Callable[[str], None]:
		def onStats(line: str) -> None:
			text: str = line.replace("\r", " ").strip()
			if text == "":
				return
			self.progress(text[-240:])

		return onStats

	def success(self, body: str) -> None:
		self.replaceId = notify(self.title, body, self.replaceId)

	def error(self, body: str) -> None:
		self.replaceId = notify(self.title, body, self.replaceId)


def notifyDirChanged(itemPath: str) -> None:
	from fastonedrive import config

	urls: list[str] = _itemUrls(itemPath, _kioRoot())
	profile: config.Profile | None = config.activeProfile()
	if profile is not None and profile.legacy:
		urls.extend(_itemUrls(itemPath, config.KIO_SCHEME + ":"))
	# A mesma mudança vista na pasta FUSE (~/OneDrive): o Dolphin não recebe inotify
	# de um FUSE, e o plugin de emblemas espera o sinal org.fastonedrive.State.
	mount: Path = config.mountPath()
	local: str = str(mount / itemPath) if itemPath != "" else str(mount)
	parent: str = str(Path(local).parent) if itemPath != "" else local
	urls.extend(_fileUrls([parent, local] if parent != local else [local]))
	_runAsync(lambda: _emitKdirNotify(urls))
	emitStateChanged([local])


def notifyConflict(itemPath: str) -> None:
	"""Como no Windows: avisar que há duas versões, sem esperar que o utilizador repare no emblema."""
	name: str = itemPath.rsplit("/", 1)[-1]
	notify(
		"OneDrive: conflito de sincronização",
		f"{name} mudou aqui e na nuvem. As duas versões foram guardadas; "
		"a cópia local tem «(conflito local …)» no nome.",
	)


def notifyStateChanged(itemPath: str) -> None:
	"""Só o emblema mudou (dirty → synced, por ex.): avisa o plugin, sem relistar a pasta."""
	from fastonedrive import config

	mount: Path = config.mountPath()
	emitStateChanged([str(mount / itemPath) if itemPath != "" else str(mount)])


def notifyLocalDirsChanged(localDirs: list[str]) -> None:
	"""Pastas locais (dentro de uma montagem) cujo conteúdo mudou na nuvem."""
	if len(localDirs) == 0:
		return
	# FilesAdded(pasta) faz o Dolphin relistar a pasta (ficheiros novos e apagados);
	# FilesChanged só refresca os itens que já conhece.
	urls: list[str] = _fileUrls(localDirs)

	def emitAdded() -> None:
		url: str
		for url in urls:
			_emitDirListingChanged(url)

	_runAsync(emitAdded)
	emitStateChanged(localDirs)


def _emitDirListingChanged(dirUrl: str) -> None:
	busctl: str | None = shutil.which("busctl")
	if busctl is None:
		return
	try:
		subprocess.run(
			[busctl, "--user", "emit", "/org/kde/KDirNotify", "org.kde.KDirNotify", "FilesAdded", "s", dirUrl],
			check=False,
			capture_output=True,
			timeout=5,
		)
	except (OSError, subprocess.TimeoutExpired):
		return


def _fileUrls(paths: list[str]) -> list[str]:
	return [Path(item).as_uri() for item in paths if item.startswith("/")]


_asyncJobs: "queue.SimpleQueue[Callable[[], None]] | None" = None


def useAsyncSignals() -> None:
	"""No processo FUSE (longo) os sinais saem numa thread: um write não espera pelo busctl."""
	global _asyncJobs
	if _asyncJobs is not None:
		return
	_asyncJobs = queue.SimpleQueue()
	threading.Thread(target=_notifyWorker, args=(_asyncJobs,), name="fod-signals", daemon=True).start()


def _notifyWorker(pending: "queue.SimpleQueue[Callable[[], None]]") -> None:
	while True:
		job: Callable[[], None] = pending.get()
		_runJob(job)
		while True:
			try:
				extra: Callable[[], None] = pending.get_nowait()
			except queue.Empty:
				break
			_runJob(extra)


def _runJob(job: Callable[[], None]) -> None:
	# Uma excepção num sinal matava a thread: daí em diante nenhum emblema nem
	# relistagem chegava ao Dolphin até reiniciar o FUSE.
	try:
		job()
	except Exception as error:  # noqa: BLE001
		from fastonedrive.log import logError

		logError(f"sinal D-Bus falhou: {error}")


def _runAsync(job: Callable[[], None]) -> None:
	if _asyncJobs is not None:
		_asyncJobs.put(job)
		return
	job()


def emitStateChanged(localPaths: list[str]) -> None:
	"""Sinal org.fastonedrive.State.Changed(as) para o plugin de emblemas do Dolphin."""
	paths: list[str] = [item for item in localPaths if item.startswith("/")]
	if len(paths) == 0:
		return
	_runAsync(lambda: _sendStateChanged(paths))


def _sendStateChanged(paths: list[str]) -> None:
	busctl: str | None = shutil.which("busctl")
	if busctl is None:
		return
	try:
		_runBusctl(busctl, paths)
	except (OSError, subprocess.TimeoutExpired):
		return


def _runBusctl(busctl: str, paths: list[str]) -> None:
	subprocess.run(
		[
			busctl,
			"--user",
			"emit",
			"/org/fastonedrive/State",
			"org.fastonedrive.State",
			"Changed",
			"as",
			str(len(paths)),
			*paths,
		],
		check=False,
		capture_output=True,
		timeout=5,
	)


def _itemUrls(itemPath: str, root: str) -> list[str]:
	urls: list[str] = []
	if itemPath == "":
		urls.append(f"{root}/")
		return urls
	urls.append(f"{root}/item/{_kioItemPath(itemPath)}")
	if "/" in itemPath:
		parent: str = itemPath.rsplit("/", 1)[0]
		urls.append(f"{root}/item/{_kioItemPath(parent)}")
	else:
		urls.append(f"{root}/")
	return urls


def _kioItemPath(itemPath: str) -> str:
	chars: list[str] = []
	char: str
	for char in itemPath:
		if char in _QURL_PATH_ENCODE:
			chars.append(quote(char, safe=""))
			continue
		chars.append(char)
	return "".join(chars)


def _kioRoot() -> str:
	from fastonedrive import config

	profile: config.Profile | None = config.activeProfile()
	if profile is None:
		return config.KIO_SCHEME + ":"
	return f"{config.KIO_SCHEME}:/p/{profile.id}"


def _emitKdirNotify(urls: list[str]) -> None:
	if len(urls) == 0:
		return
	busctl: str | None = shutil.which("busctl")
	if busctl is not None:
		command: list[str] = [
			busctl,
			"--user",
			"emit",
			"/org/kde/KDirNotify",
			"org.kde.KDirNotify",
			"FilesChanged",
			"as",
			str(len(urls)),
			*urls,
		]
		try:
			result: subprocess.CompletedProcess[bytes] = subprocess.run(
				command,
				check=False,
				capture_output=True,
				timeout=5,
			)
			if result.returncode == 0:
				return
		except (OSError, subprocess.TimeoutExpired):
			pass
	dbus: str | None = shutil.which("dbus-send")
	if dbus is None:
		return
	encoded: list[str] = [url.replace(",", "%2C") for url in urls]
	args: list[str] = [
		dbus,
		"--session",
		"--type=signal",
		"/org/kde/KDirNotify",
		"org.kde.KDirNotify.FilesChanged",
		"array:string:" + ",".join(encoded),
	]
	try:
		subprocess.run(
			args,
			check=False,
			capture_output=True,
			timeout=5,
		)
	except (OSError, subprocess.TimeoutExpired):
		return


def runNotified(title: str, startBody: str, action: Callable[[NotifyJob], T]) -> T:
	job: NotifyJob = NotifyJob(title, startBody)
	try:
		result: T = action(job)
	except FastOneDriveError as error:
		job.error(str(error))
		raise
	return result
