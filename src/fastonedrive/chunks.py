import os
import sqlite3
import threading
from pathlib import Path

from fastonedrive import FastOneDriveError
from fastonedrive.index import connectDb
from fastonedrive.rclone import catRange
from fastonedrive.search import applyStoredMode, cacheLocalPath

CHUNK_SIZE: int = 4 * 1024 * 1024
_chunkLocks: dict[tuple[str, int], threading.Lock] = {}
_chunkLockGuard: threading.Lock = threading.Lock()
_pendingWrites: dict[str, list[tuple[int, int]]] = {}


def _remoteStamp(itemPath: str) -> tuple[float, float] | None:
	from fastonedrive.index import getEntry
	from fastonedrive.rclone import parseRemoteModTime

	entry = getEntry(itemPath)
	if entry is None:
		return None
	remoteWhen = parseRemoteModTime(str(entry["modTime"] or ""))
	if remoteWhen is None:
		return None
	epoch: float = remoteWhen.timestamp()
	return (epoch, epoch)


def _restoreFdTime(fd: int, stamp: tuple[float, float] | None) -> None:
	if stamp is None:
		return
	try:
		os.utime(fd, stamp)
	except OSError:
		pass


def _cacheIsDirty(itemPath: str) -> bool:
	from fastonedrive.hydrate import isCacheDirty

	return isCacheDirty(itemPath)


def isPartial(itemPath: str) -> bool:
	if itemPath == "":
		return False
	connection: sqlite3.Connection = connectDb()
	try:
		row: sqlite3.Row | None = connection.execute(
			"SELECT 1 FROM file_chunks WHERE path = ?",
			(itemPath,),
		).fetchone()
		return row is not None
	finally:
		connection.close()


def deleteChunkRow(itemPath: str) -> None:
	if itemPath == "":
		return
	connection: sqlite3.Connection = connectDb()
	try:
		connection.execute("DELETE FROM file_chunks WHERE path = ?", (itemPath,))
		connection.execute("DELETE FROM chunk_writes WHERE path = ?", (itemPath,))
		connection.commit()
	finally:
		connection.close()


def prepareSparse(itemPath: str, remoteSize: int, etag: str) -> Path:
	target: Path = cacheLocalPath(itemPath)
	target.parent.mkdir(parents=True, exist_ok=True)
	row: tuple[str, int, bytes] | None = _loadRow(itemPath)
	if row is not None and (row[0] != etag or row[1] != remoteSize):
		if _cacheIsDirty(itemPath):
			return target
		target.unlink(missing_ok=True)
		deleteChunkRow(itemPath)
		row = None
	existed: bool = target.is_file()
	stamp: tuple[float, float] | None = None
	if existed:
		info: os.stat_result = target.stat()
		stamp = (info.st_atime, info.st_mtime)
	fd: int = os.open(str(target), os.O_CREAT | os.O_RDWR, 0o644)
	try:
		current: int = int(os.fstat(fd).st_size)
		if current < remoteSize:
			os.ftruncate(fd, remoteSize)
		if stamp is None:
			stamp = _remoteStamp(itemPath)
		from fastonedrive.disk import suppressPath

		suppressPath(itemPath)
	finally:
		os.close(fd)
		if stamp is not None and target.is_file():
			try:
				os.utime(target, stamp)
			except OSError:
				pass
	applyStoredMode(itemPath, target)
	if row is None:
		_saveRow(
			itemPath,
			etag,
			remoteSize,
			bytes(_byteCount(_chunkCount(remoteSize))),
		)
	return target


def noteWrite(itemPath: str, offset: int, length: int) -> None:
	if itemPath == "" or length <= 0:
		return
	with _chunkLockGuard:
		_pendingWrites.setdefault(itemPath, []).append((offset, length))
	_storePendingWrite(itemPath, offset, length)


def chunkLocks(itemPath: str, first: int, last: int) -> object:
	return _ChunkGuard(itemPath, first, last)


class _ChunkGuard:
	_itemPath: str
	_first: int
	_last: int
	_locks: list[threading.Lock]

	def __init__(self, itemPath: str, first: int, last: int) -> None:
		self._itemPath = itemPath
		self._first = first
		self._last = last
		self._locks = []

	def __enter__(self) -> None:
		self._locks = _locksFor(self._itemPath, self._first, self._last)
		for lock in self._locks:
			lock.acquire()

	def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
		for lock in reversed(self._locks):
			lock.release()


def ensureRange(
	itemPath: str,
	remotePath: str,
	fd: int,
	offset: int,
	length: int,
	fileSize: int,
) -> None:
	if length <= 0 or fileSize <= 0 or offset >= fileSize:
		return
	if not isPartial(itemPath):
		return
	end: int = min(offset + length, fileSize)
	if end <= offset:
		return
	first: int = offset // CHUNK_SIZE
	last: int = (end - 1) // CHUNK_SIZE
	_withChunkLocks(
		itemPath,
		first,
		last,
		lambda: _fillSpan(itemPath, remotePath, fd, first, last, fileSize, []),
	)


def fillHoles(
	itemPath: str,
	remotePath: str,
	fd: int,
	writes: list[tuple[int, int]],
) -> None:
	if not isPartial(itemPath):
		return
	fileSize: int = int(os.fstat(fd).st_size)
	if fileSize <= 0:
		deleteChunkRow(itemPath)
		return
	row: tuple[str, int, bytes] | None = _loadRow(itemPath)
	if row is None:
		return
	total: int = _chunkCount(int(row[1]))
	if total <= 0:
		deleteChunkRow(itemPath)
		return
	_withChunkLocks(
		itemPath,
		0,
		total - 1,
		lambda: _fillSpan(itemPath, remotePath, fd, 0, total - 1, fileSize, writes),
	)


def _fillSpan(
	itemPath: str,
	remotePath: str,
	fd: int,
	first: int,
	last: int,
	fileSize: int,
	writes: list[tuple[int, int]],
) -> None:
	row: tuple[str, int, bytes] | None = _loadRow(itemPath)
	if row is None:
		return
	etag: str = row[0]
	storedSize: int = int(row[1])
	bitmap: bytearray = bytearray(row[2])
	stamp: tuple[float, float] | None = None
	try:
		info: os.stat_result = os.fstat(fd)
		stamp = (info.st_atime, info.st_mtime)
	except OSError:
		stamp = None
	total: int = _chunkCount(storedSize)
	index: int = first
	try:
		while index <= last:
			if index >= total or _bitIsSet(bitmap, index):
				index += 1
				continue
			chunkOffset: int = index * CHUNK_SIZE
			if chunkOffset >= fileSize:
				index += 1
				continue
			endIndex: int = index
			while endIndex + 1 <= last and endIndex + 1 < total and not _bitIsSet(bitmap, endIndex + 1):
				nextOffset: int = (endIndex + 1) * CHUNK_SIZE
				if nextOffset >= fileSize:
					break
				endIndex += 1
			spanEnd: int = min((endIndex + 1) * CHUNK_SIZE, storedSize, fileSize)
			spanLength: int = spanEnd - chunkOffset
			if spanLength <= 0:
				index = endIndex + 1
				continue
			coveredBy: list[tuple[int, int]] = [*writes, *_pendingFor(itemPath)]
			if _rangeCovered(coveredBy, chunkOffset, spanEnd):
				for bit in range(index, endIndex + 1):
					_setBit(bitmap, bit)
				_dropPending(itemPath, chunkOffset, spanEnd)
			else:
				downloaded: bytes = catRange(remotePath, chunkOffset, spanLength)
				merged: bytearray = bytearray(downloaded)
				_overlayWrites(fd, merged, chunkOffset, coveredBy)
				os.pwrite(fd, bytes(merged), chunkOffset)
				for bit in range(index, endIndex + 1):
					_setBit(bitmap, bit)
				_dropPending(itemPath, chunkOffset, spanEnd)
			index = endIndex + 1
		if not _hasHole(bitmap, total, fileSize):
			deleteChunkRow(itemPath)
			return
		_saveRow(itemPath, etag, storedSize, bytes(bitmap))
	finally:
		_restoreFdTime(fd, stamp)


def _withChunkLocks(
	itemPath: str,
	first: int,
	last: int,
	work: object,
) -> None:
	locks: list[threading.Lock] = _locksFor(itemPath, first, last)
	for lock in locks:
		lock.acquire()
	try:
		if callable(work):
			work()
	finally:
		for lock in reversed(locks):
			lock.release()


def _locksFor(itemPath: str, first: int, last: int) -> list[threading.Lock]:
	found: list[threading.Lock] = []
	with _chunkLockGuard:
		index: int
		for index in range(first, last + 1):
			key: tuple[str, int] = (itemPath, index)
			lock: threading.Lock | None = _chunkLocks.get(key)
			if lock is None:
				lock = threading.Lock()
				_chunkLocks[key] = lock
			found.append(lock)
	return found


def _loadRow(itemPath: str) -> tuple[str, int, bytes] | None:
	connection: sqlite3.Connection = connectDb()
	try:
		row: sqlite3.Row | None = connection.execute(
			"SELECT etag, size, bitmap FROM file_chunks WHERE path = ?",
			(itemPath,),
		).fetchone()
		if row is None:
			return None
		bitmap: bytes = bytes(row["bitmap"] or b"")
		return (str(row["etag"] or ""), int(row["size"] or 0), bitmap)
	finally:
		connection.close()


def _saveRow(itemPath: str, etag: str, size: int, bitmap: bytes) -> None:
	connection: sqlite3.Connection = connectDb()
	try:
		connection.execute(
			"""
			INSERT INTO file_chunks (path, etag, size, bitmap)
			VALUES (?, ?, ?, ?)
			ON CONFLICT(path) DO UPDATE SET
				etag = excluded.etag,
				size = excluded.size,
				bitmap = excluded.bitmap
			""",
			(itemPath, etag, size, bitmap),
		)
		connection.commit()
	finally:
		connection.close()


def _chunkCount(size: int) -> int:
	if size <= 0:
		return 0
	return (size + CHUNK_SIZE - 1) // CHUNK_SIZE


def _byteCount(chunks: int) -> int:
	if chunks <= 0:
		return 0
	return (chunks + 7) // 8


def _bitIsSet(bitmap: bytes | bytearray, index: int) -> bool:
	byteIndex: int = index // 8
	if byteIndex < 0 or byteIndex >= len(bitmap):
		return False
	return (bitmap[byteIndex] & (1 << (index % 8))) != 0


def _setBit(bitmap: bytearray, index: int) -> None:
	byteIndex: int = index // 8
	if byteIndex < 0 or byteIndex >= len(bitmap):
		return
	bitmap[byteIndex] = bitmap[byteIndex] | (1 << (index % 8))


def _hasHole(bitmap: bytes | bytearray, total: int, fileSize: int) -> bool:
	index: int
	for index in range(total):
		if index * CHUNK_SIZE >= fileSize:
			return False
		if not _bitIsSet(bitmap, index):
			return True
	return False


def _pendingFor(itemPath: str) -> list[tuple[int, int]]:
	with _chunkLockGuard:
		memory: list[tuple[int, int]] = list(_pendingWrites.get(itemPath, []))
	return memory + _loadStoredWrites(itemPath)


def _storePendingWrite(itemPath: str, offset: int, length: int) -> None:
	connection: sqlite3.Connection = connectDb()
	try:
		connection.execute(
			"INSERT INTO chunk_writes (path, start, length) VALUES (?, ?, ?)",
			(itemPath, offset, length),
		)
		connection.commit()
	finally:
		connection.close()


def _loadStoredWrites(itemPath: str) -> list[tuple[int, int]]:
	connection: sqlite3.Connection = connectDb()
	try:
		rows: list[sqlite3.Row] = list(
			connection.execute(
				"SELECT start, length FROM chunk_writes WHERE path = ?",
				(itemPath,),
			)
		)
	finally:
		connection.close()
	return [(int(row["start"]), int(row["length"])) for row in rows]


def _dropPending(itemPath: str, start: int, end: int) -> None:
	with _chunkLockGuard:
		kept: list[tuple[int, int]] = []
		offset: int
		length: int
		for offset, length in _pendingWrites.get(itemPath, []):
			if offset < start or offset + length > end:
				kept.append((offset, length))
		if len(kept) == 0:
			_pendingWrites.pop(itemPath, None)
		else:
			_pendingWrites[itemPath] = kept
	connection: sqlite3.Connection = connectDb()
	try:
		connection.execute(
			"""
			DELETE FROM chunk_writes
			WHERE path = ? AND start >= ? AND start + length <= ?
			""",
			(itemPath, start, end),
		)
		connection.commit()
	finally:
		connection.close()


def _rangeCovered(writes: list[tuple[int, int]], start: int, end: int) -> bool:
	if end <= start:
		return True
	spans: list[tuple[int, int]] = []
	offset: int
	length: int
	for offset, length in writes:
		wStart: int = max(start, offset)
		wEnd: int = min(end, offset + length)
		if wEnd > wStart:
			spans.append((wStart, wEnd))
	if len(spans) == 0:
		return False
	spans.sort()
	cursor: int = start
	wStart = 0
	wEnd = 0
	for wStart, wEnd in spans:
		if wStart > cursor:
			return False
		cursor = max(cursor, wEnd)
		if cursor >= end:
			return True
	return cursor >= end


def _overlayWrites(
	fd: int,
	data: bytearray,
	chunkOffset: int,
	writes: list[tuple[int, int]],
) -> None:
	offset: int
	length: int
	for offset, length in writes:
		wStart: int = max(chunkOffset, offset)
		wEnd: int = min(chunkOffset + len(data), offset + length)
		if wEnd <= wStart:
			continue
		local: bytes = os.pread(fd, wEnd - wStart, wStart)
		begin: int = wStart - chunkOffset
		data[begin:begin + len(local)] = local
