# Contexto (histórico — não reabrir)

Tudo isto aconteceu na máquina do autor (josefm, CachyOS, Plasma) **antes** deste repo. O próximo chat não precisa “descobrir” de novo.

## Setup rclone já existente

- Pacote: rclone **1.76.0** (`/usr/bin/rclone`, CachyOS)
- Remote único: `onedrive:` (OneDrive **pessoal**, sem `client_id` próprio)
- Config: `~/.config/rclone/rclone.conf` (tokens; **nunca** commitar)
- Mount systemd user `rclone-onedrive.service` → `/home/josefm/OneDrive`
- Flags do mount: `--vfs-cache-mode full`, cache 150 G, `--dir-cache-time 9999h`, `--poll-interval 15s`, `--vfs-fast-fingerprint`, `--exclude "/Cofre Pessoal/**"`, `--rc --rc-no-auth --rc-addr=127.0.0.1:5572`
- Pós-start: `rclone rc vfs/refresh recursive=true _async=true`
- Drive: ~**1 TiB** total, ~**824 GiB** usados, ~**205 GiB** livres
- `Documentos` concentra ~**700 GiB** de backups
- Há pasta compartilhada `TRABALHO` **na raiz** (atalho em Meus arquivos — rclone já a lista)
- ROMs: `onedrive:Documentos/A Meus Documentos/Programas/Emuladores/roms/Switch/` (XCI ~3 GiB, etc.)

Estas flags aceleram **listagem** (`ls`/`stat` em memória). **Não** são um índice para o KFind. O KFind não consulta o dir-cache.

## O que falhou (não repetir)

1. **KFind** em `~/OneDrive` com padrão `*` / `COMEMORA`  
   Achou `COMEMORA - CESSÃO - AMANDA LANG.docx`, depois o KIO **abriu** o ficheiro. rclone VFS tomou **HTTP 401**, FUSE não devolveu o read. KFind em estado **D** (`folio_wait_bit_common`), SIGKILL inútil. Dolphin KIO no mesmo wait. Baloo indexa `$HOME/` (inclui o mount) e também falava FUSE.

2. **Cópia Dolphin** de XCI ~2,89 GiB (`…/roms/Switch/`) para `~/Downloads`  
   FUSE + `--vfs-cache-mode full` = download **single-stream** ~**1,3 MB/s** (~40 min) para o cache VFS, depois copy no mesmo NVMe. Parou ~543 MiB; destino não apareceu em Downloads. Sem 401 nesse XCI — só lento + ocioso.  
   **Causa:** não é o disco. É Dolphin a ler POSIX no mount. `rclone copy` (multi-thread) é o caminho certo.

3. **abrauengg** foi considerado e **rejeitado para este projeto**  
   Sem Files On-Demand: ou baixa a pasta ou ela não existe local. Blacklist (`skip_dir`) funciona, mas:
   - `Documentos` tem 700 GiB de backup — não se espelha
   - se `roms` fica no skip, o abraunegg **não** serve para baixar o XCI
   - o autor decidiu **não instalar** abraunegg  
   Só se reusa a **ideia** de SQLite + filtros + monitor.

4. **onedriver** — On-Demand em FUSE; autor do projeto avisa que ficheiro de vários GB é mau. Fora de âmbito.

5. **KFind + índice rclone** — não existe essa ligação. FSearch no **mount** pode indexar nomes (readdir/stat) se o dir-cache estiver quente, mas abrir resultado ainda é FUSE. Fast-OneDrive **não** depende do FSearch no FUSE; o índice é `lsjson` → SQLite.

6. **Cópia Dolphin de várias pastas em `fastonedrive:/…/roms/Switch`** — o job mostrou 1 122 ficheiros / 387,5 GiB (índice: ~41 ficheiros / ~144 GiB). O CopyJob faz `listRecursive`; o worker emitia `.` com `UDS_URL` da própria pasta. O ListJob usa `UDS_URL.fileName()` (ex. `Firmware`) para decidir se recua, não o `UDS_NAME` `.`, e relistava os 3 zips em ciclo (memória a crescer). Não voltar a emitir `.` no `listDir`.

## O que o autor já tem a correr (convive, não se substitui)

- Mount FUSE em `~/OneDrive` para **navegar**
- **RClone Manager** (AUR) no `rclone.conf` padrão (`onedrive:`). **Não** clicar Montar/Sincronizar/Bisync do Drive inteiro. Fast-OneDrive tem rclone.conf próprio e o mount `fast-onedrive-mount.service`. `rclone-onedrive.service` (5572) está desligado.
- RC **5572** = mount antigo. Qualquer `core/quit` nessa porta arriscava o FUSE. O mount Fast-OneDrive usa **5573**; jobs são subprocesso.

## O que o autor quer

Experiência **parecida** com OneDrive no Windows no dia a dia de **trabalho**:

- ver a estrutura (já tem: FUSE)
- copiar grande **rápido** (rclone copy / este add-on)
- pesquisar **nomes** no Drive inteiro **sem travar** (índice SQLite; KRunner de atalho; lista no Dolphin via Local + KIO)
- pastas de trabalho “sempre locais” (pin seletivo, não 824 GiB)
- **sem terminal** no uso quotidiano (CLI só para o MVP técnico)
- **sem** abraunegg

100% Windows (placeholders NTFS, Cloud Filter, Search da Microsoft no Explorer) **não é possível** no Linux. Este projeto é o 80% útil: Plasma + rclone API.

## Integração KDE — alvo certo

- **Sim:** Dolphin service menus, KRunner, KIO + **Local** no Dolphin (busca como pasta de resultados)
- **Não:** KFind, Ctrl+F no mount, FSearch no FUSE (não consomem o índice; abrir ainda é FUSE)

## Palavras-chave para o agente

`COMEMORA`, `TRABALHO`, `roms/Switch`, 401, estado D, 5572 vs 5573, 700 GiB Documentos, skip roms, vfs-cache-mode full, dir-cache ≠ índice de busca.
