"""Regressões dos achados críticos da auditoria geral de 2026-11-17."""

import fcntl
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import addRow

from fastonedrive import chunks

BASE_TIME: str = "2026-01-01T00:00:00Z"
BASE_EPOCH: float = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()


def _payload(offset: int, count: int) -> bytes:
	return bytes((offset + index) % 251 for index in range(count))


@pytest.fixture()
def noBackgroundSync(monkeypatch: pytest.MonkeyPatch) -> list[str]:
	from fastonedrive import watcher

	fired: list[str] = []
	monkeypatch.setattr(watcher, "_runSync", lambda itemPaths: fired.extend(itemPaths))
	return fired


def _syncedCacheFile(db: object, itemPath: str, body: bytes) -> Path:
	from fastonedrive.index import setBaseline
	from fastonedrive.search import cacheLocalPath

	addRow(db, itemPath, size=len(body))
	setBaseline(itemPath, BASE_TIME, "qxh:base", len(body))
	target: Path = cacheLocalPath(itemPath)
	target.parent.mkdir(parents=True, exist_ok=True)
	target.write_bytes(body)
	os.utime(target, (BASE_EPOCH, BASE_EPOCH))
	return target


def test_mover_pasta_em_cache_nao_marca_dirty(db: object, noBackgroundSync: list[str]) -> None:
	from fastonedrive.index import getEntry
	from fastonedrive.watcher import _scheduleMovedTree

	addRow(db, "Nova", isDir=True)
	same: Path = _syncedCacheFile(db, "Nova/igual.txt", b"conteudo")
	edited: Path = _syncedCacheFile(db, "Nova/editado.txt", b"conteudo")
	os.utime(edited, (BASE_EPOCH + 600, BASE_EPOCH + 600))
	_scheduleMovedTree(same.parent)
	assert getEntry("Nova/igual.txt")["syncState"] in ("", "synced")
	assert getEntry("Nova/editado.txt")["syncState"] == "dirty"


def test_truncate_com_fh_limpa_o_bitmap(db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	calls: list[int] = []

	def fetch(itemPath: str, remotePath: str, offset: int, count: int) -> bytes:
		calls.append(offset)
		return _payload(offset, count)

	monkeypatch.setattr(chunks, "fetchRange", fetch)
	size: int = chunks.CHUNK_SIZE * 3
	addRow(db, "grande.bin", size=size)
	target: Path = chunks.prepareSparse("grande.bin", size, "qxh:a")
	fd: int = os.open(target, os.O_RDWR)
	try:
		chunks.truncatePartial("grande.bin", "onedrive:grande.bin", fd, 0)
		os.pwrite(fd, b"novo", chunks.CHUNK_SIZE * 2)
		chunks.fillHoles("grande.bin", "onedrive:grande.bin", fd, [])
	finally:
		os.close(fd)
	assert calls == []
	assert not chunks.isPartial("grande.bin")
	data: bytes = target.read_bytes()
	assert data[: chunks.CHUNK_SIZE * 2] == bytes(chunks.CHUNK_SIZE * 2)
	assert data[chunks.CHUNK_SIZE * 2 :] == b"novo"


def test_truncate_mais_curto_guarda_o_inicio_da_nuvem(db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(chunks, "fetchRange", lambda i, r, offset, count: _payload(offset, count))
	size: int = chunks.CHUNK_SIZE * 3
	addRow(db, "grande.bin", size=size)
	target: Path = chunks.prepareSparse("grande.bin", size, "qxh:a")
	fd: int = os.open(target, os.O_RDWR)
	try:
		chunks.truncatePartial("grande.bin", "onedrive:grande.bin", fd, 100)
	finally:
		os.close(fd)
	assert not chunks.isPartial("grande.bin")
	assert target.read_bytes() == _payload(0, 100)


def test_gravacao_atomica_sobre_parcial_tira_o_bitmap(
	db: object,
	home: Path,
	monkeypatch: pytest.MonkeyPatch,
	noBackgroundSync: list[str],
) -> None:
	pytest.importorskip("fastonedrive.fuse")
	from fastonedrive import fuse
	from fastonedrive.search import cacheLocalPath

	monkeypatch.setattr(chunks, "fetchRange", lambda *args: pytest.fail("encheu blocos antigos"))
	size: int = chunks.CHUNK_SIZE * 2
	addRow(db, "doc.bin", size=size)
	chunks.prepareSparse("doc.bin", size, "qxh:a")
	temp: Path = cacheLocalPath(".goutputstream-ABC")
	temp.write_bytes(b"conteudo novo")
	fs = fuse.FastOneDriveFS(home / "OneDrive", "")
	fs.rename("/.goutputstream-ABC", "/doc.bin")
	assert not chunks.isPartial("doc.bin")
	assert cacheLocalPath("doc.bin").read_bytes() == b"conteudo novo"


def test_cache_limpo_diferente_nao_fica_com_a_data_nova(db: object) -> None:
	from fastonedrive.hydrate import adoptDownloadedFile
	from fastonedrive.search import cacheLocalPath, pinnedLocalPath

	_syncedCacheFile(db, "a.txt", b"versao 1")
	pinned: Path = pinnedLocalPath("a.txt")
	pinned.parent.mkdir(parents=True, exist_ok=True)
	pinned.write_bytes(b"versao 2")
	stat: dict[str, object] = {"ModTime": "2026-02-01T00:00:00Z", "Size": 8}
	adoptDownloadedFile("a.txt", pinned, stat, False)
	assert not cacheLocalPath("a.txt").exists()


def test_cache_diferente_durante_envio_fica_intacto(db: object) -> None:
	from fastonedrive.hydrate import adoptDownloadedFile
	from fastonedrive.index import setSyncState
	from fastonedrive.search import pinnedLocalPath

	cached: Path = _syncedCacheFile(db, "a.txt", b"edicao nova")
	setSyncState("a.txt", "syncing")
	pinned: Path = pinnedLocalPath("a.txt")
	pinned.parent.mkdir(parents=True, exist_ok=True)
	pinned.write_bytes(b"versao 2")
	stat: dict[str, object] = {"ModTime": "2026-02-01T00:00:00Z", "Size": 8}
	adoptDownloadedFile("a.txt", pinned, stat)
	assert cached.read_bytes() == b"edicao nova"
	assert cached.stat().st_mtime == BASE_EPOCH


def test_manter_com_edicao_pendente_e_recusado(db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import pin, syncpins
	from fastonedrive.index import markDirty
	from fastonedrive.search import pinnedLocalPath

	addRow(db, "a.txt", size=4, pinned=1)
	local: Path = pinnedLocalPath("a.txt")
	local.parent.mkdir(parents=True, exist_ok=True)
	local.write_bytes(b"meu!")
	markDirty("a.txt")
	monkeypatch.setattr(syncpins, "syncPins", lambda **kwargs: None)
	monkeypatch.setattr(pin, "copyToReplace", lambda *args: pytest.fail("substituiu a edição local"))
	with pytest.raises(Exception, match="alterações locais por enviar"):
		pin.pinRemote("onedrive:a.txt")
	assert local.read_bytes() == b"meu!"


def test_put_cancelado_nao_estraga_o_destino(db: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	from fastonedrive import mutate

	original: Path = _syncedCacheFile(db, "a.txt", b"original")
	source: Path = tmp_path / "novo.txt"
	source.write_bytes(b"conteudo grande")

	def interrupted(src: Path, dst: Path) -> None:
		Path(dst).write_bytes(b"cont")
		raise KeyboardInterrupt

	monkeypatch.setattr(mutate, "copy2", interrupted)
	monkeypatch.setattr(mutate, "_scheduleStaged", lambda paths: None)
	with pytest.raises(KeyboardInterrupt):
		mutate.stageLocalWrite(str(source), "onedrive:a.txt", True)
	assert original.read_bytes() == b"original"
	assert not original.with_name(".fod-staging-a.txt").exists()


def test_put_de_nome_temporario_e_enviado(db: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	from fastonedrive import mutate

	sent: list[str] = []
	source: Path = tmp_path / "notas.tmp"
	source.write_bytes(b"x")
	monkeypatch.setattr(
		mutate,
		"putLocalToRemote",
		lambda local, dest, onStats=None, overwrite=False: sent.append(dest) or dest,
	)
	mutate.stageLocalWrite(str(source), "onedrive:notas.tmp")
	assert sent == ["onedrive:notas.tmp"]


def test_url_do_kio_com_percentagem_nao_e_descodificada_duas_vezes(home: Path) -> None:
	from fastonedrive.paths import toRemotePath

	assert toRemotePath("fastonedrive:/item/Relatorio%2520Q1.pdf") == "onedrive:Relatorio%20Q1.pdf"
	assert toRemotePath("fastonedrive:/item/Relatorio%20Q1.pdf") == "onedrive:Relatorio Q1.pdf"


def test_importar_do_sistema_nao_apaga_perfis(home: Path) -> None:
	from fastonedrive import config

	system: Path = config.systemRcloneConfigPath()
	system.parent.mkdir(parents=True, exist_ok=True)
	system.write_text("[onedrive]\ntype = onedrive\ntoken = SISTEMA\n\n[outro]\ntype = local\n", encoding="utf-8")
	dest: Path = config.rcloneConfigPath()
	dest.parent.mkdir(parents=True, exist_ok=True)
	dest.write_text("[onedrive]\ntype = onedrive\ntoken = MEU\n\n[trabalho]\ntype = onedrive\ntoken = T\n", encoding="utf-8")
	config.importSystemRcloneConfig()
	body: str = dest.read_text(encoding="utf-8")
	assert "token = MEU" in body and "SISTEMA" not in body
	assert "[trabalho]" in body and "token = T" in body
	assert "[outro]" in body
	assert oct(dest.stat().st_mode & 0o777) == "0o600"


def test_remover_perfil_com_sync_em_curso_e_recusado(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import FastOneDriveError, config, kioinstall, mount, profiles

	conf: Path = config.configDir()
	conf.mkdir(parents=True, exist_ok=True)
	(conf / "config.toml").write_text(
		'remoteName = "onedrive"\n\n'
		"[[profiles]]\n"
		'id = "trabalho"\n'
		'name = "Trabalho"\n'
		'remoteName = "trabalho"\n'
		f'mountPath = "{home}/OneDrive-Trabalho"\n'
		"mountEnabled = false\n",
		encoding="utf-8",
	)
	monkeypatch.setattr(mount, "disableProfileMount", lambda profile: None)
	monkeypatch.setattr(kioinstall, "removeProfilePlace", lambda profileId: None)
	profile = config.profileById("trabalho")
	assert profile is not None
	state: Path = config.profileStateDir(profile)
	(state / "pinned").mkdir(parents=True)
	(state / "pinned" / "a.txt").write_text("mantido", encoding="utf-8")
	with (state / "pins.lock").open("a+", encoding="utf-8") as held:
		fcntl.flock(held.fileno(), fcntl.LOCK_EX)
		with pytest.raises(FastOneDriveError, match="sincronização em curso"):
			profiles.removeProfile("trabalho")
	assert (state / "pinned" / "a.txt").is_file()
