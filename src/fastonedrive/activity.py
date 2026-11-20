import json
import os
import time
from pathlib import Path
from typing import Any

from fastonedrive import config

HISTORY_KEEP: int = 100


def setActivity(state: str, detail: str = "", kind: str = "") -> None:
	config.ensureDataDir()
	payload: dict[str, str] = {"state": state, "detail": detail}
	path: Path = config.activityPath()
	tmp: Path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
	try:
		tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
		os.replace(tmp, path)
	except Exception:
		tmp.unlink(missing_ok=True)
		raise
	if detail != "" and (kind != "" or state == "error"):
		_appendHistory({"time": time.time(), "state": state, "kind": kind or state, "detail": detail})


def historyPath() -> Path:
	return config.activityPath().with_name("activity-history.jsonl")


def _appendHistory(entry: dict[str, Any]) -> None:
	"""Últimas transferências para o menu "Actividade recente" da bandeja."""
	path: Path = historyPath()
	try:
		lines: list[str] = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
	except OSError:
		lines = []
	lines.append(json.dumps(entry, ensure_ascii=False))
	lines = lines[-HISTORY_KEEP:]
	tmp: Path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
	try:
		tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
		os.replace(tmp, path)
	except OSError:
		tmp.unlink(missing_ok=True)


def readHistory(limit: int = 10) -> list[dict[str, Any]]:
	path: Path = historyPath()
	if not path.is_file():
		return []
	entries: list[dict[str, Any]] = []
	try:
		lines: list[str] = path.read_text(encoding="utf-8").splitlines()
	except OSError:
		return []
	for line in reversed(lines):
		try:
			item: Any = json.loads(line)
		except json.JSONDecodeError:
			continue
		if isinstance(item, dict):
			entries.append(item)
		if len(entries) >= limit:
			break
	return entries


def readActivity() -> dict[str, Any]:
	path: Path = config.activityPath()
	if not path.is_file():
		return {"state": "idle", "detail": ""}
	try:
		payload: Any = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		return {"state": "idle", "detail": ""}
	if not isinstance(payload, dict):
		return {"state": "idle", "detail": ""}
	return {
		"state": str(payload.get("state") or "idle"),
		"detail": str(payload.get("detail") or ""),
	}
