#!/usr/bin/env python3
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def cliWrapperPath() -> Path:
	local: Path = Path.home() / ".local" / "bin" / "fastonedrive"
	system: Path = Path("/usr/bin/fastonedrive")
	if not local.is_file() and system.is_file():
		return system
	return local


def logPath() -> Path:
	return Path.home() / ".local" / "share" / "fast-onedrive" / "fastonedrive.log"


def appendLog(message: str) -> None:
	path: Path = logPath()
	path.parent.mkdir(parents=True, exist_ok=True)
	stamp: str = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
	with path.open("a", encoding="utf-8") as handle:
		handle.write(f"{stamp} ERROR menu  {message}\n")


def actionArgs(action: str, paths: list[str]) -> list[str]:
	if action == "copy":
		return ["copy", "--notify", "--pick-dest", *paths]
	if action == "copy-pick":
		return ["copy", "--notify", "--pick-dest", *paths]
	if action == "pin":
		return ["pin", "--notify", *paths]
	if action == "unpin":
		return ["unpin", "--notify", *paths]
	if action == "share":
		return ["link", "--notify", *paths]
	if action == "free-space":
		return ["free-space", "--notify", *paths]
	text: str = f"ação desconhecida: {action}"
	appendLog(text)
	sys.stderr.write(text + "\n")
	raise SystemExit(1)


def spawnCli(args: list[str]) -> None:
	wrapper: Path = cliWrapperPath()
	if not wrapper.is_file():
		text: str = f"CLI não encontrado: {wrapper}"
		appendLog(text)
		sys.stderr.write(text + "\n")
		raise SystemExit(1)
	subprocess.Popen(
		[str(wrapper), *args],
		start_new_session=True,
		stdin=subprocess.DEVNULL,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		env=os.environ.copy(),
	)


def main() -> int:
	if len(sys.argv) < 2:
		sys.stderr.write("uso: servicemenu.py AÇÃO [CAMINHOS…]\n")
		return 1
	action: str = sys.argv[1]
	paths: list[str] = sys.argv[2:]
	spawnCli(actionArgs(action, paths))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
