import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from fastonedrive import FastOneDriveError


def test_campos_extra_na_seccao_do_rclone() -> None:
	from fastonedrive.profiles import _upsertSectionFields

	lines: list[str] = ["[a]", "type = onedrive", "token = x", "", "[b]", "type = onedrive"]
	out: list[str] = _upsertSectionFields(lines, "[a]", {"client_id": "id1", "client_secret": "s"})
	assert out == [
		"[a]",
		"type = onedrive",
		"token = x",
		"client_id = id1",
		"client_secret = s",
		"",
		"[b]",
		"type = onedrive",
	]
	again: list[str] = _upsertSectionFields(out, "[a]", {"client_id": "id2"})
	assert "client_id = id2" in again and "client_id = id1" not in again


def test_controlo_de_sync(home: Path) -> None:
	from fastonedrive import synccontrol

	control = synccontrol.load()
	assert control.bwLimit == "" and control.remotePollSeconds == synccontrol.DEFAULT_POLL_SECONDS
	control.bwLimit = "10M:2M"
	control.remotePollSeconds = 1
	synccontrol.save(control)
	loaded = synccontrol.load()
	assert loaded.bwLimit == "10M:2M"
	assert loaded.remotePollSeconds == synccontrol.MIN_POLL_SECONDS
	assert synccontrol.bwLimitFlags() == ["--bwlimit", "10M:2M"]
	control.bwLimit = "$(rm -rf ~)"
	synccontrol.save(control)
	assert synccontrol.bwLimitFlags() == []


def test_pausa(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import synccontrol

	monkeypatch.setattr(synccontrol, "isMetered", lambda: False)
	assert synccontrol.pauseReason() == ""
	synccontrol.pauseFor(3600)
	assert "pausa" in synccontrol.pauseReason()
	synccontrol.resume()
	assert synccontrol.pauseReason() == ""
	monkeypatch.setattr(synccontrol, "isMetered", lambda: True)
	assert "rede limitada" in synccontrol.pauseReason()


def test_sdnotify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import sdnotify

	import tempfile

	path: Path = Path(tempfile.mkdtemp(prefix="fod", dir="/tmp")) / "n.sock"
	server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
	server.bind(str(path))
	try:
		monkeypatch.setenv("NOTIFY_SOCKET", str(path))
		assert sdnotify.ready("montado")
		assert server.recv(100) == b"READY=1\nSTATUS=montado"
	finally:
		server.close()
	monkeypatch.delenv("NOTIFY_SOCKET")
	assert not sdnotify.ready()


class _RangeHandler(BaseHTTPRequestHandler):
	body: bytes = bytes(range(256)) * 64
	ignoreRange: bool = False

	def do_GET(self) -> None:  # noqa: N802
		if self.path.startswith("/redir"):
			# Como o downloadUrl de contas pessoais: 302 para o servidor de ficheiros.
			self.send_response(302)
			self.send_header("Location", "/conteudo?assinado=1")
			self.send_header("Content-Length", "0")
			self.end_headers()
			return
		header: str = self.headers.get("Range", "")
		if self.ignoreRange or not header.startswith("bytes="):
			self.send_response(200)
			self.send_header("Content-Length", str(len(self.body)))
			self.end_headers()
			self.wfile.write(self.body)
			return
		start, end = (int(part) for part in header[6:].split("-"))
		chunk: bytes = self.body[start : end + 1]
		self.send_response(206)
		self.send_header("Content-Length", str(len(chunk)))
		self.end_headers()
		self.wfile.write(chunk)

	def log_message(self, *args: object) -> None:
		return


@pytest.fixture()
def server() -> object:
	httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
	thread = threading.Thread(target=httpd.serve_forever, daemon=True)
	thread.start()
	yield httpd
	httpd.shutdown()


def test_range_http_com_ligacao_persistente(server: object) -> None:
	from fastonedrive import rangeread

	port: int = server.server_address[1]  # type: ignore[attr-defined]
	url: str = f"http://127.0.0.1:{port}/conteudo?token=abc"
	assert rangeread._rangeGet(url, 10, 20) == _RangeHandler.body[10:30]
	assert rangeread._rangeGet(url, 1000, 5) == _RangeHandler.body[1000:1005]


def test_range_segue_o_302_e_guarda_o_destino(server: object) -> None:
	from fastonedrive import rangeread

	port: int = server.server_address[1]  # type: ignore[attr-defined]
	url: str = f"http://127.0.0.1:{port}/redir?x=1"
	assert rangeread._rangeGet(url, 5, 10) == _RangeHandler.body[5:15]
	assert rangeread._resolved[url][0].endswith("/conteudo?assinado=1")
	assert rangeread._rangeGet(url, 100, 3) == _RangeHandler.body[100:103]


def test_servidor_que_ignora_range_nao_descarrega_tudo(server: object) -> None:
	from fastonedrive import rangeread

	_RangeHandler.ignoreRange = True
	try:
		port: int = server.server_address[1]  # type: ignore[attr-defined]
		with pytest.raises(FastOneDriveError):
			rangeread._rangeGet(f"http://127.0.0.1:{port}/x", 10, 20)
	finally:
		_RangeHandler.ignoreRange = False


def test_falha_do_graph_cai_no_rclone(monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import rangeread, rclone

	def broken(itemPath: str, offset: int, count: int) -> bytes:
		raise FastOneDriveError("sem itemId")

	monkeypatch.setattr(rangeread, "_graphRange", broken)
	monkeypatch.setattr(rclone, "catRange", lambda remote, offset, count: b"r" * count)
	assert rangeread.fetchRange("a", "onedrive:a", 0, 4) == b"rrrr"


def test_graph_stat_no_formato_do_rclone(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	import base64

	from fastonedrive import graphitems
	from fastonedrive.rclone import contentTokenFromStat

	raw: bytes = bytes(range(20))
	payload = {
		"id": "ABC",
		"name": "a.txt",
		"size": 5,
		"lastModifiedDateTime": "2026-01-02T00:00:00Z",
		"fileSystemInfo": {"lastModifiedDateTime": "2026-01-01T00:00:00Z"},
		"file": {"mimeType": "text/plain", "hashes": {"quickXorHash": base64.b64encode(raw).decode()}},
		"@microsoft.graph.downloadUrl": "https://exemplo/download",
	}
	monkeypatch.setattr(graphitems, "itemUrl", lambda itemPath: "https://graph/x")
	monkeypatch.setattr(graphitems, "graphRequest", lambda method, url, body=None: payload)
	stat, url = graphitems.graphStat("docs/a.txt")
	assert url == "https://exemplo/download"
	assert stat["ModTime"] == "2026-01-01T00:00:00Z"
	assert stat["Hashes"]["quickxor"] == raw.hex()
	# Token canónico (base64, como o delta), venha o hash em hex ou base64.
	import base64

	assert contentTokenFromStat(stat) == "qxh:" + base64.b64encode(raw).decode("ascii")
	assert graphitems.downloadUrl("docs/a.txt") == "https://exemplo/download"


def test_id_do_rclone_com_drive(db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from conftest import addRow

	from fastonedrive import graphitems

	addRow(db, "Linux/app.appimage")
	db.execute(  # type: ignore[attr-defined]
		"UPDATE files SET itemId = ? WHERE path = 'Linux/app.appimage'",
		("522361507C81323D#522361507C81323D!sf78124",),
	)
	db.commit()  # type: ignore[attr-defined]
	url: str = graphitems.itemUrl("Linux/app.appimage")
	assert url.endswith("/drives/522361507C81323D/items/522361507C81323D%21sf78124")
	assert "#" not in url


def test_ficheiros_de_trabalho_dos_editores_nao_sobem() -> None:
	from fastonedrive.disk import isEphemeralName

	for name in (".cloudflared config.txt.kate-swp", "notas.txt~", "4913", ".#notas.txt", "#notas.txt#"):
		assert isEphemeralName(name), name
	for name in ("notas.txt", "~ideias.txt", "C#.txt"):
		assert not isEphemeralName(name), name


def test_part_do_kio_nao_sobe() -> None:
	from fastonedrive.disk import isEphemeralName, isEphemeralPath, isStagingRel

	for name in ("notas.txt.part", "Notas.PART", "notas.txt.partial"):
		assert isEphemeralName(name), name
	for name in ("notas.txt", "report.partition", "part", "partes.txt"):
		assert not isEphemeralName(name), name
	assert isEphemeralPath("docs/notas.txt.part")
	assert not isEphemeralPath("docs/notas.txt")
	assert not isStagingRel("docs/Relatorio.part")
	assert isStagingRel("docs/nome.partial")


def test_editar_cache_antigo_sem_base_adopta_a_nuvem(db: object, home: Path) -> None:
	from conftest import addRow

	from fastonedrive.hydrate import ensureCacheBaseline
	from fastonedrive.index import getEntry
	from fastonedrive.rclone import hashLocalQuickXor
	from fastonedrive.search import cacheLocalPath

	addRow(db, "linux/config.txt", size=5)
	target = cacheLocalPath("linux/config.txt")
	target.parent.mkdir(parents=True, exist_ok=True)
	target.write_text("olá!!", encoding="utf-8")
	token: str = hashLocalQuickXor(target)
	db.execute("UPDATE files SET etag = ?, baseEtag = '' WHERE path = 'linux/config.txt'", (token,))  # type: ignore[attr-defined]
	db.commit()  # type: ignore[attr-defined]
	ensureCacheBaseline("linux/config.txt")
	assert getEntry("linux/config.txt")["baseEtag"] == token  # type: ignore[index]


def test_conflito_de_conflito_nao_existe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	from fastonedrive import syncpins

	uploads: list[str] = []
	monkeypatch.setattr(syncpins, "_uploadLocal", lambda item, local, stats, onStats: uploads.append(item))
	local: Path = tmp_path / "rom (conflito local 2026-11-17 214856).nup"
	local.write_bytes(b"x")
	stats: dict[str, int] = {"conflicts": 0}
	syncpins._writeConflict("roms/" + local.name, local, {}, stats, None)
	assert uploads == ["roms/" + local.name]
	assert stats["conflicts"] == 0
	assert list(tmp_path.iterdir()) == [local]


def test_nao_envia_copia_vazia_nem_com_buracos(db: object, tmp_path: Path) -> None:
	from conftest import addRow

	from fastonedrive.syncpins import unsafeUploadReason

	addRow(db, "roms/jogo.nup", size=7 * 1024 * 1024)
	db.execute("UPDATE files SET baseEtag = '' WHERE path = 'roms/jogo.nup'")  # type: ignore[attr-defined]
	db.commit()  # type: ignore[attr-defined]
	empty: Path = tmp_path / "vazio.nup"
	empty.write_bytes(b"")
	assert "vazia" in unsafeUploadReason("roms/jogo.nup", empty)
	hollow: Path = tmp_path / "buracos.nup"
	with hollow.open("wb") as handle:
		handle.truncate(8 * 1024 * 1024)
	assert "buracos" in unsafeUploadReason("roms/outro.nup", hollow)
	full: Path = tmp_path / "cheio.bin"
	full.write_bytes(os.urandom(2 * 1024 * 1024))
	assert unsafeUploadReason("roms/outro.nup", full) == ""
	small: Path = tmp_path / "notas.txt"
	small.write_text("", encoding="utf-8")
	assert unsafeUploadReason("notas-sem-indice.txt", small) == ""


def test_envio_adia_se_o_editor_ainda_tem_o_ficheiro(
	monkeypatch: pytest.MonkeyPatch,
	db: object,
	tmp_path: Path,
) -> None:
	from contextlib import contextmanager
	from collections.abc import Iterator

	from fastonedrive import syncpins
	from fastonedrive import watcher

	retries: list[tuple[str, float | None]] = []

	@contextmanager
	def busy(_path: str) -> Iterator[bool]:
		yield False

	monkeypatch.setattr(syncpins, "tryExclusiveWriteGuard", busy)
	monkeypatch.setattr(
		watcher,
		"debounceSync",
		lambda path, delay=None: retries.append((path, delay)),
	)
	local: Path = tmp_path / "notas.txt"
	local.write_text("oi", encoding="utf-8")
	stats: dict[str, int] = {"skipped": 0, "errors": 0, "uploaded": 0}
	syncpins._uploadLocal("linux/notas.txt", local, stats, None)
	assert stats["skipped"] == 1
	assert retries == [("linux/notas.txt", watcher.RETRY_SEC)]


def test_estado_do_item(db: object) -> None:
	from conftest import addRow

	from fastonedrive.search import itemStatus

	addRow(db, "docs", isDir=True)
	addRow(db, "docs/a.txt")
	addRow(db, "docs/b.txt")
	assert itemStatus("docs/a.txt") == "cloud"
	assert itemStatus("docs") == "cloud"
	db.execute("UPDATE files SET syncState = 'dirty' WHERE path = 'docs/b.txt'")  # type: ignore[attr-defined]
	db.commit()  # type: ignore[attr-defined]
	assert itemStatus("docs") == "syncing"
	db.execute("UPDATE files SET syncState = 'conflict' WHERE path = 'docs/a.txt'")  # type: ignore[attr-defined]
	db.commit()  # type: ignore[attr-defined]
	assert itemStatus("docs") == "conflict"
	assert itemStatus("") == "conflict"
	assert itemStatus("nao/existe") == ""


def test_historico_de_actividade(home: Path) -> None:
	from fastonedrive.activity import readHistory, setActivity

	setActivity("syncing", "pins")
	setActivity("syncing", "docs/a.txt", "upload")
	setActivity("error", "falhou")
	entries = readHistory(5)
	assert [entry["kind"] for entry in entries] == ["error", "upload"]
	assert time.time() - float(entries[0]["time"]) < 60
