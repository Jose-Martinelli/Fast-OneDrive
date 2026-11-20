# Ambiente e paths

## Dependências típicas

- SO: Linux com KDE Plasma (testado em Arch/CachyOS)
- `rclone` ≥ 1.68 em `/usr/bin/rclone`
- Python ≥ 3.11, PyQt6, dbus, gobject; `fuse2` + `fuse3`; SQLite ≥ 3.45

## rclone

- Config do sistema (opcional, ex. RClone Manager): `~/.config/rclone/rclone.conf`
- Config do Fast-OneDrive: `~/.config/fast-onedrive/rclone.conf` (um remote por perfil)
- Perfil migrado: remote `onedrive:` → pasta `~/OneDrive`
- Mount: FUSE próprio (`fastonedrive fuse` / `fast-onedrive-mount@<perfil>.service`, `Type=notify`, sem RC)
- Unidade antiga `rclone-onedrive.service` (RC 5572): desligada; o Fast-OneDrive não a usa
- Jobs: subprocesso rclone com `--config ~/.config/fast-onedrive/rclone.conf`

## Paths Fast-OneDrive

```text
DB:      ~/.local/share/fast-onedrive/profiles/<id>/index.db
Pin:     ~/.local/share/fast-onedrive/profiles/<id>/pinned/
Cache:   ~/.local/share/fast-onedrive/profiles/<id>/cache/
Config:  ~/.config/fast-onedrive/config.toml   ([[profiles]])
rclone:  ~/.config/fast-onedrive/rclone.conf
UI:      fastonedrive settings
Sync:    ~/.config/fast-onedrive/sync-control.json
Plugins: ~/.local/lib/qt6/plugins/kf6/{overlayicon,kfileitemaction}/
FUSE:    fast-onedrive-mount@<id>.service → pasta do perfil
Legado:  perfil onedrive em ~/OneDrive
Pacote:  packaging/arch/PKGBUILD  (ainda não AUR)
```

## Apps à volta (não são dependências)

- RClone Manager — pode coexistir; Fast-OneDrive usa a sua própria cópia do `rclone.conf`
- Baloo — o `install-kio` acrescenta a pasta de cada perfil a `excludeFolders`
- FSearch / KFind — fora de âmbito; a busca é o índice SQLite (KRunner / `fastonedrive:/search/…`)

## Variáveis de ambiente (opcionais)

O CLI **não** carrega `.env` automaticamente. Ver [`.env.example`](../.env.example) na raiz.

| Variável | Efeito |
|---|---|
| `FASTONEDRIVE_BIN` | Caminho do CLI para KIO/plugins (omissão: `~/.local/bin/fastonedrive`) |
| `FASTONEDRIVE_NO_PUSH=1` | Desliga avisos Graph em tempo real; só poll |
| `FASTONEDRIVE_QUIET=1` | Suprime `logInfo` (erros mantêm-se) |
| `FASTONEDRIVE_UI_TAB=rclone` | Abre Definições no separador rclone |
| `FUSE_LIBRARY_PATH` | Caminho alternativo da libfuse |
| `NOTIFY_SOCKET` | Socket systemd `Type=notify` (unidade do mount) |
| `QT_PLUGIN_PATH` | Pesquisa de plugins Qt/KF |

Internas (o processo define-as; não configurar à mão): `FASTONEDRIVE_INDEXING`, `FASTONEDRIVE_DELTA_ONLY`, `RCLONE_PASSWORD_COMMAND`, `RCLONE_ASK_PASSWORD`.

Segredos (tokens OAuth, `client_secret`, senha do `rclone.conf`) vivem em `~/.config/fast-onedrive/rclone.conf` e/ou na carteira do sistema — **nunca** em variáveis de ambiente de sessão nem no repositório.
