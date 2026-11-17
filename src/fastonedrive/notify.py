import shutil
import subprocess
from collections.abc import Callable
from typing import TypeVar
from urllib.parse import quote

from fastonedrive import FastOneDriveError

T = TypeVar("T")
_QURL_PATH_ENCODE: frozenset[str] = frozenset("\"#%<>?[\\]^`{|}")


def notify(title: str, body: str, replaceId: int | None = None) -> int | None:
	binary: str | None = shutil.which("notify-send")
	if binary is None:
		return replaceId
	command: list[str] = [
		binary,
		"--app-name=Fast-OneDrive",
		"--icon=cloud-download",
		"--hint=string:desktop-entry:fastonedrive",
		"--expire-time=8000",
	]
	if replaceId is not None:
		command.append(f"--replace-id={replaceId}")
	command.extend(["--print-id", title, body])
	result: subprocess.CompletedProcess[str] = subprocess.run(
		command,
		check=False,
		capture_output=True,
		text=True,
		encoding="utf-8",
	)
	if result.returncode != 0:
		fallback: list[str] = [
			binary,
			"--app-name=Fast-OneDrive",
			title,
			body,
		]
		subprocess.run(fallback, check=False, capture_output=True)
		return replaceId
	text: str = result.stdout.strip()
	if text.isdigit():
		return int(text)
	return replaceId


class NotifyJob:
	title: str
	replaceId: int | None

	def __init__(self, title: str, startBody: str) -> None:
		self.title = title
		self.replaceId = notify(title, startBody)

	def progress(self, body: str) -> None:
		self.replaceId = notify(self.title, body, self.replaceId)

	def statsCallback(self) -> Callable[[str], None]:
		def onStats(line: str) -> None:
			text: str = line.replace("\r", " ").strip()
			if text == "":
				return
			self.progress(text[-240:])

		return onStats

	def success(self, body: str) -> None:
		self.replaceId = notify(self.title, body, self.replaceId)

	def error(self, body: str) -> None:
		self.replaceId = notify(self.title, body, self.replaceId)


def notifyDirChanged(itemPath: str) -> None:
	from fastonedrive import config

	urls: list[str] = _itemUrls(itemPath, _kioRoot())
	profile: config.Profile | None = config.activeProfile()
	if profile is not None and profile.legacy:
		urls.extend(_itemUrls(itemPath, config.KIO_SCHEME + ":"))
	_emitKdirNotify(urls)


def _itemUrls(itemPath: str, root: str) -> list[str]:
	urls: list[str] = []
	if itemPath == "":
		urls.append(f"{root}/")
		return urls
	urls.append(f"{root}/item/{_kioItemPath(itemPath)}")
	if "/" in itemPath:
		parent: str = itemPath.rsplit("/", 1)[0]
		urls.append(f"{root}/item/{_kioItemPath(parent)}")
	else:
		urls.append(f"{root}/")
	return urls


def _kioItemPath(itemPath: str) -> str:
	chars: list[str] = []
	char: str
	for char in itemPath:
		if char in _QURL_PATH_ENCODE:
			chars.append(quote(char, safe=""))
			continue
		chars.append(char)
	return "".join(chars)


def _kioRoot() -> str:
	from fastonedrive import config

	profile: config.Profile | None = config.activeProfile()
	if profile is None:
		return config.KIO_SCHEME + ":"
	return f"{config.KIO_SCHEME}:/p/{profile.id}"


def _emitKdirNotify(urls: list[str]) -> None:
	if len(urls) == 0:
		return
	qdbus: str | None = shutil.which("qdbus") or shutil.which("qdbus6")
	if qdbus is not None:
		command: list[str] = [
			qdbus,
			"org.kde.KDirNotify",
			"/org/kde/KDirNotify",
			"org.kde.KDirNotify.FilesChanged",
			*urls,
		]
		subprocess.run(command, check=False, capture_output=True)
		return
	dbus: str | None = shutil.which("dbus-send")
	if dbus is None:
		return
	encoded: list[str] = [url.replace(",", "%2C") for url in urls]
	args: list[str] = [
		dbus,
		"--session",
		"--type=signal",
		"/org/kde/KDirNotify",
		"org.kde.KDirNotify.FilesChanged",
		"array:string:" + ",".join(encoded),
	]
	subprocess.run(args, check=False, capture_output=True)


def runNotified(title: str, startBody: str, action: Callable[[NotifyJob], T]) -> T:
	job: NotifyJob = NotifyJob(title, startBody)
	try:
		result: T = action(job)
	except FastOneDriveError as error:
		job.error(str(error))
		raise
	return result
