"""Controlo da sincronização: pausa, rede limitada, limite de banda e intervalo de poll.

Fica num JSON à parte (``~/.config/fast-onedrive/sync-control.json``) para não mexer
no escritor do config.toml. A pausa só trava envios e downloads de fundo (sync-pins e
o poll da nuvem); abrir um ficheiro continua a descarregá-lo, como no Windows.
"""

import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from fastonedrive import config

DEFAULT_POLL_SECONDS: int = 60
MIN_POLL_SECONDS: int = 15
METERED_CACHE_SEC: float = 30.0

_meteredCache: tuple[float, bool] | None = None


@dataclass
class SyncControl:
	pausedUntil: float = 0.0
	pauseOnMetered: bool = True
	bwLimit: str = ""
	remotePollSeconds: int = DEFAULT_POLL_SECONDS


def controlPath() -> Path:
	return config.configDir() / "sync-control.json"


def load() -> SyncControl:
	path: Path = controlPath()
	if not path.is_file():
		return SyncControl()
	try:
		raw: Any = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		return SyncControl()
	if not isinstance(raw, dict):
		return SyncControl()
	control: SyncControl = SyncControl()
	try:
		control.pausedUntil = float(raw.get("pausedUntil") or 0.0)
		control.pauseOnMetered = bool(raw.get("pauseOnMetered", True))
		control.bwLimit = _cleanBwLimit(str(raw.get("bwLimit") or ""))
		control.remotePollSeconds = max(
			MIN_POLL_SECONDS, int(raw.get("remotePollSeconds") or DEFAULT_POLL_SECONDS)
		)
	except (TypeError, ValueError):
		return SyncControl()
	return control


def save(control: SyncControl) -> Path:
	path: Path = controlPath()
	path.parent.mkdir(parents=True, exist_ok=True)
	control.bwLimit = _cleanBwLimit(control.bwLimit)
	tmp: Path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
	try:
		tmp.write_text(json.dumps(asdict(control), indent=2) + "\n", encoding="utf-8")
		os.replace(tmp, path)
	except Exception:
		tmp.unlink(missing_ok=True)
		raise
	return path


def _cleanBwLimit(value: str) -> str:
	text: str = value.strip()
	if text.lower() in ("", "off", "0", "none"):
		return ""
	# Aceita o formato do rclone: 10M, 512k, "10M:2M" (upload:download), horários
	# com "off" (ex.: "08:00,512k 23:00,off").
	allowed: set[str] = set("0123456789.kKmMgGbBtTiI:,-@ ofOF")
	if any(char not in allowed for char in text):
		return ""
	return text


def pauseFor(seconds: float) -> SyncControl:
	control: SyncControl = load()
	control.pausedUntil = time.time() + max(0.0, seconds)
	save(control)
	return control


def resume() -> SyncControl:
	control: SyncControl = load()
	control.pausedUntil = 0.0
	save(control)
	return control


def isMetered() -> bool:
	"""Rede limitada segundo o NetworkManager (Metered = 1 sim ou 3 provável)."""
	global _meteredCache
	now: float = time.monotonic()
	if _meteredCache is not None and now - _meteredCache[0] < METERED_CACHE_SEC:
		return _meteredCache[1]
	metered: bool = False
	busctl: str | None = shutil.which("busctl")
	if busctl is not None:
		try:
			result: subprocess.CompletedProcess[str] = subprocess.run(
				[
					busctl,
					"--system",
					"get-property",
					"org.freedesktop.NetworkManager",
					"/org/freedesktop/NetworkManager",
					"org.freedesktop.NetworkManager",
					"Metered",
				],
				check=False,
				capture_output=True,
				text=True,
				timeout=3,
			)
			parts: list[str] = result.stdout.split()
			if result.returncode == 0 and len(parts) == 2 and parts[1].isdigit():
				metered = int(parts[1]) in (1, 3)
		except (OSError, subprocess.TimeoutExpired):
			metered = False
	_meteredCache = (now, metered)
	return metered


def pauseReason(control: SyncControl | None = None) -> str:
	"""Motivo da pausa, ou "" se a sincronização de fundo pode correr."""
	current: SyncControl = control if control is not None else load()
	if current.pausedUntil > time.time():
		until: str = time.strftime("%H:%M", time.localtime(current.pausedUntil))
		return f"sincronização em pausa até às {until}"
	if current.pauseOnMetered and isMetered():
		return "rede limitada: sincronização em pausa"
	return ""


def pauseRetryDelay(control: SyncControl | None = None) -> float:
	"""Segundos até voltar a tentar um envio adiado pela pausa (no máx. 5 min)."""
	current: SyncControl = control if control is not None else load()
	remaining: float = current.pausedUntil - time.time()
	if remaining > 0.0:
		return min(300.0, max(5.0, remaining + 1.0))
	return 300.0


def bwLimitFlags() -> list[str]:
	limit: str = load().bwLimit
	if limit == "":
		return []
	return ["--bwlimit", limit]
