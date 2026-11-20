import json
import logging
import os
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastonedrive import config

LOG_MAX_BYTES: int = 1024 * 1024
LOG_BACKUP_COUNT: int = 3
LOG_TAIL_LINES: int = 80
_LOGGER_NAME: str = "fastonedrive"
_configured: bool = False


def ensureLogger() -> logging.Logger:
	global _configured
	logger: logging.Logger = logging.getLogger(_LOGGER_NAME)
	if _configured:
		return logger
	config.ensureDataDir()
	handler: RotatingFileHandler = RotatingFileHandler(
		config.logPath(),
		maxBytes=LOG_MAX_BYTES,
		backupCount=LOG_BACKUP_COUNT,
		encoding="utf-8",
	)
	handler.setFormatter(
		logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
	)
	logger.setLevel(logging.INFO)
	logger.addHandler(handler)
	logger.propagate = False
	_configured = True
	return logger


def logInfo(message: str, extra: dict[str, object] | None = None) -> None:
	if os.environ.get("FASTONEDRIVE_QUIET", "") == "1":
		return
	ensureLogger().info(_withExtra(message, extra))


def logError(message: str, extra: dict[str, object] | None = None) -> None:
	ensureLogger().error(_withExtra(message, extra))


def logException(error: BaseException, extra: dict[str, object] | None = None) -> None:
	text: str = f"{type(error).__name__}: {error}"
	trace: str = traceback.format_exc().strip()
	if trace != "" and trace != "NoneType: None":
		text = f"{text}\n{trace}"
	ensureLogger().error(_withExtra(text, extra))


def readLogTail(limit: int = LOG_TAIL_LINES) -> str:
	path: Path = config.logPath()
	if not path.is_file():
		return ""
	text: str = path.read_text(encoding="utf-8", errors="replace")
	lines: list[str] = text.splitlines()
	if limit > 0:
		lines = lines[-limit:]
	return "\n".join(lines)


def _withExtra(message: str, extra: dict[str, object] | None) -> str:
	if extra is None or len(extra) == 0:
		return message
	parts: list[str] = [message]
	for key, value in extra.items():
		if key == "argv":
			parts.append(f"argv={json.dumps(value, ensure_ascii=False)}")
			continue
		parts.append(f"{key}={value}")
	return "  ".join(parts)
