# Agente — Fast-OneDrive

## O que é

Add-on Plasma **em cima** do rclone. O rclone não se modifica. Integração = subprocessos `rclone` + FUSE próprio por perfil (índice SQLite + `pinned/` + `cache/` em `~/.local/share/fast-onedrive/profiles/<id>/`) + um Local `fastonedrive:/p/<id>/` no Dolphin. A conta já existente migra para o perfil `onedrive` em `~/OneDrive`.

## Regras

- Alterar só o necessário; inglês camelCase em código; UI em português
- Tipar funções (parâmetros e retorno)
- Worker KIO `fastonedrive://`: listDir = SQLite imediato se a pasta já está no índice; `lsjson --max-depth 1` em segundo plano (TTL ~15s) + KDirNotify se mudou; sem índice ainda, listDir espera o lsjson; índice recursivo em segundo plano só se o SQLite está vazio; search no índice; get com cópia local faz `UDS_LOCAL_PATH` + redirect `file://` (não stream); hydrate `cache/` gravável só em abertura explícita (não cat); thumbnail/preview sem cópia local não hidrata; pin = só disco (`pinned/` + `sync-pins`); Delete/mkdir/rename no KIO = rclone + índice (Delete apaga a nuvem; unpin não); put grava `pinned/` ou `cache/`, upsert `dirty` e devolve; copy dentro do Drive faz o mesmo se a origem está no disco, senão `copyto` remoto sem `cache.lock`; o upload é `sync-pins --path` da raiz noutro processo; `stat` negativo só com `listingCompleted` do pai (senão lista o pai uma vez); `dirty` sem `baseEtag` não faz `copyto` por cima de um remoto existente; cache dirty sobe no `sync-pins` (3 vias + inotify); menus pin/unpin/partilhar/libertar espaço só neste protocolo; sem rclone VFS/5572/5573
- Não integrar KFind nem o Ctrl+F do Dolphin no mount
- Não usar o RC `127.0.0.1:5572` (mount antigo). O FUSE Fast-OneDrive **não** usa RC 5573. Jobs: subprocesso rclone
- Cada perfil tem FUSE próprio (`fast-onedrive-mount@<id>.service`). `getattr` só SQLite/disco (nunca `lsjson`); `readdir` TTL ~15s; rename atómico de editores marca dirty sem `deletefile` remoto; não enviar `.goutputstream-*`, `.~lock.*`, `*.swp`, `*.tmp`; inotify `IN_CLOSE_WRITE`/`IN_MOVED_TO` em `pinned/` e `cache/` dispara `sync-pins --path`. O CLI infere o perfil por `fastonedrive:/p/<id>/…` ou pelo `mountPath`; `index` e `sync-pins` sem caminho percorrem todos os perfis, cada um no seu `try/except`
- Não fazer `rclone sync` do Drive inteiro
- Copy de ficheiro grande: `rclone copy` / RC `sync/copy` ou `operations/copyfile`, nunca ler pelo FUSE
- Índice: nomes/metadados. Ciclo completo: Graph `/delta` (fallback `lsjson --recursive --onedrive-delta`). Ciclos seguintes: deltaLink. Pastas partilhadas (ex. TRABALHO) têm delta próprio. Locais: `lsjson --max-depth 1` sem `--onedrive-delta`. Conteúdo de Office = Graph Search (fase 4)
- Filtros por omissão: não manter localmente `roms`, `Backups` (`skipPin`); sem teto de tamanho no pin
- Jobs rclone: `--config` no rclone.conf do Fast-OneDrive (`~/.config/fast-onedrive/rclone.conf`); nunca a porta 5572
- Não commitar `rclone.conf`, tokens, `.env`

## Stack (fases 0–3)

Python 3 + SQLite + systemd user unit. Service menus Dolphin (`.desktop`), KRunner D-Bus, worker KF6 KIO (`kio/`) + Local de busca no Dolphin.

## Docs

`README.md`, `docs/CONTEXT.md`, `docs/ENVIRONMENT.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, `docs/DECISIONS.md`.
