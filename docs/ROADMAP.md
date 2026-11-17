# Roadmap

Não avançar KIO sem índice + copy estáveis.

## Fase 0 — motor (primeiro chat de implementação)

- [x] Pacote Python `fastonedrive`
- [x] Subprocesso `rclone lsjson` / `rclone copy` (rcd 5573 opcional nesta fase)
- [x] `fastonedrive index` → `~/.local/share/fast-onedrive/index.db`
- [x] `fastonedrive search QUERY`
- [x] `fastonedrive copy onedrive:PATH DESTINO` multi-thread
- [x] Mapear `~/OneDrive/...` → `onedrive:...`
- [x] Timer systemd user opcional para reindex
- [x] Teste manual: search `COMEMORA` sem tocar no FUSE; copy de ficheiro pequeno para `/tmp`

**Feito:** os dois testes acima passam.

## Fase 1 — Dolphin

- [x] Service menus Copiar / Pin
- [x] Notificação progresso/erro
- [x] Destino por omissão configurável (`~/Downloads`)

**Feito:** botão direito num XCI em `~/OneDrive` usa ritmo de `rclone copy`, não ~1 MB/s.

## Fase 2 — Plasma Search

- [x] KRunner
- [x] Não abrir pelo FUSE ficheiros acima do limiar de tamanho

**Feito:** Alt+F2 encontra `comemora` no SQLite.

## Fase 3 — KIO + busca no Dolphin

O Local de busca **precisa** do worker; esta fase não é opcional.

- [x] Worker list + copy (`fastonedrive://`)
- [x] Pasta virtual de busca: query → SQLite → listagem na vista do Dolphin
- [x] Local na barra do Dolphin (“OneDrive”)
- [x] Arquivo > limiar: copy/pin, não abrir pelo FUSE

**Feito:** clicar o Local, buscar `comemora`, ver os hits no Dolphin sem tocar no FUSE.

## Fase 4

- [ ] Microsoft Graph Search (conteúdo Office)
- [ ] UI de skip_dir (índice; o skip de **pin** já está na telinha de definições)
- [x] Vários perfis (Local, FUSE e dados próprios; assistente de conta na janela)
- [x] Overlay no Local/KRunner (pin abre a cópia em `pinned/`; sem union FUSE)
- [x] Janela Fast-OneDrive (PyQt): Pesquisa + rclone + Fast-OneDrive
- [x] Bandeja do Plasma (`QSystemTrayIcon`): autostart, Fechar esconde, Abrir / Sair
- [x] Esqueleto do pacote Arch `fast-onedrive` (`packaging/arch/`)
- [x] On-demand no Local: hydrate → `cache/` gravável; `sync-pins` pins + cache dirty
- [x] Local lista o Drive todo; pin = só disco; KIO put/mkdir/del/rename
- [x] Listagem Places pelo SQLite (lsjson em segundo plano, TTL ~15s); índice recursivo em segundo plano com commits em lotes
- [x] Índice Graph `/delta` (incremental) + fallback `lsjson --onedrive-delta`; `itemId` + pastas partilhadas
- [x] rclone.conf próprio nas definições (isolado do mount / RClone Manager)
- [x] Flags rclone nas definições; mount Fast-OneDrive = FUSE próprio (sem RC 5573); `rclone-onedrive.service` desligado
- [x] Sync 3 vias (`baseModTime`/`baseEtag`), conflitos, dirty imediato (FUSE release / KIO put / inotify)
- [x] Emblemas sync/erro, KDirNotify, quota, `rclone link`, libertar espaço

**Feito (overlay KIO):** ficheiro fixado em `fastonedrive:/` e no KRunner usa `~/.local/share/fast-onedrive/pinned/`, não o FUSE.

**Feito (definições + pacote):** `fastonedrive settings` grava o toml; PKGBUILD ainda não publicado no AUR.

**Feito (on-demand):** o Local mostra o Drive todo; abrir hidrata cache gravável; pin só decide o que fica no disco; Delete no Local apaga a nuvem; unpin não. Não é Cloud Filter em `~/OneDrive`.

## Árvore sugerida (fase 0)

```text
src/fastonedrive/
  __init__.py
  config.py
  rclone.py
  index.py
  search.py
  copy.py
  paths.py      # ~/OneDrive ↔ onedrive:
  cli.py
systemd/fast-onedrive-index.service
systemd/fast-onedrive-index.timer
```
