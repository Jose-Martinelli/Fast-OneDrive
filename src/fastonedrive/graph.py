import configparser
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.rclone import contentToken, parseRemoteModTime

GRAPH_BASE: str = "https://graph.microsoft.com/v1.0"
TOKEN_SKEW: timedelta = timedelta(minutes=2)
GRAPH_TIMEOUT_SEC: int = 60
MAX_RETRIES: int = 4
DELTA_SELECT: str = (
	"id,name,size,lastModifiedDateTime,file,folder,deleted,"
	"parentReference,remoteItem,eTag,root"
)


class GraphDeltaError(FastOneDriveError):
	pass


class GraphDeltaExpired(GraphDeltaError):
	pass


def parseDeltaItem(item: dict[str, Any]) -> dict[str, Any] | None:
	itemId: str = str(item.get("id") or "")
	if itemId == "":
		return None
	if item.get("root") is not None:
		return None
	name: str = str(item.get("name") or "")
	parent: Any = item.get("parentReference")
	parentRef: dict[str, Any] = parent if isinstance(parent, dict) else {}
	parentId: str = str(parentRef.get("id") or "")
	parentHint: str
	parentAtRoot: bool
	parentHint, parentAtRoot = _parentPathHint(parentRef)
	remote: Any = item.get("remoteItem")
	remoteItem: dict[str, Any] = remote if isinstance(remote, dict) else {}
	remoteId: str = str(remoteItem.get("id") or "")
	remoteParent: Any = remoteItem.get("parentReference")
	remoteParentRef: dict[str, Any] = remoteParent if isinstance(remoteParent, dict) else {}
	deleted: bool = isinstance(item.get("deleted"), dict)
	isDir: bool = isinstance(item.get("folder"), dict) or isinstance(remoteItem.get("folder"), dict)
	if name == "" and not deleted:
		return None
	fileMeta: Any = item.get("file")
	fileDict: dict[str, Any] = fileMeta if isinstance(fileMeta, dict) else {}
	mimeType: str = str(fileDict.get("mimeType") or "")
	if isDir:
		mimeType = "inode/directory"
	hashesRaw: Any = fileDict.get("hashes")
	hashes: dict[str, Any] = hashesRaw if isinstance(hashesRaw, dict) else {}
	size: int = int(item.get("size") or 0)
	modTime: str = str(item.get("lastModifiedDateTime") or "")
	return {
		"itemId": itemId,
		"name": name,
		"parentId": parentId,
		"parentHint": parentHint,
		"parentAtRoot": parentAtRoot,
		"isDir": isDir,
		"size": size,
		"modTime": modTime,
		"mimeType": mimeType,
		"etag": contentToken(
			parseRemoteModTime(modTime),
			size,
			str(hashes.get("quickXorHash") or ""),
			str(hashes.get("sha1Hash") or ""),
		),
		"deleted": deleted,
		"remoteDriveId": str(remoteParentRef.get("driveId") or ""),
		"remoteItemId": remoteId,
	}


def _parentPathHint(parentRef: dict[str, Any]) -> tuple[str, bool]:
	raw: str = str(parentRef.get("path") or "")
	if raw == "":
		return "", False
	marker: str = "/root:"
	index: int = raw.find(marker)
	if index < 0:
		return "", False
	rest: str = urllib.parse.unquote(raw[index + len(marker) :].lstrip("/"))
	if rest == "":
		return "", True
	return rest, False


def iterDriveDelta(deltaLink: str = "") -> tuple[Iterator[dict[str, Any]], list[str]]:
	holder: list[str] = [""]
	start: str
	if deltaLink != "":
		start = deltaLink
	else:
		start = _rootDeltaUrl()
	return _iterDeltaPages(start, holder), holder


def iterItemDelta(
	driveId: str,
	itemId: str,
	deltaLink: str = "",
) -> tuple[Iterator[dict[str, Any]], list[str]]:
	holder: list[str] = [""]
	start: str
	if deltaLink != "":
		start = deltaLink
	else:
		start = (
			f"{GRAPH_BASE}/drives/{urllib.parse.quote(driveId, safe='')}"
			f"/items/{urllib.parse.quote(itemId, safe='')}/delta"
			f"?{urllib.parse.urlencode({'$select': DELTA_SELECT})}"
		)
	return _iterDeltaPages(start, holder), holder


def _rootDeltaUrl() -> str:
	driveId: str = _remoteField("drive_id")
	select: str = urllib.parse.urlencode({"$select": DELTA_SELECT})
	if driveId != "":
		encoded: str = urllib.parse.quote(driveId, safe="")
		return f"{GRAPH_BASE}/drives/{encoded}/root/delta?{select}"
	return f"{GRAPH_BASE}/me/drive/root/delta?{select}"


def _iterDeltaPages(
	startUrl: str,
	deltaHolder: list[str],
) -> Iterator[dict[str, Any]]:
	url: str = startUrl
	while url != "":
		payload: dict[str, Any] = _graphGet(url)
		items: Any = payload.get("value")
		if isinstance(items, list):
			for item in items:
				if isinstance(item, dict):
					yield item
		nextLink: str = str(payload.get("@odata.nextLink") or payload.get("odata.nextLink") or "")
		deltaLink: str = str(payload.get("@odata.deltaLink") or payload.get("odata.deltaLink") or "")
		if nextLink != "":
			url = nextLink
			continue
		deltaHolder[0] = deltaLink
		return
	deltaHolder[0] = ""


def _graphGet(url: str) -> dict[str, Any]:
	token: str = accessToken()
	lastError: str = ""
	attempt: int = 0
	while attempt < MAX_RETRIES:
		attempt += 1
		request = urllib.request.Request(
			url,
			headers={
				"Authorization": f"Bearer {token}",
				"Accept": "application/json",
			},
			method="GET",
		)
		try:
			handle = urllib.request.urlopen(request, timeout=GRAPH_TIMEOUT_SEC)
		except urllib.error.HTTPError as error:
			status: int = int(error.code)
			body: str = ""
			try:
				body = error.read().decode("utf-8", errors="replace")
			except Exception:
				body = ""
			if status in (401, 403) and attempt == 1:
				token = accessToken(forceRefresh=True)
				continue
			if status == 410 or (status in (400, 404) and _isDeltaGone(body)):
				raise GraphDeltaExpired("token de delta expirado; é preciso um ciclo completo") from error
			if status == 429:
				_sleepRetry(error, attempt)
				continue
			if status >= 500:
				_sleepRetry(error, attempt)
				continue
			lastError = _graphErrorMessage(status, body)
			raise GraphDeltaError(lastError) from error
		except urllib.error.URLError as error:
			lastError = str(error.reason if error.reason else error)
			if attempt < MAX_RETRIES:
				_sleepSeconds(min(8, 2 ** attempt))
				continue
			raise GraphDeltaError(f"Graph inacessível: {lastError}") from error
		with handle:
			raw: str = handle.read().decode("utf-8", errors="replace")
		try:
			payload: Any = json.loads(raw or "{}")
		except json.JSONDecodeError as error:
			raise GraphDeltaError("resposta Graph inválida") from error
		if not isinstance(payload, dict):
			raise GraphDeltaError("resposta Graph inválida")
		return payload
	raise GraphDeltaError(lastError or "Graph falhou")


def accessToken(forceRefresh: bool = False) -> str:
	if forceRefresh:
		_refreshViaRclone()
	token: dict[str, Any] = _tokenPayload()
	access: str = str(token.get("access_token") or "")
	if access == "":
		raise GraphDeltaError("token OAuth em falta no rclone.conf")
	if forceRefresh:
		return access
	expiry: datetime | None = _parseExpiry(str(token.get("expiry") or ""))
	now: datetime = datetime.now(timezone.utc)
	if expiry is None or expiry - TOKEN_SKEW <= now:
		_refreshViaRclone()
		token = _tokenPayload()
		access = str(token.get("access_token") or "")
		if access == "":
			raise GraphDeltaError("token OAuth em falta após refresh")
	return access


def _refreshViaRclone() -> None:
	from fastonedrive.rclone import rcloneBin

	command: list[str] = [
		rcloneBin(),
		"--config",
		str(config.rcloneConfigPath()),
		"about",
		config.remoteRoot(),
		"--json",
	]
	with config.rcloneConfigLock():
		_runRcloneAbout(command)


def _runRcloneAbout(command: list[str]) -> None:
	result: subprocess.CompletedProcess[str] = subprocess.run(
		command,
		check=False,
		capture_output=True,
		text=True,
		encoding="utf-8",
	)
	if result.returncode != 0:
		detail: list[str] = (result.stderr or result.stdout or "").strip().splitlines()
		last: str = detail[-1] if detail else f"código {result.returncode}"
		raise GraphDeltaError(f"falha a renovar o token rclone: {last}")


def _tokenPayload() -> dict[str, Any]:
	raw: str = _remoteField("token")
	if raw == "":
		raise GraphDeltaError("remote rclone sem token")
	try:
		payload: Any = json.loads(raw)
	except json.JSONDecodeError as error:
		raise GraphDeltaError("token rclone.conf inválido") from error
	if not isinstance(payload, dict):
		raise GraphDeltaError("token rclone.conf inválido")
	return payload


def _remoteField(key: str) -> str:
	config.ensureRcloneConfig()
	from fastonedrive.secretconf import readConfigText

	parser: configparser.RawConfigParser = configparser.RawConfigParser()
	try:
		parser.read_string(readConfigText())
	except (OSError, configparser.Error, FastOneDriveError) as error:
		raise GraphDeltaError(f"rclone.conf ilegível: {error}") from error
	section: str = config.remoteName()
	if not parser.has_section(section):
		raise GraphDeltaError(f"remote {section} não encontrado no rclone.conf")
	if not parser.has_option(section, key):
		return ""
	return parser.get(section, key).strip().strip("'").strip('"')


def _parseExpiry(text: str) -> datetime | None:
	if text == "":
		return None
	try:
		return datetime.fromisoformat(text.replace("Z", "+00:00"))
	except ValueError:
		return None


def _isDeltaGone(body: str) -> bool:
	lower: str = body.lower()
	return "resync" in lower or "resyncrequired" in lower or "delta" in lower


def _graphErrorMessage(status: int, body: str) -> str:
	code: str = ""
	message: str = ""
	try:
		payload: Any = json.loads(body or "{}")
	except json.JSONDecodeError:
		payload = {}
	error: Any = payload.get("error") if isinstance(payload, dict) else None
	if isinstance(error, dict):
		code = str(error.get("code") or "")
		message = str(error.get("message") or "")
	if message == "":
		message = body.strip().splitlines()[-1] if body.strip() else f"HTTP {status}"
	if code != "":
		return f"Graph {status} {code}: {message}"
	return f"Graph {status}: {message}"


def _sleepRetry(error: urllib.error.HTTPError, attempt: int) -> None:
	retryAfter: str = str(error.headers.get("Retry-After") or "") if error.headers else ""
	seconds: int = 0
	if retryAfter.isdigit():
		seconds = int(retryAfter)
	if seconds <= 0:
		seconds = min(16, 2 ** attempt)
	_sleepSeconds(seconds)


def _sleepSeconds(seconds: int) -> None:
	import time

	time.sleep(max(1, seconds))
