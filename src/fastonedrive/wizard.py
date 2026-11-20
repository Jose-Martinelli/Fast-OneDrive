import threading
from pathlib import Path
from typing import Any

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.oauthsetup import OauthQuestion, OauthSession
from fastonedrive.profiles import allocateProfileId, defaultMountPath, mountPathTaken


def runProfileWizard(parent: Any) -> bool:
	from PyQt6.QtCore import QObject, pyqtSignal
	from PyQt6.QtWidgets import (
		QComboBox,
		QDialog,
		QDialogButtonBox,
		QFileDialog,
		QFormLayout,
		QHBoxLayout,
		QLabel,
		QLineEdit,
		QMessageBox,
		QPushButton,
		QVBoxLayout,
		QWidget,
	)

	class LoginBridge(QObject):
		finished = pyqtSignal(object, object)

	class ProfileWizard(QDialog):
		nameEdit: QLineEdit
		mountEdit: QLineEdit
		typeCombo: QComboBox
		driveCombo: QComboBox
		answerEdit: QLineEdit
		extraBox: QWidget
		statusLabel: QLabel
		loginButton: QPushButton
		continueButton: QPushButton
		bridge: LoginBridge
		session: OauthSession | None
		busy: bool
		created: bool
		cancelled: bool
		mountTouched: bool
		profileId: str
		question: OauthQuestion | None

		def __init__(self) -> None:
			super().__init__(parent)
			self.session = None
			self.busy = False
			self.created = False
			self.cancelled = False
			self.mountTouched = False
			self.profileId = ""
			self.question = None
			self.setWindowTitle("Novo perfil OneDrive")
			self.setMinimumWidth(520)
			self.bridge = LoginBridge(self)
			self.bridge.finished.connect(self._onLoginFinished)
			layout: QVBoxLayout = QVBoxLayout(self)
			intro: QLabel = QLabel(
				"Dá um nome ao perfil, escolhe a pasta neste PC e entra com a conta Microsoft."
			)
			intro.setWordWrap(True)
			layout.addWidget(intro)
			form: QFormLayout = QFormLayout()
			self.nameEdit = QLineEdit()
			self.nameEdit.setPlaceholderText("Trabalho")
			self.nameEdit.textChanged.connect(self._suggestMount)
			form.addRow("Nome:", self.nameEdit)
			mountRow: QWidget = QWidget()
			mountLayout: QHBoxLayout = QHBoxLayout(mountRow)
			mountLayout.setContentsMargins(0, 0, 0, 0)
			self.mountEdit = QLineEdit()
			self.mountEdit.textEdited.connect(self._touchMount)
			browse: QPushButton = QPushButton("Escolher…")
			browse.clicked.connect(self._pickMount)
			mountLayout.addWidget(self.mountEdit, 1)
			mountLayout.addWidget(browse)
			form.addRow("Pasta no PC:", mountRow)
			self.typeCombo = QComboBox()
			self.typeCombo.addItem("Pessoal", "personal")
			self.typeCombo.addItem("Empresa", "business")
			form.addRow("Tipo de conta:", self.typeCombo)
			self.clientIdEdit = QLineEdit()
			self.clientIdEdit.setPlaceholderText("opcional — app Azure própria")
			self.clientSecretEdit = QLineEdit()
			self.clientSecretEdit.setEchoMode(QLineEdit.EchoMode.Password)
			self.clientSecretEdit.setPlaceholderText("opcional")
			form.addRow("Client ID:", self.clientIdEdit)
			form.addRow("Client secret:", self.clientSecretEdit)
			layout.addLayout(form)
			self.driveCombo = QComboBox()
			self.answerEdit = QLineEdit()
			self.extraBox = QWidget()
			extraLayout: QVBoxLayout = QVBoxLayout(self.extraBox)
			extraLayout.setContentsMargins(0, 0, 0, 0)
			extraLayout.addWidget(self.driveCombo)
			extraLayout.addWidget(self.answerEdit)
			self.extraBox.setVisible(False)
			layout.addWidget(self.extraBox)
			self.statusLabel = QLabel("")
			self.statusLabel.setWordWrap(True)
			layout.addWidget(self.statusLabel)
			buttons: QDialogButtonBox = QDialogButtonBox()
			self.loginButton = QPushButton("Entrar com a Microsoft")
			self.continueButton = QPushButton("Continuar")
			self.continueButton.setVisible(False)
			cancel: QPushButton = QPushButton("Cancelar")
			self.loginButton.clicked.connect(self._startLogin)
			self.continueButton.clicked.connect(self._continueLogin)
			cancel.clicked.connect(self.reject)
			buttons.addButton(self.loginButton, QDialogButtonBox.ButtonRole.ActionRole)
			buttons.addButton(self.continueButton, QDialogButtonBox.ButtonRole.ActionRole)
			buttons.addButton(cancel, QDialogButtonBox.ButtonRole.RejectRole)
			layout.addWidget(buttons)

		def reject(self) -> None:
			self.cancelled = True
			if self.session is not None:
				self.session.cancel()
			super().reject()

		def _touchMount(self, _text: str) -> None:
			self.mountTouched = True

		def _suggestMount(self, text: str) -> None:
			if self.mountTouched:
				return
			self.mountEdit.setText(config.pathDisplay(defaultMountPath(text)))

		def _pickMount(self) -> None:
			chosen: str = QFileDialog.getExistingDirectory(
				self,
				"Pasta do perfil",
				str(config.homeDir()),
			)
			if chosen == "":
				return
			self.mountTouched = True
			self.mountEdit.setText(config.pathDisplay(Path(chosen)))

		def _startLogin(self) -> None:
			if self.busy:
				return
			name: str = self.nameEdit.text().strip()
			if name == "":
				QMessageBox.warning(self, "Fast-OneDrive", "Indica o nome do perfil.")
				return
			mountText: str = self.mountEdit.text().strip()
			if mountText == "":
				mountText = config.pathDisplay(defaultMountPath(name))
				self.mountEdit.setText(mountText)
			mount: Path = Path(mountText).expanduser()
			if mountPathTaken(mount):
				QMessageBox.warning(
					self,
					"Fast-OneDrive",
					"Essa pasta já pertence a outro perfil.",
				)
				return
			if not _rcloneInstalled():
				QMessageBox.warning(
					self,
					"Fast-OneDrive",
					"O rclone não está instalado. Instala o pacote rclone e tenta de novo.",
				)
				return
			self.profileId = allocateProfileId(name)
			driveType: str = str(self.typeCombo.currentData() or "personal")
			self.session = OauthSession(
				self.profileId,
				driveType,
				self.clientIdEdit.text(),
				self.clientSecretEdit.text(),
			)
			self.question = None
			self._setBusy(True, "A abrir o browser para entrares na Microsoft…")
			self._runAdvance(None)

		def _continueLogin(self) -> None:
			if self.created:
				self.accept()
				return
			if self.busy or self.session is None or self.question is None:
				return
			answer: str = ""
			if self.driveCombo.isVisible() and self.driveCombo.count() > 0:
				answer = str(self.driveCombo.currentData() or "")
			elif self.answerEdit.isVisible():
				answer = self.answerEdit.text().strip()
			if answer == "":
				QMessageBox.warning(self, "Fast-OneDrive", "Escolhe o drive.")
				return
			self._setBusy(True, "A concluir a conta…")
			self._runAdvance(answer)

		def _runAdvance(self, answer: str | None) -> None:
			session: OauthSession | None = self.session
			if session is None:
				return

			def work() -> None:
				question: OauthQuestion | None = None
				errorText: str | None = None
				try:
					question = session.advance(answer)
				except FastOneDriveError as error:
					errorText = str(error)
				except Exception as error:
					errorText = str(error)
				self.bridge.finished.emit(question, errorText)

			threading.Thread(target=work, daemon=True, name="fod-oauth").start()

		def _onLoginFinished(self, question: object, errorText: object) -> None:
			self.busy = False
			self.loginButton.setEnabled(True)
			if self.cancelled:
				self._rollbackNewProfile()
				return
			error: str | None = errorText if isinstance(errorText, str) else None
			if error is not None:
				self.statusLabel.setText(error)
				self.continueButton.setVisible(False)
				return
			if isinstance(question, OauthQuestion):
				self._showQuestion(question)
				return
			try:
				self._createProfile()
			except FastOneDriveError as failure:
				self.statusLabel.setText(str(failure))
				return
			except Exception as failure:
				self.statusLabel.setText(str(failure))
				return
			self.created = True
			self.statusLabel.setText(
				"Perfil criado. A indexação começou. Reabre o Dolphin para veres o Local."
			)
			self.loginButton.setVisible(False)
			self.continueButton.setText("Fechar")
			self.continueButton.setVisible(True)
			self.continueButton.setEnabled(True)

		def _showQuestion(self, question: OauthQuestion) -> None:
			self.question = question
			self.driveCombo.clear()
			for value, helpText in question.examples:
				self.driveCombo.addItem(helpText, value)
			hasExamples: bool = self.driveCombo.count() > 0
			self.driveCombo.setVisible(hasExamples)
			self.answerEdit.setVisible(not hasExamples)
			self.answerEdit.clear()
			self.extraBox.setVisible(True)
			text: str = question.helpText.strip() or "Escolhe uma opção para continuar."
			if question.error != "":
				text = f"{question.error}\n{text}"
			self.statusLabel.setText(text)
			self.continueButton.setVisible(True)
			self.loginButton.setEnabled(False)

		def _setBusy(self, busy: bool, status: str) -> None:
			self.busy = busy
			self.loginButton.setEnabled(not busy)
			self.continueButton.setEnabled(not busy)
			self.nameEdit.setEnabled(not busy)
			self.mountEdit.setEnabled(not busy)
			self.typeCombo.setEnabled(not busy)
			self.statusLabel.setText(status)

		def _createProfile(self) -> None:
			name: str = self.nameEdit.text().strip()
			mountText: str = self.mountEdit.text().strip()
			profile: config.Profile = config.Profile(
				self.profileId,
				name,
				self.profileId,
				mountText,
				True,
				False,
			)
			try:
				config.replaceProfile(profile)
				config.activateProfile(profile.id)
				_ensureIntegrations()
				from fastonedrive.kioinstall import upsertProfilePlace
				from fastonedrive.mount import enableProfileMount

				upsertProfilePlace(profile)
				enableProfileMount(profile)
				from fastonedrive.index import startBackgroundIndex

				startBackgroundIndex(True)
			except Exception:
				try:
					self._rollbackNewProfile()
				except Exception:
					pass
				raise

		def _rollbackNewProfile(self) -> None:
			if self.profileId == "" or self.created:
				return
			from fastonedrive.profiles import removeProfile, removeRemoteSection

			if config.profileById(self.profileId) is not None:
				try:
					removeProfile(self.profileId, True)
				except FastOneDriveError:
					pass
				if config.activeProfile() is not None and config.activeProfile().id == self.profileId:
					config.clearActiveProfile()
				return
			try:
				removeRemoteSection(self.profileId)
			except FastOneDriveError:
				pass

	dialog: ProfileWizard = ProfileWizard()
	dialog.exec()
	return dialog.created


def _rcloneInstalled() -> bool:
	try:
		from fastonedrive.rclone import rcloneBin

		return Path(rcloneBin()).is_file()
	except FastOneDriveError:
		return False


def _ensureIntegrations() -> None:
	from fastonedrive.kioinstall import installKio
	from fastonedrive.krunner import installRunner
	from fastonedrive.menus import autostartDesktopPath, installMenus, serviceMenuPath

	if Path("/usr/bin/fastonedrive").is_file() and not Path.home().joinpath(".local", "bin", "fastonedrive").is_file():
		return
	if not serviceMenuPath().is_file() or not autostartDesktopPath().is_file():
		installMenus()
	pluginRoot: Path = Path.home() / ".local" / "lib" / "qt6" / "plugins"
	hasPlugin: bool = pluginRoot.is_dir() and any(pluginRoot.glob("**/kio_fastonedrive.so"))
	if not hasPlugin:
		installKio()
	runner: Path = (
		Path.home() / ".local" / "share" / "dbus-1" / "services" / "org.fastonedrive.krunner.service"
	)
	if not runner.is_file():
		installRunner()
