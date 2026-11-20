"""Leitura de intervalos de um ficheiro na nuvem para o FUSE.

Antes, cada bloco de 4 MiB era um subprocesso ``rclone cat --offset --count``: um
processo novo, uma ligação TLS nova e a releitura do token por bloco. Aqui o URL de
download do Graph é pedido uma vez (cache de 10 min) e os intervalos saem por HTTP
``Range`` numa ligação persistente por thread. Qualquer falha cai no ``rclone cat``.
"""

import http.client
import threading
import time
import urllib.parse

from fastonedrive import FastOneDriveError
from fastonedrive.log import logError

RANGE_TIMEOUT_SEC: float = 60.0
GRAPH_BACKOFF_SEC: float = 60.0

_local: threading.local = threading.local()
_backoffUntil: float = 0.0
_backoffLock: threading.Lock = threading.Lock()


class _UrlExpired(Exception):
	pass


def fetchRange(itemPath: str, remotePath: str, offset: int, count: int) -> bytes:
	if count <= 0:
		return b""
	if offset < 0:
		raise FastOneDriveError(f"intervalo inválido em {remotePath}")
	if _graphAllowed():
		try:
			return _graphRange(itemPath, offset, count)
		except (OSError, http.client.HTTPException) as error:
			_backoff()
			logError(f"leitura HTTP falhou, a usar rclone cat: {error}", extra={"path": itemPath})
		except FastOneDriveError as error:
			from fastonedrive.graphitems import GraphUnreachable

			if isinstance(error, GraphUnreachable):
				_backoff()
			logError(f"leitura Graph falhou, a usar rclone cat: {error}", extra={"path": itemPath})
	from fastonedrive.rclone import catRange

	return catRange(remotePath, offset, count)


def _graphAllowed() -> bool:
	with _backoffLock:
		return time.monotonic() >= _backoffUntil


def _backoff() -> None:
	global _backoffUntil
	with _backoffLock:
		_backoffUntil = time.monotonic() + GRAPH_BACKOFF_SEC


def _graphRange(itemPath: str, offset: int, count: int) -> bytes:
	from fastonedrive.graphitems import downloadUrl

	url: str = downloadUrl(itemPath)
	try:
		return _rangeGet(url, offset, count)
	except _UrlExpired:
		url = downloadUrl(itemPath, refresh=True)
	try:
		return _rangeGet(url, offset, count)
	except _UrlExpired as error:
		raise FastOneDriveError("URL de download recusado pelo OneDrive") from error


def _connection(scheme: str, host: str) -> http.client.HTTPConnection:
	pool: dict[tuple[str, str], http.client.HTTPConnection] | None = getattr(_local, "pool", None)
	if pool is None:
		pool = {}
		_local.pool = pool
	key: tuple[str, str] = (scheme, host)
	found: http.client.HTTPConnection | None = pool.get(key)
	if found is not None:
		return found
	created: http.client.HTTPConnection
	if scheme == "https":
		created = http.client.HTTPSConnection(host, timeout=RANGE_TIMEOUT_SEC)
	else:
		created = http.client.HTTPConnection(host, timeout=RANGE_TIMEOUT_SEC)
	pool[key] = created
	return created


def _dropConnection(scheme: str, host: str) -> None:
	pool: dict[tuple[str, str], http.client.HTTPConnection] | None = getattr(_local, "pool", None)
	if pool is None:
		return
	found: http.client.HTTPConnection | None = pool.pop((scheme, host), None)
	if found is not None:
		found.close()


REDIRECT_CODES: frozenset[int] = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS: int = 5
# O URL do Graph para contas pessoais responde 302 para o servidor de ficheiros.
# Guarda-se o destino (também pré-assinado) para não repetir o salto em cada bloco.
_resolved: dict[str, tuple[str, float]] = {}
_resolvedLock: threading.Lock = threading.Lock()
RESOLVED_TTL_SEC: float = 600.0


def _rangeGet(url: str, offset: int, count: int) -> bytes:
	with _resolvedLock:
		found: tuple[str, float] | None = _resolved.get(url)
	target: str = found[0] if found is not None and found[1] > time.monotonic() else url
	for _hop in range(MAX_REDIRECTS + 1):
		result: bytes | str = _rangeGetOnce(target, offset, count)
		if isinstance(result, bytes):
			if target != url:
				with _resolvedLock:
					if len(_resolved) > 1024:
						_resolved.clear()
					_resolved[url] = (target, time.monotonic() + RESOLVED_TTL_SEC)
			return result
		target = urllib.parse.urljoin(target, result)
	raise FastOneDriveError("demasiados redireccionamentos ao ler intervalo")


def _rangeGetOnce(url: str, offset: int, count: int) -> bytes | str:
	"""Devolve os bytes, ou o Location de um redireccionamento."""
	parsed: urllib.parse.SplitResult = urllib.parse.urlsplit(url)
	target: str = parsed.path + (f"?{parsed.query}" if parsed.query else "")
	headers: dict[str, str] = {"Range": f"bytes={offset}-{offset + count - 1}"}
	lastError: Exception | None = None
	for _attempt in range(2):
		connection: http.client.HTTPConnection = _connection(parsed.scheme, parsed.netloc)
		finished: bool = False
		try:
			connection.request("GET", target, headers=headers)
			response: http.client.HTTPResponse = connection.getresponse()
			status: int = response.status
			if status in REDIRECT_CODES:
				location: str = response.getheader("Location") or ""
				response.read()
				if location == "":
					raise FastOneDriveError(f"HTTP {status} sem Location")
				finished = True
				return location
			if status in (401, 403, 404, 410):
				response.read()
				raise _UrlExpired(str(status))
			if status == 200:
				# Servidor ignorou o Range. Só serve se o pedido era o ficheiro inteiro;
				# senão a ligação fecha (finally) sem descarregar o resto.
				if offset == 0 and int(response.getheader("Content-Length") or -1) == count:
					return response.read()
				raise FastOneDriveError("o servidor ignorou o pedido de intervalo")
			if status != 206:
				response.read()
				raise FastOneDriveError(f"HTTP {status} ao ler intervalo")
			data: bytes = response.read()
			if len(data) != count:
				raise FastOneDriveError(f"intervalo incompleto: {len(data)} de {count}")
			finished = True
			return data
		except (OSError, http.client.HTTPException) as error:
			# Ligação keep-alive fechada pelo servidor: refazer uma vez.
			lastError = error
			continue
		finally:
			if not finished:
				# Resposta lida a meio ou erro: a ligação não volta ao pool.
				_dropConnection(parsed.scheme, parsed.netloc)
	assert lastError is not None
	raise lastError
