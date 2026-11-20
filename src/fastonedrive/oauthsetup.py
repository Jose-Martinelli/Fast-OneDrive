import json
import subprocess
import threading
from typing import Any

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.profiles import remoteSectionStatus, removeRemoteSection, writeRemoteToken
from fastonedrive.rclone import rcloneBin

_OAUTH_TIMEOUT_SEC: int = 360


class OauthQuestion:
	state: str
	name: str
	helpText: str
	examples: list[tuple[str, str]]
	error: str

	def __init__(
		self,
		state: str,
		name: str,
		helpText: str,
		examples: list[tuple[str, str]],
		error: str,
	) -> None:
		self.state = state
		self.name = name
		self.helpText = helpText
		self.examples = examples
		self.error = error


class OauthSession:
	remoteId: str
	driveType: str
	_process: subprocess.Popen[str] | None
	_pendingState: str

	def __init__(self, remoteId: str, driveType: str, clientId: str = "", clientSecret: str = "") -> None:
		self.remoteId = remoteId
		self.driveType = driveType
		# App Azure própria: evita os limites (429) da app partilhada do rclone.
		self.clientId = clientId.strip()
		self.clientSecret = clientSecret.strip()
		self._process = None
		self._pendingState = ""
		self._cancelled = False
		self._guard = threading.Lock()

	def cancel(self) -> None:
		with self._guard:
			self._cancelled = True
			process: subprocess.Popen[str] | None = self._process
		if process is None or process.poll() is not None:
			return
		process.kill()
		try:
			process.wait(timeout=5)
		except subprocess.TimeoutExpired:
			return

	def advance(self, answer: str | None) -> OauthQuestion | None:
		if self._cancelled:
			raise FastOneDriveError("login cancelado")
		if answer is None and self._pendingState == "":
			self._prepareRemote()
			self._storeAuthorizedToken()
			return self._consume(self._call(False, "", ""))
		if self._pendingState == "":
			raise FastOneDriveError("não há uma pergunta rclone pendente")
		state: str = self._pendingState
		self._pendingState = ""
		return self._consume(self._call(True, state, answer or ""))

	def _prepareRemote(self) -> None:
		status: dict[str, str] | None = remoteSectionStatus(self.remoteId)
		if status is None:
			return
		if status.get("hasToken") and status.get("drive_id") and status.get("drive_type"):
			if config.profileById(self.remoteId) is None:
				return
			raise FastOneDriveError("já existe uma conta rclone com este nome")
		removeRemoteSection(self.remoteId)

	def _consume(self, payload: dict[str, Any]) -> OauthQuestion | None:
		current: dict[str, Any] = payload
		tokenWrites: int = 0
		while True:
			state: str = str(current.get("State") or "")
			optionRaw: object = current.get("Option")
			option: dict[str, Any] = optionRaw if isinstance(optionRaw, dict) else {}
			error: str = str(current.get("Error") or "").strip()
			if state == "" and len(option) == 0:
				if error != "":
					raise FastOneDriveError(error)
				self._verifyRemote()
				return None
			name: str = str(option.get("Name") or "")
			if name == "config_token":
				if tokenWrites >= 2:
					raise FastOneDriveError("não foi possível gravar o token da conta")
				tokenWrites += 1
				self._storeAuthorizedToken()
				current = self._call(False, "", "")
				continue
			auto: str | None = self._autoAnswer(name, option)
			if auto is None:
				if error != "" and name == "":
					raise FastOneDriveError(error)
				examples: list[tuple[str, str]] = _examples(option)
				self._pendingState = state
				return OauthQuestion(
					state,
					name,
					str(option.get("Help") or ""),
					examples,
					error,
				)
			current = self._call(True, state, auto)

	def _storeAuthorizedToken(self) -> None:
		if self._cancelled:
			raise FastOneDriveError("login cancelado")
		token: str = self._authorizeOnedrive()
		if self._cancelled:
			raise FastOneDriveError("login cancelado")
		fields: dict[str, str] = {}
		if self.clientId != "":
			fields["client_id"] = self.clientId
			fields["client_secret"] = self.clientSecret
		writeRemoteToken(self.remoteId, token, fields)

	def _authorizeOnedrive(self) -> str:
		process: subprocess.Popen[str] = subprocess.Popen(
			[
				rcloneBin(),
				"authorize",
				"onedrive",
				*([self.clientId, self.clientSecret] if self.clientId != "" else []),
			],
			stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			encoding="utf-8",
		)
		self._beginProcess(process)
		stdout: str = ""
		stderr: str = ""
		try:
			out: str | None
			err: str | None
			out, err = process.communicate(timeout=_OAUTH_TIMEOUT_SEC)
			stdout = out or ""
			stderr = err or ""
		except subprocess.TimeoutExpired as error:
			process.kill()
			process.communicate()
			raise FastOneDriveError("o login excedeu o tempo") from error
		finally:
			self._endProcess(process)
		if self._cancelled:
			raise FastOneDriveError("login cancelado")
		if process.returncode != 0:
			detail: str = stderr.strip() or stdout.strip()
			last: str = detail.splitlines()[-1] if detail != "" else "o rclone authorize falhou"
			raise FastOneDriveError(last)
		token: str | None = _tokenJson(stdout) or _tokenJson(stderr)
		if token is None:
			raise FastOneDriveError("o rclone authorize não devolveu o token")
		return token

	def _beginProcess(self, process: subprocess.Popen[str]) -> None:
		with self._guard:
			if self._cancelled:
				process.kill()
				try:
					process.wait(timeout=5)
				except subprocess.TimeoutExpired:
					pass
				raise FastOneDriveError("login cancelado")
			self._process = process

	def _endProcess(self, process: subprocess.Popen[str]) -> None:
		with self._guard:
			if self._process is process:
				self._process = None

	def _autoAnswer(self, name: str, option: dict[str, Any]) -> str | None:
		if name == "config_refresh_token":
			return "false"
		if name == "config_is_local":
			return "true"
		if name == "config_type":
			return "onedrive"
		if name == "config_drive_ok":
			return "true"
		if name == "config_driveid":
			return _singleDrive(option, self.driveType)
		return None

	def _verifyRemote(self) -> None:
		status: dict[str, str] | None = remoteSectionStatus(self.remoteId)
		if status is None or not status.get("hasToken"):
			raise FastOneDriveError("o rclone não gravou o token da conta")
		if status.get("drive_id", "") == "" or status.get("drive_type", "") == "":
			raise FastOneDriveError("o rclone não gravou drive_id e drive_type")

	def _call(self, continued: bool, state: str, result: str) -> dict[str, Any]:
		if _resultIsToken(result):
			raise FastOneDriveError("o token não pode ir na linha de comandos")
		command: list[str] = [
			rcloneBin(),
			"config",
			"update",
			self.remoteId,
			"--non-interactive",
			"--config",
			str(config.rcloneConfigPath()),
		]
		if continued:
			command.extend(
				[
					"--continue",
					"--state",
					state,
					"--result",
					result,
				]
			)
		with config.rcloneConfigLock():
			return self._spawnConfigUpdate(command)

	def _spawnConfigUpdate(self, command: list[str]) -> dict[str, Any]:
		process: subprocess.Popen[str] = subprocess.Popen(
			command,
			stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			encoding="utf-8",
		)
		self._beginProcess(process)
		stdout: str = ""
		stderr: str = ""
		try:
			out: str | None
			err: str | None
			out, err = process.communicate(timeout=_OAUTH_TIMEOUT_SEC)
			stdout = out or ""
			stderr = err or ""
		except subprocess.TimeoutExpired as error:
			process.kill()
			process.communicate()
			raise FastOneDriveError("o login excedeu o tempo") from error
		finally:
			self._endProcess(process)
		if self._cancelled:
			raise FastOneDriveError("login cancelado")
		payload: dict[str, Any] | None = _parseJson(stdout)
		if payload is None:
			detail: str = stderr.strip() or stdout.strip() or f"rclone saiu com código {process.returncode}"
			raise FastOneDriveError(detail.splitlines()[-1])
		return payload


def _examples(option: dict[str, Any]) -> list[tuple[str, str]]:
	raw: object = option.get("Examples")
	if not isinstance(raw, list):
		return []
	found: list[tuple[str, str]] = []
	for item in raw:
		if not isinstance(item, dict):
			continue
		value: str = str(item.get("Value") or "").strip()
		if value == "":
			continue
		helpText: str = str(item.get("Help") or value).strip()
		found.append((value, helpText))
	return found


def _singleDrive(option: dict[str, Any], driveType: str) -> str | None:
	examples: list[tuple[str, str]] = _examples(option)
	if len(examples) == 0:
		return None
	if len(examples) == 1:
		return examples[0][0]
	matched: list[tuple[str, str]] = []
	suffix: str = f"({driveType})"
	for value, helpText in examples:
		if helpText.lower().endswith(suffix):
			matched.append((value, helpText))
	if len(matched) == 1:
		return matched[0][0]
	return None


def _tokenJson(text: str) -> str | None:
	start: int = text.find("{")
	while start >= 0:
		try:
			parsed: Any
			end: int
			parsed, end = json.JSONDecoder().raw_decode(text[start:])
		except json.JSONDecodeError:
			start = text.find("{", start + 1)
			continue
		if isinstance(parsed, dict) and str(parsed.get("access_token") or "") != "":
			return json.dumps(parsed, separators=(",", ":"))
		start = text.find("{", start + end)
	return None


def _resultIsToken(result: str) -> bool:
	text: str = result.strip()
	if text == "" or not text.startswith("{"):
		return False
	return "access_token" in text or "refresh_token" in text


def _parseJson(stdout: str) -> dict[str, Any] | None:
	start: int = stdout.find("{")
	if start < 0:
		return None
	try:
		parsed: Any
		parsed, _end = json.JSONDecoder().raw_decode(stdout[start:])
	except json.JSONDecodeError:
		return None
	if not isinstance(parsed, dict):
		return None
	return parsed
