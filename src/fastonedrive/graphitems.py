"""Pedidos Graph sobre um item: ligação web, versões e URL de download.

O índice guarda o ``itemId`` de cada caminho. Itens dentro de uma pasta partilhada
(``remoteItem``, ex. TRABALHO) vivem noutro drive: o ``delta_state`` dessa pasta
guarda o ``driveId`` e o ``prefix`` local. Sem ``itemId`` no índice, cai no
endereçamento por caminho (``/root:/caminho:``), que só serve para o drive do perfil.
"""

import base64
import binascii
import json
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.graph import GRAPH_BASE, GRAPH_TIMEOUT_SEC, accessToken, _remoteField

MAX_ATTEMPTS: int = 4
DOWNLOAD_URL_TTL_SEC: float = 600.0

_tokenCache: dict[str, tuple[str, float]] = {}
_tokenLock: threading.Lock = threading.Lock()
_downloadCache: dict[tuple[str, str], tuple[str, float]] = {}
_downloadLock: threading.Lock = threading.Lock()


class GraphItemError(FastOneDriveError):
	pass


class GraphItemGone(GraphItemError):
	pass


class GraphUnreachable(GraphItemError):
	"""Sem rede ou o Graph não responde: quem lê blocos deve parar de tentar um bocado."""


def _cachedToken(forceRefresh: bool = False) -> str:
	key: str = str(config.rcloneConfigPath()) + "|" + config.remoteName()
	now: float = time.monotonic()
	with _tokenLock:
		if not forceRefresh:
			found: tuple[str, float] | None = _tokenCache.get(key)
			if found is not None and found[1] > now:
				return found[0]
		token: str = accessToken(forceRefresh=forceRefresh)
		# accessToken já renova com 2 min de folga; 5 min de cache em memória poupam
		# a releitura do rclone.conf em cada bloco lido pelo FUSE.
		_tokenCache[key] = (token, now + 300.0)
		return token


# Depois de uma falha de rede, os pedidos seguintes falham logo durante uns segundos:
# offline, cada apagar/renomear no Dolphin esperava ~2 s pelas tentativas.
OFFLINE_HOLD_SEC: float = 15.0
_offlineUntil: float = 0.0


def markOffline() -> None:
	global _offlineUntil
	_offlineUntil = time.monotonic() + OFFLINE_HOLD_SEC


def assumedOffline() -> bool:
	return time.monotonic() < _offlineUntil


def graphRequest(method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
	if assumedOffline():
		raise GraphUnreachable("Graph inacessível (sem rede há instantes)")
	try:
		return _graphRequest(method, url, body)
	except GraphUnreachable:
		markOffline()
		raise


def _graphRequest(method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
	token: str = _cachedToken()
	attempt: int = 0
	lastError: str = ""
	data: bytes | None = None if body is None else json.dumps(body).encode("utf-8")
	while attempt < MAX_ATTEMPTS:
		attempt += 1
		headers: dict[str, str] = {
			"Authorization": f"Bearer {token}",
			"Accept": "application/json",
		}
		if data is not None:
			headers["Content-Type"] = "application/json"
		request = urllib.request.Request(url, data=data, headers=headers, method=method)
		try:
			with urllib.request.urlopen(request, timeout=GRAPH_TIMEOUT_SEC) as handle:
				raw: str = handle.read().decode("utf-8", errors="replace")
		except urllib.error.HTTPError as error:
			status: int = int(error.code)
			text: str = ""
			try:
				text = error.read().decode("utf-8", errors="replace")
			except OSError:
				text = ""
			if status in (401, 403) and attempt == 1:
				token = _cachedToken(forceRefresh=True)
				continue
			if status in (404, 410):
				raise GraphItemGone(_errorMessage(status, text)) from error
			if status == 429 or status >= 500:
				retryAfter: str = str(error.headers.get("Retry-After") or "") if error.headers else ""
				time.sleep(int(retryAfter) if retryAfter.isdigit() else min(8, 2 ** attempt))
				lastError = _errorMessage(status, text)
				continue
			raise GraphItemError(_errorMessage(status, text)) from error
		except urllib.error.URLError as error:
			lastError = str(error.reason if error.reason else error)
			if attempt >= 2:
				# Duas falhas de rede seguidas: não prender o leitor (FUSE) em mais esperas.
				raise GraphUnreachable(f"Graph inacessível: {lastError}") from error
			time.sleep(1)
			continue
		except (TimeoutError, OSError) as error:
			raise GraphUnreachable(f"Graph inacessível: {error}") from error
		if raw.strip() == "":
			return {}
		try:
			payload: Any = json.loads(raw)
		except json.JSONDecodeError as error:
			raise GraphItemError("resposta Graph inválida") from error
		return payload if isinstance(payload, dict) else {}
	raise GraphItemError(lastError or "Graph falhou")


def _errorMessage(status: int, body: str) -> str:
	try:
		payload: Any = json.loads(body or "{}")
	except json.JSONDecodeError:
		payload = {}
	error: Any = payload.get("error") if isinstance(payload, dict) else None
	if isinstance(error, dict) and error.get("message"):
		return f"Graph {status}: {error.get('message')}"
	return f"Graph {status}"


def _sharedScope(itemPath: str) -> tuple[str, str, str] | None:
	"""Devolve (driveId, itemIdDaRaiz, prefix) da pasta partilhada que contém o caminho."""
	from fastonedrive.index import connectDb

	connection: sqlite3.Connection = connectDb()
	try:
		try:
			rows: list[sqlite3.Row] = list(
				connection.execute(
					"SELECT driveId, itemId, prefix FROM delta_state WHERE prefix != '' AND driveId != ''"
				)
			)
		except sqlite3.OperationalError:
			return None
	finally:
		connection.close()
	best: tuple[str, str, str] | None = None
	for row in rows:
		prefix: str = str(row["prefix"]).strip("/")
		if prefix == "":
			continue
		if itemPath == prefix or itemPath.startswith(prefix + "/"):
			if best is None or len(prefix) > len(best[2]):
				best = (str(row["driveId"]), str(row["itemId"]), prefix)
	return best


def _splitRcloneId(itemId: str) -> tuple[str, str]:
	"""O índice feito pelo rclone guarda "driveId#itemId"; o feito pelo delta, só o itemId."""
	if "#" in itemId:
		drive, _sep, item = itemId.partition("#")
		return drive, item
	return "", itemId


def itemUrl(itemPath: str) -> str:
	"""URL Graph do item (sem query)."""
	from fastonedrive.index import getEntry

	clean: str = itemPath.strip("/")
	if clean != "":
		found = getEntry(clean)
		rawId: str = str(found["itemId"] or "") if found is not None else ""
		driveFromId, plainId = _splitRcloneId(rawId)
		if driveFromId != "" and plainId != "":
			# O id já diz em que drive está (inclui pastas partilhadas como TRABALHO).
			return (
				f"{GRAPH_BASE}/drives/{urllib.parse.quote(driveFromId, safe='')}"
				f"/items/{urllib.parse.quote(plainId, safe='')}"
			)
	shared: tuple[str, str, str] | None = _sharedScope(clean) if clean != "" else None
	if shared is not None:
		driveId, rootId, prefix = shared
		drive: str = f"{GRAPH_BASE}/drives/{urllib.parse.quote(driveId, safe='')}"
		if clean == prefix:
			return f"{drive}/items/{urllib.parse.quote(rootId, safe='')}"
		entry = getEntry(clean)
		itemId: str = str(entry["itemId"] or "") if entry is not None else ""
		if itemId != "":
			return f"{drive}/items/{urllib.parse.quote(itemId, safe='')}"
		relative: str = clean[len(prefix) + 1 :]
		return f"{drive}/items/{urllib.parse.quote(rootId, safe='')}:/{urllib.parse.quote(relative)}:"
	driveId = _remoteField("drive_id")
	base: str = (
		f"{GRAPH_BASE}/drives/{urllib.parse.quote(driveId, safe='')}"
		if driveId != ""
		else f"{GRAPH_BASE}/me/drive"
	)
	if clean == "":
		return f"{base}/root"
	entry = getEntry(clean)
	itemId = str(entry["itemId"] or "") if entry is not None else ""
	if itemId != "":
		return f"{base}/items/{urllib.parse.quote(itemId, safe='')}"
	return f"{base}/root:/{urllib.parse.quote(clean)}:"


def itemWebUrl(itemPath: str) -> str:
	payload: dict[str, Any] = graphRequest("GET", itemUrl(itemPath) + "?$select=webUrl")
	url: str = str(payload.get("webUrl") or "")
	if url == "":
		raise GraphItemError("o OneDrive não devolveu a ligação web")
	return url


def listVersions(itemPath: str) -> list[dict[str, Any]]:
	payload: dict[str, Any] = graphRequest("GET", itemUrl(itemPath) + "/versions")
	value: Any = payload.get("value")
	if not isinstance(value, list):
		return []
	return [item for item in value if isinstance(item, dict)]


def restoreVersion(itemPath: str, versionId: str) -> None:
	quoted: str = urllib.parse.quote(versionId, safe="")
	graphRequest("POST", itemUrl(itemPath) + f"/versions/{quoted}/restoreVersion", {})


def downloadUrl(itemPath: str, refresh: bool = False) -> str:
	"""URL pré-assinado do conteúdo (não precisa de Authorization; vale ~1 h)."""
	key: tuple[str, str] = (str(config.dbPath()), itemPath)
	now: float = time.monotonic()
	if not refresh:
		with _downloadLock:
			found: tuple[str, float] | None = _downloadCache.get(key)
			if found is not None and found[1] > now:
				return found[0]
	# Sem $select: a anotação @microsoft.graph.downloadUrl nem sempre vem quando se
	# selecciona campos; o item inteiro traz sempre o URL (ficheiros).
	_stat, url = graphStat(itemPath)
	return url


def _rememberDownloadUrl(itemPath: str, url: str) -> None:
	if url == "":
		return
	key: tuple[str, str] = (str(config.dbPath()), itemPath)
	with _downloadLock:
		if len(_downloadCache) > 2048:
			_downloadCache.clear()
		_downloadCache[key] = (url, time.monotonic() + DOWNLOAD_URL_TTL_SEC)


def graphStat(itemPath: str) -> tuple[dict[str, Any], str]:
	"""Metadados no formato do ``rclone lsjson --stat --hash`` e o URL de download.

	Um pedido HTTP (~0,2 s) em vez de arrancar o rclone (vários segundos por chamada).
	ModTime vem de fileSystemInfo, como no rclone; o QuickXor vai em hex, como no rclone.
	"""
	payload: dict[str, Any] = graphRequest("GET", itemUrl(itemPath))
	url: str = str(payload.get("@microsoft.graph.downloadUrl") or "")
	fsInfo: Any = payload.get("fileSystemInfo")
	fsDict: dict[str, Any] = fsInfo if isinstance(fsInfo, dict) else {}
	modTime: str = str(fsDict.get("lastModifiedDateTime") or payload.get("lastModifiedDateTime") or "")
	fileMeta: Any = payload.get("file")
	fileDict: dict[str, Any] = fileMeta if isinstance(fileMeta, dict) else {}
	hashesRaw: Any = fileDict.get("hashes")
	hashes: dict[str, Any] = hashesRaw if isinstance(hashesRaw, dict) else {}
	out: dict[str, str] = {}
	quick: str = str(hashes.get("quickXorHash") or "")
	if quick != "":
		try:
			out["quickxor"] = base64.b64decode(quick).hex()
		except (ValueError, binascii.Error):
			out["quickxor"] = quick
	sha1: str = str(hashes.get("sha1Hash") or "")
	if sha1 != "":
		out["sha1"] = sha1.lower()
	name: str = str(payload.get("name") or itemPath.rsplit("/", 1)[-1])
	stat: dict[str, Any] = {
		"Path": itemPath,
		"Name": name,
		"Size": int(payload.get("size") or 0),
		"MimeType": str(fileDict.get("mimeType") or ""),
		"ModTime": modTime,
		"IsDir": isinstance(payload.get("folder"), dict),
		"ID": str(payload.get("id") or ""),
		"Hashes": out,
	}
	_rememberDownloadUrl(itemPath, url)
	if url == "" and not stat["IsDir"]:
		raise GraphItemError("o OneDrive não devolveu o URL de download")
	return stat, url


def downloadToFile(itemPath: str, dest: Path, url: str = "") -> None:
	"""Descarrega o ficheiro inteiro por HTTP para dest (substitui de forma atómica)."""
	source: str = url or downloadUrl(itemPath)
	partial: Path = dest.with_name(f"{dest.name}.fod-partial")
	partial.unlink(missing_ok=True)
	try:
		try:
			handle = urllib.request.urlopen(source, timeout=GRAPH_TIMEOUT_SEC)
		except urllib.error.HTTPError as error:
			if int(error.code) in (401, 403, 404, 410) and url != "":
				forgetDownloadUrl(itemPath)
				handle = urllib.request.urlopen(downloadUrl(itemPath, refresh=True), timeout=GRAPH_TIMEOUT_SEC)
			else:
				raise GraphItemError(f"HTTP {error.code} ao descarregar") from error
		except (urllib.error.URLError, TimeoutError, OSError) as error:
			raise GraphUnreachable(f"download falhou: {error}") from error
		with handle, partial.open("wb") as out:
			shutil.copyfileobj(handle, out, 1024 * 1024)
		partial.replace(dest)
	except BaseException:
		partial.unlink(missing_ok=True)
		raise


def fastRemoteStat(remotePath: str) -> dict[str, Any] | None:
	"""remoteStat pelo Graph, com o rclone como alternativa."""
	from fastonedrive.paths import remoteItemPath
	from fastonedrive.rclone import remoteStat

	try:
		itemPath: str = remoteItemPath(remotePath)
		if itemPath != "":
			return graphStat(itemPath)[0]
	except GraphUnreachable:
		# Sem rede: o rclone também não chega lá, e arrancá-lo só atrasa quem abriu.
		raise
	except FastOneDriveError:
		# Inclui 404: o itemId do índice pode estar velho (item movido). Só o rclone,
		# por caminho, decide que o item não existe.
		pass
	return remoteStat(remotePath)


def forgetDownloadUrl(itemPath: str) -> None:
	with _downloadLock:
		_downloadCache.pop((str(config.dbPath()), itemPath), None)


SIMPLE_UPLOAD_MAX_BYTES: int = 4 * 1024 * 1024


def uploadSmallFile(itemPath: str, local: Path) -> dict[str, Any]:
	"""Envia um ficheiro pequeno com um PUT /content e copia o mtime local para o
	``fileSystemInfo`` (como o rclone). Devolve o stat no formato do ``rclone lsjson``.

	Um arranque do rclone por gravação custava vários segundos; isto são 2–3 pedidos.
	Quem chama decide conflitos antes (base vs nuvem), como com o ``rclone copyto``.
	"""
	from datetime import datetime, timezone

	info = local.stat()
	if int(info.st_size) > SIMPLE_UPLOAD_MAX_BYTES:
		raise GraphItemError("ficheiro grande demais para envio simples")
	data: bytes = local.read_bytes()
	url: str = itemUrl(itemPath) + "/content"
	payload: dict[str, Any] = _putContent(url, data)
	itemId: str = str(payload.get("id") or "")
	parent: Any = payload.get("parentReference")
	driveId: str = str(parent.get("driveId") or "") if isinstance(parent, dict) else ""
	if itemId == "" or driveId == "":
		raise GraphItemError("resposta do envio sem id")
	stamp: str = (
		datetime.fromtimestamp(info.st_mtime, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
	)
	graphRequest(
		"PATCH",
		f"{GRAPH_BASE}/drives/{urllib.parse.quote(driveId, safe='')}/items/{urllib.parse.quote(itemId, safe='')}",
		{"fileSystemInfo": {"lastModifiedDateTime": stamp}},
	)
	return graphStat(itemPath)[0]


def _putContent(url: str, data: bytes) -> dict[str, Any]:
	if assumedOffline():
		raise GraphUnreachable("Graph inacessível (sem rede há instantes)")
	token: str = _cachedToken()
	attempt: int = 0
	lastError: str = ""
	while attempt < MAX_ATTEMPTS:
		attempt += 1
		request = urllib.request.Request(
			url,
			data=data,
			headers={
				"Authorization": f"Bearer {token}",
				"Accept": "application/json",
				"Content-Type": "application/octet-stream",
			},
			method="PUT",
		)
		try:
			with urllib.request.urlopen(request, timeout=GRAPH_TIMEOUT_SEC) as handle:
				raw: str = handle.read().decode("utf-8", errors="replace")
		except urllib.error.HTTPError as error:
			status: int = int(error.code)
			text: str = ""
			try:
				text = error.read().decode("utf-8", errors="replace")
			except OSError:
				text = ""
			if status in (401, 403) and attempt == 1:
				token = _cachedToken(forceRefresh=True)
				continue
			if status == 429 or status >= 500:
				retryAfter: str = str(error.headers.get("Retry-After") or "") if error.headers else ""
				time.sleep(int(retryAfter) if retryAfter.isdigit() else min(8, 2 ** attempt))
				lastError = _errorMessage(status, text)
				continue
			raise GraphItemError(_errorMessage(status, text)) from error
		except urllib.error.URLError as error:
			markOffline()
			raise GraphUnreachable(f"Graph inacessível: {error.reason if error.reason else error}") from error
		except (TimeoutError, OSError) as error:
			markOffline()
			raise GraphUnreachable(f"Graph inacessível: {error}") from error
		try:
			payload: Any = json.loads(raw or "{}")
		except json.JSONDecodeError as error:
			raise GraphItemError("resposta Graph inválida") from error
		return payload if isinstance(payload, dict) else {}
	raise GraphItemError(lastError or "envio Graph falhou")


class GraphConflict(GraphItemError):
	"""409: já existe um item com esse nome no destino."""


_rootIds: dict[str, str] = {}


def driveOf(itemPath: str) -> str:
	"""Drive onde o item vive: o do id do índice, o da pasta partilhada ou o do perfil."""
	from fastonedrive.index import getEntry

	clean: str = itemPath.strip("/")
	if clean != "":
		found = getEntry(clean)
		drive, _plain = _splitRcloneId(str(found["itemId"] or "") if found is not None else "")
		if drive != "":
			return drive.lower()
		shared: tuple[str, str, str] | None = _sharedScope(clean)
		if shared is not None:
			return shared[0].lower()
	return _remoteField("drive_id").lower()


def plainItemId(itemPath: str) -> str:
	"""itemId sem o prefixo "driveId#"; a raiz do drive é pedida ao Graph (e guardada)."""
	from fastonedrive.index import getEntry

	clean: str = itemPath.strip("/")
	if clean == "":
		key: str = str(config.rcloneConfigPath()) + "|" + config.remoteName()
		if key not in _rootIds:
			payload: dict[str, Any] = graphRequest("GET", itemUrl("") + "?$select=id")
			_rootIds[key] = str(payload.get("id") or "")
		return _rootIds[key]
	found = getEntry(clean)
	return _splitRcloneId(str(found["itemId"] or "") if found is not None else "")[1]


def moveItem(itemPath: str, newPath: str) -> None:
	"""Renomeia/move no Graph (PATCH name + parentReference). 409 → GraphConflict."""
	from fastonedrive.index import parentPath

	body: dict[str, Any] = {"name": newPath.rsplit("/", 1)[-1]}
	if parentPath(itemPath) != parentPath(newPath):
		parentId: str = plainItemId(parentPath(newPath))
		if parentId == "":
			raise GraphItemError("pasta de destino ainda sem id na nuvem")
		body["parentReference"] = {"id": parentId}
	try:
		graphRequest("PATCH", itemUrl(itemPath) + "?@microsoft.graph.conflictBehavior=fail", body)
	except GraphItemError as error:
		if str(error).startswith("Graph 409"):
			raise GraphConflict(str(error)) from error
		raise


def folderHasChildren(itemPath: str) -> bool:
	payload: dict[str, Any] = graphRequest("GET", itemUrl(itemPath) + "/children?$top=1&$select=id")
	value: Any = payload.get("value")
	return isinstance(value, list) and len(value) > 0


def deleteItem(itemPath: str) -> None:
	"""DELETE no Graph (vai para a Reciclagem do OneDrive). Já apagado conta como feito."""
	try:
		graphRequest("DELETE", itemUrl(itemPath))
	except GraphItemGone:
		return



def createFolder(itemPath: str, depth: int = 0) -> dict[str, Any]:
	"""mkdir no Graph (POST children, falha se já existe → usa o existente). Cria as
	pastas de cima que ainda não subiram. Devolve o stat no formato do rclone."""
	from fastonedrive.index import parentPath

	parent: str = parentPath(itemPath)
	body: dict[str, Any] = {
		"name": itemPath.rsplit("/", 1)[-1],
		"folder": {},
		"@microsoft.graph.conflictBehavior": "fail",
	}
	try:
		payload: dict[str, Any] = graphRequest("POST", itemUrl(parent) + "/children", body)
	except GraphItemGone:
		if parent == "" or depth >= 32:
			raise
		createFolder(parent, depth + 1)
		payload = graphRequest("POST", itemUrl(parent) + "/children", body)
	except GraphItemError as error:
		if not str(error).startswith("Graph 409"):
			raise
		payload = graphRequest("GET", itemUrl(itemPath))
	return {
		"Path": itemPath,
		"Name": str(payload.get("name") or body["name"]),
		"Size": 0,
		"ModTime": str(payload.get("lastModifiedDateTime") or ""),
		"IsDir": True,
		"ID": str(payload.get("id") or ""),
		"Hashes": {},
	}
