import atexit
import base64
import ctypes
import json
import os
import re
import select
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timezone
from json import JSONDecoder
from pathlib import Path
from typing import IO, Any

from fastonedrive import FastOneDriveError
from fastonedrive import config

_activeCopy: subprocess.Popen[Any] | None = None


def armParentDeathSignal() -> None:
	try:
		libc = ctypes.CDLL("libc.so.6", use_errno=True)
		libc.prctl.argtypes = [
			ctypes.c_int,
			ctypes.c_ulong,
			ctypes.c_ulong,
			ctypes.c_ulong,
			ctypes.c_ulong,
		]
		libc.prctl.restype = ctypes.c_int
		libc.prctl(1, signal.SIGTERM, 0, 0, 0)
	except Exception:
		return


def _stopActiveCopy() -> None:
	global _activeCopy
	process: subprocess.Popen[Any] | None = _activeCopy
	_activeCopy = None
	if process is not None:
		_kill(process)


atexit.register(_stopActiveCopy)


def rcloneBin() -> str:
	path: str | None = shutil.which("rclone")
	if path is not None:
		return path
	fallback: Path = Path(config.RCLONE_BIN)
	if fallback.is_file():
		return str(fallback)
	raise FastOneDriveError("rclone não encontrado (esperado /usr/bin/rclone)")


def rcloneVersion() -> str:
	result: subprocess.CompletedProcess[str] = _run(
		["version"],
		capture=True,
		useFilters=False,
	)
	firstLine: str = result.stdout.splitlines()[0] if result.stdout else ""
	return firstLine.strip() or "rclone"


def iterLsjson(
	remotePath: str,
	recursive: bool = True,
	maxDepth: int | None = None,
	useOnedriveDelta: bool = False,
	useHash: bool = False,
) -> Iterator[dict[str, Any]]:
	args: list[str] = ["lsjson"]
	if recursive:
		args.extend(["--recursive", "--fast-list"])
		if useOnedriveDelta:
			args.append("--onedrive-delta")
	if maxDepth is not None:
		args.extend(["--max-depth", str(maxDepth)])
	if useHash:
		args.append("--hash")
	args.append(remotePath)
	process: subprocess.Popen[str] = _popen(args)
	assert process.stdout is not None
	try:
		yield from _iterJsonArray(process.stdout)
		_finish(process)
	except json.JSONDecodeError as error:
		_kill(process)
		raise FastOneDriveError("saída inválida do rclone lsjson") from error
	except Exception:
		_kill(process)
		raise


def copyTo(
	sourceRemote: str,
	destPath: str,
	onStats: Callable[[str], None] | None = None,
) -> None:
	_runCopy(
		[
			"copyto",
			sourceRemote,
			destPath,
		],
		onStats,
		False,
	)


def catRange(remotePath: str, offset: int, count: int) -> bytes:
	if count < 0 or offset < 0:
		raise FastOneDriveError(f"intervalo inválido em {remotePath}")
	if count == 0:
		return b""
	command: list[str] = _command(
		[
			"cat",
			"--offset",
			str(offset),
			"--count",
			str(count),
			remotePath,
		],
		False,
	)
	result: subprocess.CompletedProcess[bytes] = subprocess.run(
		command,
		check=False,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
	)
	if result.returncode != 0:
		detail: str = result.stderr.decode("utf-8", errors="replace").strip()
		raise FastOneDriveError(
			detail or f"rclone cat saiu com código {result.returncode}"
		)
	data: bytes = result.stdout
	if len(data) < count:
		raise FastOneDriveError(
			f"intervalo incompleto em {remotePath}: {len(data)} de {count}"
		)
	return data[:count]


def copyToReplace(
	sourceRemote: str,
	destPath: str,
	onStats: Callable[[str], None] | None = None,
) -> None:
	dest: Path = Path(destPath)
	dest.parent.mkdir(parents=True, exist_ok=True)
	partial: Path = dest.with_name(f"{dest.name}.fod-partial")
	partial.unlink(missing_ok=True)
	try:
		copyTo(sourceRemote, str(partial), onStats)
		partial.replace(dest)
	except Exception:
		partial.unlink(missing_ok=True)
		raise


def copyDir(
	sourceRemote: str,
	destDir: str,
	onStats: Callable[[str], None] | None = None,
	extraArgs: list[str] | None = None,
) -> None:
	extra: list[str] = list(extraArgs) if extraArgs is not None else []
	_runCopy(
		[
			*extra,
			"copy",
			sourceRemote,
			destDir,
		],
		onStats,
	)


def copyRemoteToRemote(
	sourceRemote: str,
	destRemote: str,
	onStats: Callable[[str], None] | None = None,
) -> None:
	_runCopy(
		[
			"copyto",
			sourceRemote,
			destRemote,
		],
		onStats,
		False,
	)


def copyFromLocal(
	localPath: str,
	destRemote: str,
	onStats: Callable[[str], None] | None = None,
) -> None:
	_runCopy(
		[
			"copyto",
			localPath,
			destRemote,
		],
		onStats,
		False,
	)


_NOT_FOUND_MARKERS: tuple[str, ...] = (
	"directory not found",
	"object not found",
	"itemnotfound",
	"item not found",
	"can't find source directory",
	"cannot find source directory",
)


def remoteStat(remotePath: str) -> dict[str, Any] | None:
	command: list[str] = _command(
		["lsjson", "--stat", "--hash", remotePath],
		useFilters=False,
	)
	result: subprocess.CompletedProcess[str] = subprocess.run(
		command,
		check=False,
		capture_output=True,
		text=True,
		encoding="utf-8",
	)
	if result.returncode != 0:
		detail: str = f"{result.stderr or ''}\n{result.stdout or ''}"
		if _isNotFoundMessage(detail):
			return None
		lines: list[str] = detail.strip().splitlines()
		last: str = lines[-1] if lines else f"código {result.returncode}"
		raise FastOneDriveError(f"rclone stat falhou: {last}")
	try:
		payload: Any = json.loads(result.stdout or "{}")
	except json.JSONDecodeError as error:
		raise FastOneDriveError("saída inválida do rclone stat") from error
	if not isinstance(payload, dict):
		raise FastOneDriveError("saída inválida do rclone stat")
	return payload


def _isNotFoundMessage(text: str) -> bool:
	lower: str = text.lower()
	return any(marker in lower for marker in _NOT_FOUND_MARKERS)


def parseRemoteModTime(text: str) -> datetime | None:
	if text == "":
		return None
	raw: str = text.strip().replace("Z", "+00:00")
	if "." in raw:
		head: str
		tail: str
		head, tail = raw.split(".", 1)
		digits: list[str] = []
		index: int = 0
		while index < len(tail) and tail[index].isdigit():
			if len(digits) < 6:
				digits.append(tail[index])
			index += 1
		rest: str = tail[index:]
		if len(digits) > 0:
			raw = f"{head}.{''.join(digits)}{rest}"
		else:
			raw = f"{head}{rest}"
	try:
		parsed: datetime = datetime.fromisoformat(raw)
	except ValueError:
		return None
	if parsed.tzinfo is None:
		return parsed.replace(tzinfo=timezone.utc)
	return parsed


def isContentToken(value: str) -> bool:
	return value.startswith("qxh:") or value.startswith("sha1:") or value.startswith("mt:")


def contentToken(
	modTime: datetime | None,
	size: int,
	quickXor: str = "",
	sha1: str = "",
) -> str:
	quick: str = quickXor.strip()
	if quick != "":
		return f"qxh:{quick}"
	digest: str = sha1.strip()
	if digest != "":
		return f"sha1:{digest}"
	if modTime is None:
		return ""
	return f"mt:{int(modTime.timestamp())}:{int(size)}"


def tokenKind(value: str) -> str:
	if value.startswith("qxh:"):
		return "qxh"
	if value.startswith("sha1:"):
		return "sha1"
	if value.startswith("mt:"):
		return "mt"
	return ""


def statSize(stat: Mapping[str, object] | None) -> int | None:
	if stat is None:
		return None
	raw: object | None = None
	if "Size" in stat:
		raw = stat.get("Size")
	elif "size" in stat:
		raw = stat.get("size")
	else:
		return None
	if raw is None or raw == "":
		return None
	try:
		return int(str(raw))
	except ValueError:
		return None


def tokensDiffer(
	left: str,
	right: str,
	leftTime: datetime | None,
	rightTime: datetime | None,
	leftSize: int | None,
	rightSize: int | None,
) -> bool:
	leftKind: str = tokenKind(left)
	rightKind: str = tokenKind(right)
	if leftKind != "" and leftKind == rightKind:
		return left != right
	return _clockOrSizeDiffer(leftTime, rightTime, leftSize, rightSize)


def quickXorEqual(left: str, right: str) -> bool | None:
	if tokenKind(left) != "qxh" or tokenKind(right) != "qxh":
		return None
	leftRaw: str = left.split(":", 1)[1].strip()
	rightRaw: str = right.split(":", 1)[1].strip()
	leftBytes: bytes | None = _decodeQuickXor(leftRaw)
	rightBytes: bytes | None = _decodeQuickXor(rightRaw)
	if leftBytes is None or rightBytes is None:
		return leftRaw == rightRaw
	return leftBytes == rightBytes


def _decodeQuickXor(text: str) -> bytes | None:
	raw: str = text.strip()
	if raw == "":
		return None
	found: list[bytes] = []
	if len(raw) % 2 == 0 and all(char in "0123456789abcdefABCDEF" for char in raw):
		try:
			found.append(bytes.fromhex(raw))
		except ValueError:
			pass
	try:
		padded: str = raw + ("=" * ((4 - len(raw) % 4) % 4))
		decoded: bytes = base64.b64decode(padded, validate=False)
		if len(decoded) > 0:
			found.append(decoded)
	except (ValueError, TypeError):
		pass
	item: bytes
	for item in found:
		if len(item) == 20:
			return item
	if len(found) == 0:
		return None
	return found[0]


def hashLocalQuickXor(path: Path) -> str:
	if not path.is_file():
		return ""
	result: subprocess.CompletedProcess[str] = _run(
		["hashsum", "quickxor", str(path)],
		capture=True,
		useFilters=False,
	)
	line: str
	for line in result.stdout.splitlines():
		parts: list[str] = line.strip().split()
		if len(parts) == 0:
			continue
		digest: str = parts[0].strip()
		if digest != "":
			return f"qxh:{digest}"
	return ""


def _clockOrSizeDiffer(
	leftTime: datetime | None,
	rightTime: datetime | None,
	leftSize: int | None,
	rightSize: int | None,
) -> bool:
	if leftSize is not None and rightSize is not None and leftSize != rightSize:
		return True
	timesClose: bool = False
	if leftTime is not None and rightTime is not None:
		delta: float = abs((leftTime - rightTime).total_seconds())
		if delta > 2:
			return True
		timesClose = True
	if leftSize is None or rightSize is None:
		return True
	if leftSize == 0 and rightSize == 0:
		return not timesClose
	return False


def contentTokenFromStat(stat: dict[str, Any]) -> str:
	hashesRaw: object = stat.get("Hashes")
	if not isinstance(hashesRaw, dict):
		hashesRaw = stat.get("hashes")
	hashes: dict[str, Any] = hashesRaw if isinstance(hashesRaw, dict) else {}
	when: datetime | None = parseRemoteModTime(
		str(stat.get("ModTime") or stat.get("lastModifiedDateTime") or "")
	)
	size: int = int(stat.get("Size") or stat.get("size") or 0)
	return contentToken(
		when,
		size,
		_hashByNames(hashes, ("quickxor", "quickxorhash")),
		_hashByNames(hashes, ("sha1", "sha1hash")),
	)


def _hashByNames(hashes: dict[str, Any], names: tuple[str, ...]) -> str:
	wanted: set[str] = {_normHashKey(name) for name in names}
	for key, value in hashes.items():
		if _normHashKey(str(key)) not in wanted:
			continue
		text: str = str(value or "").strip()
		if text != "":
			return text
	return ""


def _normHashKey(key: str) -> str:
	return key.lower().replace("-", "").replace("_", "")


def remoteAbout(remoteRoot: str = "") -> dict[str, Any]:
	root: str = remoteRoot.strip()
	if root == "":
		root = config.remoteRoot()
	result: subprocess.CompletedProcess[str] = _run(
		["about", root, "--json"],
		capture=True,
		useFilters=False,
		timeout=20.0,
	)
	try:
		payload: Any = json.loads(result.stdout or "{}")
	except json.JSONDecodeError as error:
		raise FastOneDriveError("saída inválida do rclone about") from error
	if not isinstance(payload, dict):
		raise FastOneDriveError("saída inválida do rclone about")
	return payload


def linkRemote(remotePath: str) -> str:
	result: subprocess.CompletedProcess[str] = _run(
		["link", remotePath],
		capture=True,
	)
	lines: list[str] = [
		line.strip()
		for line in (result.stdout or "").splitlines()
		if line.strip() != ""
	]
	url: str = lines[-1] if len(lines) > 0 else ""
	if url == "":
		raise FastOneDriveError("rclone link não devolveu URL")
	return url


def deleteRemoteFile(remotePath: str) -> None:
	_run(
		["deletefile", remotePath],
		capture=True,
		useFilters=False,
	)


def mkdirRemote(remotePath: str) -> None:
	_run(["mkdir", remotePath], capture=True)


def rmdirRemote(remotePath: str) -> None:
	_run(["rmdir", remotePath], capture=True, useFilters=False)


def purgeRemote(remotePath: str) -> None:
	_run(["purge", remotePath], capture=True)


def moveRemote(sourceRemote: str, destRemote: str) -> None:
	_run(
		["moveto", sourceRemote, destRemote],
		capture=True,
		useFilters=False,
	)


def remoteSizeBytes(
	remotePath: str,
	extraArgs: list[str] | None = None,
	useFilters: bool = True,
) -> int:
	extra: list[str] = list(extraArgs) if extraArgs is not None else []
	result: subprocess.CompletedProcess[str] = _run(
		[*extra, "size", "--json", remotePath],
		capture=True,
		useFilters=useFilters,
	)
	payload: dict[str, Any] = json.loads(result.stdout or "{}")
	return int(payload.get("bytes") or 0)


def isRemoteDir(remotePath: str) -> bool:
	from fastonedrive.paths import remoteBasename, remoteItemPath
	itemPath: str = remoteItemPath(remotePath)
	if itemPath == "":
		return True
	name: str = remoteBasename(remotePath)
	parent: str
	if "/" in itemPath:
		parent = itemPath.rsplit("/", 1)[0]
		listRoot: str = config.remotePath(parent)
	else:
		listRoot = config.remoteRoot()
	result: subprocess.CompletedProcess[str] = subprocess.run(
		_command(
			[
				"lsjson",
				listRoot,
				"--max-depth",
				"1",
			]
		),
		check=False,
		capture_output=True,
		text=True,
		encoding="utf-8",
	)
	if result.returncode != 0:
		detail: str = f"{result.stderr or ''}\n{result.stdout or ''}"
		if _isNotFoundMessage(detail):
			raise FastOneDriveError(f"item não encontrado no remote: {remotePath}")
		lines: list[str] = detail.strip().splitlines()
		last: str = lines[-1] if lines else f"código {result.returncode}"
		raise FastOneDriveError(f"rclone lsjson falhou: {last}")
	try:
		payload: Any = json.loads(result.stdout or "[]")
	except json.JSONDecodeError as error:
		raise FastOneDriveError("saída inválida do rclone lsjson") from error
	if not isinstance(payload, list):
		raise FastOneDriveError("saída inválida do rclone lsjson")
	for item in payload:
		if not isinstance(item, dict):
			continue
		if str(item.get("Name") or "") == name:
			return bool(item.get("IsDir"))
	raise FastOneDriveError(f"item não encontrado no remote: {remotePath}")


def _runCopy(
	copyArgs: list[str],
	onStats: Callable[[str], None] | None,
	useFilters: bool = True,
) -> None:
	args: list[str] = [
		*copyArgs,
		"--multi-thread-streams",
		str(config.MULTI_THREAD_STREAMS),
		"--multi-thread-cutoff",
		config.MULTI_THREAD_CUTOFF,
	]
	if onStats is None:
		args.append("--progress")
		_run(args, capture=False, useFilters=useFilters)
		return
	args.extend(["--stats", "1s"])
	_runStreaming(args, onStats, useFilters)


def _runStreaming(
	args: list[str],
	onStats: Callable[[str], None],
	useFilters: bool = True,
) -> None:
	global _activeCopy
	command: list[str] = _command(args, useFilters=useFilters)
	parentPid: int = os.getppid()
	process: subprocess.Popen[bytes] = subprocess.Popen(
		command,
		stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT,
		start_new_session=True,
	)
	_activeCopy = process
	assert process.stdout is not None
	fd: int = process.stdout.fileno()

	def stop(_signum: int, _frame: object) -> None:
		_kill(process)

	previous: tuple[Any, Any, Any] | None = _bindCopySignals(stop)
	try:
		buf: str = ""
		while True:
			if os.getppid() != parentPid:
				_kill(process)
				raise FastOneDriveError("cópia cancelada")
			ready: list[int]
			ready, _, _ = select.select([fd], [], [], 0.2)
			if process.poll() is not None and len(ready) == 0:
				break
			if len(ready) == 0:
				continue
			raw: bytes = os.read(fd, 256)
			if raw == b"":
				break
			buf += raw.decode("utf-8", errors="replace")
			buf = buf.replace("\r", "\n")
			while "\n" in buf:
				line: str
				line, buf = buf.split("\n", 1)
				text: str = line.strip()
				if text != "":
					onStats(text)
		if buf.strip() != "":
			onStats(buf.strip())
		code: int = process.wait()
		if code != 0:
			raise FastOneDriveError(f"rclone saiu com código {code}")
	finally:
		_restoreCopySignals(previous)
		if process.poll() is None:
			_kill(process)
		if _activeCopy is process:
			_activeCopy = None


def _bindCopySignals(stop: Callable[[int, object], None]) -> tuple[Any, Any, Any] | None:
	if threading.current_thread() is not threading.main_thread():
		return None
	return (
		signal.signal(signal.SIGTERM, stop),
		signal.signal(signal.SIGINT, stop),
		signal.signal(signal.SIGHUP, stop),
	)


def _restoreCopySignals(previous: tuple[Any, Any, Any] | None) -> None:
	if previous is None:
		return
	signal.signal(signal.SIGTERM, previous[0])
	signal.signal(signal.SIGINT, previous[1])
	signal.signal(signal.SIGHUP, previous[2])


_SIZE_UNITS: dict[str, int] = {
	"B": 1,
	"K": 1024,
	"M": 1024 ** 2,
	"G": 1024 ** 3,
	"T": 1024 ** 4,
	"P": 1024 ** 5,
	"KIB": 1024,
	"MIB": 1024 ** 2,
	"GIB": 1024 ** 3,
	"TIB": 1024 ** 4,
	"PIB": 1024 ** 5,
	"KB": 1000,
	"MB": 1000 ** 2,
	"GB": 1000 ** 3,
	"TB": 1000 ** 4,
	"PB": 1000 ** 5,
}
_TRANSFER_RE: re.Pattern[str] = re.compile(
	r"(?:Transferred:\s*)?"
	r"([\d.]+)\s+(B|KiB|MiB|GiB|TiB|PiB|KB|MB|GB|TB|PB|K|M|G|T)\s*/\s*"
	r"([\d.]+)\s+(B|KiB|MiB|GiB|TiB|PiB|KB|MB|GB|TB|PB|K|M|G|T)"
	r",\s*(?:-|[\d.]+%)"
	r"(?:,\s*([\d.]+)\s+(B|KiB|MiB|GiB|TiB|PiB|KB|MB|GB|TB|PB|K|M|G|T)/s)?",
	re.IGNORECASE,
)


def parseRcloneTransfer(line: str) -> tuple[int, int, int] | None:
	text: str = line.replace("\r", " ").strip()
	if text.startswith("{"):
		return _parseJsonTransfer(text)
	match: re.Match[str] | None = _TRANSFER_RE.search(text)
	if match is None:
		return None
	speedText: str | None = match.group(5)
	speedUnit: str | None = match.group(6)
	speed: int = _sizeToBytes(speedText, speedUnit) if speedText is not None and speedUnit is not None else 0
	return (
		_sizeToBytes(match.group(1), match.group(2)),
		_sizeToBytes(match.group(3), match.group(4)),
		speed,
	)


def _parseJsonTransfer(text: str) -> tuple[int, int, int] | None:
	try:
		payload: Any = json.loads(text)
	except json.JSONDecodeError:
		return None
	if not isinstance(payload, dict):
		return None
	stats: Any = payload.get("stats")
	if not isinstance(stats, dict):
		return None
	return (
		int(stats.get("bytes") or 0),
		int(stats.get("totalBytes") or 0),
		int(float(stats.get("speed") or 0)),
	)


def _sizeToBytes(value: str, unit: str) -> int:
	amount: float = float(value)
	factor: int = _SIZE_UNITS.get(unit.upper(), 1)
	return int(amount * factor)


def _popen(args: list[str]) -> subprocess.Popen[str]:
	command: list[str] = _command(args)
	return subprocess.Popen(
		command,
		stdout=subprocess.PIPE,
		stderr=None,
		text=True,
		encoding="utf-8",
	)


def _run(
	args: list[str],
	capture: bool,
	useFilters: bool = True,
	timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
	command: list[str] = _command(args, useFilters=useFilters)
	try:
		result: subprocess.CompletedProcess[str] = subprocess.run(
			command,
			check=False,
			capture_output=capture,
			text=True,
			encoding="utf-8",
			timeout=timeout,
		)
	except subprocess.TimeoutExpired as error:
		raise FastOneDriveError("rclone excedeu o tempo") from error
	if result.returncode != 0:
		detail: str = ""
		if capture and result.stderr:
			detail = result.stderr.strip().splitlines()[-1]
		if detail != "":
			raise FastOneDriveError(f"rclone falhou: {detail}")
		raise FastOneDriveError(f"rclone saiu com código {result.returncode}")
	return result


def listRemoteNames() -> list[str]:
	result: subprocess.CompletedProcess[str] = _run(
		["listremotes"],
		capture=True,
		useFilters=False,
	)
	names: list[str] = []
	for line in result.stdout.splitlines():
		name: str = line.strip().rstrip(":")
		if name != "":
			names.append(name)
	return names


def probeRemote() -> str:
	result: subprocess.CompletedProcess[str] = _run(
		["lsd", config.remoteRoot(), "--max-depth", "1"],
		capture=True,
	)
	lines: list[str] = [line.strip() for line in result.stdout.splitlines() if line.strip() != ""]
	if len(lines) == 0:
		return f"remote {config.remoteRoot()} ok (pasta vazia)"
	return f"remote {config.remoteRoot()} ok ({len(lines)} pastas na raiz)"


_FILE_ONLY_OPS: frozenset[str] = frozenset({"copyto", "moveto", "deletefile"})
_RCLONE_OPS: frozenset[str] = frozenset(
	{
		"about",
		"cat",
		"copy",
		"copyto",
		"deletefile",
		"link",
		"listremotes",
		"lsd",
		"lsjson",
		"mkdir",
		"moveto",
		"purge",
		"rmdir",
		"size",
		"version",
	}
)


def _rcloneOperation(args: list[str]) -> str:
	for arg in args:
		if arg in _RCLONE_OPS:
			return arg
	return ""


def _command(args: list[str], useFilters: bool = True) -> list[str]:
	config.ensureRcloneConfig()
	conf: str = str(config.rcloneConfigPath())
	applyFilters: bool = useFilters and _rcloneOperation(args) not in _FILE_ONLY_OPS
	filters: list[str] = config.jobFilterFlags() if applyFilters else []
	command: list[str] = [rcloneBin(), "--config", conf, *filters, *args]
	for arg in command:
		if arg.startswith("--rc"):
			raise FastOneDriveError("flags RC não são permitidas neste processo")
		if "127.0.0.1:5572" in arg:
			raise FastOneDriveError("a porta 5572 é o mount antigo; não usar")
	return command


def _finish(process: subprocess.Popen[str]) -> None:
	code: int | None = process.poll()
	if code is None:
		code = process.wait()
	if code != 0:
		raise FastOneDriveError(f"rclone lsjson saiu com código {code}")


def _kill(process: subprocess.Popen[Any]) -> None:
	if process.poll() is not None:
		return
	pid: int | None = process.pid
	if pid is not None:
		try:
			os.killpg(pid, signal.SIGTERM)
		except OSError:
			process.terminate()
		try:
			process.wait(timeout=2)
			return
		except subprocess.TimeoutExpired:
			try:
				os.killpg(pid, signal.SIGKILL)
			except OSError:
				process.kill()
			process.wait()
			return
	process.kill()
	process.wait()


def _iterJsonArray(stream: IO[str]) -> Iterator[dict[str, Any]]:
	decoder: JSONDecoder = JSONDecoder()
	buf: str = ""
	started: bool = False
	while True:
		chunk: str = stream.read(65536)
		if chunk == "":
			buf = buf.strip()
			if buf in ("", "]", "[", "[]"):
				return
			raise json.JSONDecodeError("JSON incompleto", buf, 0)
		buf += chunk
		while True:
			buf = buf.lstrip()
			if not started:
				if buf == "":
					break
				if buf[0] != "[":
					raise json.JSONDecodeError("esperado array JSON", buf, 0)
				buf = buf[1:]
				started = True
				continue
			if buf == "":
				break
			if buf[0] == "]":
				return
			if buf[0] == ",":
				buf = buf[1:]
				continue
			try:
				obj: Any
				index: int
				obj, index = decoder.raw_decode(buf)
			except json.JSONDecodeError:
				break
			buf = buf[index:]
			if isinstance(obj, dict):
				yield obj
