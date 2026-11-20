"""Ciclo completo com um rclone real e um remote local (alias para uma pasta).

Não usa rede nem conta Microsoft: o "OneDrive" é tmp/cloud. Corre só se houver
``rclone`` no PATH. Cobre índice, pin, envio, descarga, conflito, pausa, unpin e hydrate.
"""

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT: Path = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone não instalado")


class Env:
	def __init__(self, base: Path) -> None:
		self.base: Path = base
		self.cloud: Path = base / "cloud"
		self.pinned: Path = base / ".local/share/fast-onedrive/profiles/onedrive/pinned"
		self.cache: Path = base / ".local/share/fast-onedrive/profiles/onedrive/cache"
		self.clock: float = time.time()

	def run(self, *args: str) -> str:
		env: dict[str, str] = dict(os.environ)
		env["HOME"] = str(self.base)
		result = subprocess.run(
			[sys.executable, str(ROOT / "bin" / "fastonedrive"), *args],
			capture_output=True,
			text=True,
			env=env,
			timeout=120,
		)
		assert result.returncode == 0, result.stderr
		return result.stdout + result.stderr

	def write(self, path: Path, text: str) -> None:
		# Relógio artificial a avançar: a ordem das edições não depende da resolução do mtime.
		self.clock += 60
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(text, encoding="utf-8")
		os.utime(path, (self.clock, self.clock))


@pytest.fixture()
def env(tmp_path: Path) -> Env:
	current = Env(tmp_path)
	(current.cloud / "TRABALHO" / "sub").mkdir(parents=True)
	current.clock = time.time() - 3600
	current.write(current.cloud / "TRABALHO" / "a.txt", "versão 1")
	current.write(current.cloud / "TRABALHO" / "sub" / "b.txt", "b original")
	(current.cloud / "grande.bin").write_bytes(os.urandom(3 * 1024 * 1024))
	conf: Path = tmp_path / ".config" / "fast-onedrive"
	conf.mkdir(parents=True)
	(conf / "rclone.conf").write_text(f"[onedrive]\ntype = alias\nremote = {current.cloud}\n", encoding="utf-8")
	(tmp_path / "OneDrive").mkdir()
	(conf / "config.toml").write_text(
		f'copyDest = "{tmp_path}/Downloads"\n'
		"openMaxBytes = 52428800\n"
		'skipPin = ["roms"]\n'
		f'rcloneConfig = "{conf}/rclone.conf"\n'
		'remoteName = "onedrive"\n'
		"mountEnabled = false\n\n"
		"[[profiles]]\n"
		'id = "onedrive"\n'
		'name = "OneDrive"\n'
		'remoteName = "onedrive"\n'
		f'mountPath = "{tmp_path}/OneDrive"\n'
		"mountEnabled = false\n"
		"legacy = true\n",
		encoding="utf-8",
	)
	# Os subprocessos que o CLI lança (resume, put) usam este, não o fastonedrive instalado.
	_writeWrapper(tmp_path / ".local" / "bin" / "fastonedrive")
	# Sem pausa por rede limitada no ambiente de teste.
	current.run("sync-control", "--pause-on-metered", "off")
	current.run("index")
	return current


def _writeWrapper(wrapper: Path) -> None:
	wrapper.parent.mkdir(parents=True, exist_ok=True)
	wrapper.write_text(
		f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(ROOT / 'bin' / 'fastonedrive'))} \"$@\"\n",
		encoding="utf-8",
	)
	wrapper.chmod(0o755)


def test_busca_depois_do_indice(env: Env) -> None:
	assert "onedrive:TRABALHO/a.txt" in env.run("search", "a.txt")


def test_ciclo_de_pins(env: Env) -> None:
	env.run("pin", "onedrive:TRABALHO")
	assert (env.pinned / "TRABALHO" / "a.txt").read_text(encoding="utf-8") == "versão 1"
	assert (env.pinned / "TRABALHO" / "sub" / "b.txt").is_file()

	env.write(env.pinned / "TRABALHO" / "a.txt", "editado aqui")
	assert "enviados 1" in env.run("sync-pins")
	assert (env.cloud / "TRABALHO" / "a.txt").read_text(encoding="utf-8") == "editado aqui"

	env.write(env.cloud / "TRABALHO" / "sub" / "b.txt", "editado na nuvem")
	env.run("index")
	assert "baixados 1" in env.run("sync-pins")
	assert (env.pinned / "TRABALHO" / "sub" / "b.txt").read_text(encoding="utf-8") == "editado na nuvem"

	env.write(env.pinned / "TRABALHO" / "a.txt", "local X")
	env.write(env.cloud / "TRABALHO" / "a.txt", "nuvem Y")
	env.run("index")
	assert "conflitos 1" in env.run("sync-pins")
	names: list[str] = sorted(path.name for path in (env.pinned / "TRABALHO").iterdir())
	conflicts: list[str] = [name for name in names if "conflito local" in name]
	assert len(conflicts) == 1
	assert (env.pinned / "TRABALHO" / "a.txt").read_text(encoding="utf-8") == "nuvem Y"
	assert (env.pinned / "TRABALHO" / conflicts[0]).read_text(encoding="utf-8") == "local X"

	env.run("pause", "2h")
	env.write(env.pinned / "TRABALHO" / "sub" / "b.txt", "durante a pausa")
	assert "pausa" in env.run("sync-pins")
	assert (env.cloud / "TRABALHO" / "sub" / "b.txt").read_text(encoding="utf-8") == "editado na nuvem"
	env.run("resume")
	# O resume lança um sync-pins em segundo plano: o envio pode ser desse processo.
	env.run("sync-pins")
	assert (env.cloud / "TRABALHO" / "sub" / "b.txt").read_text(encoding="utf-8") == "durante a pausa"

	env.run("unpin", "onedrive:TRABALHO")
	assert not (env.pinned / "TRABALHO" / "sub" / "b.txt").exists()
	assert (env.cloud / "TRABALHO" / "sub" / "b.txt").is_file()


def test_cache_editado_com_nuvem_mudada_gera_conflito(env: Env) -> None:
	env.run("hydrate", "onedrive:TRABALHO/a.txt")
	env.write(env.cache / "TRABALHO" / "a.txt", "editado no cache")
	env.write(env.cloud / "TRABALHO" / "a.txt", "editado na web")
	env.run("index")
	assert "conflitos 1" in env.run("sync-pins")
	assert (env.cloud / "TRABALHO" / "a.txt").read_text(encoding="utf-8") == "editado na web"
	conflicts: list[Path] = [path for path in (env.cloud / "TRABALHO").iterdir() if "conflito local" in path.name]
	assert len(conflicts) == 1
	assert conflicts[0].read_text(encoding="utf-8") == "editado no cache"


def test_poll_da_nuvem_traz_mudancas_para_os_pins(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import config, remotewatch

	env.run("pin", "onedrive:TRABALHO")
	wrapper: Path = env.base / "bin" / "fastonedrive"
	_writeWrapper(wrapper)
	monkeypatch.setenv("HOME", str(env.base))
	monkeypatch.setattr(Path, "home", classmethod(lambda cls: env.base))
	monkeypatch.setattr(config, "cliBin", lambda: str(wrapper))
	monkeypatch.setattr(remotewatch, "ALLOW_FULL_LISTING", True)
	config.activateProfile("onedrive")
	notified: list[list[str]] = []
	from fastonedrive import notify

	monkeypatch.setattr(notify, "notifyLocalDirsChanged", lambda dirs: notified.append(dirs))
	try:
		env.write(env.cloud / "TRABALHO" / "sub" / "b.txt", "mudou noutro PC")
		watcher = remotewatch.RemoteWatcher("onedrive", env.base / "OneDrive")
		watcher._cycle()
		assert watcher.healthy()
		assert (env.pinned / "TRABALHO" / "sub" / "b.txt").read_text(encoding="utf-8") == "mudou noutro PC"
		assert any(str(env.base / "OneDrive" / "TRABALHO" / "sub") in dirs for dirs in notified)
	finally:
		config.clearActiveProfile()


def test_poll_sem_graph_nao_lista_o_drive_inteiro(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import config, remotewatch

	wrapper: Path = env.base / "bin" / "fastonedrive"
	_writeWrapper(wrapper)
	monkeypatch.setenv("HOME", str(env.base))
	monkeypatch.setattr(Path, "home", classmethod(lambda cls: env.base))
	monkeypatch.setattr(config, "cliBin", lambda: str(wrapper))
	config.activateProfile("onedrive")
	try:
		env.write(env.cloud / "novo.txt", "x")
		watcher = remotewatch.RemoteWatcher("onedrive", env.base / "OneDrive")
		watcher._cycle()
		assert not watcher.healthy()
		assert watcher._failures == 1
		assert "novo.txt" not in env.run("search", "novo")
	finally:
		config.clearActiveProfile()


def test_apagar_no_explorador_com_envio_pendente(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
	from fastonedrive import config
	from fastonedrive.index import getEntry, setSyncState
	from fastonedrive.mutate import removeRemotePath

	env.run("pin", "onedrive:TRABALHO")
	env.write(env.pinned / "TRABALHO" / "a.txt", "por enviar")
	monkeypatch.setenv("HOME", str(env.base))
	monkeypatch.setattr(Path, "home", classmethod(lambda cls: env.base))
	config.activateProfile("onedrive")
	try:
		setSyncState("TRABALHO/a.txt", "syncing")
		with pytest.raises(Exception):
			removeRemotePath("onedrive:TRABALHO/a.txt", True)
		removeRemotePath("onedrive:TRABALHO/a.txt", True, discardLocalEdits=True)
		assert not (env.cloud / "TRABALHO" / "a.txt").exists()
		assert not (env.pinned / "TRABALHO" / "a.txt").exists()
		assert getEntry("TRABALHO/a.txt") is None
	finally:
		config.clearActiveProfile()


def test_hydrate_e_limite_de_banda(env: Env) -> None:
	env.run("sync-control", "--bwlimit", "50M")
	env.run("hydrate", "onedrive:grande.bin")
	assert (env.cache / "grande.bin").read_bytes() == (env.cloud / "grande.bin").read_bytes()
	assert "50M" in env.run("sync-control")
