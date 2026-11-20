import os
import threading
from pathlib import Path

import pytest
from conftest import addRow

from fastonedrive import chunks


def _payload(offset: int, count: int) -> bytes:
	return bytes((offset + index) % 251 for index in range(count))


@pytest.fixture()
def fakeRemote(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
	calls: list[tuple[int, int]] = []
	lock = threading.Lock()

	def fetch(itemPath: str, remotePath: str, offset: int, count: int) -> bytes:
		with lock:
			calls.append((offset, count))
		return _payload(offset, count)

	monkeypatch.setattr(chunks, "fetchRange", fetch)
	return calls


def test_preencher_buracos_respeita_o_limite_do_pedido(db: object, fakeRemote: list[tuple[int, int]]) -> None:
	size: int = chunks.CHUNK_SIZE * (chunks.MAX_SPAN_CHUNKS + 3) + 100
	addRow(db, "grande.bin", size=size)
	target: Path = chunks.prepareSparse("grande.bin", size, "qxh:a")
	fd: int = os.open(target, os.O_RDWR)
	try:
		chunks.fillHoles("grande.bin", "onedrive:grande.bin", fd, [])
	finally:
		os.close(fd)
	assert max(count for _offset, count in fakeRemote) <= chunks.CHUNK_SIZE * chunks.MAX_SPAN_CHUNKS
	assert not chunks.isPartial("grande.bin")
	data: bytes = target.read_bytes()
	assert data[: 1000] == _payload(0, 1000)
	assert data[-100:] == _payload(size - 100, 100)


def test_threads_em_blocos_diferentes_nao_perdem_bits(db: object, fakeRemote: list[tuple[int, int]]) -> None:
	total: int = 12
	size: int = chunks.CHUNK_SIZE * total
	addRow(db, "v.mkv", size=size)
	target: Path = chunks.prepareSparse("v.mkv", size, "qxh:a")

	def work(index: int) -> None:
		fd: int = os.open(target, os.O_RDWR)
		try:
			chunks.ensureRange("v.mkv", "onedrive:v.mkv", fd, index * chunks.CHUNK_SIZE, 10, size)
		finally:
			os.close(fd)

	threads = [threading.Thread(target=work, args=(index,)) for index in range(total - 1)]
	for thread in threads:
		thread.start()
	for thread in threads:
		thread.join()
	assert chunks.holesIn("v.mkv", 0, total - 1) == [total - 1]
	# Cada bloco foi descarregado uma só vez: nenhum bit se perdeu entre threads.
	assert len(fakeRemote) == total - 1


def test_copia_a_pedido_nasce_com_linha_de_base(db: object) -> None:
	from fastonedrive.index import getEntry

	addRow(db, "notas.txt", size=100)
	chunks.prepareSparse("notas.txt", 100, "qxh:a")
	entry = getEntry("notas.txt")
	assert entry is not None and entry["baseEtag"] == "qxh:a"
	assert entry["syncState"] in ("", "synced")


def test_bitmap_de_outro_etag_nao_e_marcado(db: object, fakeRemote: list[tuple[int, int]]) -> None:
	size: int = chunks.CHUNK_SIZE * 3
	addRow(db, "a.bin", size=size)
	chunks.prepareSparse("a.bin", size, "qxh:a")
	assert chunks._markChunks("a.bin", "qxh:outro", range(0, 3)) is False
	assert chunks.holesIn("a.bin", 0, 2) == [0, 1, 2]
