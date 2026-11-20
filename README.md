# Fast-OneDrive

<p align="center">
  <img src="icon.png" alt="Fast-OneDrive" width="160" height="160">
</p>

Integração do **OneDrive no KDE Plasma / Dolphin**, o mais perto possível do OneDrive no Explorer do Windows, em cima do [rclone](https://rclone.org/) (o motor; não é modificado).

Um só lugar: a pasta `~/OneDrive` nos Locais do Dolphin mostra o Drive inteiro com ficheiros **a pedido** (só descarregam quando se abrem), emblemas de estado (na nuvem, neste PC, mantido, a sincronizar, conflito, erro) e o submenu **OneDrive** no botão direito (manter sempre neste dispositivo, libertar espaço, partilhar, ver online, histórico de versões). As mudanças feitas noutros dispositivos aparecem em cerca de um minuto. Busca por nome sem acentos no KRunner, na janela e em `fastonedrive:/search/…`.

Não é o cliente Windows. Não é abraunegg. Não é onedriver. É o companheiro Plasma do rclone.

## Problema que resolve

| Hoje | Fast-OneDrive |
|---|---|
| Dolphin copia `~/OneDrive` → Downloads a ~1 MB/s e pode travar | menu → `rclone copy` multi-thread |
| KFind no mount entra em estado D | SQLite (só nomes): KRunner e Local no Dolphin |
| rclone dir-cache acelera `ls`, mas o KFind não o usa | índice próprio (`lsjson` → SQLite); resultados no Dolphin sem FUSE |

## Estrutura

```text
icon.png            # ícone do projeto (GitHub / fonte)
icons/hicolor/      # resoluções para o tema de ícones (fastonedrive)
src/fastonedrive/   # CLI, FUSE, índice SQLite, Graph, sync, UI
kio/                # worker KF6 (fastonedrive://)
dolphin-plugin/     # emblemas + submenu OneDrive
dolphin/            # .desktop e servicemenu
krunner/            # runner Plasma
systemd/            # unidades user (index, sync, mount)
packaging/          # PKGBUILD Arch / esqueleto AUR
tests/              # pytest
docs/               # arquitectura, decisões, ambiente, packaging
bin/fastonedrive    # launcher de desenvolvimento
```

## Pré-requisitos

- Linux com KDE Plasma (testado em Arch/CachyOS)
- `rclone` ≥ 1.68 (cifra do conf; jobs e OAuth)
- Python ≥ 3.11
- `python-pyqt6`, `python-dbus`, `python-gobject` (UI, bandeja, KRunner)
- `fuse2` + `fuse3`, `cmake`, `extra-cmake-modules`, `kio`, `kdialog`, SQLite ≥ 3.45
- Conta OneDrive já utilizável via rclone (ou criar perfil pelo assistente nas Definições)

## Instalação (desenvolvimento)

```text
# Dependências de teste (opcional)
python -m pip install -e ".[dev]"
# ou: uv sync --extra dev

./bin/fastonedrive install-menus   # CLI em ~/.local/bin, app, bandeja, menus
./bin/fastonedrive install-kio     # worker KIO + plugins Dolphin + Locais + exclusão Baloo
./bin/fastonedrive install-runner  # KRunner
./bin/fastonedrive mount on        # ~/OneDrive (FUSE próprio, Type=notify)
kquitapp6 dolphin                  # reabrir o Dolphin para carregar os plugins
```

Pacote Arch (esqueleto): `packaging/arch/PKGBUILD` — ver [`docs/PACKAGING.md`](docs/PACKAGING.md). Esqueleto AUR: `packaging/aur/` (substituir `CHANGE-ME` no `_repo` antes de publicar).

## Configuração

Não há `.env` obrigatório. Configuração e segredos:

| Ficheiro | Conteúdo |
|---|---|
| `~/.config/fast-onedrive/config.toml` | Perfis, flags rclone/exclude |
| `~/.config/fast-onedrive/rclone.conf` | Remotes e tokens (**nunca** commit) |
| `~/.config/fast-onedrive/sync-control.json` | Pausa, banda, rede limitada, poll |
| `~/.local/share/fast-onedrive/profiles/<id>/` | `index.db`, `pinned/`, `cache/` |

Variáveis de ambiente opcionais: [`.env.example`](.env.example) e [`docs/ENVIRONMENT.md`](docs/ENVIRONMENT.md).

## Uso

```text
./bin/fastonedrive index                 # Graph delta (1.º ciclo completo; seguintes incrementais)
./bin/fastonedrive index onedrive:PATH   # só um prefixo (rclone lsjson)
./bin/fastonedrive search COMEMORA
./bin/fastonedrive search --json QUERY   # para o worker KIO
./bin/fastonedrive copy onedrive:PATH ~/Downloads
./bin/fastonedrive pin onedrive:PATH     # manter localmente
./bin/fastonedrive unpin onedrive:PATH   # deixar de manter (não apaga a nuvem)
./bin/fastonedrive install-menus
./bin/fastonedrive install-runner
./bin/fastonedrive install-kio
./bin/fastonedrive dolphin-search
./bin/fastonedrive status
./bin/fastonedrive log
./bin/fastonedrive settings              # precisa python-pyqt6
./bin/fastonedrive settings --tray
./bin/fastonedrive hydrate onedrive:PATH
./bin/fastonedrive sync-pins
./bin/fastonedrive fuse                  # FUSE ~/OneDrive (primeiro plano)
./bin/fastonedrive link PATH
./bin/fastonedrive free-space PATH
./bin/fastonedrive about
./bin/fastonedrive put LOCAL DEST
./bin/fastonedrive mkdir DEST
./bin/fastonedrive rm PATH               # apaga na nuvem (não é unpin)
./bin/fastonedrive mv SRC DEST
./bin/fastonedrive open-web PATH
./bin/fastonedrive versions PATH
./bin/fastonedrive pause 2h|8h|24h
./bin/fastonedrive resume
./bin/fastonedrive sync-control --bwlimit 10M --pause-on-metered on --poll-seconds 60
./bin/fastonedrive compact
./bin/fastonedrive config-encryption on  # senha na carteira; rclone ≥ 1.68
./bin/fastonedrive open-folder [ITEM]
./bin/fastonedrive mount on|off|status
./bin/fastonedrive doctor                # diagnóstico rápido
```

Busca: `comemoracao` encontra `COMEMORAÇÃO`; filtros `ext:pdf`, `tipo:pasta`, `em:TRABALHO`.

A partir da 0.2 o Local de cada perfil é a pasta FUSE (decisão 27 em `docs/DECISIONS.md`); `fastonedrive:/p/<id>/` só aparece nos Locais com o mount desligado.

App **Fast-OneDrive** no menu: selector de perfil, separadores **Pesquisa**, **rclone** e **Fast-OneDrive**. **Novo perfil** abre o assistente (login Microsoft no browser). A primeira conta já configurada passa a ser o perfil **OneDrive** em `~/OneDrive`. Cada perfil tem índice, pins, cache e FUSE próprios.

KRunner: Alt+F2 → `comemora`. Local no Dolphin: barra **OneDrive**. Erros → `~/.local/share/fast-onedrive/fastonedrive.log` (`fastonedrive log`).

Timer opcional: copiar `systemd/fast-onedrive-index.service` / `.timer` e `fast-onedrive-sync.service` / `.timer` para `~/.config/systemd/user/` e `systemctl --user enable --now fast-onedrive-index.timer fast-onedrive-sync.timer`.

## Testes

```text
python -m pip install -e ".[dev]"
python -m pytest
ruff check src tests
```

Os testes em `tests/` não precisam de rede nem de `/dev/fuse`. Com `rclone` no `PATH` corre também o ciclo completo contra um remote local.

## Documentação

| Ficheiro | Conteúdo |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Componentes e fluxos |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Decisões fechadas |
| [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) | Paths, remotes, variáveis de ambiente |
| [docs/PACKAGING.md](docs/PACKAGING.md) | Pacote Arch `fast-onedrive` |
| [AGENTS.md](AGENTS.md) | Regras para agentes Cursor |

## Nome

Pasta do repo: `Fast-OneDrive`. Pacote Python / CLI: `fastonedrive` (sem hífen). Pacote Arch: `fast-onedrive`. Unidade systemd: `fast-onedrive-mount@<id>.service`.

## Licença

[LGPL-3.0-or-later](LICENSE) — GNU Lesser General Public License v3.0 ou posterior.
