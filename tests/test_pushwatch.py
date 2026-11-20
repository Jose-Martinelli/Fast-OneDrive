"""Protocolo socket.io dos avisos do Graph, contra um servidor falso num socketpair."""

import json
import socket
import struct
import threading
import time

import pytest


def _serverFrame(text: str) -> bytes:
	payload: bytes = text.encode("utf-8")
	assert len(payload) < 126
	return bytes([0x81, len(payload)]) + payload


def _readClientText(sock: socket.socket) -> str:
	first, second = sock.recv(2, socket.MSG_WAITALL)
	length: int = second & 0x7F
	if length == 126:
		length = struct.unpack(">H", sock.recv(2, socket.MSG_WAITALL))[0]
	assert second & 0x80, "frame do cliente tem de ser mascarado"
	mask: bytes = sock.recv(4, socket.MSG_WAITALL)
	data: bytes = sock.recv(length, socket.MSG_WAITALL)
	return bytes(byte ^ mask[index % 4] for index, byte in enumerate(data)).decode("utf-8")


def test_split_notification_url() -> None:
	from fastonedrive.pushwatch import splitNotificationUrl

	assert splitNotificationUrl("https://f3hb0mpua.svc.ms/zbaehwg/callback?snthgk=1ff3-23") == (
		"f3hb0mpua.svc.ms",
		"/zbaehwg",
		"snthgk=1ff3-23",
	)


def test_conversa_engineio_v4(monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import pushwatch

	monkeypatch.setattr(pushwatch, "WAKE_DEBOUNCE_SEC", 0.05)
	woke: threading.Event = threading.Event()
	watcher = pushwatch.PushWatcher("p", woke.set)
	client, server = socket.socketpair()
	seen: list[str] = []

	def serve() -> None:
		server.sendall(_serverFrame("0" + json.dumps({"sid": "x", "pingInterval": 25000, "pingTimeout": 20000})))
		seen.append(_readClientText(server))
		server.sendall(_serverFrame("40/zbaehwg,{\"sid\":\"y\"}"))
		server.sendall(_serverFrame("2"))
		seen.append(_readClientText(server))
		server.sendall(_serverFrame('42/zbaehwg,["notification",{"clientState":null}]'))
		time.sleep(0.2)
		server.sendall(_serverFrame("41/zbaehwg,"))

	thread = threading.Thread(target=serve)
	thread.start()
	watcher._speak(client, "/zbaehwg", 4)  # type: ignore[arg-type]
	thread.join(2)
	assert seen == ["40/zbaehwg,", "3"]
	assert woke.wait(1)
	client.close()
	server.close()
