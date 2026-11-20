"""Operações do FUSE chamadas directamente (sem montar: a CI não tem /dev/fuse)."""

import errno
from pathlib import Path

import pytest
from conftest import addRow


@pytest.fixture()
def fs(db: object, home: Path) -> object:
	pytest.importorskip("fastonedrive.fuse")
	from fastonedrive.fuse import FastOneDriveFS

	return FastOneDriveFS(home / "OneDrive", "")


def test_xattr_de_estado(fs: object, db: object) -> None:
	from fastonedrive._fusepy import FuseOSError

	addRow(db, "docs", isDir=True)
	addRow(db, "docs/a.txt")
	assert fs.getxattr("/docs/a.txt", "user.fastonedrive.state") == b"cloud"  # type: ignore[attr-defined]
	db.execute("UPDATE files SET syncState = 'error' WHERE path = 'docs/a.txt'")  # type: ignore[attr-defined]
	db.commit()  # type: ignore[attr-defined]
	assert fs.getxattr("/docs", "user.fastonedrive.state") == b"error"  # type: ignore[attr-defined]
	with pytest.raises(FuseOSError) as raised:
		fs.getxattr("/docs/a.txt", "user.outro")  # type: ignore[attr-defined]
	assert raised.value.errno == errno.ENODATA
	# Não listado: o KIO e o cp -a não copiam o atributo para fora do OneDrive.
	assert fs.listxattr("/docs/a.txt") == []  # type: ignore[attr-defined]


def test_leitura_a_pedido_pelo_fuse(fs: object, db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	import os

	from fastonedrive import chunks

	calls: list[tuple[int, int]] = []

	def fetch(itemPath: str, remotePath: str, offset: int, count: int) -> bytes:
		calls.append((offset, count))
		return bytes((offset + index) % 251 for index in range(count))

	monkeypatch.setattr(chunks, "fetchRange", fetch)
	size: int = chunks.CHUNK_SIZE * 3
	addRow(db, "video.mkv", size=size)
	fh: int = fs.open("/video.mkv", os.O_RDONLY)  # type: ignore[attr-defined]
	try:
		data: bytes = b""
		for offset in range(0, 1024 * 1024, 128 * 1024):
			data += fs.read("/video.mkv", 128 * 1024, offset, fh)  # type: ignore[attr-defined]
		assert data[:10] == bytes(range(10))
		first_block_calls = [call for call in calls if call[0] == 0]
		assert len(first_block_calls) == 1
	finally:
		fs.release("/video.mkv", fh)  # type: ignore[attr-defined]


def test_dolphin_so_a_ver_nao_descarrega(fs: object, db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	import os

	from fastonedrive import fuse
	from fastonedrive.search import cacheLocalPath, itemStatus

	monkeypatch.setattr(fuse, "_browsingMode", lambda: "stream")
	monkeypatch.setattr(
		fuse, "fetchRange", lambda i, r, offset, count: bytes((offset + n) % 251 for n in range(count))
	)
	addRow(db, "foto.jpg", size=600 * 1024)
	fh: int = fs.open("/foto.jpg", os.O_RDONLY)  # type: ignore[attr-defined]
	assert fh >= fuse.STREAM_FH_BASE
	head: bytes = fs.read("/foto.jpg", 4096, 0, fh)  # type: ignore[attr-defined]
	middle: bytes = fs.read("/foto.jpg", 1000, 300 * 1024, fh)  # type: ignore[attr-defined]
	assert head[:5] == bytes(range(5))
	assert middle[0] == (300 * 1024) % 251
	assert fs.getattr("/foto.jpg", fh)["st_size"] == 600 * 1024  # type: ignore[attr-defined]
	fs.flush("/foto.jpg", fh)  # type: ignore[attr-defined]
	fs.release("/foto.jpg", fh)  # type: ignore[attr-defined]
	assert not cacheLocalPath("foto.jpg").exists()
	assert itemStatus("foto.jpg") == "cloud"


def test_quem_pede_vem_do_pid_do_contexto(monkeypatch: pytest.MonkeyPatch) -> None:
	import os
	from pathlib import Path as P

	from fastonedrive import fuse

	# fusepy devolve (uid, gid, pid): o pid é o TERCEIRO valor.
	monkeypatch.setattr(fuse, "fuse_get_context", lambda: (os.getuid(), os.getgid(), os.getpid()))
	process, thread = fuse._callerInfo()
	expected: str = P(f"/proc/{os.getpid()}/comm").read_text(encoding="utf-8").strip()
	assert process == expected and thread == expected
	monkeypatch.setattr(fuse, "BROWSING_COMMS", frozenset({expected}))
	assert fuse._isBrowsingProcess()


def test_thread_da_interface_nao_espera_pela_nuvem(fs: object, db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	import os

	from fastonedrive import fuse

	monkeypatch.setattr(fuse, "_browsingMode", lambda: "ui")
	monkeypatch.setattr(fuse, "fetchRange", lambda *args: pytest.fail("foi à nuvem na thread da interface"))
	addRow(db, "jogo.nsp", size=10 * 1024 * 1024)
	fh: int = fs.open("/jogo.nsp", os.O_RDONLY)  # type: ignore[attr-defined]
	assert fs.read("/jogo.nsp", 4096, 0, fh) == b""  # type: ignore[attr-defined]
	fs.release("/jogo.nsp", fh)  # type: ignore[attr-defined]


def test_lixo_do_kio_nao_vai_para_a_nuvem(fs: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import fuse
	from fastonedrive._fusepy import FuseOSError

	monkeypatch.setattr(fuse, "mkdirRemoteFast", lambda remote: pytest.fail("mkdir na nuvem"))
	for name in ("/.Trash-1000", "/.Trash"):
		with pytest.raises(FuseOSError) as raised:
			fs.mkdir(name, 0o700)  # type: ignore[attr-defined]
		assert raised.value.errno == errno.EACCES
	with pytest.raises(FuseOSError):
		fs.rename("/a.txt", "/.Trash-1000/files/a.txt")  # type: ignore[attr-defined]
	with pytest.raises(FuseOSError) as hidden:
		fs.getattr("/.Trash-1000")  # type: ignore[attr-defined]
	assert hidden.value.errno == errno.ENOENT
	assert not fuse._isTrashDir("docs/.Trash-1000")
	assert not fuse._isTrashDir(".Trash-abc")


def test_mkdir_local_primeiro_e_nuvem_em_segundo_plano(fs: object, monkeypatch: pytest.MonkeyPatch) -> None:
	import threading
	import time

	from fastonedrive import fuse
	from fastonedrive.index import getEntry

	called: threading.Event = threading.Event()
	remotes: list[str] = []
	release: threading.Event = threading.Event()

	def slowMkdir(remote: str) -> str:
		remotes.append(remote)
		called.set()
		release.wait(5)
		return remote

	monkeypatch.setattr(fuse, "mkdirRemoteFast", slowMkdir)
	started: float = time.monotonic()
	fs.mkdir("/nova", 0o755)  # type: ignore[attr-defined]
	elapsed: float = time.monotonic() - started
	try:
		assert elapsed < 1.0
		assert fuse._backingPath("nova").is_dir()
		entry = getEntry("nova")
		assert entry is not None and bool(entry["isDir"])
		assert str(entry["syncState"] or "") == "dirty"
		assert called.wait(2), "mkdir remoto não foi agendado"
		assert len(remotes) == 1 and remotes[0].endswith("nova")
	finally:
		release.set()


def test_mkdir_remoto_com_falha_nao_desfaz_o_local(fs: object, monkeypatch: pytest.MonkeyPatch) -> None:
	import threading

	from fastonedrive import fuse
	from fastonedrive import FastOneDriveError
	from fastonedrive.index import getEntry

	called: threading.Event = threading.Event()

	def failingMkdir(remote: str) -> str:
		called.set()
		raise FastOneDriveError("sem rede")

	monkeypatch.setattr(fuse, "mkdirRemoteFast", failingMkdir)
	fs.mkdir("/offline", 0o755)  # type: ignore[attr-defined]
	assert called.wait(2)
	assert fuse._backingPath("offline").is_dir()
	entry = getEntry("offline")
	assert entry is not None
	import time

	deadline: float = time.monotonic() + 2
	while time.monotonic() < deadline and str(entry["syncState"] or "") != "error":
		time.sleep(0.05)
		entry = getEntry("offline")
		assert entry is not None
	assert str(entry["syncState"] or "") == "error"


def test_mkdir_efemero_fica_so_local(fs: object, monkeypatch: pytest.MonkeyPatch) -> None:
	import time

	from fastonedrive import fuse

	calls: list[str] = []
	monkeypatch.setattr(fuse, "mkdirRemoteFast", lambda remote: calls.append(remote) or remote)
	for name in ("rascunho.tmp", "pasta.part"):
		fs.mkdir(f"/{name}", 0o755)  # type: ignore[attr-defined]
		assert fuse._backingPath(name).is_dir()
	time.sleep(0.3)
	assert calls == []


def test_mkdir_lixo_continua_bloqueado_sem_criar_nada(fs: object, monkeypatch: pytest.MonkeyPatch) -> None:
	import time

	from fastonedrive import fuse
	from fastonedrive._fusepy import FuseOSError
	from fastonedrive.index import getEntry

	calls: list[str] = []
	monkeypatch.setattr(fuse, "mkdirRemoteFast", lambda remote: calls.append(remote) or remote)
	with pytest.raises(FuseOSError) as raised:
		fs.mkdir("/.Trash-1000", 0o700)  # type: ignore[attr-defined]
	assert raised.value.errno == errno.EACCES
	time.sleep(0.3)
	assert calls == []
	assert not fuse._backingPath(".Trash-1000").exists()
	assert getEntry(".Trash-1000") is None


def test_mkdir_remoto_nao_recria_pasta_apagada(fs: object, monkeypatch: pytest.MonkeyPatch) -> None:
	import threading
	import time

	from fastonedrive import fuse

	started: threading.Event = threading.Event()
	release: threading.Event = threading.Event()
	undone: list[str] = []

	def slowMkdir(remote: str) -> str:
		started.set()
		release.wait(5)
		return remote

	monkeypatch.setattr(fuse, "mkdirRemoteFast", slowMkdir)
	monkeypatch.setattr(fuse, "rmdirRemote", lambda remote: undone.append(remote))
	fs.mkdir("/tmpgone", 0o755)  # type: ignore[attr-defined]
	assert started.wait(2)
	fs.rmdir("/tmpgone")  # type: ignore[attr-defined]
	release.set()
	deadline: float = time.monotonic() + 2
	while time.monotonic() < deadline and len(undone) == 0:
		time.sleep(0.05)
	assert len(undone) == 1


def test_criar_ficheiro_fica_dirty_com_emblema(fs: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import fuse
	from fastonedrive.index import getEntry

	synced: list[str] = []
	monkeypatch.setattr(fuse, "scheduleItemSync", lambda path: synced.append(path))
	fh: int = fs.create("/notas.txt", 0o644)  # type: ignore[attr-defined]
	fs.release("/notas.txt", fh)  # type: ignore[attr-defined]
	entry = getEntry("notas.txt")
	assert entry is not None and str(entry["syncState"] or "") == "dirty"
	assert fs.getxattr("/notas.txt", "user.fastonedrive.state") == b"syncing"  # type: ignore[attr-defined]
	assert synced == ["notas.txt"]


def test_chmod_nao_espera_pelo_dbus(fs: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import fuse
	from fastonedrive import notify as notifyMod

	monkeypatch.setattr(fuse, "scheduleItemSync", lambda path: None)
	called: list[str] = []
	monkeypatch.setattr(notifyMod, "notifyDirChanged", lambda path: called.append(path))
	fh: int = fs.create("/modo.txt", 0o644)  # type: ignore[attr-defined]
	fs.release("/modo.txt", fh)  # type: ignore[attr-defined]
	called.clear()
	fs.chmod("/modo.txt", 0o600)  # type: ignore[attr-defined]
	assert called == []


def test_aviso_de_exclusao_em_massa(fs: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import fuse

	warned: list[int] = []
	monkeypatch.setattr(fuse, "_warnMassDelete", lambda count: warned.append(count))
	monkeypatch.setattr(fuse, "MASS_DELETE_COUNT", 5)
	for _ in range(12):
		fs._noteDelete()  # type: ignore[attr-defined]
	import time

	time.sleep(0.2)
	assert warned == [5]
