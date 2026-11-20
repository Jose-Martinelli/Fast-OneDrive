"""Pré-leitura de ficheiros parciais (sparse) no FUSE.

O kernel pede ao FUSE leituras de 128 KiB. Sem pré-leitura, cada bloco de 4 MiB só
era pedido à nuvem quando a leitura lá chegava, um de cada vez. Quando um handle lê
de forma sequencial, os blocos seguintes são pedidos em paralelo, com uma janela que
dobra até 64 MiB. Cada tarefa abre o seu próprio descritor: o do handle pode fechar
(release) a meio da pré-leitura.
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

from fastonedrive import FastOneDriveError
from fastonedrive.log import logError

START_WINDOW: int = 2
MAX_WINDOW: int = 16
WORKERS: int = 4


class _HandleState:
	__slots__ = ("nextOffset", "window", "lastChunk")

	def __init__(self) -> None:
		self.nextOffset: int = -1
		self.window: int = 0
		self.lastChunk: int = -1


class Readahead:
	def __init__(self) -> None:
		self._executor: ThreadPoolExecutor | None = None
		self._lock: threading.Lock = threading.Lock()
		self._handles: dict[int, _HandleState] = {}
		self._inflight: set[tuple[str, int]] = set()

	def _pool(self) -> ThreadPoolExecutor:
		if self._executor is None:
			self._executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="fod-readahead")
		return self._executor

	def onRead(
		self,
		fh: int,
		itemPath: str,
		remotePath: str,
		offset: int,
		size: int,
		fileSize: int,
	) -> None:
		from fastonedrive.chunks import CHUNK_SIZE, holesIn

		end: int = min(offset + size, fileSize)
		with self._lock:
			state: _HandleState | None = self._handles.get(fh)
			if state is None:
				state = _HandleState()
				self._handles[fh] = state
			sequential: bool = state.nextOffset >= 0 and abs(offset - state.nextOffset) <= CHUNK_SIZE
			state.nextOffset = end
			if not sequential:
				state.window = 0 if offset != 0 else START_WINDOW
				if state.window == 0:
					state.lastChunk = -1
					return
			elif state.window == 0:
				state.window = START_WINDOW
			chunk: int = (end - 1) // CHUNK_SIZE if end > 0 else 0
			if chunk == state.lastChunk:
				return
			state.lastChunk = chunk
			if sequential:
				state.window = min(MAX_WINDOW, max(START_WINDOW, state.window * 2))
			window: int = state.window
		first: int = chunk + 1
		last: int = first + window - 1
		try:
			holes: list[int] = holesIn(itemPath, first, last)
		except Exception as error:  # noqa: BLE001 — pré-leitura nunca falha a leitura
			logError(f"pré-leitura: {error}", extra={"path": itemPath})
			return
		for index in holes:
			key: tuple[str, int] = (itemPath, index)
			with self._lock:
				if key in self._inflight:
					continue
				self._inflight.add(key)
			self._pool().submit(self._fetch, itemPath, remotePath, index, fileSize)

	def forget(self, fh: int) -> None:
		with self._lock:
			self._handles.pop(fh, None)

	def _fetch(self, itemPath: str, remotePath: str, index: int, fileSize: int) -> None:
		from fastonedrive.chunks import CHUNK_SIZE, ensureRange
		from fastonedrive.disk import suppressPath
		from fastonedrive.search import cacheLocalPath

		fd: int = -1
		try:
			fd = os.open(str(cacheLocalPath(itemPath)), os.O_RDWR)
			ensureRange(itemPath, remotePath, fd, index * CHUNK_SIZE, CHUNK_SIZE, fileSize)
		except (OSError, FastOneDriveError) as error:
			logError(f"pré-leitura falhou: {error}", extra={"path": itemPath, "chunk": index})
		except Exception as error:  # noqa: BLE001
			logError(f"pré-leitura falhou: {error}", extra={"path": itemPath, "chunk": index})
		finally:
			if fd >= 0:
				# O watcher do mesmo processo ignora o IN_CLOSE_WRITE deste descritor:
				# encher buracos não é uma edição.
				suppressPath(itemPath)
				os.close(fd)
			with self._lock:
				self._inflight.discard((itemPath, index))
