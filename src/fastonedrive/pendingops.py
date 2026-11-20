"""Fila de operações feitas sem rede (como o OneDrive do Windows).

Sem rede, apagar, renomear/mover e apagar pastas no ``~/OneDrive`` são aplicados logo
no disco e no índice, e a operação na nuvem fica aqui (SQLite do perfil). O poll da
nuvem (``remotewatch``) envia a fila antes de ler o delta, para o delta não desfazer
o que foi feito offline. Pastas criadas e ficheiros editados offline não entram aqui:
ficam ``dirty`` e sobem pelo ``sync-pins``.

Cada operação guarda o URL do item por id (vale mesmo depois de outro rename local).
Erro definitivo (409, 403, pasta com filhos) tira a operação da fila e avisa.
"""

import sqlite3
import time
from typing import Any

from fastonedrive import FastOneDriveError
from fastonedrive.log import logError, logInfo

OP_DELETE: str = "delete"
OP_MOVE: str = "move"
OP_RMDIR: str = "rmdir"

_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS pending_ops (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	op TEXT NOT NULL,
	path TEXT NOT NULL,
	dest TEXT NOT NULL DEFAULT '',
	url TEXT NOT NULL,
	createdAt REAL NOT NULL,
	attempts INTEGER NOT NULL DEFAULT 0,
	lastError TEXT NOT NULL DEFAULT ''
)
"""


def _connect() -> sqlite3.Connection:
	from fastonedrive.index import connectDb

	connection: sqlite3.Connection = connectDb()
	connection.execute(_SCHEMA)
	return connection


def enqueue(op: str, path: str, url: str, dest: str = "") -> None:
	connection: sqlite3.Connection = _connect()
	try:
		connection.execute(
			"INSERT INTO pending_ops (op, path, dest, url, createdAt) VALUES (?, ?, ?, ?, ?)",
			(op, path, dest, url, time.time()),
		)
		connection.commit()
	finally:
		connection.close()
	logInfo("operação offline na fila", extra={"op": op, "path": path, "dest": dest})


def pendingCount() -> int:
	connection: sqlite3.Connection = _connect()
	try:
		return int(connection.execute("SELECT COUNT(*) FROM pending_ops").fetchone()[0])
	finally:
		connection.close()


def listPending() -> list[dict[str, Any]]:
	connection: sqlite3.Connection = _connect()
	try:
		return [
			dict(row)
			for row in connection.execute(
				"SELECT id, op, path, dest, url, createdAt, attempts, lastError FROM pending_ops ORDER BY id"
			)
		]
	finally:
		connection.close()


def _finish(opId: int) -> None:
	connection: sqlite3.Connection = _connect()
	try:
		connection.execute("DELETE FROM pending_ops WHERE id = ?", (opId,))
		connection.commit()
	finally:
		connection.close()


def _noteFailure(opId: int, error: str) -> None:
	connection: sqlite3.Connection = _connect()
	try:
		connection.execute(
			"UPDATE pending_ops SET attempts = attempts + 1, lastError = ? WHERE id = ?",
			(error, opId),
		)
		connection.commit()
	finally:
		connection.close()


def flushPending() -> int:
	"""Envia a fila por ordem. Pára na primeira falha de rede. Devolve quantas saíram."""
	from fastonedrive import graphitems

	done: int = 0
	for item in listPending():
		opId: int = int(item["id"])
		try:
			_apply(item)
		except graphitems.GraphUnreachable as error:
			_noteFailure(opId, str(error))
			break
		except _Permanent as error:
			_finish(opId)
			logError(f"operação offline descartada: {error}", extra={"op": item["op"], "path": item["path"]})
			_warn(item, str(error))
			continue
		except FastOneDriveError as error:
			# Outros erros do Graph (5xx esgotado, token): tentar no próximo ciclo.
			_noteFailure(opId, str(error))
			break
		_finish(opId)
		done += 1
	if done > 0:
		logInfo("fila offline enviada", extra={"ops": done})
	return done


class _Permanent(FastOneDriveError):
	pass


def _apply(item: dict[str, Any]) -> None:
	from fastonedrive import graphitems
	from fastonedrive.index import parentPath

	op: str = str(item["op"])
	url: str = str(item["url"])
	try:
		if op == OP_DELETE:
			try:
				graphitems.graphRequest("DELETE", url)
			except graphitems.GraphItemGone:
				return
			return
		if op == OP_RMDIR:
			try:
				payload: dict[str, Any] = graphitems.graphRequest("GET", url + "/children?$top=1&$select=id")
			except graphitems.GraphItemGone:
				return
			children: Any = payload.get("value")
			if isinstance(children, list) and len(children) > 0:
				raise _Permanent("a pasta tinha ficheiros novos na nuvem; não foi apagada")
			graphitems.graphRequest("DELETE", url)
			return
		if op == OP_MOVE:
			oldPath: str = str(item["path"])
			newPath: str = str(item["dest"])
			body: dict[str, Any] = {"name": newPath.rsplit("/", 1)[-1]}
			if parentPath(oldPath) != parentPath(newPath):
				parentId: str = graphitems.plainItemId(parentPath(newPath))
				if parentId == "":
					# A pasta de destino foi criada offline e ainda não subiu.
					raise FastOneDriveError("pasta de destino ainda sem id na nuvem")
				body["parentReference"] = {"id": parentId}
			graphitems.graphRequest("PATCH", url + "?@microsoft.graph.conflictBehavior=rename", body)
			return
	except graphitems.GraphItemGone as error:
		raise _Permanent(f"o item já não existe na nuvem ({error})") from error
	except graphitems.GraphUnreachable:
		raise
	except graphitems.GraphItemError as error:
		text: str = str(error)
		if text.startswith(("Graph 400", "Graph 403", "Graph 409")):
			raise _Permanent(text) from error
		raise
	raise _Permanent(f"operação desconhecida: {op}")


def _warn(item: dict[str, Any], reason: str) -> None:
	from fastonedrive.notify import notify

	name: str = str(item["path"]).rsplit("/", 1)[-1]
	notify(
		"OneDrive: alteração offline não aplicada",
		f"{name}: {reason}. Na próxima sincronização a pasta mostra o que está na nuvem.",
	)
