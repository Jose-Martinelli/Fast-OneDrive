# Fast-OneDrive

Add-on **KDE Plasma / Dolphin** em cima do [rclone](https://rclone.org/). O rclone não se modifica: é o motor (CLI). Este projeto é a integração no desktop — busca por nome, cópia rápida, pin e um FUSE próprio em `~/OneDrive`. Busca e cópia de ficheiros grandes **não** passam pelo FUSE.

Não é o cliente Windows. Não é abraunegg. Não é onedriver. É o companheiro Plasma do rclone que o autor já tem a correr.

## Problema que resolve

| Hoje | Fast-OneDrive |
|---|---|
| Dolphin copia `~/OneDrive` → Downloads a ~1 MB/s e pode travar | menu → `rclone copy` multi-thread |
| KFind no mount entra em estado D | SQLite (só nomes): KRunner e Local no Dolphin |
| rclone dir-cache acelera `ls`, mas o KFind não o usa | índice próprio (`lsjson` → SQLite); resultados no Dolphin sem FUSE |

## Continuar noutro chat

Workspace: `/mnt/programacao/Repositorios e Tomcats/Fast-OneDrive`

Colar [`CHAT_STARTER.md`](CHAT_STARTER.md) na primeira mensagem Agent. Ler **todos** os ficheiros em `docs/` antes de código.

## Documentação

| Ficheiro | Conteúdo |
|---|---|
| [docs/CONTEXT.md](docs/CONTEXT.md) | Histórico da máquina, o que já foi tentado, o que não fazer |
| [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) | rclone, mount, portas, discos, remotes |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Componentes e fluxos |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Fases 0–4 e critério de feito |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Decisões fechadas |
| [docs/PACKAGING.md](docs/PACKAGING.md) | Pacote Arch `fast-onedrive` |
| [AGENTS.md](AGENTS.md) | Regras para o agente Cursor |

## Uso (fases 0–4 overlay KIO)

```text
./bin/fastonedrive index                 # Graph delta (1.º ciclo completo; seguintes incrementais)
./bin/fastonedrive index onedrive:PATH   # só um prefixo (rclone lsjson)
./bin/fastonedrive search COMEMORA
./bin/fastonedrive search --json QUERY   # para o worker KIO
./bin/fastonedrive copy onedrive:PATH ~/Downloads
./bin/fastonedrive pin onedrive:PATH     # manter localmente
./bin/fastonedrive unpin onedrive:PATH   # deixar de manter (não apaga a nuvem)
./bin/fastonedrive install-menus         # botão direito no Dolphin
./bin/fastonedrive install-runner        # Alt+F2 (KRunner)
./bin/fastonedrive install-kio           # Local + protocolo fastonedrive://
./bin/fastonedrive dolphin-search        # kdialog → Dolphin na pasta de busca
./bin/fastonedrive status
./bin/fastonedrive log                   # últimas linhas de ~/.local/share/fast-onedrive/fastonedrive.log
./bin/fastonedrive settings              # pesquisa + definições (precisa python-pyqt6)
./bin/fastonedrive settings --tray       # só o ícone na bandeja (autostart)
./bin/fastonedrive hydrate onedrive:PATH # cache/ gravável (abrir no Local)
./bin/fastonedrive sync-pins             # 3 vias nos pins + upload de cache editado
./bin/fastonedrive fuse                  # FUSE ~/OneDrive (primeiro plano)
./bin/fastonedrive link PATH             # ligação de partilha
./bin/fastonedrive free-space PATH       # unpin ou apagar cache
./bin/fastonedrive about                 # quota OneDrive
./bin/fastonedrive put LOCAL DEST        # enviar para o OneDrive
./bin/fastonedrive mkdir DEST            # pasta no OneDrive
./bin/fastonedrive rm PATH               # apagar na nuvem (não é unpin)
./bin/fastonedrive mv SRC DEST           # mover/renomear no OneDrive
```

App **Fast-OneDrive** no menu: selector de perfil, separadores **Pesquisa**, **rclone** e **Fast-OneDrive**. **Novo perfil** abre o assistente (nome, pasta no PC, conta pessoal ou empresa, login Microsoft no browser) sem terminal. **Renomear** muda o nome visível e o título do Local; o endereço `fastonedrive:/p/<id>/` e a pasta no PC ficam. A primeira conta já configurada passa a ser o perfil **OneDrive** em `~/OneDrive` e no Local `fastonedrive:/p/onedrive/`. Cada perfil tem índice, pins, cache e FUSE próprios. O `install-menus` também instala o autostart: ícone na bandeja do Plasma (Abrir / Sair; tooltip de actividade); Fechar esconde a janela. Se o Plasma meter o ícone no overflow, fixá-lo em Configurar bandeja do sistema. Reabra o Dolphin depois de `install-menus` e `install-kio`. Recompile o worker KIO se actualizar emblemas.

```text
./bin/fastonedrive mount on    # FUSE próprio em ~/OneDrive (desliga rclone-onedrive)
./bin/fastonedrive mount off
./bin/fastonedrive mount status
```

KRunner: Alt+F2 → `comemora`. Mantido localmente abre `pinned/`. Não mantido: hidrata `cache/` (não o FUSE). > 50 MB no Alt+F2: só copiar/manter localmente. Se não aparecer: `kquitapp6 krunner`.

Local no Dolphin: `install-kio` (precisa `cmake`; `extra-cmake-modules` opcional). Barra de Locais **OneDrive** abre `fastonedrive:/` e lista pelo SQLite (o `lsjson` de um nível corre em segundo plano); o índice completo serve a busca. **Manter localmente** / **Deixar de manter** só mexem no disco (`pinned/`); o nome fica. Delete nessa pasta apaga na nuvem. Abrir um ficheiro não mantido **hidrata** `cache/` gravável; o `sync-pins` envia edições. Criar e colar no Local gravam no disco e enviam em segundo plano; pasta nova e F2 usam rclone; em `~/OneDrive` o atalho continua FUSE. Busca: `fastonedrive:/search/comemora` ou `dolphin-search`. Erros (CLI, menu Dolphin, KIO) vão para `~/.local/share/fast-onedrive/fastonedrive.log`; `fastonedrive log` mostra o fim.

Timer opcional: copiar `systemd/fast-onedrive-index.service` / `.timer` e `fast-onedrive-sync.service` / `.timer` para `~/.config/systemd/user/` e `systemctl --user enable --now fast-onedrive-index.timer fast-onedrive-sync.timer`. Sem o pacote, o `ExecStart` pode apontar a `%h/.local/bin/fastonedrive`.

Pacote Arch (esqueleto, não AUR): `packaging/arch/PKGBUILD` — ver [`docs/PACKAGING.md`](docs/PACKAGING.md).

## Nome

Pasta do repo: `Fast-OneDrive`. Pacote Python / CLI: `fastonedrive` (sem hífen). Pacote Arch: `fast-onedrive`. Unidade systemd: `fast-onedrive-index.service`.
