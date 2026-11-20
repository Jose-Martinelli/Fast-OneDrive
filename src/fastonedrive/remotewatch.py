"""Mudanças da nuvem quase em tempo real (o OneDrive do Windows vê-as em segundos).

Antes, o índice só se actualizava pelo timer de 6 h e os pins pelo de 15 min. Esta
thread corre dentro do processo FUSE de cada perfil (que já está sempre activo) e, a
cada ``remotePollSeconds`` (60 s por omissão), lança ``fastonedrive index`` desse
perfil: com ``deltaLink`` guardado, é um pedido Graph quando nada mudou. Se o índice
mudou, avisa o Dolphin das pastas afectadas e, havendo pins, corre ``sync-pins``.

O índice e o sync correm em subprocesso, como os timers: uma falha não derruba o FUSE.
"""

import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.log import logError, logInfo

FIRST_DELAY_SEC: float = 20.0
# O primeiro índice completo de um Drive grande pode levar muito tempo.
SUBPROCESS_TIMEOUT_SEC: float = 6 * 3600.0
MAX_NOTIFIED_DIRS: int = 200
PENDING_HOLD_SEC: float = 600.0
# Testes com um remote local (sem Graph) ligam isto; em produção o poll só usa o delta.
ALLOW_FULL_LISTING: bool = False


class RemoteWatcher:
	def __init__(self, profileId: str, mountPoint: Path) -> None:
		self._profileId: str = profileId
		self._mountPoint: Path = mountPoint
		self._stop: threading.Event = threading.Event()
		self._wake: threading.Event = threading.Event()
		self._thread: threading.Thread | None = None
		self._lastSuccess: float = 0.0
		self._failures: int = 0
		self._interval: float = 60.0

	def healthy(self) -> bool:
		"""O delta correu bem há pouco: o índice está fresco sem lsjson por pasta."""
		if self._lastSuccess <= 0.0:
			return False
		return time.monotonic() - self._lastSuccess < 3 * self._interval + FIRST_DELAY_SEC

	def start(self) -> None:
		if self._thread is not None:
			return
		self._thread = threading.Thread(target=self._run, name="fod-remote-watch", daemon=True)
		self._thread.start()
		from fastonedrive.pushwatch import PushWatcher

		# Avisos em tempo real acordam este poll; sem eles, o poll continua a 60 s.
		self._push = PushWatcher(self._profileId, self.pollNow)
		self._push.start()

	def stop(self) -> None:
		self._stop.set()
		self._wake.set()
		push = getattr(self, "_push", None)
		if push is not None:
			push.stop()

	def pollNow(self) -> None:
		self._wake.set()

	def _run(self) -> None:
		from fastonedrive.synccontrol import load, pauseReason

		if self._stop.wait(FIRST_DELAY_SEC):
			return
		while not self._stop.is_set():
			control = load()
			self._interval = float(control.remotePollSeconds)
			try:
				reason: str = pauseReason(control)
				if reason == "":
					self._cycle()
			except Exception as error:  # noqa: BLE001 — o watcher nunca derruba o FUSE
				logError(f"poll da nuvem falhou: {error}", extra={"profile": self._profileId})
			wait: float = float(control.remotePollSeconds)
			if self._failures > 0:
				# Graph em baixo ou sem rede: 1, 2, 4, 8 min no máx. (a fila offline sai assim que a rede volta).
				wait = min(300.0, wait * (2 ** min(self._failures, 3)))
			self._wake.wait(wait)
			self._wake.clear()

	def _cycle(self) -> None:
		if not config.dbPath().is_file():
			return
		if not self._flushOfflineOps():
			return
		before: int = _counter()
		if not self._runCli(["index"]):
			self._failures += 1
			return
		self._failures = 0
		self._lastSuccess = time.monotonic()
		after: int = _counter()
		if after == before:
			return
		changedDirs: list[str] = _changedParents()
		logInfo(
			"mudanças na nuvem",
			extra={"profile": self._profileId, "dirs": len(changedDirs)},
		)
		self._notifyDolphin(changedDirs)
		if _hasPins():
			self._runCli(["sync-pins"])

	def _flushOfflineOps(self) -> bool:
		"""Envia o que foi feito sem rede antes de ler o delta (senão o delta repunha os
		itens apagados/movidos offline). False = ainda há fila recente: não ler o delta."""
		from fastonedrive import pendingops

		try:
			if pendingops.pendingCount() == 0:
				return True
			pendingops.flushPending()
			left: list[dict[str, object]] = pendingops.listPending()
		except Exception as error:  # noqa: BLE001 — a fila nunca derruba o poll
			logError(f"fila offline falhou: {error}", extra={"profile": self._profileId})
			return True
		if len(left) == 0:
			return True
		oldest: float = min(float(str(item["createdAt"])) for item in left)
		# Uma operação presa há muito (ex.: pasta de destino que nunca subiu) não pode
		# parar as mudanças da nuvem para sempre.
		return time.time() - oldest > PENDING_HOLD_SEC

	def _runCli(self, args: list[str]) -> bool:
		try:
			binary: str = config.cliBin()
		except FastOneDriveError as error:
			logError(str(error))
			return False
		command: list[str] = [binary]
		if self._profileId != "":
			command.extend(["--profile", self._profileId])
		command.extend(args)
		env: dict[str, str] = dict(os.environ)
		# Um ciclo por minuto: no log só ficam os erros destes subprocessos.
		env["FASTONEDRIVE_QUIET"] = "1"
		if args[:1] == ["index"] and not ALLOW_FULL_LISTING:
			env["FASTONEDRIVE_DELTA_ONLY"] = "1"
		try:
			result: subprocess.CompletedProcess[bytes] = subprocess.run(
				command,
				check=False,
				env=env,
				stdin=subprocess.DEVNULL,
				stdout=subprocess.DEVNULL,
				stderr=subprocess.DEVNULL,
				timeout=SUBPROCESS_TIMEOUT_SEC,
				preexec_fn=_lowPriority,
			)
		except (OSError, subprocess.TimeoutExpired) as error:
			logError(f"{' '.join(args)} falhou: {error}", extra={"profile": self._profileId})
			return False
		return result.returncode == 0

	def _notifyDolphin(self, dirs: list[str]) -> None:
		from fastonedrive.notify import notifyLocalDirsChanged

		local: list[str] = [
			str(self._mountPoint / item) if item != "" else str(self._mountPoint) for item in dirs
		]
		notifyLocalDirsChanged(local)


def _lowPriority() -> None:
	try:
		os.nice(10)
	except OSError:
		pass


def _connect() -> sqlite3.Connection:
	from fastonedrive.index import connectDb

	return connectDb()


def _counter() -> int:
	from fastonedrive.searchindex import changeCounter

	connection: sqlite3.Connection = _connect()
	try:
		return changeCounter(connection)
	finally:
		connection.close()


def _changedParents() -> list[str]:
	from fastonedrive.searchindex import takeChangedDirs

	connection: sqlite3.Connection = _connect()
	try:
		return takeChangedDirs(connection, MAX_NOTIFIED_DIRS)
	finally:
		connection.close()


def _hasPins() -> bool:
	connection: sqlite3.Connection = _connect()
	try:
		return connection.execute("SELECT 1 FROM files WHERE pinned = 1 LIMIT 1").fetchone() is not None
	finally:
		connection.close()
