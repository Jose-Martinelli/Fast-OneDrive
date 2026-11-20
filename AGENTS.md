# Agente — Fast-OneDrive

## O que é

Integração OneDrive ↔ KDE com um só lugar no explorador (`~/OneDrive`, decisão 27), **em cima** do rclone. O rclone não se modifica. Integração = subprocessos `rclone` + FUSE próprio por perfil (índice SQLite + `pinned/` + `cache/` em `~/.local/share/fast-onedrive/profiles/<id>/`) + um Local `fastonedrive:/p/<id>/` no Dolphin. A conta já existente migra para o perfil `onedrive` em `~/OneDrive`.

## Regras

- Alterar só o necessário; inglês camelCase em código; UI em português
- Tipar funções (parâmetros e retorno)
- Worker KIO `fastonedrive://`: listDir = SQLite imediato se a pasta já está no índice; `lsjson --max-depth 1` em segundo plano (TTL ~15s) + KDirNotify se mudou; sem índice ainda, listDir espera o lsjson; índice recursivo em segundo plano só se o SQLite está vazio; search no índice; get com cópia local faz `UDS_LOCAL_PATH` + redirect `file://` (não stream); hydrate `cache/` gravável só em abertura explícita (não cat); thumbnail/preview sem cópia local não hidrata; pin = só disco (`pinned/` + `sync-pins`); Delete/mkdir/rename no KIO = rclone + índice (Delete apaga a nuvem; unpin não); put grava `pinned/` ou `cache/`, upsert `dirty` e devolve; copy dentro do Drive faz o mesmo se a origem está no disco, senão `copyto` remoto sem `cache.lock`; o upload é `sync-pins --path` da raiz noutro processo; `stat` negativo só com `listingCompleted` do pai (senão lista o pai uma vez); `dirty` sem `baseEtag` não faz `copyto` por cima de um remoto existente; cache dirty sobe no `sync-pins` (3 vias + inotify); menus pin/unpin/partilhar/libertar espaço só neste protocolo; sem rclone VFS/5572/5573
- Um só lugar: a pasta FUSE do perfil é o Local no Dolphin (não voltar a escondê-la); emblemas pelo plugin `dolphin-plugin/` + xattr `user.fastonedrive.state` (não listado em `listxattr`); sinal D-Bus `org.fastonedrive.State.Changed(as)`
- Leitura de blocos a pedido: Graph `Range` (`rangeread.py`) com fallback `rclone cat`; pré-leitura em `readahead.py`; gravar bits do bitmap só via `_markChunks`
- Poll do delta no FUSE (`remotewatch.py`); pausa/banda/rede limitada em `synccontrol.py` (`sync-control.json`)
- Testes em `tests/` (`python -m pytest`) devem passar antes de entregar; achados de auditoria viram teste de regressão
- Não integrar KFind; o Baloo exclui a pasta (`install-kio`)
- Não usar o RC `127.0.0.1:5572` (mount antigo). O FUSE Fast-OneDrive **não** usa RC 5573. Jobs (cópias, uploads, pins): subprocesso rclone; leituras de blocos e pedidos de item (webUrl, versões) vão directos ao Graph com o token do rclone.conf
- Cada perfil tem FUSE próprio (`fast-onedrive-mount@<id>.service`). `getattr` só SQLite/disco (nunca `lsjson`); `readdir` TTL ~15s; `mkdir` no FUSE cria disco+índice (dirty) e agenda `rclone mkdir` em segundo plano (se a pasta já não existe depois, `rmdirRemote`; falha remota = `syncState` error; em pausa não sobe: entra no lote do `debounceSync`); `chmod` no FUSE não chama `notifyDirChanged` (bloqueava o create no `busctl`); os sinais D-Bus/KDirNotify do FUSE saem async (`useAsyncSignals`) e `_invalidate` emite `FilesAdded` no pai; rename atómico de editores marca dirty sem `deletefile` remoto; não enviar `.goutputstream-*`, `.~lock.*`, `*.swp`, `*.tmp`, `*.part` (Dolphin/KIO; `*.part` é efémero mas não é staging: `isStagingRel` só `.partial` / `.fod-*`); upload saltado por write guard de editor reagenda em 2 s; lote adiado pela pausa fica na fila e reagenda (`pauseRetryDelay`, ≤ 5 min), nunca é deitado fora; o debounce espera no máx. 3 s; `_invalidate` do FUSE só emite sinais (não limpa o TTL da listagem: cada criar lançava `refresh-parent`); `markDirty` uma vez por handle e `setSyncState` sem mudança não faz commit nem busctl; linha de ficheiro local nunca enviado sem cópia local = órfã (escondida no `getattr`/`readdir`, apagada no `sync-pins`); o plugin de emblemas não guarda xattr vazio em cache; inotify `IN_CLOSE_WRITE`/`IN_MOVED_TO` em `pinned/` e `cache/` dispara `sync-pins --path`. O CLI infere o perfil por `fastonedrive:/p/<id>/…` ou pelo `mountPath`; `index` e `sync-pins` sem caminho percorrem todos os perfis, cada um no seu `try/except`
- Envio (`_uploadLocal`): o write guard exclusivo só dura a cópia para `upload-snapshots/` (reflink, ou cópia até 64 MiB); o envio vai do instantâneo e só adopta a base se o ficheiro não mudou (inode/tamanho/mtime), senão a base passa a ser a versão enviada e continua dirty. Ficheiro ≤ 4 MiB sem limite de banda: PUT Graph `/content` + PATCH do mtime (`uploadSmallFile`), fallback `rclone copyto`; stats do envio pelo Graph (`quickStat`). Abrir cópia em cache não pergunta à nuvem se o poll está são e `etag == baseEtag`. Operação FUSE ≥ 250 ms fica no log como `fuse lento`. `unlink` no FUSE: ficheiro nunca enviado = só disco+índice; com `itemId` = DELETE Graph e depois disco+índice; senão (a enviar, Graph falhou) `removeRemotePath`. `open` só verifica cópia velha (`cleanCacheIsStale`) de itens já na nuvem
- Mutações no FUSE vão pelo Graph (rename/move = PATCH, rmdir = GET children + DELETE, mkdir = POST children, unlink = DELETE); rclone só como fallback. Sem rede (`GraphUnreachable`, e 15 s seguintes de falha rápida): apagar/renomear/rmdir aplicam disco+índice e entram em `pendingops` (tabela `pending_ops`, URL por id); mkdir e ficheiros ficam `dirty` e tentam de 60 em 60 s; o `RemoteWatcher` envia a fila antes do delta e não lê o delta com fila < 10 min
- Avisos em tempo real: `pushwatch.py` (socket.io do Graph, WebSocket em stdlib) só acorda o poll (`pollNow`); qualquer falha cai no poll. `FASTONEDRIVE_NO_PUSH=1` desliga
- Busca: FTS5 só com `name`; termo acerta no nome do item ou de uma pasta de cima (intervalo de path). `fastonedrive compact` = VACUUM (desliga o mount durante)
- `rclone.conf` cifrado opcional (`config-encryption on|off|status`, rclone ≥ 1.68): senha na carteira (`secret-tool`/`kwallet-query`), `RCLONE_PASSWORD_COMMAND=fastonedrive rclone-password`; ler o conf só por `secretconf.readConfigText`, editar o texto só dentro de `plainConfigForEdit`
- Listagem em segundo plano em `listing.py` (re-exportada por `index.py`)
- Tokens de conteúdo sempre canónicos (`canonicalToken`: QuickXor em base64, SHA1 minúsculas) ao criar; comparar só com `sameToken`/`tokensDiffer`, nunca `==` entre tokens de origens diferentes. O UPSERT do índice só marca `conflict` em `dirty` (não em `syncing`). Substituir um ficheiro sem base: se a nuvem ainda é o `etag` do índice, sobe (`_replacedKnownVersion`)
- Não fazer `rclone sync` do Drive inteiro
- Copy de ficheiro grande: `rclone copy` / RC `sync/copy` ou `operations/copyfile`, nunca ler pelo FUSE
- Índice: nomes/metadados. Ciclo completo: Graph `/delta` (fallback `lsjson --recursive --onedrive-delta`). Ciclos seguintes: deltaLink. Pastas partilhadas (ex. TRABALHO) têm delta próprio. Locais: `lsjson --max-depth 1` sem `--onedrive-delta`. Conteúdo de Office = Graph Search (fase 4)
- Filtros por omissão: não manter localmente `roms`, `Backups` (`skipPin`); sem teto de tamanho no pin
- Jobs rclone: `--config` no rclone.conf do Fast-OneDrive (`~/.config/fast-onedrive/rclone.conf`); nunca a porta 5572
- Não commitar `rclone.conf`, tokens, `.env`

## Stack (fases 0–3)

Python 3 + SQLite + systemd user unit. Service menus Dolphin (`.desktop`), KRunner D-Bus, worker KF6 KIO (`kio/`) + Local de busca no Dolphin.

## Docs

`README.md`, `docs/ENVIRONMENT.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `docs/PACKAGING.md`.
