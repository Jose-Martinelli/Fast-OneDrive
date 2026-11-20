"""Avisos da nuvem em tempo real (socket.io do Graph), como o OneDrive do Windows.

``GET {drive}/root/subscriptions/socketIo`` devolve um ``notificationUrl``. A parte
antes de ``/callback?`` é o servidor socket.io (o caminho é o namespace) e a parte
depois é a query opaca. Só o transporte WebSocket é aceite. Cada aviso não diz o que
mudou: só acorda o poll do delta (``RemoteWatcher.pollNow``), que continua a ser a
única fonte de verdade. Qualquer falha cai no poll normal (60 s) e tenta de novo com
espera crescente.

Cliente WebSocket mínimo em stdlib (sem dependências): handshake HTTP/1.1, frames de
texto mascarados, ping/pong e close. Engine.IO v4 (o servidor faz ping, respondemos
pong); se o v4 for recusado, tenta o v3 (o cliente faz ping).
"""

import base64
import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from fastonedrive import FastOneDriveError
from fastonedrive.log import logInfo

CONNECT_TIMEOUT_SEC: float = 15.0
MIN_RETRY_SEC: float = 30.0
MAX_RETRY_SEC: float = 1800.0
# Vários avisos seguidos (cópia de uma pasta) valem um só delta.
WAKE_DEBOUNCE_SEC: float = 2.0


class PushError(FastOneDriveError):
	pass


class PushWatcher:
	def __init__(self, profileId: str, wake: Callable[[], None]) -> None:
		self._profileId: str = profileId
		self._wake: Callable[[], None] = wake
		self._stop: threading.Event = threading.Event()
		self._thread: threading.Thread | None = None
		self._socket: ssl.SSLSocket | None = None
		self._connected: bool = False
		self._lastWake: float = 0.0
		self._wakeTimer: threading.Timer | None = None

	def connected(self) -> bool:
		return self._connected

	def start(self) -> None:
		if self._thread is not None or os.environ.get("FASTONEDRIVE_NO_PUSH", "") == "1":
			return
		self._thread = threading.Thread(target=self._run, name="fod-push", daemon=True)
		self._thread.start()

	def stop(self) -> None:
		self._stop.set()
		sock: ssl.SSLSocket | None = self._socket
		if sock is not None:
			try:
				sock.close()
			except OSError:
				pass

	def _run(self) -> None:
		from fastonedrive.synccontrol import pauseReason

		retry: float = MIN_RETRY_SEC
		warned: bool = False
		while not self._stop.is_set():
			if pauseReason() != "":
				self._stop.wait(60.0)
				continue
			started: float = time.monotonic()
			try:
				self._session()
			except Exception as error:  # noqa: BLE001 — sem avisos o poll continua
				if not warned:
					logInfo(f"avisos em tempo real indisponíveis, a usar o poll: {error}", extra={"profile": self._profileId})
					warned = True
			finally:
				self._connected = False
				self._socket = None
			if time.monotonic() - started > 600.0:
				# Esteve ligado um bom bocado: a ligação caiu (normal), voltar já.
				retry = MIN_RETRY_SEC
				warned = False
			self._stop.wait(retry)
			retry = min(MAX_RETRY_SEC, retry * 2)

	def _session(self) -> None:
		from fastonedrive import graphitems

		payload: dict[str, Any] = graphitems.graphRequest(
			"GET", graphitems.itemUrl("") + "/subscriptions/socketIo"
		)
		notificationUrl: str = str(payload.get("notificationUrl") or "")
		if notificationUrl == "":
			raise PushError("o Graph não devolveu notificationUrl")
		lastError: Exception | None = None
		for version in (4, 3):
			try:
				self._listen(notificationUrl, version)
				return
			except _HandshakeRefused as error:
				lastError = error
				continue
		raise PushError(f"socket.io recusado: {lastError}")

	def _listen(self, notificationUrl: str, version: int) -> None:
		host, namespace, query = splitNotificationUrl(notificationUrl)
		path: str = f"/socket.io/?EIO={version}&transport=websocket"
		if query != "":
			path += "&" + query
		sock: ssl.SSLSocket = _openWebSocket(host, path)
		self._socket = sock
		try:
			self._speak(sock, namespace, version)
		finally:
			try:
				sock.close()
			except OSError:
				pass

	def _speak(self, sock: ssl.SSLSocket, namespace: str, version: int) -> None:
		sock.settimeout(CONNECT_TIMEOUT_SEC)
		opened: str = _recvText(sock)
		if not opened.startswith("0"):
			raise _HandshakeRefused(f"pacote de abertura inesperado: {opened[:40]}")
		info: dict[str, Any] = json.loads(opened[1:] or "{}")
		interval: float = float(info.get("pingInterval") or 25000) / 1000.0
		grace: float = float(info.get("pingTimeout") or 20000) / 1000.0
		_sendText(sock, f"40{namespace},")
		sock.settimeout(interval + grace + 5.0)
		if version == 3:
			# Engine.IO v3: o cliente faz ping. Uma thread à parte, para o recv nunca
			# ser interrompido a meio de um frame.
			threading.Thread(
				target=self._pingLoop, args=(sock, interval), name="fod-push-ping", daemon=True
			).start()
		serverPinged: bool = False
		while not self._stop.is_set():
			try:
				message: str = _recvText(sock)
			except TimeoutError as error:
				if version == 4 and not serverPinged:
					# Servidor Engine.IO v3 à espera do nosso ping: tentar o v3.
					raise _HandshakeRefused("sem ping do servidor") from error
				raise
			if message == "2":
				serverPinged = True
				_sendText(sock, "3")
				continue
			if message == "3":
				continue
			if message.startswith("40"):
				if not self._connected:
					logInfo("avisos da nuvem em tempo real ligados", extra={"profile": self._profileId})
				self._connected = True
				continue
			if message.startswith("44"):
				raise PushError(f"namespace recusado: {message[:120]}")
			if message.startswith("41") or message == "1":
				return
			if message.startswith("42"):
				self._onNotification()

	def _pingLoop(self, sock: ssl.SSLSocket, interval: float) -> None:
		while not self._stop.wait(interval):
			if self._socket is not sock:
				return
			try:
				_sendText(sock, "2")
			except (OSError, PushError):
				return

	def _onNotification(self) -> None:
		now: float = time.monotonic()
		if now - self._lastWake < WAKE_DEBOUNCE_SEC:
			return
		self._lastWake = now
		timer: threading.Timer = threading.Timer(WAKE_DEBOUNCE_SEC, self._wake)
		timer.daemon = True
		self._wakeTimer = timer
		timer.start()


class _HandshakeRefused(PushError):
	pass


def splitNotificationUrl(url: str) -> tuple[str, str, str]:
	"""(host, namespace, query). Ex.: https://h.svc.ms/abc/callback?x=1 → (h.svc.ms, /abc, x=1)."""
	base, sep, query = url.partition("/callback?")
	if sep == "":
		base, _sep, query = url.partition("?")
	parsed = urllib.parse.urlsplit(base)
	if parsed.scheme not in ("https", "wss") or parsed.hostname is None:
		hostHint: str = parsed.hostname or "(sem host)"
		raise PushError(f"notificationUrl inesperado: {parsed.scheme or '?'}://{hostHint}")
	namespace: str = parsed.path.rstrip("/")
	host: str = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
	return host, namespace, query


def _openWebSocket(host: str, path: str) -> ssl.SSLSocket:
	name, _sep, portText = host.partition(":")
	port: int = int(portText) if portText != "" else 443
	raw: socket.socket = socket.create_connection((name, port), timeout=CONNECT_TIMEOUT_SEC)
	context: ssl.SSLContext = ssl.create_default_context()
	sock: ssl.SSLSocket = context.wrap_socket(raw, server_hostname=name)
	key: str = base64.b64encode(os.urandom(16)).decode("ascii")
	request: str = (
		f"GET {path} HTTP/1.1\r\n"
		f"Host: {host}\r\n"
		"Upgrade: websocket\r\n"
		"Connection: Upgrade\r\n"
		f"Sec-WebSocket-Key: {key}\r\n"
		"Sec-WebSocket-Version: 13\r\n"
		"User-Agent: fast-onedrive\r\n"
		"\r\n"
	)
	sock.sendall(request.encode("ascii"))
	header: bytes = b""
	while b"\r\n\r\n" not in header:
		chunk: bytes = sock.recv(4096)
		if chunk == b"":
			sock.close()
			raise _HandshakeRefused("ligação fechada no handshake")
		header += chunk
		if len(header) > 65536:
			sock.close()
			raise _HandshakeRefused("resposta de handshake grande demais")
	statusLine: str = header.split(b"\r\n", 1)[0].decode("latin-1")
	if " 101 " not in f"{statusLine} ":
		sock.close()
		raise _HandshakeRefused(statusLine)
	rest: bytes = header.split(b"\r\n\r\n", 1)[1]
	if rest != b"":
		# Raro: o servidor já mandou o primeiro frame junto do handshake.
		_pending[id(sock)] = rest
	return sock


_pending: dict[int, bytes] = {}


def _recvExact(sock: ssl.SSLSocket, count: int) -> bytes:
	buffered: bytes = _pending.pop(id(sock), b"")
	data: bytearray = bytearray(buffered[:count])
	if len(buffered) > count:
		_pending[id(sock)] = buffered[count:]
	while len(data) < count:
		chunk: bytes = sock.recv(count - len(data))
		if chunk == b"":
			raise PushError("ligação fechada")
		data.extend(chunk)
	return bytes(data)


def _recvFrame(sock: ssl.SSLSocket) -> tuple[int, bool, bytes]:
	first, second = _recvExact(sock, 2)
	fin: bool = bool(first & 0x80)
	opcode: int = first & 0x0F
	length: int = second & 0x7F
	if length == 126:
		length = struct.unpack(">H", _recvExact(sock, 2))[0]
	elif length == 127:
		length = struct.unpack(">Q", _recvExact(sock, 8))[0]
	mask: bytes = _recvExact(sock, 4) if second & 0x80 else b""
	payload: bytes = _recvExact(sock, length) if length > 0 else b""
	if mask != b"":
		payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
	return opcode, fin, payload


def _recvText(sock: ssl.SSLSocket) -> str:
	parts: list[bytes] = []
	while True:
		opcode, fin, payload = _recvFrame(sock)
		if opcode == 0x8:
			raise PushError("o servidor fechou a ligação")
		if opcode == 0x9:
			_sendFrame(sock, 0xA, payload)
			continue
		if opcode == 0xA:
			continue
		if opcode in (0x1, 0x2, 0x0):
			parts.append(payload)
			if fin:
				return b"".join(parts).decode("utf-8", errors="replace")


_sendLock: threading.Lock = threading.Lock()


def _sendFrame(sock: ssl.SSLSocket, opcode: int, payload: bytes) -> None:
	header: bytearray = bytearray([0x80 | opcode])
	length: int = len(payload)
	if length < 126:
		header.append(0x80 | length)
	elif length < 65536:
		header.append(0x80 | 126)
		header.extend(struct.pack(">H", length))
	else:
		header.append(0x80 | 127)
		header.extend(struct.pack(">Q", length))
	mask: bytes = os.urandom(4)
	header.extend(mask)
	masked: bytes = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
	with _sendLock:
		sock.sendall(bytes(header) + masked)


def _sendText(sock: ssl.SSLSocket, text: str) -> None:
	_sendFrame(sock, 0x1, text.encode("utf-8"))
