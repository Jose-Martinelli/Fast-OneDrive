"""Busca por nome sem distinguir maiúsculas nem acentos.

O ``COLLATE NOCASE`` do SQLite só dobra ASCII: ``AÇÃO`` não encontrava ``ação`` e
``acao`` não encontrava ``ação``. Aqui o índice é uma tabela FTS5 ``trigram`` com
``remove_diacritics`` (SQLite ≥ 3.45), mantida por triggers sobre ``files``. Os
triggers são SQL puro, por isso qualquer ligação (sqlite3 CLI incluído) continua a
poder escrever em ``files``. Sem FTS5 trigram, a busca cai num ``LIKE`` sobre uma
função ``fodFold`` registada só na ligação da busca.
"""

import re
import sqlite3
import unicodedata

FTS_TABLE: str = "files_search"
MIN_TRIGRAM: int = 3

# Só o nome: com o caminho, cada ficheiro repetia os trigramas de todas as pastas de
# cima (355 MB num Drive de 226 mil itens). A busca por pasta de cima faz-se por
# intervalo no índice de path (ver runSearch).
_TRIGGERS_SQL: str = """
CREATE TRIGGER IF NOT EXISTS files_search_ai AFTER INSERT ON files BEGIN
	INSERT INTO files_search(rowid, name) VALUES (new.rowid, new.name);
END;
CREATE TRIGGER IF NOT EXISTS files_search_ad AFTER DELETE ON files BEGIN
	INSERT INTO files_search(files_search, rowid, name) VALUES ('delete', old.rowid, old.name);
END;
CREATE TRIGGER IF NOT EXISTS files_search_au AFTER UPDATE OF name ON files BEGIN
	INSERT INTO files_search(files_search, rowid, name) VALUES ('delete', old.rowid, old.name);
	INSERT INTO files_search(rowid, name) VALUES (new.rowid, new.name);
END;
"""


def foldText(text: str) -> str:
	decomposed: str = unicodedata.normalize("NFKD", text.casefold())
	return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _tokenizerSpec(connection: sqlite3.Connection) -> str:
	for spec in ("trigram remove_diacritics 1", "trigram"):
		try:
			connection.execute(
				f"CREATE VIRTUAL TABLE temp.fod_probe USING fts5(a, tokenize='{spec}')"
			)
			connection.execute("DROP TABLE temp.fod_probe")
			return spec
		except sqlite3.OperationalError:
			continue
	return ""


STATE_TABLE: str = "files_search_state"
# Até este número de linhas, a migração enche o índice na hora. Acima, quem enche é o
# próximo "fastonedrive index" (subprocesso): a primeira ligação depois da actualização
# pode ser um getattr do FUSE, que não deve ficar parado dezenas de segundos.
INLINE_REBUILD_ROWS: int = 20000


def hasSearchIndex(connection: sqlite3.Connection) -> bool:
	row: object = connection.execute(
		"SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
		(FTS_TABLE,),
	).fetchone()
	if row is None:
		return False
	try:
		ready: object = connection.execute(f"SELECT ready FROM {STATE_TABLE}").fetchone()
	except sqlite3.OperationalError:
		return True
	return ready is not None and int(ready[0]) == 1  # type: ignore[index]


def _setReady(connection: sqlite3.Connection, ready: bool) -> None:
	connection.execute(f"CREATE TABLE IF NOT EXISTS {STATE_TABLE} (ready INTEGER NOT NULL)")
	connection.execute(f"DELETE FROM {STATE_TABLE}")
	connection.execute(f"INSERT INTO {STATE_TABLE}(ready) VALUES (?)", (1 if ready else 0,))


def populateSearchIndexIfNeeded(connection: sqlite3.Connection) -> bool:
	"""Enche o índice de busca se a migração o deixou vazio. Devolve True se encheu."""
	exists: object = connection.execute(
		"SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
		(FTS_TABLE,),
	).fetchone()
	if exists is None or hasSearchIndex(connection):
		return False
	connection.execute(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES ('rebuild')")
	_setReady(connection, True)
	connection.commit()
	return True


def _indexIgnoresAccents(connection: sqlite3.Connection) -> bool:
	row: sqlite3.Row | tuple[object, ...] | None = connection.execute(
		"SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
		(FTS_TABLE,),
	).fetchone()
	if row is None:
		return False
	return "remove_diacritics" in str(row[0] or "")


def _indexHasPathColumn(connection: sqlite3.Connection) -> bool:
	row: sqlite3.Row | tuple[object, ...] | None = connection.execute(
		"SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
		(FTS_TABLE,),
	).fetchone()
	if row is None:
		return False
	return "name, path" in str(row[0] or "")


def ensureSearchIndex(connection: sqlite3.Connection) -> None:
	if _indexHasPathColumn(connection):
		# Índice antigo (nome + caminho): sai e volta só com o nome; quem o enche é o
		# próximo "fastonedrive index". Para o ficheiro encolher: "fastonedrive compact".
		dropSearchIndex(connection)
	if hasSearchIndex(connection):
		return
	spec: str = _tokenizerSpec(connection)
	if spec == "":
		return
	connection.execute(
		f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
		f"name, content='files', content_rowid='rowid', tokenize='{spec}')"
	)
	connection.executescript(_TRIGGERS_SQL)
	rows: int = int(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0])
	if rows <= INLINE_REBUILD_ROWS:
		connection.execute(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES ('rebuild')")
		_setReady(connection, True)
	else:
		_setReady(connection, False)


def ensureStateIndex(connection: sqlite3.Connection) -> None:
	"""Índice parcial dos itens por sincronizar: o emblema de uma pasta procura o pior
	estado dos descendentes, e sem isto lia a subárvore inteira em cada getxattr."""
	connection.execute(
		"CREATE INDEX IF NOT EXISTS idx_files_pending ON files(path) "
		"WHERE syncState IN ('conflict', 'error', 'dirty', 'syncing')"
	)


# Sem "INSERT OR IGNORE" nos triggers: a política de conflito da instrução de fora
# (ex. INSERT ... ON CONFLICT DO UPDATE) substitui a do trigger e o IGNORE vira ABORT.
_CHANGE_SQL: str = """
DROP TRIGGER IF EXISTS files_change_ai;
DROP TRIGGER IF EXISTS files_change_ad;
DROP TRIGGER IF EXISTS files_change_au;
CREATE TABLE IF NOT EXISTS change_log (id INTEGER PRIMARY KEY CHECK (id = 1), counter INTEGER NOT NULL);
INSERT OR IGNORE INTO change_log (id, counter) VALUES (1, 0);
CREATE TABLE IF NOT EXISTS changed_dirs (parent TEXT PRIMARY KEY);
CREATE TRIGGER IF NOT EXISTS files_change_ai AFTER INSERT ON files BEGIN
	UPDATE change_log SET counter = counter + 1 WHERE id = 1;
	INSERT INTO changed_dirs (parent) SELECT new.parent WHERE NOT EXISTS (SELECT 1 FROM changed_dirs WHERE parent = new.parent);
END;
CREATE TRIGGER IF NOT EXISTS files_change_ad AFTER DELETE ON files BEGIN
	UPDATE change_log SET counter = counter + 1 WHERE id = 1;
	INSERT INTO changed_dirs (parent) SELECT old.parent WHERE NOT EXISTS (SELECT 1 FROM changed_dirs WHERE parent = old.parent);
END;
CREATE TRIGGER IF NOT EXISTS files_change_au AFTER UPDATE OF path, parent, size, etag, modTime ON files
WHEN old.path IS NOT new.path OR old.size IS NOT new.size OR old.etag IS NOT new.etag
	OR old.modTime IS NOT new.modTime
BEGIN
	UPDATE change_log SET counter = counter + 1 WHERE id = 1;
	INSERT INTO changed_dirs (parent) SELECT old.parent WHERE NOT EXISTS (SELECT 1 FROM changed_dirs WHERE parent = old.parent);
	INSERT INTO changed_dirs (parent) SELECT new.parent WHERE NOT EXISTS (SELECT 1 FROM changed_dirs WHERE parent = new.parent);
END;
"""


def ensureChangeTracking(connection: sqlite3.Connection) -> None:
	"""Contador de mudanças de conteúdo e pastas tocadas, mantidos por triggers.

	O poll da nuvem compara o contador antes e depois do ``index`` (o ``updatedAt`` tem
	resolução de segundos e muda também com o estado de sync) e relista no Dolphin só as
	pastas tocadas, incluindo as de onde algo foi apagado.
	"""
	connection.executescript(_CHANGE_SQL)


def changeCounter(connection: sqlite3.Connection) -> int:
	try:
		row: object = connection.execute("SELECT counter FROM change_log WHERE id = 1").fetchone()
	except sqlite3.OperationalError:
		return -1
	return int(row[0]) if row is not None else -1  # type: ignore[index]


def takeChangedDirs(connection: sqlite3.Connection, limit: int) -> list[str]:
	try:
		rows: list[sqlite3.Row] = list(connection.execute("SELECT parent FROM changed_dirs"))
		connection.execute("DELETE FROM changed_dirs")
		connection.commit()
	except sqlite3.OperationalError:
		return []
	dirs: list[str] = [str(row[0] or "") for row in rows]
	if len(dirs) > limit:
		# Muitas pastas (índice completo): a raiz chega para o Dolphin relistar o que está aberto.
		return [""]
	return dirs


def dropSearchIndex(connection: sqlite3.Connection) -> None:
	for trigger in ("files_search_ai", "files_search_ad", "files_search_au"):
		connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
	connection.execute(f"DROP TABLE IF EXISTS {FTS_TABLE}")
	connection.execute(f"DROP TABLE IF EXISTS {STATE_TABLE}")


_FILTER_RE: re.Pattern[str] = re.compile(r"^(ext|tipo|type|em|in):(.+)$", re.IGNORECASE)


class SearchQuery:
	"""Termos livres mais filtros ``ext:pdf``, ``tipo:pasta|ficheiro``, ``em:TRABALHO``."""

	def __init__(self, text: str) -> None:
		self.terms: list[str] = []
		self.extensions: list[str] = []
		self.kind: str = ""
		self.under: list[str] = []
		for token in _splitQuery(text):
			match: re.Match[str] | None = _FILTER_RE.match(token)
			if match is None:
				self.terms.append(token)
				continue
			key: str = match.group(1).lower()
			value: str = match.group(2).strip()
			if key == "ext":
				self.extensions.extend(
					part.lstrip(".").lower() for part in value.split(",") if part.strip(".") != ""
				)
			elif key in ("tipo", "type"):
				lowered: str = foldText(value)
				if lowered.startswith(("pasta", "dir", "folder")):
					self.kind = "dir"
				elif lowered.startswith(("fich", "arq", "file")):
					self.kind = "file"
			else:
				self.under.append(value.strip("/"))

	def isEmpty(self) -> bool:
		return (
			len(self.terms) == 0
			and len(self.extensions) == 0
			and self.kind == ""
			and len(self.under) == 0
		)


def _splitQuery(text: str) -> list[str]:
	tokens: list[str] = []
	for quoted, bare in re.findall(r'"([^"]*)"|(\S+)', text):
		value: str = quoted if quoted != "" else bare
		if value.strip() != "":
			tokens.append(value)
	return tokens


def _ftsPhrase(term: str) -> str:
	return '"' + term.replace('"', '""') + '"'


def _escapeLike(value: str) -> str:
	return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def runSearch(
	connection: sqlite3.Connection,
	text: str,
	select: str,
	limit: int,
) -> list[sqlite3.Row]:
	query: SearchQuery = SearchQuery(text)
	connection.create_function("fodFold", 1, _sqlFold, deterministic=True)
	where: list[str] = []
	params: list[object] = []
	# O FTS só serve se o tokenizer ignora acentos (SQLite ≥ 3.45) e cada termo
	# tem pelo menos 3 caracteres (trigram). Senão, LIKE sobre fodFold.
	useFts: bool = (
		len(query.terms) > 0
		and all(len(term) >= MIN_TRIGRAM for term in query.terms)
		and hasSearchIndex(connection)
		and _indexIgnoresAccents(connection)
	)
	nameRank: str = "0"
	rankParams: list[object] = []
	# O FTS só tem nomes. Um termo acerta no item se está no nome dele ou no de uma
	# pasta de cima: os descendentes saem por intervalo no índice de path
	# ("pasta/" < path < "pasta0"), sem repetir os trigramas do caminho em cada linha.
	if useFts:
		sets: list[str] = []
		for term in query.terms:
			phrase: str = _ftsPhrase(term)
			hits: str = f"SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?"
			sets.append(
				f"SELECT rowid FROM files WHERE rowid IN ({hits}) UNION "
				f"SELECT f.rowid FROM files d JOIN files f ON f.path > d.path || '/' AND f.path < d.path || '0' "
				f"WHERE d.isDir = 1 AND d.rowid IN ({hits})"
			)
			params.extend([phrase, phrase])
		where.append("rowid IN (" + " INTERSECT ".join(sets) + ")")
		nameRank = " AND ".join(
			f"rowid IN (SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?)" for _ in query.terms
		)
		rankParams.extend(_ftsPhrase(term) for term in query.terms)
	else:
		for term in query.terms:
			where.append("fodFold(path) LIKE ? ESCAPE '\\'")
			params.append(f"%{_escapeLike(foldText(term))}%")
		if len(query.terms) > 0:
			nameRank = " AND ".join("fodFold(name) LIKE ? ESCAPE '\\'" for _ in query.terms)
			rankParams.extend(f"%{_escapeLike(foldText(term))}%" for term in query.terms)
	if len(query.extensions) > 0:
		where.append(
			"(" + " OR ".join("fodFold(name) LIKE ? ESCAPE '\\'" for _ in query.extensions) + ")"
		)
		params.extend(f"%.{_escapeLike(foldText(ext))}" for ext in query.extensions)
	if query.kind == "dir":
		where.append("isDir = 1")
	elif query.kind == "file":
		where.append("isDir = 0")
	for folder in query.under:
		where.append("(fodFold(path) LIKE ? ESCAPE '\\' OR fodFold(path) LIKE ? ESCAPE '\\')")
		folded: str = _escapeLike(foldText(folder))
		params.append(f"{folded}/%")
		params.append(f"%/{folded}/%")
	if len(where) == 0:
		return []
	sql: str = (
		f"SELECT {select}, ({nameRank}) AS fodNameHit FROM files WHERE "
		+ " AND ".join(where)
		+ " ORDER BY fodNameHit DESC, isDir DESC, pinned DESC, modTime DESC, name COLLATE NOCASE"
	)
	allParams: list[object] = [*rankParams, *params]
	if limit > 0:
		sql += " LIMIT ?"
		allParams.append(limit)
	return list(connection.execute(sql, allParams))


def _sqlFold(value: object) -> str:
	if value is None:
		return ""
	return foldText(str(value))
