from conftest import addRow

from fastonedrive.searchindex import SearchQuery, dropSearchIndex, foldText


def _paths(rows: list[object]) -> list[str]:
	return [row["path"] for row in rows]  # type: ignore[index]


def _seed(db: object) -> None:
	addRow(db, "TRABALHO", isDir=True)
	addRow(db, "TRABALHO/COMEMORAÇÃO - Amanda.docx")
	addRow(db, "Documentos/acao.pdf")
	addRow(db, "Fotos/praia.jpg")


def test_foldText_ignora_acentos_e_maiusculas() -> None:
	assert foldText("AÇÃO Ñandú") == "acao nandu"


def test_busca_sem_acentos_nem_maiusculas(db: object) -> None:
	from fastonedrive.search import search

	_seed(db)
	assert _paths(search("comemoracao")) == ["TRABALHO/COMEMORAÇÃO - Amanda.docx"]
	assert set(_paths(search("AÇÃO"))) == {"Documentos/acao.pdf", "TRABALHO/COMEMORAÇÃO - Amanda.docx"}
	assert set(_paths(search("acao"))) == {"Documentos/acao.pdf", "TRABALHO/COMEMORAÇÃO - Amanda.docx"}


def test_filtros(db: object) -> None:
	from fastonedrive.search import search

	_seed(db)
	assert _paths(search("ext:pdf")) == ["Documentos/acao.pdf"]
	assert _paths(search("tipo:pasta")) == ["TRABALHO"]
	assert _paths(search("em:TRABALHO amanda")) == ["TRABALHO/COMEMORAÇÃO - Amanda.docx"]
	assert _paths(search("praia jpg")) == ["Fotos/praia.jpg"]


def test_termos_curtos_usam_like(db: object) -> None:
	from fastonedrive.search import search

	_seed(db)
	assert _paths(search("co")) == ["TRABALHO/COMEMORAÇÃO - Amanda.docx"]


def test_triggers_mantem_o_indice(db: object) -> None:
	from fastonedrive.search import search

	_seed(db)
	db.execute("UPDATE files SET path = 'X/praia.jpg', parent = 'X' WHERE path = 'Fotos/praia.jpg'")  # type: ignore[attr-defined]
	db.execute("DELETE FROM files WHERE path = 'Documentos/acao.pdf'")  # type: ignore[attr-defined]
	db.commit()  # type: ignore[attr-defined]
	assert _paths(search("praia")) == ["X/praia.jpg"]
	assert _paths(search("acao")) == ["TRABALHO/COMEMORAÇÃO - Amanda.docx"]


def test_sem_fts_cai_no_like(db: object) -> None:
	from fastonedrive.search import search

	_seed(db)
	dropSearchIndex(db)  # type: ignore[arg-type]
	db.commit()  # type: ignore[attr-defined]
	assert set(_paths(search("ação"))) == {"Documentos/acao.pdf", "TRABALHO/COMEMORAÇÃO - Amanda.docx"}


def test_nome_vem_antes_do_caminho(db: object) -> None:
	from fastonedrive.search import search

	addRow(db, "praia", isDir=True)
	addRow(db, "praia/foto.jpg")
	addRow(db, "Fotos/praia.png")
	found = _paths(search("praia"))
	assert found.index("Fotos/praia.png") < found.index("praia/foto.jpg")


def test_indice_grande_enche_no_proximo_index(db: object, monkeypatch: object) -> None:
	from fastonedrive import searchindex
	from fastonedrive.search import search

	_seed(db)
	searchindex.dropSearchIndex(db)  # type: ignore[arg-type]
	monkeypatch.setattr(searchindex, "INLINE_REBUILD_ROWS", 1)  # type: ignore[attr-defined]
	searchindex.ensureSearchIndex(db)  # type: ignore[arg-type]
	db.commit()  # type: ignore[attr-defined]
	assert not searchindex.hasSearchIndex(db)  # type: ignore[arg-type]
	# Ainda sem FTS cheio, a busca cai no LIKE e encontra na mesma.
	assert _paths(search("comemoracao")) == ["TRABALHO/COMEMORAÇÃO - Amanda.docx"]
	assert searchindex.populateSearchIndexIfNeeded(db)  # type: ignore[arg-type]
	assert searchindex.hasSearchIndex(db)  # type: ignore[arg-type]
	assert _paths(search("comemoracao")) == ["TRABALHO/COMEMORAÇÃO - Amanda.docx"]


def test_estado_da_pasta_usa_indice_parcial(db: object) -> None:
	plan = [
		tuple(row)
		for row in db.execute(  # type: ignore[attr-defined]
			"EXPLAIN QUERY PLAN SELECT DISTINCT syncState FROM files WHERE path >= ? AND path < ? "
			"AND syncState IN ('conflict', 'error', 'dirty', 'syncing')",
			("a/", "a0"),
		)
	]
	assert any("idx_files_pending" in str(row[3]) for row in plan)


def test_query_vazia() -> None:
	assert SearchQuery("   ").isEmpty()
	assert not SearchQuery("ext:pdf").isEmpty()
