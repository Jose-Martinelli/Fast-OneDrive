import os
import threading
import time

import pytest
from conftest import addRow

from fastonedrive import chunks
from fastonedrive.readahead import Readahead


def test_leitura_sequencial_pede_os_blocos_seguintes(db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	fetched: set[int] = set()
	lock = threading.Lock()

	def fetch(itemPath: str, remotePath: str, offset: int, count: int) -> bytes:
		with lock:
			for index in range(offset // chunks.CHUNK_SIZE, (offset + count - 1) // chunks.CHUNK_SIZE + 1):
				fetched.add(index)
		return bytes(count)

	monkeypatch.setattr(chunks, "fetchRange", fetch)
	size: int = chunks.CHUNK_SIZE * 20
	addRow(db, "filme.mkv", size=size)
	target = chunks.prepareSparse("filme.mkv", size, "qxh:a")
	fd: int = os.open(target, os.O_RDWR)
	readahead = Readahead()
	try:
		offset: int = 0
		step: int = 128 * 1024
		while offset < chunks.CHUNK_SIZE * 2:
			chunks.ensureRange("filme.mkv", "onedrive:filme.mkv", fd, offset, step, size)
			readahead.onRead(fd, "filme.mkv", "onedrive:filme.mkv", offset, step, size)
			offset += step
		deadline: float = time.monotonic() + 5
		while time.monotonic() < deadline:
			with lock:
				if {2, 3, 4, 5}.issubset(fetched):
					break
			time.sleep(0.05)
	finally:
		os.close(fd)
	assert {0, 1, 2, 3, 4, 5}.issubset(fetched)


def test_acesso_aleatorio_nao_pre_le(db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	calls: list[int] = []
	monkeypatch.setattr(chunks, "fetchRange", lambda i, r, offset, count: calls.append(offset) or bytes(count))
	size: int = chunks.CHUNK_SIZE * 20
	addRow(db, "iso.img", size=size)
	target = chunks.prepareSparse("iso.img", size, "qxh:a")
	fd: int = os.open(target, os.O_RDWR)
	readahead = Readahead()
	try:
		for chunk in (10, 3, 15):
			offset = chunk * chunks.CHUNK_SIZE + 4096
			chunks.ensureRange("iso.img", "onedrive:iso.img", fd, offset, 4096, size)
			readahead.onRead(fd, "iso.img", "onedrive:iso.img", offset, 4096, size)
		time.sleep(0.3)
	finally:
		os.close(fd)
	assert len(calls) == 3
