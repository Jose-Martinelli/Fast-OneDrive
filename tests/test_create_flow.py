"""Criar pasta → criar ficheiro (Dolphin, via .part) → editar → gravar, com e sem pausa."""

import errno
import os
from pathlib import Path

import pytest
from conftest import addRow


@pytest.fixture()
def fs(db: object, home: Path, monkeypatch: pytest.MonkeyPatch) -> object:
	pytest.importorskip("fastonedrive.fuse")
	from fastonedrive import fuse, notify

	monkeypatch.setattr(notify, "_runAsync", lambda job: None)
	monkeypatch.setattr(fuse, "scheduleItemSync", lambda itemPath: None)
	return fuse.FastOneDriveFS(home / "OneDrive", "")


def test_pausa_nao_deita_fora_o_lote(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import synccontrol, watcher

	armed: list[tuple[set[str], float, bool]] = []
	monkeypatch.setattr(synccontrol, "pauseReason", lambda control=None: "sincronização em pausa até às 22:20")
	monkeypatch.setattr(synccontrol, "pauseRetryDelay", lambda control=None: 42.0)
	monkeypatch.setattr(watcher, "_armBatch", lambda paths, wait, capped: armed.append((paths, wait, capped)))
	watcher._runSync(["docs/a.txt", "docs"])
	assert armed == [({"docs/a.txt", "docs"}, 42.0, False)]


def test_pausa_retry_ate_ao_fim(home: Path) -> None:
	import time

	from fastonedrive import synccontrol

	control = synccontrol.SyncControl(pausedUntil=time.time() + 30)
	assert 30.0 <= synccontrol.pauseRetryDelay(control) <= 32.0
	control.pausedUntil = time.time() + 86400
	assert synccontrol.pauseRetryDelay(control) == 300.0


def test_mkdir_remoto_respeita_a_pausa(fs: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import fuse, synccontrol, watcher

	sent: list[str] = []
	queued: list[str] = []
	monkeypatch.setattr(fuse, "_scheduleRemoteMkdir", lambda itemPath: None)
	fs.mkdir("/teste", 0o755)  # type: ignore[attr-defined]
	monkeypatch.setattr(synccontrol, "pauseReason", lambda control=None: "em pausa")
	monkeypatch.setattr(fuse, "mkdirRemoteFast", lambda remote: sent.append(remote))
	monkeypatch.setattr(watcher, "debounceSync", lambda itemPath, delay=None: queued.append(itemPath))
	fuse._remoteMkdirWorker("teste")
	assert sent == []
	assert queued == ["teste"]


def test_criar_por_part_nao_limpa_ttl_da_listagem(fs: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import listing

	cleared: list[str] = []
	monkeypatch.setattr(listing, "_clearListingStamp", lambda parent: cleared.append(parent))
	fs.mkdir("/teste", 0o755)  # type: ignore[attr-defined]
	fh: int = fs.create("/teste/novo.txt.part", 0o644)  # type: ignore[attr-defined]
	fs.write("/teste/novo.txt.part", b"ola", 0, fh)  # type: ignore[attr-defined]
	fs.release("/teste/novo.txt.part", fh)  # type: ignore[attr-defined]
	fs.rename("/teste/novo.txt.part", "/teste/novo.txt")  # type: ignore[attr-defined]
	assert cleared == []
	assert fs.getxattr("/teste/novo.txt", "user.fastonedrive.state") == b"syncing"  # type: ignore[attr-defined]


def test_escritas_marcam_dirty_uma_vez(fs: object, db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import fuse

	marks: list[str] = []
	real = fuse.markDirty
	monkeypatch.setattr(fuse, "markDirty", lambda itemPath: (marks.append(itemPath), real(itemPath)))
	fh: int = fs.create("/grande.bin", 0o644)  # type: ignore[attr-defined]
	for index in range(20):
		fs.write("/grande.bin", b"x" * 1024, index * 1024, fh)  # type: ignore[attr-defined]
	fs.release("/grande.bin", fh)  # type: ignore[attr-defined]
	assert marks == ["grande.bin"]


def test_set_sync_state_igual_nao_avisa(db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import index, notify

	sent: list[str] = []
	monkeypatch.setattr(notify, "notifyStateChanged", lambda itemPath: sent.append(itemPath))
	addRow(db, "a.txt")
	index.markDirty("a.txt")
	index.markDirty("a.txt")
	index.markDirty("nao-existe.txt")
	assert sent == ["a.txt"]


def test_linha_orfa_nao_aparece_e_sai_no_sync(fs: object, db: object) -> None:
	from fastonedrive import syncpins
	from fastonedrive._fusepy import FuseOSError
	from fastonedrive.index import getEntry, upsertEntry

	upsertEntry("teste2.txt", "teste2.txt", False, 0, None)
	db.execute("UPDATE files SET syncState = 'dirty', etag = '' WHERE path = 'teste2.txt'")  # type: ignore[attr-defined]
	db.commit()  # type: ignore[attr-defined]
	addRow(db, "nuvem.txt")
	with pytest.raises(FuseOSError) as raised:
		fs.getattr("/teste2.txt")  # type: ignore[attr-defined]
	assert raised.value.errno == errno.ENOENT
	names: list[str] = fs.readdir("/", 0)  # type: ignore[attr-defined]
	assert "teste2.txt" not in names and "nuvem.txt" in names
	stats: dict[str, int] = {"deletedLocal": 0}
	syncpins._dropOrphanRows(stats)
	assert getEntry("teste2.txt") is None
	assert getEntry("nuvem.txt") is not None
	assert stats["deletedLocal"] == 1


def test_prune_nao_apaga_download_a_correr(home: Path) -> None:
	from fastonedrive.disk import pruneJobArtifacts

	root: Path = home / "cache"
	root.mkdir()
	fresh: Path = root / "video.mkv.fod-partial"
	fresh.write_bytes(b"a")
	old: Path = root / "velho.mkv.fod-partial"
	old.write_bytes(b"a")
	os.utime(old, (1, 1))
	pruneJobArtifacts(root, includePartials=True)
	assert fresh.exists()
	assert not old.exists()


def _fakeUpload(monkeypatch: pytest.MonkeyPatch, during: object = None) -> list[str]:
	from fastonedrive import syncpins

	sent: list[str] = []

	def copy(local: str, remote: str, onStats: object = None) -> None:
		sent.append(Path(local).read_text(encoding="utf-8"))
		if callable(during):
			during()

	monkeypatch.setattr(syncpins, "copyFromLocal", copy)
	monkeypatch.setattr(
		syncpins,
		"quickStat",
		lambda itemPath, remotePath: (
			None
			if len(sent) == 0
			else {"ModTime": "2026-11-18T12:00:00Z", "Size": len(sent[-1]), "ID": "x!1", "Hashes": {"quickxor": "ab" * 20}, "IsDir": False}
		),
	)
	return sent


def test_envio_nao_bloqueia_o_editor(home: Path, db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	import fcntl

	from fastonedrive import syncpins
	from fastonedrive.disk import writeGuardFile
	from fastonedrive.index import getEntry, markDirty, upsertEntry
	from fastonedrive.search import cacheLocalPath

	local: Path = cacheLocalPath("doc.txt")
	local.parent.mkdir(parents=True, exist_ok=True)
	local.write_text("v1", encoding="utf-8")
	upsertEntry("doc.txt", "doc.txt", False, 2, None)
	markDirty("doc.txt")
	free: list[bool] = []

	def editorOpens() -> None:
		fd: int = os.open(str(writeGuardFile("doc.txt")), os.O_CREAT | os.O_RDWR, 0o644)
		try:
			fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
			free.append(True)
		except BlockingIOError:
			free.append(False)
		finally:
			os.close(fd)

	sent: list[str] = _fakeUpload(monkeypatch, editorOpens)
	stats: dict[str, int] = {key: 0 for key in ("uploaded", "errors", "skipped")}
	syncpins._uploadLocal("doc.txt", local, stats, None)
	assert sent == ["v1"]
	assert free == [True]
	assert stats["uploaded"] == 1
	entry = getEntry("doc.txt")
	assert entry is not None and str(entry["syncState"]) == "synced"
	assert not any((home / ".local").rglob("upload-snapshots/*"))


def test_gravado_durante_envio_fica_por_enviar(home: Path, db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import syncpins, watcher
	from fastonedrive.index import getEntry, markDirty, upsertEntry
	from fastonedrive.search import cacheLocalPath

	local: Path = cacheLocalPath("doc.txt")
	local.parent.mkdir(parents=True, exist_ok=True)
	local.write_text("v1", encoding="utf-8")
	upsertEntry("doc.txt", "doc.txt", False, 2, None)
	markDirty("doc.txt")
	queued: list[str] = []
	monkeypatch.setattr(watcher, "debounceSync", lambda itemPath, delay=None: queued.append(itemPath))
	_fakeUpload(monkeypatch, lambda: local.write_text("v2 mais longo", encoding="utf-8"))
	stats: dict[str, int] = {key: 0 for key in ("uploaded", "errors", "skipped")}
	syncpins._uploadLocal("doc.txt", local, stats, None)
	entry = getEntry("doc.txt")
	assert entry is not None
	assert str(entry["syncState"]) == "dirty"
	assert str(entry["baseEtag"]) != ""
	assert local.read_text(encoding="utf-8") == "v2 mais longo"
	assert queued == ["doc.txt"]


def test_temporario_ja_renomeado_nao_vai_a_nuvem(home: Path, db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import syncpins

	def boom(itemPath: str, remotePath: str) -> None:
		raise AssertionError("não devia consultar a nuvem")

	monkeypatch.setattr(syncpins, "quickStat", boom)
	stats: dict[str, int] = {key: 0 for key in ("uploaded", "errors", "skipped", "downloaded", "deletedLocal")}
	syncpins._syncOnePath("pasta/nome.txt.AbCdEf", stats, None, [])
	assert stats["skipped"] == 1


def test_envio_simples_pelo_graph(home: Path, db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import graphitems

	calls: list[tuple[str, str, object]] = []
	monkeypatch.setattr(graphitems, "itemUrl", lambda itemPath: "https://g/me/drive/root:/a/b.txt:")
	monkeypatch.setattr(
		graphitems,
		"_putContent",
		lambda url, data: calls.append(("PUT", url, data)) or {"id": "ID1", "parentReference": {"driveId": "D1"}},
	)
	monkeypatch.setattr(
		graphitems, "graphRequest", lambda method, url, body=None: calls.append((method, url, body)) or {}
	)
	monkeypatch.setattr(graphitems, "graphStat", lambda itemPath: ({"Size": 3, "ModTime": "x"}, ""))
	local: Path = home / "b.txt"
	local.write_bytes(b"abc")
	os.utime(local, (1795000000, 1795000000))
	stat = graphitems.uploadSmallFile("a/b.txt", local)
	assert stat["Size"] == 3
	assert calls[0] == ("PUT", "https://g/me/drive/root:/a/b.txt:/content", b"abc")
	assert calls[1][0] == "PATCH" and calls[1][1].endswith("/drives/D1/items/ID1")
	assert calls[1][2] == {"fileSystemInfo": {"lastModifiedDateTime": "2026-11-18T11:06:40Z"}}


def test_apagar_ficheiro_nunca_enviado_sem_rede(fs: object, db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import fuse, graphitems
	from fastonedrive.index import getEntry
	from fastonedrive.search import cacheLocalPath

	monkeypatch.setattr(fuse, "removeRemotePath", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rclone")))
	monkeypatch.setattr(graphitems, "graphRequest", lambda *a, **k: (_ for _ in ()).throw(AssertionError("graph")))
	fh: int = fs.create("/teste.txt.FFrWzy", 0o644)  # type: ignore[attr-defined]
	fs.release("/teste.txt.FFrWzy", fh)  # type: ignore[attr-defined]
	fs.unlink("/teste.txt.FFrWzy")  # type: ignore[attr-defined]
	assert getEntry("teste.txt.FFrWzy") is None
	assert not cacheLocalPath("teste.txt.FFrWzy").exists()


def test_apagar_item_da_nuvem_pelo_graph(fs: object, db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import fuse, graphitems
	from fastonedrive.index import getEntry

	calls: list[tuple[str, str]] = []
	monkeypatch.setattr(fuse, "removeRemotePath", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rclone")))
	monkeypatch.setattr(graphitems, "itemUrl", lambda itemPath: f"https://g/items/{itemPath}")
	monkeypatch.setattr(graphitems, "graphRequest", lambda method, url, body=None: calls.append((method, url)) or {})
	addRow(db, "nuvem.txt")
	db.execute("UPDATE files SET itemId = 'D#1' WHERE path = 'nuvem.txt'")  # type: ignore[attr-defined]
	db.commit()  # type: ignore[attr-defined]
	fs.unlink("/nuvem.txt")  # type: ignore[attr-defined]
	assert calls == [("DELETE", "https://g/items/nuvem.txt")]
	assert getEntry("nuvem.txt") is None


def test_abrir_part_nao_pergunta_a_nuvem(fs: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import fuse

	monkeypatch.setattr(fuse, "cleanCacheIsStale", lambda itemPath: (_ for _ in ()).throw(AssertionError("stale")))
	fh: int = fs.create("/novo.txt.part", 0o644)  # type: ignore[attr-defined]
	fs.release("/novo.txt.part", fh)  # type: ignore[attr-defined]
	fh = fs.open("/novo.txt.part", os.O_WRONLY)  # type: ignore[attr-defined]
	fs.release("/novo.txt.part", fh)  # type: ignore[attr-defined]
	fh = fs.create("/novo2.txt", 0o644)  # type: ignore[attr-defined]
	fs.release("/novo2.txt", fh)  # type: ignore[attr-defined]
	fh = fs.open("/novo2.txt", os.O_RDONLY)  # type: ignore[attr-defined]
	fs.release("/novo2.txt", fh)  # type: ignore[attr-defined]
