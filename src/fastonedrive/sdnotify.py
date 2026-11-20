"""sd_notify mínimo (sem libsystemd): avisa o systemd quando o FUSE está montado.

A unidade do mount é Type=notify: o systemd só a dá por arrancada quando o FUSE
responde, em vez do antigo ``ExecStartPre=/bin/sleep 1``.
"""

import os
import socket


def notify(message: str) -> bool:
	address: str = os.environ.get("NOTIFY_SOCKET", "")
	if address == "":
		return False
	if address.startswith("@"):
		address = "\0" + address[1:]
	try:
		with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as sock:
			sock.connect(address)
			sock.sendall(message.encode("utf-8"))
	except OSError:
		return False
	return True


def ready(status: str = "") -> bool:
	text: str = "READY=1"
	if status != "":
		text += f"\nSTATUS={status}"
	return notify(text)


def status(text: str) -> bool:
	return notify(f"STATUS={text}")


def stopping() -> bool:
	return notify("STOPPING=1")
