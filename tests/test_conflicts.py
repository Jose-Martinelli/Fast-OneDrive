"""Falsos "conflito local": o mesmo QuickXor em hex e base64, e substituir um ficheiro
que nunca foi aberto neste PC."""

import base64
from pathlib import Path

import pytest

RAW: bytes = bytes.fromhex("e51d1cccabe3de5d8d34f542587318bcb49c551e")
HEX: str = "qxh:" + RAW.hex()
B64: str = "qxh:" + base64.b64encode(RAW).decode("ascii")


def test_mesmo_hash_em_hex_e_base64_nao_muda() -> None:
	from fastonedrive.rclone import canonicalToken, sameToken, tokensDiffer

	assert canonicalToken(HEX) == B64
	assert canonicalToken(B64) == B64
	assert sameToken(HEX, B64)
	assert not tokensDiffer(HEX, B64, None, None, 10, 10)
	assert tokensDiffer(HEX, "qxh:" + base64.b64encode(bytes(20)).decode(), None, None, 10, 10)


def _row(db: object, path: str, etag: str, base: str, state: str) -> None:
	from conftest import addRow

	addRow(db, path)
	db.execute(  # type: ignore[attr-defined]
		"UPDATE files SET etag = ?, baseEtag = ?, baseModTime = '2026-11-18T13:48:07Z', syncState = ? WHERE path = ?",
		(etag, base, state, path),
	)
	db.commit()  # type: ignore[attr-defined]


def test_delta_com_a_nossa_versao_nao_marca_conflito(db: object) -> None:
	from fastonedrive.index import UPSERT_SQL, getEntry

	_row(db, "planilha.xlsx", B64, B64, "dirty")
	db.execute(  # type: ignore[attr-defined]
		UPSERT_SQL,
		("planilha.xlsx", "planilha.xlsx", "", 0, 10, "2026-11-18T13:48:07Z", "", B64, "", "x", "", ""),
	)
	db.commit()  # type: ignore[attr-defined]
	entry = getEntry("planilha.xlsx")
	assert entry is not None and str(entry["syncState"]) == "dirty"


def test_envio_em_curso_nao_vira_conflito_pelo_delta(db: object) -> None:
	from fastonedrive.index import UPSERT_SQL, getEntry

	other: str = "qxh:" + base64.b64encode(bytes(range(20))).decode()
	_row(db, "planilha.xlsx", B64, B64, "syncing")
	db.execute(  # type: ignore[attr-defined]
		UPSERT_SQL,
		("planilha.xlsx", "planilha.xlsx", "", 0, 10, "2026-11-18T13:50:00Z", "", other, "", "x", "", ""),
	)
	db.commit()  # type: ignore[attr-defined]
	entry = getEntry("planilha.xlsx")
	assert entry is not None and str(entry["syncState"]) == "syncing"


def test_mudanca_real_na_nuvem_com_edicao_local_continua_conflito(db: object) -> None:
	from fastonedrive.index import UPSERT_SQL, getEntry

	other: str = "qxh:" + base64.b64encode(bytes(range(20))).decode()
	_row(db, "planilha.xlsx", B64, B64, "dirty")
	db.execute(  # type: ignore[attr-defined]
		UPSERT_SQL,
		("planilha.xlsx", "planilha.xlsx", "", 0, 10, "2026-11-18T13:50:00Z", "", other, "", "x", "", ""),
	)
	db.commit()  # type: ignore[attr-defined]
	entry = getEntry("planilha.xlsx")
	assert entry is not None and str(entry["syncState"]) == "conflict"


def test_migracao_normaliza_e_desfaz_falso_conflito(db: object) -> None:
	from fastonedrive.index import _canonicalizeTokens, getEntry

	_row(db, "planilha.xlsx", B64, HEX, "conflict")
	_canonicalizeTokens(db)  # type: ignore[arg-type]
	db.commit()  # type: ignore[attr-defined]
	entry = getEntry("planilha.xlsx")
	assert entry is not None
	assert str(entry["baseEtag"]) == B64
	assert str(entry["syncState"]) == "dirty"


def test_substituir_ficheiro_nunca_aberto_sobe_sem_conflito(
	home: Path, db: object, monkeypatch: pytest.MonkeyPatch
) -> None:
	from fastonedrive import syncpins
	from fastonedrive.search import cacheLocalPath

	_row(db, "contrato.pdf", B64, "", "dirty")
	local: Path = cacheLocalPath("contrato.pdf")
	local.parent.mkdir(parents=True, exist_ok=True)
	local.write_bytes(b"versao nova")
	uploads: list[str] = []
	conflicts: list[str] = []
	monkeypatch.setattr(syncpins, "hashLocalQuickXor", lambda path: "qxh:" + base64.b64encode(bytes(20)).decode())
	monkeypatch.setattr(syncpins, "_uploadLocal", lambda itemPath, *a, **k: uploads.append(itemPath))
	monkeypatch.setattr(syncpins, "_writeConflict", lambda itemPath, *a, **k: conflicts.append(itemPath))
	stat = {"ModTime": "2026-01-01T00:00:00Z", "Size": 10, "Hashes": {"quickxor": RAW.hex()}, "IsDir": False}
	stats: dict[str, int] = {"skipped": 0, "errors": 0}
	syncpins._resolveHashedBaseline("contrato.pdf", local, stat, stats, None)
	assert uploads == ["contrato.pdf"]
	assert conflicts == []


def test_substituir_quando_a_nuvem_mudou_entretanto_e_conflito(
	home: Path, db: object, monkeypatch: pytest.MonkeyPatch
) -> None:
	from fastonedrive import syncpins
	from fastonedrive.search import cacheLocalPath

	_row(db, "contrato.pdf", B64, "", "dirty")
	local: Path = cacheLocalPath("contrato.pdf")
	local.parent.mkdir(parents=True, exist_ok=True)
	local.write_bytes(b"versao nova")
	conflicts: list[str] = []
	monkeypatch.setattr(syncpins, "hashLocalQuickXor", lambda path: "qxh:" + base64.b64encode(bytes(20)).decode())
	monkeypatch.setattr(syncpins, "_uploadLocal", lambda itemPath, *a, **k: pytest.fail("subiu por cima"))
	monkeypatch.setattr(syncpins, "_writeConflict", lambda itemPath, *a, **k: conflicts.append(itemPath))
	changed = bytes(range(20))
	stat = {"ModTime": "2026-11-18T15:00:00Z", "Size": 11, "Hashes": {"quickxor": changed.hex()}, "IsDir": False}
	syncpins._resolveHashedBaseline("contrato.pdf", local, stat, {"skipped": 0, "errors": 0}, None)
	assert conflicts == ["contrato.pdf"]
