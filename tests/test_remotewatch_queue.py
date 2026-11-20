"""O poll não lê o delta enquanto há operações offline recentes por enviar."""

import time
from pathlib import Path

import pytest


def test_delta_espera_pela_fila(home: Path, db: object, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import pendingops, remotewatch

	watcher = remotewatch.RemoteWatcher("", home / "OneDrive")
	monkeypatch.setattr(pendingops, "flushPending", lambda: 0)
	assert watcher._flushOfflineOps() is True
	pendingops.enqueue("delete", "a.txt", "https://g/a")
	assert watcher._flushOfflineOps() is False
	monkeypatch.setattr(time, "time", lambda: 10**12)
	assert watcher._flushOfflineOps() is True
