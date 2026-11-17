# Arquitetura

```
Dolphin / KRunner / CLI
        │
        ▼
fastonedrive (daemon + CLI)
        │  SQLite (índice + pins)
        ▼
rclone (subprocesso)
        │
        ▼
Microsoft Graph (OneDrive)
```

Cada perfil tem o seu FUSE (`fastonedrive fuse --profile <id>` / `fast-onedrive-mount@<id>.service`): metadados no SQLite desse perfil, bytes em `profiles/<id>/pinned/` e `cache/`. O perfil migrado monta `~/OneDrive`. `rclone-onedrive.service` (5572) fica desligado. Não há RC 5573. Busca e cópia de ficheiros grandes **não** lêem o FUSE: são subprocessos rclone.

Cada subprocesso rclone leva `--config` apontando ao rclone.conf do Fast-OneDrive (`~/.config/fast-onedrive/rclone.conf`) mais `--exclude` e flags extras do toml.

## Componentes

### 1. CLI `fastonedrive` (fase 0)

```text
fastonedrive index
fastonedrive search QUERY
fastonedrive search --json QUERY
fastonedrive copy REMOTE:PATH LOCALDIR
fastonedrive pin REMOTE:PATH
fastonedrive unpin REMOTE:PATH
fastonedrive install-kio
fastonedrive dolphin-search
fastonedrive status
fastonedrive log
fastonedrive settings
fastonedrive hydrate PATH
fastonedrive open-url fastonedrive:/item/PATH
fastonedrive sync-pins [--path PATH]
fastonedrive fuse
fastonedrive link PATH
fastonedrive free-space PATH
fastonedrive about
fastonedrive put LOCAL DEST
fastonedrive mkdir DEST
fastonedrive rm PATH
fastonedrive mv SRC DEST
```

Implementação: Python 3, pacote `fastonedrive`.

- `index`: Graph `drive/root/delta` (token do rclone.conf); se o `deltaLink` é válido só aplica mudanças (inclui apagados e moves por `itemId`). Sem token ou Graph em falha: `rclone lsjson --recursive --fast-list --onedrive-delta`. Pastas partilhadas (`remoteItem`, ex. `TRABALHO`) têm delta próprio. Upsert em lotes; `pinned` preservado.  
- `children`: SQLite imediato; `lsjson --max-depth 1` em segundo plano (TTL ~15s partilhado entre processos CLI, KDirNotify se mudou). Só bloqueia se a pasta ainda não está no índice. Não relança `index` em cada pasta.
- `search`: FTS/LIKE em `name` + `path`, case insensitive  
- `copy`: `rclone copy` com `--multi-thread-streams 8 --multi-thread-cutoff 64M`  
- `hydrate`: `rclone copyto` → `cache/` gravável (`chmod 644`); reutiliza só se tamanho e mtime coincidirem com o remoto; eviction 8 GiB usa `st_blocks * 512` (sparse) e salta cache dirty ou ficheiros com `flock`  
- `open` no FUSE: até `openMaxBytes`, hydrate completo; acima, sparse file e `rclone cat --offset --count` em blocos de 4 MiB (`file_chunks` no SQLite)  
- `put` sem `--kio-progress` / `mkdir` / `rm` / `mv`: rclone `copyto` / `mkdir` / `deletefile|purge` / `moveto` + upsert no SQLite. `put --kio-progress` grava em `pinned/` ou `cache/` e devolve; se o pai está pinado, o write-back fica em `pinned/`  
- `sync-pins`: 3 vias com `baseModTime`/`baseEtag`; cache dirty só upload; `--path` é a raiz (os `dirty` por baixo vêm do SQLite); nunca sync da raiz do Drive  
- `fuse`: `getattr` SQLite/disco; `readdir` TTL 15s; rename atómico sem `deletefile`; inotify em `pinned/`+`cache/`  
- Conversão: `file:///home/josefm/OneDrive/X` → `<remote>:X` (omissão `onedrive:`)
- Log: `~/.local/share/fast-onedrive/fastonedrive.log` (global; erros CLI/menu/KIO; `fastonedrive log`). Índice e pins: `profiles/<id>/`.

Jobs rclone são **sempre subprocesso**. O mount já não usa `rclone mount` nem RC.

### 2. SQLite (ideia abraunegg, código nosso)

```text
path TEXT PRIMARY KEY
name TEXT
parent TEXT
isDir INTEGER
size INTEGER
modTime TEXT
mimeType TEXT
etag TEXT
itemId TEXT
pinned INTEGER DEFAULT 0
updatedAt TEXT
baseModTime TEXT
baseEtag TEXT
syncState TEXT
syncError TEXT
delta_state (scope, driveId, itemId, deltaLink, prefix)
```

Índice FTS5 em `name` + `path` se couber; senão `LIKE` na fase 0.

### 3. Daemon / systemd (fase 0–1)

- Timer de reindex (ex. 6 em 6 h + manual)
- Timer de `sync-pins` (15 min): 3 vias nos pins + upload de cache editado
- inotify `IN_CLOSE_WRITE` / `IN_MOVED_TO` no processo FUSE sobre `pinned/` e `cache/`

### 4. Dolphin (fase 1)

`~/.local/share/kio/servicemenus/fast-onedrive.desktop`

O `Exec` chama `/usr/bin/python3` + `~/.local/share/fast-onedrive/servicemenu.py` (ELF do sistema; o helper não tem espaços no caminho). O helper dispara `~/.local/bin/fastonedrive` em sessão nova. Não usa `systemd-run` nem o script do repo como primeiro token — o KIO recusa executar scripts.

- Manter localmente / Deixar de manter / Partilhar ligação / Libertar espaço (só em `fastonedrive://`)
- Ctrl+C/V no Local usa o worker (rclone); não chamar `cp` no FUSE

### 5. KRunner (fase 2)

Query → SQLite → lista. Mantido: abre `pinned/`. Até `openMaxBytes`: hidrata `cache/` e abre. Acima, com o FUSE montado: abre `~/OneDrive/<path>` (leitura por blocos). Sem mount, pede para o activar. Pasta: Dolphin em `fastonedrive:/item/…`.

### 6. KIO + Local no Dolphin (fase 3)

Worker KF6 `fastonedrive://` (`kio/kio_fastonedrive.cpp`) chama o CLI Python; não lê `~/OneDrive` nem a porta 5572.

- **Listagem:** SQLite imediato. Sem entrada `.` — o CopyJob do Dolphin faz `listRecursive` e o `UDS_URL` de `.` seria a própria pasta (relistagem sem fim, memória a crescer). `lsjson --max-depth 1` em segundo plano (TTL ~15s partilhado entre processos CLI); o Dolphin actualiza via KDirNotify se a pasta mudou. Pastas reportam tamanho 0 (o size do Graph não é o total a copiar). Emblemas: a sincronizar / conflito / erro / mantido / em cache / só na nuvem.
- **Cópia para fora:** Ctrl+C/V e arrastar → `copy()` → rclone (ou bytes já em `pinned/`/`cache/` completo). Ficheiro até `openMaxBytes` sem cópia local: `hydrate` para `cache/` e depois cópia local. Ficheiro acima do limiar não entra no cache por este caminho.
- **Cópia para dentro / criar:** `put`/`copyFromFile` gravam já em `pinned/` (item ou pasta pai pinada) ou `cache/`, fazem upsert com `size`/`modTime` locais, `syncState=dirty` e o `baseEtag` anterior, notificam o Dolphin e devolvem. O envio é um `sync-pins --path` da raiz noutro processo (os filhos `dirty` saem do SQLite). `stat`/`exists` de um filho ausente só responde pelo SQLite se a listagem do pai está fechada (`listingCompleted`); se não está, lista o pai uma vez. Um `dirty` sem `baseEtag` que já existe na nuvem não faz `copyto`: QuickXor igual adopta a baseline; diferente gera conflito.
- **Cópia dentro do Drive:** se a origem já tem bytes íntegros em `pinned/` ou `cache/`, o destino segue o mesmo write-back. Se está só na nuvem, `rclone copyto` remoto, sem `cache.lock` durante o rclone.
- **Apagar / F2 / mkdir:** `del`/`rename`/`mkdir` → CLI rclone + SQLite.
- **Atributos:** `chmod` grava `unixMode` no SQLite e, se já existe cópia em `pinned/` ou `cache/`, aplica `os.chmod`. Não marca dirty e não chama rclone. Sem modo gravado, ficheiro só na nuvem continua `0444` e cópia local mostra o modo do disco. O OneDrive não guarda estes bits.
- **Abrir (`get`):** se já há bytes completos em `pinned/` ou `cache/`, `UDS_LOCAL_PATH` + `redirection(file://)`. Até `openMaxBytes`, `hydrate` → `cache/` e o mesmo redirect. Acima, com o FUSE montado, redirect para `~/OneDrive/<path>` (o `stat`/`listDir` não publicam esse caminho, para o CopyJob não ler a ROM pelo FUSE). Duplo clique que o KIO descarrega para `/tmp` recebe um symlink para o mesmo caminho. Sem mount, a abertura grande falha e pede o FUSE. `x-scheme-handler/fastonedrive` (`open-url`) abre cache, FUSE ou pasta. Preview/thumbnail **não** hidrata.
- **Busca:** Local “OneDrive” → `fastonedrive:/search/termo` (ou `dolphin-search`) → `listDir` no SQLite.
- O FUSE de cada perfil não aparece no Dolphin (Locais, Dispositivos, nem a pasta na Home). Cada perfil é um Local `fastonedrive:/p/<id>/`. `fastonedrive:/` abre o perfil migrado.
- O Ctrl+C/V em `~/OneDrive` e o Ctrl+F no mount **não** mudam (continuam FUSE / Baloo).

### 7. Definições

`fastonedrive settings` (PyQt). Processo de sessão: `QSystemTrayIcon` (StatusNotifierItem) na bandeja do Plasma; `settings --tray` arranca sem janela (autostart). Fechar / X esconde; Sair no ícone termina. Segunda invocação (menu Aplicativos) pede à instância a mostrar a janela (`QLocalServer` `fastonedrive-ui`). Não é plasmoid.

Separador **Pesquisa**: expressão → lista do SQLite; Abrir (até `openMaxBytes`, hydrate em segundo plano se não estiver no disco; acima, abre o FUSE se estiver montado; pastas no Local), Localizar no Dolphin, Copiar (área de transferência `fastonedrive:/` / Ctrl+V no Dolphin). Hidratar não espera por `rclone copy` para fora (ex. ROMs): cadeado `cache.lock`, não o `disk.lock` da cópia. Separador **rclone**: `rclone.conf`, remotes, exclude/flags de jobs, checkbox do FUSE `~/OneDrive`. Separador **Fast-OneDrive**: bloco **Indexação**; quota `rclone about`; `openMaxBytes`, `skipPin`. Ficheiro: `~/.config/fast-onedrive/config.toml`. Progresso do job: `~/.local/share/fast-onedrive/index-status.json`. Bandeja: tooltip de actividade de sync.

### 8. Pacote Arch

Nome `fast-onedrive` → `/usr` (CLI, KIO, menus, KRunner, unidades systemd user). Esqueleto: `packaging/arch/PKGBUILD`. Desenvolvimento continua em `~/.local`. Ver `docs/PACKAGING.md`.

## Fluxos

**Busca COMEMORA** — `search` no SQLite; zero FUSE. Atalho: KRunner. Lista cheia: Local no Dolphin.

**XCI 3 GiB** — no Local, Ctrl+C → Downloads usa `rclone copy`. Abrir, com o FUSE montado, segue `~/OneDrive/…` e só descarrega os blocos de 4 MiB lidos.

**Manter TRABALHO localmente** — copy para `pinned/TRABALHO/` + `pinned=1` na pasta e nos descendentes elegíveis (`skipPin` excluído no rclone). O Local mostra o Drive todo; pinados têm emblema “Mantido neste PC”. `sync-pins` é 3 vias (baseline); conflito gera cópia `nome (conflito local …)`. Deixar de manter tira a cópia local e **não** apaga o OneDrive. Delete no Local apaga a nuvem.

**Abrir no Local** — até `openMaxBytes`, se não está mantido, `hydrate` para `cache/` gravável. Acima disso a abertura usa o sparse file no FUSE; a listagem só recebe `UDS_LOCAL_PATH` quando a cópia está completa. Edições no cache/pinned sobem de imediato (FUSE `release`, KIO `put`, ou inotify). Num sparse parcial, o `release` preenche os buracos antes do upload. PreviewJob sem cópia local não dispara `hydrate`.

## Fora

- Fork rclone, abraunegg, onedriver
- Sync dos 824 GiB
- Graph Search no MVP
- RC 5572 / `rclone-onedrive.service`
- Overlay filesystem (union/mergerfs / segundo FUSE)
- KFind, Ctrl+F no mount, FSearch apontado ao FUSE
- Plugin no chrome do Dolphin (toolbar / search box nativo)
