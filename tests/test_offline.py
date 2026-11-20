"""Sem rede: apagar, renomear e apagar pastas ficam numa fila e saem quando a rede volta."""

from pathlib import Path

import pytest
from conftest import addRow


class _Graph:
	def __init__(self, monkeypatch: pytest.MonkeyPatch, offline: bool) -> None:
		from fastonedrive import graphitems

		self.offline: bool = offline
		self.calls: list[tuple[str, str, object]] = []
		self.module = graphitems
		monkeypatch.setattr(graphitems, "_offlineUntil", 0.0)
		monkeypatch.setattr(graphitems, "_graphRequest", self.request)
		monkeypatch.setattr(graphitems, "itemUrl", lambda itemPath: f"https://g/items/{itemPath}")
		monkeypatch.setattr(graphitems, "driveOf", lambda itemPath: "d")
		monkeypatch.setattr(graphitems, "plainItemId", lambda itemPath: f"id-{itemPath}")

	def request(self, method: str, url: str, body: object = None) -> dict[str, object]:
		if self.offline:
			raise self.module.GraphUnreachable("sem rede")
		self.calls.append((method, url, body))
		return {"value": []}


@pytest.fixture()
def fs(db: object, home: Path, monkeypatch: pytest.MonkeyPatch) -> object:
	pytest.importorskip("fastonedrive.fuse")
	from fastonedrive import fuse, notify

	monkeypatch.setattr(notify, "_runAsync", lambda job: None)
	monkeypatch.setattr(fuse, "scheduleItemSync", lambda itemPath: None)
	monkeypatch.setattr(fuse, "removeRemotePath", lambda *a, **k: pytest.fail("rclone"))
	monkeypatch.setattr(fuse, "moveRemotePath", lambda *a, **k: pytest.fail("rclone"))
	monkeypatch.setattr(fuse, "rmdirRemote", lambda *a, **k: pytest.fail("rclone"))
	return fuse.FastOneDriveFS(home / "OneDrive", "")


def _cloudFile(db: object, path: str, isDir: bool = False) -> None:
	addRow(db, path, isDir=isDir)
	db.execute("UPDATE files SET itemId = ? WHERE path = ?", (f"d#{path}", path))  # type: ignore[attr-defined]
	db.commit()  # type: ignore[attr-defined]


def test_apagar_renomear_e_rmdir_offline_e_enviar_depois(
	fs: object, db: object, monkeypatch: pytest.MonkeyPatch
) -> None:
	from fastonedrive import pendingops
	from fastonedrive.index import getEntry
	from fastonedrive.search import cacheLocalPath

	graph = _Graph(monkeypatch, offline=True)
	_cloudFile(db, "docs", isDir=True)
	_cloudFile(db, "docs/a.txt")
	_cloudFile(db, "docs/b.txt")
	_cloudFile(db, "vazia", isDir=True)
	cached: Path = cacheLocalPath("docs/b.txt")
	cached.parent.mkdir(parents=True, exist_ok=True)
	cached.write_text("b", encoding="utf-8")
	fs.unlink("/docs/a.txt")  # type: ignore[attr-defined]
	fs.rename("/docs/b.txt", "/docs/c.txt")  # type: ignore[attr-defined]
	fs.rmdir("/vazia")  # type: ignore[attr-defined]
	assert getEntry("docs/a.txt") is None
	assert getEntry("docs/c.txt") is not None and getEntry("docs/b.txt") is None
	assert cacheLocalPath("docs/c.txt").read_text(encoding="utf-8") == "b"
	assert getEntry("vazia") is None
	assert [item["op"] for item in pendingops.listPending()] == ["delete", "move", "rmdir"]
	assert pendingops.flushPending() == 0
	graph.offline = False
	monkeypatch.setattr(graph.module, "_offlineUntil", 0.0)
	assert pendingops.flushPending() == 3
	assert graph.calls == [
		("DELETE", "https://g/items/docs/a.txt", None),
		("PATCH", "https://g/items/docs/b.txt?@microsoft.graph.conflictBehavior=rename", {"name": "c.txt"}),
		("GET", "https://g/items/vazia/children?$top=1&$select=id", None),
		("DELETE", "https://g/items/vazia", None),
	]
	assert pendingops.pendingCount() == 0


def test_online_renomear_e_apagar_pelo_graph(fs: object, db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import pendingops
	from fastonedrive.index import getEntry

	graph = _Graph(monkeypatch, offline=False)
	_cloudFile(db, "pasta", isDir=True)
	_cloudFile(db, "x.txt")
	fs.rename("/x.txt", "/pasta/y.txt")  # type: ignore[attr-defined]
	assert getEntry("pasta/y.txt") is not None
	assert graph.calls == [
		(
			"PATCH",
			"https://g/items/x.txt?@microsoft.graph.conflictBehavior=fail",
			{"name": "y.txt", "parentReference": {"id": "id-pasta"}},
		)
	]
	assert pendingops.pendingCount() == 0


def test_rmdir_com_filhos_na_nuvem_recusa(fs: object, db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	import errno

	from fastonedrive._fusepy import FuseOSError

	graph = _Graph(monkeypatch, offline=False)
	monkeypatch.setattr(graph, "request", lambda method, url, body=None: {"value": [{"id": "1"}]})
	monkeypatch.setattr(graph.module, "_graphRequest", graph.request)
	_cloudFile(db, "cheia", isDir=True)
	with pytest.raises(FuseOSError) as raised:
		fs.rmdir("/cheia")  # type: ignore[attr-defined]
	assert raised.value.errno == errno.ENOTEMPTY


def test_operacao_recusada_sai_da_fila(home: Path, db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import graphitems, notify, pendingops

	warned: list[str] = []
	monkeypatch.setattr(notify, "notify", lambda title, body, replaceId=None: warned.append(body))
	monkeypatch.setattr(graphitems, "_offlineUntil", 0.0)

	def refuse(method: str, url: str, body: object = None) -> dict[str, object]:
		raise graphitems.GraphItemError("Graph 403: proibido")

	monkeypatch.setattr(graphitems, "_graphRequest", refuse)
	pendingops.enqueue("delete", "x.txt", "https://g/items/x")
	assert pendingops.flushPending() == 0
	assert pendingops.pendingCount() == 0
	assert len(warned) == 1


def test_sem_rede_falha_logo(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
	import time

	from fastonedrive import graphitems

	monkeypatch.setattr(graphitems, "_offlineUntil", 0.0)
	calls: list[str] = []

	def down(method: str, url: str, body: object = None) -> dict[str, object]:
		calls.append(url)
		raise graphitems.GraphUnreachable("sem rede")

	monkeypatch.setattr(graphitems, "_graphRequest", down)
	for _ in range(3):
		with pytest.raises(graphitems.GraphUnreachable):
			graphitems.graphRequest("GET", "https://g/x")
	assert calls == ["https://g/x"]
	assert graphitems._offlineUntil > time.monotonic()
