from pathlib import Path

from fastonedrive import config

XBEL: str = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xbel>
<xbel xmlns:bookmark="http://www.freedesktop.org/standards/desktop-bookmarks" xmlns:kdepriv="http://www.kde.org/kdepriv" xmlns:mime="http://www.freedesktop.org/standards/shared-mime-info">
 <bookmark href="file:///home/x">
  <title>Home</title>
 </bookmark>
 <bookmark href="fastonedrive:/p/onedrive/">
  <title>OneDrive</title>
  <info>
   <metadata owner="http://freedesktop.org"><bookmark:icon name="folder-cloud"/></metadata>
   <metadata owner="http://www.kde.org"><ID>FastOneDrivePlace-onedrive</ID><IsHidden>false</IsHidden></metadata>
  </info>
 </bookmark>
 <bookmark href="fastonedrive:/p/onedrive/item/TRABALHO/Relat%C3%B3rios">
  <title>Relatórios</title>
 </bookmark>
</xbel>
"""


def _setup(home: Path) -> Path:
	mount: Path = home / "OneDrive"
	mount.mkdir()
	config.replaceProfiles([config.Profile("onedrive", "OneDrive", "onedrive", str(mount), True, True)])
	places: Path = home / ".local" / "share" / "user-places.xbel"
	places.parent.mkdir(parents=True, exist_ok=True)
	places.write_text(XBEL, encoding="utf-8")
	(home / ".hidden").write_text("OneDrive\noutra\n", encoding="utf-8")
	return places


def test_um_so_lugar_na_pasta_fuse(home: Path) -> None:
	from fastonedrive.kioinstall import showFuseInDolphin

	places: Path = _setup(home)
	showFuseInDolphin()
	text: str = places.read_text(encoding="utf-8")
	mountUri: str = (home / "OneDrive").as_uri()
	assert f'href="{mountUri}"' in text
	assert 'href="fastonedrive:/p/onedrive/"' not in text
	assert f'href="{mountUri}/TRABALHO/Relat%C3%B3rios"' in text
	assert text.count("FastOneDrivePlace-onedrive") == 1
	assert (home / ".hidden").read_text(encoding="utf-8") == "outra\n"


def test_e_idempotente(home: Path) -> None:
	from fastonedrive.kioinstall import showFuseInDolphin

	places: Path = _setup(home)
	showFuseInDolphin()
	first: str = places.read_text(encoding="utf-8")
	showFuseInDolphin()
	assert places.read_text(encoding="utf-8") == first


def test_sem_mount_fica_no_protocolo(home: Path) -> None:
	from fastonedrive.kioinstall import placeHref

	profile = config.Profile("trabalho", "Trabalho", "trabalho", str(home / "Trabalho"), False, False)
	assert placeHref(profile) == "fastonedrive:/p/trabalho/"
