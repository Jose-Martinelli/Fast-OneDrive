# Ambiente do autor

Atualizar este ficheiro se o mount/portas mudarem.

## SO

- CachyOS (Arch), kernel recente, Plasma
- User: `josefm` (uid 1000), shell fish
- Um utilizador humano

## Discos (aprox.)

| Mount | Uso | Notas |
|---|---|---|
| `/home` (NVMe ~928 GiB) | ~512 GiB livres | `~/OneDrive`, `~/Downloads`, cache VFS |
| `/mnt/jogos` | ~114 GiB livres | destino preferido de ROMs se couber |
| `/mnt/programacao` | ~381 GiB livres | **este repo** e código |
| `/mnt/removivel` | USB | irrelevante para o MVP |

## rclone

- Binário: `/usr/bin/rclone` (1.76.0)
- Config do **mount**: `/home/josefm/.config/rclone/rclone.conf`
- Remote: `onedrive:` (`type=onedrive`, `drive_type=personal`)
- Mount: `/home/josefm/OneDrive` — FUSE **próprio** do Fast-OneDrive (`fastonedrive fuse`)
- Unidade antiga: `rclone-onedrive.service` (**desligada**, RC 5572)
- Unidade Fast-OneDrive: `~/.config/systemd/user/fast-onedrive-mount.service` (sem RC 5573)
- Jobs: subprocesso rclone com `--config ~/.config/fast-onedrive/rclone.conf`
- Sem `client_id` Azure próprio (app partilhada do rclone; risco de 429)

## Paths OneDrive úteis

```text
onedrive:Documentos/                    # ~700 GiB backups — não pin/sync
onedrive:TRABALHO/                      # partilhada, na raiz
onedrive:Área de Trabalho/
onedrive:Documentos/A Meus Documentos/Programas/Emuladores/roms/Switch/
```

## Fast-OneDrive

```text
DB:      ~/.local/share/fast-onedrive/profiles/<id>/index.db
Pin:     ~/.local/share/fast-onedrive/profiles/<id>/pinned/
Cache:   ~/.local/share/fast-onedrive/profiles/<id>/cache/
Config:  ~/.config/fast-onedrive/config.toml   ([[profiles]])
rclone:  ~/.config/fast-onedrive/rclone.conf  (um remote por perfil)
UI:      fastonedrive settings
FUSE:    fast-onedrive-mount@<id>.service → pasta do perfil (sem RC)
Legado:  perfil onedrive em ~/OneDrive
Pacote:  packaging/arch/PKGBUILD  (fast-onedrive; ainda não AUR)
```

## Apps à volta (não são dependências)

- RClone Manager — cópia GUI já a funcionar; Fast-OneDrive deve conviver
- FSearch — o autor chegou a apontar a `~/OneDrive`; este projeto **não** exige FSearch
- Baloo — indexa `$HOME/` incluindo o FUSE; **excluir** `~/OneDrive` no Plasma (não é código deste repo, mas o CONTEXT manda não martelar o mount)
