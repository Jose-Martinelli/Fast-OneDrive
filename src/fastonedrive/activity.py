import json
import os
from pathlib import Path
from typing import Any

from fastonedrive import config


def setActivity(state: str, detail: str = "") -> None:
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
