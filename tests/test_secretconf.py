"""rclone.conf cifrado: ligar, ler o token pelo rclone e editar perfis sem perder a cifra."""

import os
import stat
import sys
from pathlib import Path

import pytest

FAKE_RCLONE: str = r'''#!PYTHON
import base64, os, subprocess, sys
args = sys.argv[1:]
conf = args[args.index("--config") + 1]
rest = [a for i, a in enumerate(args) if a != "--config" and (i == 0 or args[i - 1] != "--config")]
MARK = "# Encrypted rclone configuration File\n"
def password():
    return subprocess.run(os.environ["RCLONE_PASSWORD_COMMAND"], shell=True, capture_output=True, text=True).stdout.strip()
text = open(conf).read()
if rest[:3] == ["config", "encryption", "set"]:
    assert os.environ.get("RCLONE_PASSWORD_CHANGE") == "1"
    plain = base64.b64decode(text.split("\n", 1)[1]).decode() if text.startswith(MARK) else text
    open(conf, "w").write(MARK + base64.b64encode((password() + "\0" + plain).encode()).decode())
elif rest[:3] == ["config", "encryption", "remove"]:
    raw = base64.b64decode(text.split("\n", 1)[1]).decode()
    pw, plain = raw.split("\0", 1)
    assert pw == password()
    open(conf, "w").write(plain)
elif rest[:3] == ["config", "encryption", "check"]:
    pass
elif rest[:2] == ["config", "show"]:
    raw = base64.b64decode(text.split("\n", 1)[1]).decode()
    pw, plain = raw.split("\0", 1)
    assert pw == password()
    sys.stdout.write(plain)
else:
    sys.exit(2)
'''


@pytest.fixture()
def fakeTools(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
	from fastonedrive import secretconf

	bin_: Path = home / "bin"
	bin_.mkdir()
	rclone: Path = bin_ / "rclone"
	rclone.write_text(FAKE_RCLONE.replace("PYTHON", sys.executable), encoding="utf-8")
	rclone.chmod(rclone.stat().st_mode | stat.S_IEXEC)
	store: Path = home / "wallet.txt"
	monkeypatch.setenv("PATH", f"{bin_}:{os.environ.get('PATH', '')}")
	monkeypatch.setattr(secretconf, "storePassword", lambda password: store.write_text(password, encoding="utf-8"))
	monkeypatch.setattr(secretconf, "readPassword", lambda: store.read_text(encoding="utf-8"))
	monkeypatch.setattr(secretconf, "passwordCommand", lambda: f"cat {store}")
	return store


def test_cifra_le_e_edita(home: Path, fakeTools: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import config, secretconf
	from fastonedrive.graph import _remoteField
	from fastonedrive.profiles import remoteSectionStatus

	monkeypatch.setattr(secretconf, "_requireRclone168", lambda: None)

	conf: Path = config.ensureRcloneConfig()
	conf.write_text("[onedrive]\ntype = onedrive\ndrive_id = ABC\ntoken = {\"access_token\":\"x\"}\n", encoding="utf-8")
	secretconf.enable()
	assert secretconf.isEncrypted(conf)
	assert "drive_id" not in conf.read_text(encoding="utf-8")
	assert _remoteField("drive_id") == "ABC"
	status = remoteSectionStatus("onedrive")
	assert status is not None and status["hasToken"] == "1"
	with secretconf.plainConfigForEdit(conf):
		assert not secretconf.isEncrypted(conf)
		conf.write_text(conf.read_text(encoding="utf-8") + "[outro]\ntype = onedrive\n", encoding="utf-8")
	assert secretconf.isEncrypted(conf)
	assert "[outro]" in secretconf.readConfigText(conf)
	secretconf.disable()
	assert "[outro]" in conf.read_text(encoding="utf-8")
	assert stat.S_IMODE(conf.stat().st_mode) == 0o600
