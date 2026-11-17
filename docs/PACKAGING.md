# Pacote `fast-onedrive`

Nome Arch/CachyOS: **fast-onedrive**. CLI e módulo Python: **fastonedrive** (`pyproject.toml`).

Isto é o esqueleto. Ainda **não** está no AUR. Configuração do utilizador continua em `~/.config/fast-onedrive/config.toml` e `~/.config/fast-onedrive/rclone.conf` (fora do pacote). `rclone.conf` e tokens **nunca** entram no pacote. A porta **5572** é o mount FUSE — o pacote não a usa.

## Layout em `/usr`

```text
/usr/bin/fastonedrive
/usr/lib/python3.*/site-packages/fastonedrive/
/usr/lib/qt6/plugins/kf6/kio/kio_fastonedrive.so
/usr/lib/fast-onedrive/servicemenu.py
/usr/share/kio/servicemenus/fast-onedrive.desktop
/usr/share/applications/fast-onedrive.desktop
/etc/xdg/autostart/fast-onedrive.desktop
/usr/share/krunner/dbusplugins/plasma-runner-fastonedrive.desktop
/usr/share/dbus-1/services/org.fastonedrive.krunner.service
/usr/lib/systemd/user/fast-onedrive-index.service
/usr/lib/systemd/user/fast-onedrive-index.timer
/usr/lib/systemd/user/fast-onedrive-sync.service
/usr/lib/systemd/user/fast-onedrive-sync.timer
```

Depends: `rclone`, `python`, `python-pyqt6`, `python-dbus`, `python-gobject`, `kf6-kio`, `kdialog`.

## Construir neste repo

```text
cd packaging/arch
makepkg -s
# depois, se quiseres instalar: makepkg -si
```

O `PKGBUILD` lê a raiz do git (o path do workspace tem espaços; por isso `source=` fica vazio). Uma receita AUR deve apontar a um tarball ou URL git, não a este `_reporoot`.

Depois de instalar: `kbuildsycoca6`, reabrir Dolphin/KRunner, `fastonedrive settings`, `systemctl --user enable --now fast-onedrive-index.timer fast-onedrive-sync.timer`.

## Desenvolvimento (sem pacote)

Continua `./bin/fastonedrive install-menus` / `install-runner` / `install-kio` → `~/.local`. O wrapper `~/.local/bin/fastonedrive`, a telinha no menu Aplicativos e o autostart da bandeja (`~/.config/autostart/fast-onedrive.desktop`) vêm do `install-menus`.
