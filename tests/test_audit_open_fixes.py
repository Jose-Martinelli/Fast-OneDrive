"""Regressões dos achados abertos da auditoria de 2026-11-17."""

import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from conftest import addRow
from test_audit_regressions import _syncedCacheFile

from fastonedrive import chunks


def _cliFlagPairs(flags: list[str]) -> list[str]:
	return [flags[index + 1] for index, flag in enumerate(flags) if flag == "--exclude"]


def test_skip_pin_exclui_pasta_no_topo_e_aninhada(monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import config
	from fastonedrive.pin import skipPinCopyFlags

	monkeypatch.setattr(config, "skipPinSegments", lambda: ("roms",))
	excludes: list[str] = _cliFlagPairs(skipPinCopyFlags())
	assert "/roms/**" in excludes
	assert "/roms" in excludes
	assert "**/roms/**" in excludes
	assert "**/roms" in excludes


@pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone ausente")
def test_skip_pin_filtro_rclone_real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import config
	from fastonedrive.pin import skipPinCopyFlags

	monkeypatch.setattr(config, "skipPinSegments", lambda: ("roms",))
	for rel in ("roms/a/f", "roms/top", "x/roms/b/g", "y/h"):
		target: Path = tmp_path / "src" / rel
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_text("x")
	result: subprocess.CompletedProcess[str] = subprocess.run(
		["rclone", "lsf", "-R", "--files-only", *skipPinCopyFlags(), str(tmp_path / "src")],
		check=True,
		capture_output=True,
		text=True,
	)
	assert result.stdout.split() == ["y/h"]


def test_sanitize_flags_salta_o_valor_do_flag_removido() -> None:
	from fastonedrive.config import _sanitizeUserFlags

	parts: list[str] = [
		"--vfs-cache-max-size",
		"128M",
		"--transfers",
		"4",
		"--rc-user",
		"bob",
		"--vfs-refresh",
		"--bwlimit",
		"1M",
		"--vfs-cache-mode=full",
	]
	assert _sanitizeUserFlags(parts, allowVfs=False) == ["--transfers", "4", "--bwlimit", "1M"]
	kept: list[str] = _sanitizeUserFlags(parts, allowVfs=True)
	assert kept[:2] == ["--vfs-cache-max-size", "128M"]
	assert "bob" not in kept


def test_falha_de_descarga_nao_bloqueia_nem_conta_como_edicao(db: object) -> None:
	from datetime import datetime, timezone

	from fastonedrive.index import SYNC_ERROR, SYNC_SYNCING, getEntry, setSyncState
	from fastonedrive.syncpins import (
		DOWNLOAD_MARK,
		_downloadBlocked,
		_downloadStateBlocked,
		_localChanged,
	)

	addRow(db, "doc.txt")
	now: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)
	setSyncState("doc.txt", SYNC_ERROR, f"{DOWNLOAD_MARK}: sem rede")
	entry = getEntry("doc.txt")
	assert not _downloadBlocked("doc.txt", False)
	assert not _downloadStateBlocked("doc.txt")
	assert not _localChanged(now, now, entry)
	setSyncState("doc.txt", SYNC_SYNCING, DOWNLOAD_MARK)
	assert not _downloadBlocked("doc.txt", False)
	assert not _localChanged(now, now, getEntry("doc.txt"))
	setSyncState("doc.txt", SYNC_ERROR, "envio falhou")
	assert _downloadBlocked("doc.txt", False)
	assert _localChanged(now, now, getEntry("doc.txt"))


def test_libertar_pasta_limpa_chunks_da_subarvore(db: object, home: Path) -> None:
	from fastonedrive import config
	from fastonedrive.pin import freeLocalSpace

	addRow(db, "Pasta", isDir=True)
	addRow(db, "Pasta/sub", isDir=True)
	for itemPath in ("Pasta/sub/a.bin", "Outra/b.bin"):
		_syncedCacheFile(db, itemPath, b"x")
		chunks._saveRow(itemPath, "qxh:base", chunks.CHUNK_SIZE * 2, bytes(1))
	chunks._pendingWrites["Pasta/sub/a.bin"] = [(0, 1)]
	freeLocalSpace(config.remotePath("Pasta"))
	assert chunks._loadRow("Pasta/sub/a.bin") is None
	assert "Pasta/sub/a.bin" not in chunks._pendingWrites
	assert chunks._loadRow("Outra/b.bin") is not None


def test_prepare_sparse_encolhe_quando_remoto_diminui(db: object) -> None:
	size: int = chunks.CHUNK_SIZE * 2
	addRow(db, "grande.bin", size=size)
	target: Path = chunks.prepareSparse("grande.bin", size, "qxh:a")
	assert target.stat().st_size == size
	chunks._saveRow("grande.bin", "qxh:a", chunks.CHUNK_SIZE, bytes(1))
	again: Path = chunks.prepareSparse("grande.bin", chunks.CHUNK_SIZE, "qxh:a")
	assert again.stat().st_size == chunks.CHUNK_SIZE


def test_debounce_junta_varios_ficheiros_num_so_sync(
	db: object,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	from fastonedrive import watcher

	batches: list[list[str]] = []
	done: threading.Event = threading.Event()

	def fake(itemPaths: list[str]) -> None:
		batches.append(itemPaths)
		done.set()

	monkeypatch.setattr(watcher, "_runSync", fake)
	monkeypatch.setattr(watcher, "DEBOUNCE_SEC", 0.05)
	for name in ("a.txt", "b.txt", "c.txt"):
		watcher.debounceSync(name)
	assert done.wait(2.0)
	assert batches == [["a.txt", "b.txt", "c.txt"]]


def test_run_sync_respeita_a_pausa(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import synccontrol, syncpins, watcher

	called: list[str] = []
	monkeypatch.setattr(synccontrol, "pauseReason", lambda control=None: "em pausa")
	monkeypatch.setattr(syncpins, "_syncPinsLocked", lambda onStats, itemPath: called.append(str(itemPath)))
	watcher._runSync(["a.txt"])
	assert called == []
	monkeypatch.setattr(synccontrol, "pauseReason", lambda control=None: "")
	monkeypatch.setattr("fastonedrive.disk.pinsLock", _noLock)
	watcher._runSync(["a.txt", "b.txt"])
	assert called == ["a.txt", "b.txt"]


class _noLock:
	def __enter__(self) -> None:
		return None

	def __exit__(self, *args: object) -> None:
		return None


def test_staged_raizes_e_agendamento_unico(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import config, mutate

	assert mutate._stagedRoots(["d", "d/a", "d/b/c", "e.txt"]) == ["d", "e.txt"]
	launched: list[list[str]] = []
	monkeypatch.setattr(config, "cliBin", lambda: "/bin/fastonedrive")
	monkeypatch.setattr(mutate.subprocess, "Popen", lambda command, **kwargs: launched.append(command))
	monkeypatch.setattr(mutate, "STAGED_DEBOUNCE_SEC", 0.05)
	mutate._scheduleStaged(["d", "d/a"])
	mutate._scheduleStaged(["e.txt"])
	timer: threading.Timer | None = mutate._stagedTimer
	assert timer is not None
	timer.join(2.0)
	assert len(launched) == 1
	assert launched[0][-4:] == ["--path", "d", "--path", "e.txt"]


def test_kdirnotify_usa_busctl_e_cai_para_dbus_send(monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import notify

	calls: list[list[str]] = []
	codes: list[int] = [0]

	def fakeRun(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
		calls.append(command)
		assert kwargs.get("timeout") is not None
		return subprocess.CompletedProcess(command, codes[0], b"", b"")

	monkeypatch.setattr(notify.shutil, "which", lambda name: f"/usr/bin/{name}")
	monkeypatch.setattr(notify.subprocess, "run", fakeRun)
	notify._emitKdirNotify(["file:///a", "file:///b"])
	assert len(calls) == 1
	assert calls[0][1:] == [
		"--user",
		"emit",
		"/org/kde/KDirNotify",
		"org.kde.KDirNotify",
		"FilesChanged",
		"as",
		"2",
		"file:///a",
		"file:///b",
	]
	calls.clear()
	codes[0] = 1
	notify._emitKdirNotify(["file:///a"])
	assert [Path(call[0]).name for call in calls] == ["busctl", "dbus-send"]


def test_cli_bin_usa_usr_bin_sem_local(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import config

	real = Path.is_file
	monkeypatch.setattr(
		Path,
		"is_file",
		lambda self: True if str(self) == "/usr/bin/fastonedrive" else real(self),
	)
	assert config.cliBin() == "/usr/bin/fastonedrive"


def test_prepare_sparse_nao_encolhe_cache_sujo(db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	size: int = chunks.CHUNK_SIZE * 2
	addRow(db, "edit.bin", size=size)
	target: Path = chunks.prepareSparse("edit.bin", size, "qxh:a")
	chunks._saveRow("edit.bin", "qxh:a", chunks.CHUNK_SIZE, bytes(1))
	monkeypatch.setattr(chunks, "_cacheIsDirty", lambda itemPath: True)
	chunks.prepareSparse("edit.bin", chunks.CHUNK_SIZE, "qxh:a")
	assert os.stat(target).st_size == size


def test_row_from_lsjson_prefixa_filho_com_nome_do_prefixo() -> None:
	from fastonedrive.index import _rowFromLsjson

	row = _rowFromLsjson(
		{"Name": "A", "Path": "A", "IsDir": True, "Size": 0, "ModTime": "", "ID": "1"},
		"A",
		"2026-01-01T00:00:00+00:00",
	)
	assert row is not None
	assert row[0] == "A/A"
	nested = _rowFromLsjson(
		{"Name": "x", "Path": "A/x", "IsDir": False, "Size": 1, "ModTime": "", "ID": "2"},
		"A",
		"2026-01-01T00:00:00+00:00",
	)
	assert nested is not None
	assert nested[0] == "A/A/x"


def test_delta_partilha_mantem_itemid_do_atalho(db: object) -> None:
	from fastonedrive.index import _flushDeltaPending, getEntry

	ids: dict[str, str] = {"shortcut": "TRABALHO"}
	parents: dict[str, str] = {}
	seen: set[str] = set()
	shared: dict[str, tuple[str, str, str]] = {
		"shortcut": ("drive", "remoteRoot", "TRABALHO"),
	}
	db.execute(
		"INSERT INTO files (path, name, parent, isDir, size, modTime, mimeType, etag, itemId, pinned, updatedAt)"
		" VALUES (?, ?, ?, 1, 0, '', '', '', ?, 0, ?)",
		("TRABALHO", "TRABALHO", "", "shortcut", "2026-01-01T00:00:00+00:00"),
	)
	db.commit()
	_flushDeltaPending(
		db,
		[
			{
				"itemId": "remoteRoot",
				"name": "TRABALHO",
				"parentId": "",
				"isDir": True,
				"size": 0,
				"modTime": "",
				"mimeType": "",
				"etag": "e1",
				"remoteItemId": "",
				"remoteDriveId": "",
			}
		],
		ids,
		parents,
		seen,
		"TRABALHO",
		"remoteRoot",
		shared,
		scopeId="shortcut",
	)
	entry = getEntry("TRABALHO")
	assert entry is not None
	assert str(entry["itemId"]) == "shortcut"
	assert ids["shortcut"] == "TRABALHO"
	assert ids["remoteRoot"] == "TRABALHO"


def test_delete_delta_remove_item_so_na_nuvem(db: object) -> None:
	from fastonedrive.index import _flushDeltaDeletes, getEntry

	db.execute(
		"INSERT INTO files (path, name, parent, isDir, size, modTime, mimeType, etag, itemId, pinned, updatedAt, baseEtag)"
		" VALUES (?, ?, '', 0, 1, '', '', '', ?, 0, ?, '')",
		("so_nuvem.txt", "so_nuvem.txt", "id1", "2026-01-01T00:00:00+00:00"),
	)
	db.commit()
	ids: dict[str, str] = {"id1": "so_nuvem.txt"}
	seen: set[str] = {"so_nuvem.txt"}
	removed: int = _flushDeltaDeletes(
		db,
		[{"itemId": "id1"}],
		ids,
		seen,
	)
	assert removed == 1
	assert getEntry("so_nuvem.txt") is None


def test_rebuild_apos_delta_expirado_respeita_delta_only(
	db: object,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	from fastonedrive import FastOneDriveError, index
	from fastonedrive.graph import GraphDeltaError, GraphDeltaExpired

	calls: list[str] = []

	def boom(_connection: object, _root: str, _startedAt: str) -> int:
		calls.append("graph")
		if len(calls) == 1:
			raise GraphDeltaExpired("expired")
		raise GraphDeltaError("403")

	monkeypatch.setattr(index, "_rebuildGraph", boom)
	monkeypatch.setenv(index.DELTA_ONLY_ENV, "1")
	with pytest.raises(FastOneDriveError, match="poll adiado"):
		index._rebuild(db, "onedrive:", "2026-01-01T00:00:00+00:00")
	assert calls == ["graph", "graph"]


def test_rebuild_graph_partilha_completa_sem_link_incremental(
	db: object,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	from fastonedrive import index

	links: list[str] = []

	def fakeIterDrive(_link: str) -> tuple[list[dict[str, object]], list[str]]:
		return [], ["root-link"]

	def fakeIterItem(_driveId: str, _remoteId: str, link: str) -> tuple[list[dict[str, object]], list[str]]:
		links.append(link)
		return [], ["share-link"]

	def fakeApply(
		_connection: object,
		_pages: object,
		_ids: dict[str, str],
		_parents: dict[str, str],
		_seen: set[str],
		count: int,
		_root: str,
		_startedAt: str,
		_mode: str,
		_prefix: str,
		_scopeRootId: str,
		shared: dict[str, tuple[str, str, str]],
		scopeId: str = "",
	) -> tuple[int, dict[str, tuple[str, str, str]], int]:
		return count, shared, 0

	db.execute(
		"INSERT INTO delta_state (scope, driveId, itemId, deltaLink, prefix, updatedAt)"
		" VALUES ('shortcut', 'drv', 'remote', 'old-link', 'TRABALHO', '2026-01-01T00:00:00+00:00')"
	)
	db.commit()
	monkeypatch.setattr(index, "iterDriveDelta", fakeIterDrive)
	monkeypatch.setattr(index, "iterItemDelta", fakeIterItem)
	monkeypatch.setattr(index, "_applyDeltaFeed", fakeApply)
	monkeypatch.setattr(index, "_loadItemIdMap", lambda _c: {})
	monkeypatch.setattr(index, "_loadParentIdMap", lambda _c: {})
	monkeypatch.setattr(index, "_setIndexJobStatus", lambda *a, **k: None)
	index._rebuildGraph(db, "onedrive:", "2026-01-01T00:00:00+00:00")
	assert links == [""]


def test_rebuild_graph_403_na_partilha_nao_aborta(
	db: object,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	from fastonedrive import index
	from fastonedrive.graph import GraphDeltaError

	def fakeIterDrive(_link: str) -> tuple[list[dict[str, object]], list[str]]:
		return [], ["root-link"]

	def fakeIterItem(_driveId: str, _remoteId: str, _link: str) -> tuple[list[dict[str, object]], list[str]]:
		raise GraphDeltaError("403 Forbidden")

	def fakeApply(
		_connection: object,
		_pages: object,
		_ids: dict[str, str],
		_parents: dict[str, str],
		_seen: set[str],
		count: int,
		_root: str,
		_startedAt: str,
		_mode: str,
		_prefix: str,
		_scopeRootId: str,
		shared: dict[str, tuple[str, str, str]],
		scopeId: str = "",
	) -> tuple[int, dict[str, tuple[str, str, str]], int]:
		return count, shared, 0

	db.execute(
		"INSERT INTO delta_state (scope, driveId, itemId, deltaLink, prefix, updatedAt)"
		" VALUES ('shortcut', 'drv', 'remote', '', 'TRABALHO', '2026-01-01T00:00:00+00:00')"
	)
	db.commit()
	monkeypatch.setattr(index, "iterDriveDelta", fakeIterDrive)
	monkeypatch.setattr(index, "iterItemDelta", fakeIterItem)
	monkeypatch.setattr(index, "_applyDeltaFeed", fakeApply)
	monkeypatch.setattr(index, "_loadItemIdMap", lambda _c: {})
	monkeypatch.setattr(index, "_loadParentIdMap", lambda _c: {})
	monkeypatch.setattr(index, "_setIndexJobStatus", lambda *a, **k: None)
	dropped: list[str] = []
	monkeypatch.setattr(index, "_dropUnseenRows", lambda *a, **k: dropped.append("drop"))
	count: int = index._rebuildGraph(db, "onedrive:", "2026-01-01T00:00:00+00:00")
	assert count == 0
	assert dropped == []


def test_bwlimit_horario_com_off_e_aceite() -> None:
	from fastonedrive.synccontrol import _cleanBwLimit

	assert _cleanBwLimit("08:00,512k 12:00,10M 23:00,off") == "08:00,512k 12:00,10M 23:00,off"
	assert _cleanBwLimit("off") == ""
	assert _cleanBwLimit("10M:2M") == "10M:2M"


def test_prune_nao_apaga_staging_recente(tmp_path: Path) -> None:
	from fastonedrive.disk import pruneJobArtifacts

	staging: Path = tmp_path / ".fod-staging-doc.txt"
	staging.write_text("em curso")
	assert pruneJobArtifacts(tmp_path, includePartials=True) == 0
	assert staging.is_file()
	os.utime(staging, (0, 0))
	assert pruneJobArtifacts(tmp_path, includePartials=True) == 1
	assert not staging.exists()


def test_drop_unseen_remove_so_na_nuvem(db: object) -> None:
	from fastonedrive.index import _dropUnseenRows, setBaseline

	addRow(db, "vivo.txt", size=3)
	setBaseline("vivo.txt", "2026-01-01T00:00:00Z", "qxh:a")
	addRow(db, "fantasma.txt", size=3)
	_dropUnseenRows(db, {"vivo.txt"}, "", "2026-02-01T00:00:00+00:00")
	paths = {str(row[0]) for row in db.execute("SELECT path FROM files")}
	assert "vivo.txt" in paths
	assert "fantasma.txt" not in paths


def test_drop_unseen_mantem_copia_local_sem_baseline(db: object, home: Path) -> None:
	from fastonedrive.index import _dropUnseenRows
	from fastonedrive.search import cacheLocalPath

	addRow(db, "local.txt", size=3)
	cached: Path = cacheLocalPath("local.txt")
	cached.parent.mkdir(parents=True, exist_ok=True)
	cached.write_text("edit")
	_dropUnseenRows(db, set(), "", "2026-02-01T00:00:00+00:00")
	paths = {str(row[0]) for row in db.execute("SELECT path FROM files")}
	assert "local.txt" in paths
	assert cached.is_file()


def test_unpin_recusa_mtime_mais_nova_que_baseline(db: object, home: Path) -> None:
	from datetime import datetime, timedelta, timezone

	from fastonedrive.index import setBaseline, setPinned
	from fastonedrive.pin import _treeNeedsUpload
	from fastonedrive.search import pinnedLocalPath

	addRow(db, "nota.txt", size=4, pinned=1)
	setPinned("nota.txt", True)
	setBaseline("nota.txt", "2026-01-01T00:00:00Z", "qxh:a")
	target: Path = pinnedLocalPath("nota.txt")
	target.parent.mkdir(parents=True, exist_ok=True)
	target.write_text("nova")
	later: float = (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
	os.utime(target, (later, later))
	assert _treeNeedsUpload("nota.txt") is True


def test_stage_nao_deixa_linha_fantasma_se_copia_falha(
	db: object,
	home: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	from fastonedrive import mutate
	from fastonedrive.index import getEntry

	source: Path = home / "origem.txt"
	source.write_text("dados")

	def boom(_src: Path, _dst: Path) -> None:
		raise OSError("disco cheio")

	monkeypatch.setattr(mutate, "copy2", boom)
	with pytest.raises(OSError):
		mutate._stageFile(source, "novo.txt")
	assert getEntry("novo.txt") is None


def test_ciclo_completo_incompleto_nao_avanca_cursor(db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive.graph import GraphDeltaError
	from fastonedrive import index

	def fakeIterDrive(_link: str) -> tuple[list[dict[str, object]], list[str]]:
		return [], ["root-link-novo"]

	def fakeIterItem(_driveId: str, _remoteId: str, _link: str) -> tuple[list[dict[str, object]], list[str]]:
		raise GraphDeltaError("403 Forbidden")

	def fakeApply(
		_connection: object,
		_pages: object,
		_ids: dict[str, str],
		_parents: dict[str, str],
		_seen: set[str],
		count: int,
		_root: str,
		_startedAt: str,
		_mode: str,
		_prefix: str,
		_scopeRootId: str,
		shared: dict[str, tuple[str, str, str]],
		scopeId: str = "",
	) -> tuple[int, dict[str, tuple[str, str, str]], int]:
		return count, shared, 0

	db.execute(
		"INSERT INTO delta_state (scope, driveId, itemId, deltaLink, prefix, updatedAt)"
		" VALUES ('shortcut', 'drv', 'remote', '', 'TRABALHO', '2026-01-01T00:00:00+00:00')"
	)
	db.commit()
	monkeypatch.setattr(index, "iterDriveDelta", fakeIterDrive)
	monkeypatch.setattr(index, "iterItemDelta", fakeIterItem)
	monkeypatch.setattr(index, "_applyDeltaFeed", fakeApply)
	monkeypatch.setattr(index, "_loadItemIdMap", lambda _c: {})
	monkeypatch.setattr(index, "_loadParentIdMap", lambda _c: {})
	monkeypatch.setattr(index, "_setIndexJobStatus", lambda *a, **k: None)
	index._rebuildGraph(db, "onedrive:", "2026-01-01T00:00:00+00:00")
	root = db.execute("SELECT deltaLink FROM delta_state WHERE scope = 'root'").fetchone()
	assert root is None or str(root[0] or "") == ""


def test_cli_index_nao_arma_pdeathsig(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
	from fastonedrive import cli

	armed: list[bool] = []
	monkeypatch.setattr(cli, "armParentDeathSignal", lambda: armed.append(True))
	monkeypatch.setattr(cli, "_prepareProfile", lambda _args: None)
	monkeypatch.setattr(cli, "_cmdIndex", lambda _args: 0)
	monkeypatch.setattr(cli, "_cmdAbout", lambda _args: 0)
	assert cli.run(["index"]) == 0
	assert armed == []
	assert cli.run(["about"]) == 0
	assert armed == [True]


def test_flush_delete_mantem_linha_se_copia_nao_sai(
	db: object,
	home: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	from fastonedrive import index
	from fastonedrive.index import _flushDeltaDeletes, getEntry, setBaseline
	from fastonedrive.search import cacheLocalPath

	addRow(db, "ocupado.txt", size=4)
	setBaseline("ocupado.txt", "2026-01-01T00:00:00Z", "qxh:a")
	cached: Path = cacheLocalPath("ocupado.txt")
	cached.parent.mkdir(parents=True, exist_ok=True)
	cached.write_text("edit")
	db.execute("UPDATE files SET itemId = ? WHERE path = ?", ("id-ocup", "ocupado.txt"))
	db.commit()
	monkeypatch.setattr(index, "_removeVanishedCopies", lambda *_a, **_k: False)
	ids: dict[str, str] = {"id-ocup": "ocupado.txt"}
	seen: set[str] = {"ocupado.txt"}
	removed: int = _flushDeltaDeletes(db, [{"itemId": "id-ocup"}], ids, seen)
	assert removed == 0
	assert getEntry("ocupado.txt") is not None
	assert ids.get("id-ocup") == "ocupado.txt"
	assert "ocupado.txt" in seen
	assert cached.is_file()


def test_hydrate_nao_apaga_cache_ocupado_com_lock_sh(
	db: object,
	home: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	import fcntl

	from fastonedrive import config, hydrate
	from fastonedrive.disk import localFileBusy
	from fastonedrive.hydrate import _dropIfStale
	from fastonedrive.index import setBaseline
	from fastonedrive.search import cacheLocalPath

	addRow(db, "lido.bin", size=8)
	setBaseline("lido.bin", "2026-01-01T00:00:00Z", "qxh:a")
	target: Path = cacheLocalPath("lido.bin")
	target.parent.mkdir(parents=True, exist_ok=True)
	target.write_bytes(b"12345678")
	monkeypatch.setattr(config, "openMaxBytes", lambda: 1)
	monkeypatch.setattr(
		hydrate,
		"fastRemoteStat",
		lambda _remote: {
			"Size": 4,
			"ModTime": "2026-01-02T00:00:00Z",
			"Hashes": {"QuickXorHash": "bbbb"},
		},
	)
	fd: int = os.open(str(target), os.O_RDONLY)
	try:
		fcntl.flock(fd, fcntl.LOCK_SH)
		assert localFileBusy(target) is True
		assert _dropIfStale("lido.bin", target) is False
		assert target.is_file()
		assert target.stat().st_size == 8
	finally:
		os.close(fd)


def test_hydrate_parcial_ocupado_nao_unlink(
	db: object,
	home: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	import fcntl

	from fastonedrive import chunks, hydrate
	from fastonedrive.hydrate import _hydrateRemote
	from fastonedrive.index import setBaseline
	from fastonedrive.search import cacheLocalPath

	addRow(db, "parcial.bin", size=16)
	setBaseline("parcial.bin", "2026-01-01T00:00:00Z", "qxh:a")
	target: Path = cacheLocalPath("parcial.bin")
	target.parent.mkdir(parents=True, exist_ok=True)
	chunks.prepareSparse("parcial.bin", 16, "qxh:a")
	assert target.is_file()
	monkeypatch.setattr(
		hydrate,
		"fastRemoteStat",
		lambda _remote: {
			"Size": 16,
			"ModTime": "2026-01-01T00:00:00Z",
			"Hashes": {"QuickXorHash": "aaaa"},
		},
	)
	monkeypatch.setattr(hydrate, "_fillDirtyPartial", lambda *_a, **_k: None)
	monkeypatch.setattr(hydrate, "_markCacheWritable", lambda *_a, **_k: None)
	fd: int = os.open(str(target), os.O_RDONLY)
	try:
		fcntl.flock(fd, fcntl.LOCK_SH)
		result: Path = _hydrateRemote("onedrive:parcial.bin", None)
		assert result == target
		assert target.is_file()
		assert chunks.isPartial("parcial.bin")
	finally:
		os.close(fd)


def test_link_e_free_space_saltam_ajuda_kio(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
	import argparse

	from fastonedrive import cli

	linked: list[str] = []
	freed: list[str] = []
	monkeypatch.setattr(cli, "linkRemote", lambda remote: linked.append(remote) or "https://x")
	monkeypatch.setattr(cli, "freeLocalSpace", lambda source: freed.append(source) or "0 B")
	helpUrl: str = "fastonedrive:/search/foo"
	assert cli._cmdLink(argparse.Namespace(sources=[helpUrl], notify=False)) == 0
	assert cli._cmdFreeSpace(argparse.Namespace(sources=[helpUrl], notify=False)) == 0
	assert linked == []
	assert freed == []


def test_ensure_search_index_idempotente(db: object) -> None:
	from fastonedrive import searchindex

	searchindex.ensureSearchIndex(db)  # type: ignore[arg-type]
	searchindex.ensureSearchIndex(db)  # type: ignore[arg-type]
	assert searchindex.hasSearchIndex(db)  # type: ignore[arg-type]
