"""rclone.conf cifrado, com a senha guardada na carteira do KDE (ou outro Secret Service).

O rclone cifra o ficheiro inteiro (``rclone config encryption set``) e lê a senha por
``RCLONE_PASSWORD_COMMAND``. Aqui o comando é ``fastonedrive rclone-password``, que a
lê da carteira (``secret-tool``, que o KWallet 6 serve, ou ``kwallet-query``). O token
OAuth deixa de ficar legível no disco.

O Fast-OneDrive também lê o ficheiro (token para o Graph, ``drive_id``) e edita-o
(perfis, assistente). Com a cifra ligada:

- leitura: ``rclone config show`` (decifrado em memória, em cache pelo mtime);
- edição: ``plainConfigForEdit()`` tira a cifra, deixa editar e volta a cifrar.

É opcional: ``fastonedrive config-encryption on|off|status``.
"""

import os
import secrets
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastonedrive import FastOneDriveError
from fastonedrive import config

ENCRYPTED_MARK: str = "# Encrypted rclone configuration File"
SECRET_SERVICE: str = "fast-onedrive"
SECRET_KEY: str = "rclone-config"
KWALLET_FOLDER: str = "Fast-OneDrive"
KWALLET_WALLET: str = "kdewallet"

_cache: dict[str, tuple[int, int, str]] = {}
_cacheLock: threading.Lock = threading.Lock()


def isEncrypted(path: Path | None = None) -> bool:
	target: Path = path if path is not None else config.rcloneConfigPath()
	try:
		with target.open("r", encoding="utf-8", errors="replace") as handle:
			head: str = handle.read(len(ENCRYPTED_MARK) + 8)
	except OSError:
		return False
	return head.lstrip().startswith(ENCRYPTED_MARK)


def passwordCommand() -> str:
	return f"{config.cliBin()} rclone-password"


def exportPasswordEnv() -> None:
	"""Os rclone lançados por este processo (e pelos filhos) lêem a senha da carteira."""
	if not isEncrypted():
		return
	try:
		command: str = passwordCommand()
	except FastOneDriveError:
		return
	os.environ.setdefault("RCLONE_PASSWORD_COMMAND", command)
	os.environ["RCLONE_ASK_PASSWORD"] = "false"


def readConfigText(path: Path | None = None) -> str:
	target: Path = path if path is not None else config.rcloneConfigPath()
	if not isEncrypted(target):
		return target.read_text(encoding="utf-8") if target.is_file() else ""
	info: os.stat_result = target.stat()
	key: str = str(target)
	with _cacheLock:
		found: tuple[int, int, str] | None = _cache.get(key)
		if found is not None and found[0] == int(info.st_mtime_ns) and found[1] == int(info.st_size):
			return found[2]
	# Só leitura: sem o rclone.conf.lock (quem o segura pode estar a meio de uma edição
	# neste mesmo processo, e o flock de outro descritor bloquearia).
	text: str = _runRclone(["config", "show"], target, lock=False).stdout
	with _cacheLock:
		_cache[key] = (int(info.st_mtime_ns), int(info.st_size), text)
	return text


@contextmanager
def plainConfigForEdit(path: Path | None = None) -> Iterator[None]:
	"""Para quem edita o texto do rclone.conf: sem cifra durante a edição, cifra de volta."""
	target: Path = path if path is not None else config.rcloneConfigPath()
	if not isEncrypted(target):
		yield
		return
	_runRclone(["config", "encryption", "remove"], target)
	try:
		yield
	finally:
		_runRclone(["config", "encryption", "set"], target, change=True)
		target.chmod(0o600)


def enable() -> None:
	target: Path = config.ensureRcloneConfig()
	if isEncrypted(target):
		return
	_requireRclone168()
	password: str = secrets.token_urlsafe(32)
	storePassword(password)
	if readPassword() != password:
		raise FastOneDriveError("a carteira não devolveu a senha guardada; cifra não ligada")
	_runRclone(["config", "encryption", "set"], target, change=True)
	if not isEncrypted(target):
		raise FastOneDriveError("o rclone não cifrou o rclone.conf")
	_runRclone(["config", "encryption", "check"], target)
	target.chmod(0o600)


def _requireRclone168() -> None:
	import re

	from fastonedrive.rclone import rcloneVersion

	match = re.search(r"v(\d+)\.(\d+)", rcloneVersion())
	if match is None:
		return
	if (int(match.group(1)), int(match.group(2))) < (1, 68):
		raise FastOneDriveError("é preciso o rclone 1.68 ou mais recente (config encryption)")


def disable() -> None:
	target: Path = config.rcloneConfigPath()
	if not isEncrypted(target):
		return
	_runRclone(["config", "encryption", "remove"], target)
	target.chmod(0o600)


def _runRclone(
	args: list[str],
	target: Path,
	change: bool = False,
	lock: bool = True,
) -> subprocess.CompletedProcess[str]:
	from fastonedrive.rclone import rcloneBin

	env: dict[str, str] = dict(os.environ)
	env["RCLONE_PASSWORD_COMMAND"] = passwordCommand()
	env["RCLONE_ASK_PASSWORD"] = "false"
	if change:
		# Com RCLONE_PASSWORD_CHANGE=1 o rclone pede a senha nova ao mesmo comando.
		env["RCLONE_PASSWORD_CHANGE"] = "1"
	command: list[str] = [rcloneBin(), "--config", str(target), *args]
	result: subprocess.CompletedProcess[str]
	if lock:
		with config.rcloneConfigLock():
			result = _execute(command, env)
	else:
		result = _execute(command, env)
	if result.returncode != 0:
		lines: list[str] = (result.stderr or result.stdout or "").strip().splitlines()
		raise FastOneDriveError(f"rclone {' '.join(args)} falhou: {lines[-1] if lines else result.returncode}")
	return result


def _execute(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		command,
		stdin=subprocess.DEVNULL,
		capture_output=True,
		text=True,
		encoding="utf-8",
		env=env,
		timeout=60,
	)


def storePassword(password: str) -> None:
	secretTool: str | None = shutil.which("secret-tool")
	if secretTool is not None:
		result = subprocess.run(
			[secretTool, "store", "--label=Fast-OneDrive (rclone.conf)", "service", SECRET_SERVICE, "key", SECRET_KEY],
			input=password,
			capture_output=True,
			text=True,
			timeout=60,
		)
		if result.returncode == 0:
			return
	kwallet: str | None = shutil.which("kwallet-query")
	if kwallet is not None:
		result = subprocess.run(
			[kwallet, "-f", KWALLET_FOLDER, "-w", SECRET_KEY, KWALLET_WALLET],
			input=password,
			capture_output=True,
			text=True,
			timeout=60,
		)
		if result.returncode == 0:
			return
	raise FastOneDriveError("nenhuma carteira disponível (instale libsecret/secret-tool ou kwallet)")


def readPassword() -> str:
	secretTool: str | None = shutil.which("secret-tool")
	if secretTool is not None:
		result = subprocess.run(
			[secretTool, "lookup", "service", SECRET_SERVICE, "key", SECRET_KEY],
			capture_output=True,
			text=True,
			timeout=60,
		)
		if result.returncode == 0 and result.stdout.strip() != "":
			return result.stdout.strip()
	kwallet: str | None = shutil.which("kwallet-query")
	if kwallet is not None:
		result = subprocess.run(
			[kwallet, "-f", KWALLET_FOLDER, "-r", SECRET_KEY, KWALLET_WALLET],
			capture_output=True,
			text=True,
			timeout=60,
		)
		if result.returncode == 0 and result.stdout.strip() != "":
			return result.stdout.strip()
	raise FastOneDriveError("senha do rclone.conf não encontrada na carteira")
