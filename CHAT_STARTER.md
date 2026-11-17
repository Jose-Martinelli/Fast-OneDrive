# Colar noutro chat Cursor (modo Agent)

```
Workspace obrigatório: /mnt/programacao/Repositorios e Tomcats/Fast-OneDrive

Lê README.md, AGENTS.md e todos os ficheiros em docs/ (CONTEXT, ENVIRONMENT, ARCHITECTURE, ROADMAP, DECISIONS) antes de escrever código.

Fast-OneDrive é um add-on KDE/Plasma que usa rclone como motor. Não forks o rclone. Não instales abraunegg nem onedriver. Não integres KFind nem o Ctrl+F do Dolphin no mount. Busca no Dolphin = Local + pasta KIO (fastonedrive:/search/…). Não uses o RC 127.0.0.1:5572 (mount antigo, desligado). O FUSE Fast-OneDrive é próprio (sem RC 5573). Jobs: subprocesso rclone.

Fases 0–3 feitas. Overlay KIO: pinado em fastonedrive:/ e KRunner abre pinned/, sem segundo FUSE. On-demand no Local: hydrate → cache/; sync-pins dois sentidos só pinned=1 (não é Cloud Filter em ~/OneDrive). App: `fastonedrive settings` (Pesquisa + rclone + Fast-OneDrive, rclone.conf próprio, flags vfs/exclude, mount ~/OneDrive). Bandeja do Plasma: autostart `settings --tray`; Fechar esconde; Abrir / Sair. Esqueleto Arch em packaging/arch (não AUR). Falta na fase 4: Graph Search, skip_dir do índice, multi-remote — só se o utilizador pedir.

Máquina: CachyOS, Plasma, remote onedrive:, mount ~/OneDrive. Detalhes em docs/ENVIRONMENT.md e docs/CONTEXT.md.

Não sincronizes o Drive inteiro. Não pinas roms nem backups (~700 GiB em Documentos). Não abras/copies ficheiros grandes pelo FUSE.

Pergunta só o que bloquear a tarefa pedida. O resto já está em DECISIONS.md.
```
