import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT: Path = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
	"""HOME isolado: config, índice, pins e cache vão para tmp_path."""
	monkeypatch.setenv("HOME", str(tmp_path))
	monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
	from fastonedrive import config

	config.clearActiveProfile()
	yield tmp_path
	config.clearActiveProfile()


@pytest.fixture()
def db(home: Path) -> Iterator[object]:
	from fastonedrive.index import connectDb

	connection = connectDb()
	yield connection
	connection.close()


def addRow(connection: object, path: str, isDir: bool = False, size: int = 10, pinned: int = 0) -> None:
	from fastonedrive.index import UPSERT_SQL

	name: str = path.rsplit("/", 1)[-1]
	parent: str = path.rsplit("/", 1)[0] if "/" in path else ""
	connection.execute(  # type: ignore[attr-defined]
		UPSERT_SQL,
		(path, name, parent, 1 if isDir else 0, size, "2026-01-01T00:00:00Z", "", "qxh:a", "", "2026-01-01T00:00:00+00:00", "", ""),
	)
	if pinned:
		connection.execute("UPDATE files SET pinned = 1 WHERE path = ?", (path,))  # type: ignore[attr-defined]
	connection.commit()  # type: ignore[attr-defined]
