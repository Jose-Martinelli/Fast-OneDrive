import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from fastonedrive import FastOneDriveError
from fastonedrive import config
from fastonedrive.hydrate import hydrateRemote
from fastonedrive.index import indexProgress, startBackgroundIndex
from fastonedrive.mount import applyMount, fastMountActive, fuseMountReady, legacyMountActive
from fastonedrive.activity import readActivity, readHistory
from fastonedrive.rclone import listRemoteNames, probeRemote, rcloneVersion, remoteAbout
from fastonedrive.search import rowToDict, search

_UI_SOCKET_NAME: str = "fastonedrive-ui"


def _loadAppIcon() -> Any:
	from PyQt6.QtGui import QIcon

	from fastonedrive.menus import APP_ICON_NAME, appIconPath

	themed: QIcon = QIcon.fromTheme(APP_ICON_NAME)
	if not themed.isNull():
		return themed
	path: Path | None = appIconPath()
	if path is not None:
		return QIcon(str(path))
	return QIcon.fromTheme("folder-cloud")


def runSettingsWindow(startHidden: bool = False) -> int:
	try:
		from PyQt6.QtCore import QMimeData, QObject, Qt, QTimer, QUrl, pyqtSignal
		from PyQt6.QtGui import QCloseEvent, QColor, QHideEvent, QIcon, QPalette, QShowEvent
		from PyQt6.QtNetwork import QLocalServer, QLocalSocket
		from PyQt6.QtWidgets import (
			QApplication,
			QCheckBox,
			QComboBox,
			QDialog,
			QDialogButtonBox,
			QFileDialog,
			QFormLayout,
			QFrame,
			QGridLayout,
			QGroupBox,
			QHBoxLayout,
			QHeaderView,
			QInputDialog,
			QLabel,
			QLayout,
			QLineEdit,
			QListWidget,
			QListWidgetItem,
			QMessageBox,
			QPlainTextEdit,
			QProgressBar,
			QPushButton,
			QScrollArea,
			QSizePolicy,
			QSpinBox,
			QStyle,
			QTabWidget,
			QToolButton,
			QTreeWidget,
			QTreeWidgetItem,
			QVBoxLayout,
			QWidget,
		)
	except ImportError as error:
		raise FastOneDriveError(
			"PyQt6 não encontrado (pacman -S python-pyqt6)"
		) from error

	class QuotaBridge(QObject):
		finished = pyqtSignal(object, object, str)

	class OpenBridge(QObject):
		finished = pyqtSignal(object, object)

	def _themedIcon(name: str) -> QIcon:
		return QIcon.fromTheme(name)

	def _appIcon() -> QIcon:
		return cast(QIcon, _loadAppIcon())

	def _actionButton(text: str, iconName: str, slot: Callable[[], None]) -> QPushButton:
		button: QPushButton = QPushButton(text)
		icon: QIcon = _themedIcon(iconName)
		if not icon.isNull():
			button.setIcon(icon)
		button.setAutoDefault(False)
		button.clicked.connect(slot)
		return button

	def _cacheLineEdit(placeholder: str) -> QLineEdit:
		edit: QLineEdit = QLineEdit()
		edit.setPlaceholderText(placeholder)
		edit.setFixedWidth(120)
		return edit

	def _styleForm(form: QFormLayout) -> None:
		form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
		form.setLabelAlignment(
			Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
		)
		form.setHorizontalSpacing(12)
		form.setVerticalSpacing(8)

	class SettingsDialog(QDialog):
		rcloneConfigEdit: QLineEdit
		remoteCombo: QComboBox
		mountCheck: QCheckBox
		vfsModeCombo: QComboBox
		vfsCacheMaxSizeEdit: QLineEdit
		vfsCacheMaxAgeEdit: QLineEdit
		vfsWriteBackEdit: QLineEdit
		dirCacheTimeEdit: QLineEdit
		pollIntervalEdit: QLineEdit
		vfsFastCheck: QCheckBox
		excludeEdit: QLineEdit
		extraEdit: QLineEdit
		openSpin: QSpinBox
		skipList: QListWidget
		skipText: QPlainTextEdit
		skipAddButton: QPushButton
		skipRemoveButton: QPushButton
		skipTextButton: QPushButton
		skipTextMode: bool
		queryEdit: QLineEdit
		results: QTreeWidget
		openButton: QPushButton
		locateButton: QPushButton
		copyButton: QPushButton
		searchStatus: QLabel
		openBusy: bool
		openBridge: OpenBridge
		hideOnClose: bool
		indexStateLabel: QLabel
		indexRootLabel: QLabel
		indexCountedLabel: QLabel
		indexModeLabel: QLabel
		indexSqliteLabel: QLabel
		indexStartedLabel: QLabel
		indexUpdatedLabel: QLabel
		indexFinishedLabel: QLabel
		indexErrorLabel: QLabel
		indexBusy: QProgressBar
		indexStartButton: QPushButton
		indexTimer: QTimer
		indexStarting: bool
		quotaBar: QProgressBar
		quotaLabel: QLabel
		quotaBusy: bool
		quotaAt: float
		quotaPayload: dict[str, Any] | None
		quotaBridge: QuotaBridge
		rcloneStatusLabel: QLabel
		profileAddButton: QPushButton
		profileRenameButton: QPushButton
		profileRemoveButton: QPushButton
		fastStatusLabels: list[QLabel]

		def __init__(self) -> None:
			super().__init__()
			self.hideOnClose = False
			self.indexStarting = False
			self.quotaBusy = False
			self.quotaAt = 0.0
			self.quotaPayload = None
			self.quotaBridge = QuotaBridge(self)
			self.quotaBridge.finished.connect(self._onQuotaFinished)
			self.rcloneStatusLabel = QLabel()
			self.rcloneStatusLabel.setWordWrap(True)
			self.skipTextMode = False
			self.openBusy = False
			self.fastStatusLabels = []
			self.openBridge = OpenBridge(self)
			self.openBridge.finished.connect(self._onOpenFinished)
			self.setWindowTitle("Fast-OneDrive")
			self.setWindowIcon(_appIcon())
			self.setMinimumSize(720, 540)
			self.resize(820, 700)
			self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
			config.ensureUserConfig()
			root: QVBoxLayout = QVBoxLayout(self)
			root.setContentsMargins(12, 12, 12, 12)
			root.addWidget(self._profileBar())
			tabs: QTabWidget = QTabWidget()
			tabs.addTab(self._searchPage(), "Pesquisa")
			tabs.addTab(self._rclonePage(), "rclone")
			tabs.addTab(self._fastPage(), "Fast-OneDrive")
			if os.environ.get("FASTONEDRIVE_UI_TAB") == "rclone":
				tabs.setCurrentIndex(1)
			root.addWidget(tabs)
			closeBox: QDialogButtonBox = QDialogButtonBox(
				QDialogButtonBox.StandardButton.Close
			)
			closeButton: QPushButton | None = closeBox.button(
				QDialogButtonBox.StandardButton.Close
			)
			if closeButton is not None:
				closeButton.setText("Fechar")
			closeBox.rejected.connect(self.close)
			root.addWidget(closeBox)
			self._setResultActionsEnabled(False)
			self._reloadProfileCombo()
			self._loadValues()
			self.profileCombo.currentIndexChanged.connect(self._onProfileChanged)
			self.indexTimer = QTimer(self)
			self.indexTimer.setInterval(2000)
			self.indexTimer.timeout.connect(self._refreshIndexStatus)
			self._refreshIndexStatus()

		def closeEvent(self, event: QCloseEvent | None) -> None:
			if self.hideOnClose:
				if event is not None:
					event.ignore()
				self.hide()
				return
			if event is not None:
				event.accept()
			app: QApplication | None = QApplication.instance()
			if isinstance(app, QApplication):
				app.quit()

		def showEvent(self, event: QShowEvent | None) -> None:
			super().showEvent(event)
			self._refreshIndexStatus()
			QTimer.singleShot(0, self._refreshQuota)
			self.indexTimer.start()

		def hideEvent(self, event: QHideEvent | None) -> None:
			self.indexTimer.stop()
			super().hideEvent(event)

		profileCombo: QComboBox

		def _profileBar(self) -> QWidget:
			bar: QWidget = QWidget()
			layout: QHBoxLayout = QHBoxLayout(bar)
			layout.setContentsMargins(0, 0, 0, 0)
			layout.addWidget(QLabel("Perfil:"))
			self.profileCombo = QComboBox()
			self.profileCombo.setMinimumWidth(220)
			self.profileAddButton = QPushButton("Novo perfil…")
			self.profileRenameButton = QPushButton("Renomear…")
			self.profileRemoveButton = QPushButton("Remover")
			self.profileAddButton.clicked.connect(self._addProfile)
			self.profileRenameButton.clicked.connect(self._renameProfile)
			self.profileRemoveButton.clicked.connect(self._removeProfile)
			layout.addWidget(self.profileCombo, 1)
			layout.addWidget(self.profileAddButton)
			layout.addWidget(self.profileRenameButton)
			layout.addWidget(self.profileRemoveButton)
			return bar

		def _reloadProfileCombo(self) -> None:
			current: str = ""
			active: config.Profile | None = config.activeProfile()
			if active is not None:
				current = active.id
			self.profileCombo.blockSignals(True)
			self.profileCombo.clear()
			for profile in config.listProfiles():
				self.profileCombo.addItem(profile.name, profile.id)
			if current != "":
				index: int = self.profileCombo.findData(current)
				if index >= 0:
					self.profileCombo.setCurrentIndex(index)
			self.profileCombo.blockSignals(False)
			chosen: object = self.profileCombo.currentData()
			if isinstance(chosen, str) and chosen != "":
				config.activateProfile(chosen)

		def _onProfileChanged(self) -> None:
			if self.openBusy:
				self._reloadProfileCombo()
				return
			chosen: object = self.profileCombo.currentData()
			if not isinstance(chosen, str) or chosen == "":
				return
			config.activateProfile(chosen)
			self.quotaPayload = None
			self._clearSearch()
			self._loadValues()
			self._refreshIndexStatus()

		def _addProfile(self) -> None:
			from fastonedrive.wizard import runProfileWizard

			previous: config.Profile | None = config.activeProfile()
			previousId: str = previous.id if previous is not None else ""
			if not runProfileWizard(self):
				self._restoreActiveProfile(previousId)
				return
			self._reloadProfileCombo()
			self.quotaPayload = None
			self._clearSearch()
			self._loadValues()
			self._refreshIndexStatus()

		def _restoreActiveProfile(self, profileId: str) -> None:
			if profileId != "" and config.profileById(profileId) is not None:
				config.activateProfile(profileId)
			self._reloadProfileCombo()

		def _clearSearch(self) -> None:
			self.results.clear()
			self.searchStatus.setText("")
			self._setResultActionsEnabled(False)

		def _renameProfile(self) -> None:
			profile: config.Profile | None = config.activeProfile()
			if profile is None:
				return
			text, accepted = QInputDialog.getText(
				self,
				"Fast-OneDrive",
				"Nome do perfil:",
				QLineEdit.EchoMode.Normal,
				profile.name,
			)
			if not accepted:
				return
			name: str = text.strip()
			if name == "":
				QMessageBox.warning(self, "Fast-OneDrive", "O nome do perfil não pode ficar vazio.")
				return
			if name == profile.name:
				return
			renamed: config.Profile = config.Profile(
				profile.id,
				name,
				profile.remoteName,
				profile.mountPath,
				profile.mountEnabled,
				profile.legacy,
			)
			from fastonedrive.kioinstall import upsertProfilePlace

			try:
				upsertProfilePlace(renamed)
			except FastOneDriveError as error:
				QMessageBox.warning(self, "Fast-OneDrive", str(error))
				return
			config.replaceProfile(renamed)
			self._reloadProfileCombo()

		def _removeProfile(self) -> None:
			profile: config.Profile | None = config.activeProfile()
			if profile is None:
				return
			answer: QMessageBox.StandardButton = QMessageBox.question(
				self,
				"Fast-OneDrive",
				f"Remover o perfil {profile.name} deste PC?\n"
				"Os ficheiros na nuvem não são apagados.",
				QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
			)
			if answer != QMessageBox.StandardButton.Yes:
				return
			from fastonedrive.profiles import removeProfile

			try:
				removeProfile(profile.id)
			except FastOneDriveError as error:
				QMessageBox.warning(self, "Fast-OneDrive", str(error))
				return
			self._reloadProfileCombo()
			self.quotaPayload = None
			self._clearSearch()
			self._loadValues()
			self._refreshIndexStatus()

		def _searchPage(self) -> QWidget:
			page: QWidget = QWidget()
			layout: QVBoxLayout = QVBoxLayout(page)
			hint: QLabel = QLabel(
				"Busca pelo nome ou caminho no índice (não lê o conteúdo dos ficheiros)."
			)
			hint.setWordWrap(True)
			layout.addWidget(hint)
			row: QWidget = QWidget()
			rowLayout: QHBoxLayout = QHBoxLayout(row)
			rowLayout.setContentsMargins(0, 0, 0, 0)
			self.queryEdit = QLineEdit()
			self.queryEdit.setPlaceholderText(
				"nome do ficheiro, ou expressão (ex.: TRABALHO/ ou .xlsx)"
			)
			self.queryEdit.returnPressed.connect(self._runSearch)
			searchButton: QPushButton = QPushButton("Buscar")
			searchButton.clicked.connect(self._runSearch)
			rowLayout.addWidget(self.queryEdit, 1)
			rowLayout.addWidget(searchButton)
			layout.addWidget(row)
			self.results = QTreeWidget()
			self.results.setColumnCount(4)
			self.results.setHeaderLabels(["Nome", "Caminho", "Tamanho", "Estado"])
			self.results.setRootIsDecorated(False)
			self.results.setSelectionMode(
				QTreeWidget.SelectionMode.ExtendedSelection
			)
			self.results.setUniformRowHeights(True)
			header: QHeaderView | None = self.results.header()
			if header is not None:
				header.setStretchLastSection(False)
				header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
				header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
				header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
				header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
			self.results.itemSelectionChanged.connect(self._onSelectionChanged)
			self.results.itemDoubleClicked.connect(self._openSelected)
			layout.addWidget(self.results, 1)
			actions: QWidget = QWidget()
			actionsLayout: QHBoxLayout = QHBoxLayout(actions)
			actionsLayout.setContentsMargins(0, 0, 0, 0)
			self.openButton = QPushButton("Abrir")
			self.locateButton = QPushButton("Localizar no Dolphin")
			self.copyButton = QPushButton("Copiar")
			self.openButton.clicked.connect(self._openSelected)
			self.locateButton.clicked.connect(self._locateSelected)
			self.copyButton.clicked.connect(self._copySelected)
			self.searchStatus = QLabel("")
			self.searchStatus.setWordWrap(True)
			actionsLayout.addWidget(self.openButton)
			actionsLayout.addWidget(self.locateButton)
			actionsLayout.addWidget(self.copyButton)
			actionsLayout.addWidget(self.searchStatus, 1)
			layout.addWidget(actions)
			return page

		def _rclonePage(self) -> QWidget:
			return self._wrapSettingsPage(
				"",
				[
					self._rcloneGroup(),
					self._rcloneFlagsGroup(),
				],
				footerStatus=self.rcloneStatusLabel,
				saveText="Guardar alterações",
			)

		def _fastPage(self) -> QWidget:
			return self._wrapSettingsPage(
				"",
				[
					self._indexGroup(),
					self._quotaGroup(),
					self._syncGroup(),
					self._pinGroup(),
					self._statusGroup("Estado Fast-OneDrive", self._fastStatusRows()),
				],
			)

		def _indexGroup(self) -> QGroupBox:
			box: QGroupBox = QGroupBox("Indexação")
			layout: QVBoxLayout = QVBoxLayout(box)
			hint: QLabel = QLabel(
				"A pasta nos Locais lista cada pasta ao vivo. "
				"Este índice corre em segundo plano e serve a pesquisa por nome."
			)
			hint.setWordWrap(True)
			layout.addWidget(hint)
			self.indexBusy = QProgressBar()
			self.indexBusy.setRange(0, 0)
			self.indexBusy.setTextVisible(False)
			self.indexBusy.setMaximumHeight(8)
			self.indexBusy.setVisible(False)
			layout.addWidget(self.indexBusy)
			form: QFormLayout = QFormLayout()
			self.indexStateLabel = QLabel("—")
			self.indexRootLabel = QLabel("—")
			self.indexCountedLabel = QLabel("—")
			self.indexModeLabel = QLabel("—")
			self.indexSqliteLabel = QLabel("—")
			self.indexStartedLabel = QLabel("—")
			self.indexUpdatedLabel = QLabel("—")
			self.indexFinishedLabel = QLabel("—")
			self.indexErrorLabel = QLabel("—")
			for label in (
				self.indexStateLabel,
				self.indexRootLabel,
				self.indexModeLabel,
				self.indexCountedLabel,
				self.indexSqliteLabel,
				self.indexStartedLabel,
				self.indexUpdatedLabel,
				self.indexFinishedLabel,
				self.indexErrorLabel,
			):
				label.setTextInteractionFlags(
					Qt.TextInteractionFlag.TextSelectableByMouse
				)
				label.setWordWrap(True)
			form.addRow("Estado", self.indexStateLabel)
			form.addRow("Remote", self.indexRootLabel)
			form.addRow("Modo", self.indexModeLabel)
			form.addRow("Neste ciclo", self.indexCountedLabel)
			form.addRow("No índice", self.indexSqliteLabel)
			form.addRow("Início", self.indexStartedLabel)
			form.addRow("Última gravação", self.indexUpdatedLabel)
			form.addRow("Terminou", self.indexFinishedLabel)
			form.addRow("Erro", self.indexErrorLabel)
			layout.addLayout(form)
			self.indexStartButton = QPushButton("Indexar agora")
			self.indexStartButton.clicked.connect(self._startIndex)
			layout.addWidget(self.indexStartButton)
			return box

		def _quotaGroup(self) -> QGroupBox:
			box: QGroupBox = QGroupBox("Espaço OneDrive")
			layout: QVBoxLayout = QVBoxLayout(box)
			self.quotaBar = QProgressBar()
			self.quotaBar.setRange(0, 100)
			self.quotaBar.setValue(0)
			self.quotaLabel = QLabel("—")
			self.quotaLabel.setWordWrap(True)
			layout.addWidget(self.quotaBar)
			layout.addWidget(self.quotaLabel)
			return box

		def _infoMessage(self, text: str) -> QWidget:
			frame: QFrame = QFrame()
			frame.setObjectName("infoMessage")
			frame.setFrameShape(QFrame.Shape.NoFrame)
			palette: QPalette = frame.palette()
			tint: QColor = palette.color(QPalette.ColorRole.Highlight)
			tint.setAlpha(36)
			palette.setColor(QPalette.ColorRole.Window, tint)
			frame.setPalette(palette)
			frame.setAutoFillBackground(True)
			frame.setStyleSheet(
				"#infoMessage { border: 1px solid palette(highlight); border-radius: 4px; }"
			)
			inner: QHBoxLayout = QHBoxLayout(frame)
			inner.setContentsMargins(10, 8, 8, 8)
			inner.setSpacing(8)
			iconLabel: QLabel = QLabel()
			icon: QIcon = _themedIcon("dialog-information")
			style: QStyle | None = QApplication.style()
			iconSize: int = 16
			if style is not None:
				iconSize = int(
					style.pixelMetric(QStyle.PixelMetric.PM_SmallIconSize)
				)
				if icon.isNull():
					icon = style.standardIcon(
						QStyle.StandardPixmap.SP_MessageBoxInformation
					)
			iconLabel.setPixmap(icon.pixmap(iconSize, iconSize))
			iconLabel.setAlignment(Qt.AlignmentFlag.AlignTop)
			textLabel: QLabel = QLabel(text)
			textLabel.setWordWrap(True)
			closeButton: QToolButton = QToolButton()
			closeButton.setAutoRaise(True)
			closeButton.setToolTip("Fechar")
			closeIcon: QIcon = _themedIcon("window-close")
			if closeIcon.isNull():
				closeButton.setText("×")
			else:
				closeButton.setIcon(closeIcon)
			closeButton.clicked.connect(frame.hide)
			inner.addWidget(iconLabel, 0, Qt.AlignmentFlag.AlignTop)
			inner.addWidget(textLabel, 1)
			inner.addWidget(closeButton, 0, Qt.AlignmentFlag.AlignTop)
			return frame

		def _wrapSettingsPage(
			self,
			introText: str,
			groups: list[QGroupBox],
			dismissableIntro: bool = False,
			footerStatus: QLabel | None = None,
			saveText: str = "Guardar",
		) -> QWidget:
			page: QWidget = QWidget()
			outer: QVBoxLayout = QVBoxLayout(page)
			outer.setContentsMargins(0, 0, 0, 0)
			outer.setSpacing(8)
			scroll: QScrollArea = QScrollArea()
			scroll.setWidgetResizable(True)
			scroll.setFrameShape(QFrame.Shape.NoFrame)
			inner: QWidget = QWidget()
			layout: QVBoxLayout = QVBoxLayout(inner)
			layout.setContentsMargins(8, 8, 8, 12)
			layout.setSpacing(12)
			layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
			if introText != "":
				if dismissableIntro:
					layout.addWidget(self._infoMessage(introText))
				else:
					intro: QLabel = QLabel(introText)
					intro.setWordWrap(True)
					layout.addWidget(intro)
			for group in groups:
				layout.addWidget(group)
			layout.addStretch(1)
			scroll.setWidget(inner)
			outer.addWidget(scroll, 1)
			footer: QWidget = QWidget()
			footerLayout: QHBoxLayout = QHBoxLayout(footer)
			footerLayout.setContentsMargins(0, 4, 0, 0)
			if footerStatus is not None:
				muted: QColor = self.palette().color(
					QPalette.ColorGroup.Disabled,
					QPalette.ColorRole.WindowText,
				)
				footerStatus.setStyleSheet(f"color: {muted.name()};")
				footerLayout.addWidget(footerStatus, 1)
			else:
				footerLayout.addStretch(1)
			saveButton: QPushButton = QPushButton(saveText)
			saveIcon: QIcon = _themedIcon("document-save")
			if not saveIcon.isNull():
				saveButton.setIcon(saveIcon)
			saveButton.setAutoDefault(False)
			saveButton.clicked.connect(self._save)
			footerLayout.addWidget(saveButton)
			outer.addWidget(footer)
			return page

		def _rcloneGroup(self) -> QGroupBox:
			box: QGroupBox = QGroupBox("Geral")
			box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
			form: QFormLayout = QFormLayout(box)
			_styleForm(form)
			confRow: QWidget = QWidget()
			confLayout: QHBoxLayout = QHBoxLayout(confRow)
			confLayout.setContentsMargins(0, 0, 0, 0)
			confLayout.setSpacing(6)
			self.rcloneConfigEdit = QLineEdit()
			self.rcloneConfigEdit.setPlaceholderText("~/.config/fast-onedrive/rclone.conf")
			browse: QPushButton = _actionButton(
				"Escolher…",
				"document-open",
				self._pickRcloneConfig,
			)
			confLayout.addWidget(self.rcloneConfigEdit, 1)
			confLayout.addWidget(browse)
			form.addRow("rclone.conf:", confRow)
			self.remoteCombo = QComboBox()
			self.remoteCombo.setEditable(True)
			self.remoteCombo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
			self.remoteCombo.setMaximumWidth(250)
			self.remoteCombo.setSizePolicy(
				QSizePolicy.Policy.Maximum,
				QSizePolicy.Policy.Fixed,
			)
			form.addRow("Remote:", self.remoteCombo)
			buttons: QWidget = QWidget()
			buttonsLayout: QHBoxLayout = QHBoxLayout(buttons)
			buttonsLayout.setContentsMargins(0, 0, 0, 0)
			buttonsLayout.setSpacing(6)
			buttonsLayout.addWidget(
				_actionButton(
					"Importar do sistema",
					"document-import",
					self._importSystemRclone,
				)
			)
			buttonsLayout.addWidget(
				_actionButton(
					"Atualizar remotes",
					"view-refresh",
					self._refreshRemotes,
				)
			)
			buttonsLayout.addWidget(
				_actionButton("Testar", "system-run", self._testRclone)
			)
			buttonsLayout.addWidget(
				_actionButton(
					"Configurar…",
					"settings-configure",
					self._configureRclone,
				)
			)
			buttonsLayout.addStretch(1)
			form.addRow("Ações:", buttons)
			return box

		def _rcloneFlagsGroup(self) -> QGroupBox:
			box: QGroupBox = QGroupBox("Flags rclone")
			box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
			form: QFormLayout = QFormLayout(box)
			_styleForm(form)
			form.setLabelAlignment(
				Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
			)
			self.mountCheck = QCheckBox("Montar a pasta deste perfil com o FUSE do Fast-OneDrive")
			form.addRow(self.mountCheck)
			self.vfsModeCombo = QComboBox()
			for mode in config.VFS_CACHE_MODES:
				self.vfsModeCombo.addItem(mode)
			self.vfsModeCombo.setMaximumWidth(200)
			self.vfsModeCombo.setSizePolicy(
				QSizePolicy.Policy.Maximum,
				QSizePolicy.Policy.Fixed,
			)
			form.addRow("vfs-cache-mode:", self.vfsModeCombo)
			self.vfsCacheMaxSizeEdit = _cacheLineEdit("150G")
			self.vfsCacheMaxAgeEdit = _cacheLineEdit("9999h")
			self.vfsWriteBackEdit = _cacheLineEdit("5s")
			self.dirCacheTimeEdit = _cacheLineEdit("9999h")
			self.pollIntervalEdit = _cacheLineEdit("15s")
			cacheGrid: QGridLayout = QGridLayout()
			cacheGrid.setContentsMargins(0, 0, 0, 0)
			cacheGrid.setHorizontalSpacing(16)
			cacheGrid.setVerticalSpacing(6)
			cacheFields: list[tuple[str, QLineEdit]] = [
				("max-size:", self.vfsCacheMaxSizeEdit),
				("max-age:", self.vfsCacheMaxAgeEdit),
				("write-back:", self.vfsWriteBackEdit),
				("dir-cache-time:", self.dirCacheTimeEdit),
				("poll-interval:", self.pollIntervalEdit),
			]
			for index, (labelText, edit) in enumerate(cacheFields):
				row: int = index // 2
				column: int = (index % 2) * 2
				cacheGrid.addWidget(QLabel(labelText), row, column)
				cacheGrid.addWidget(edit, row, column + 1)
			cacheGrid.setColumnStretch(4, 1)
			cacheWidget: QWidget = QWidget()
			cacheWidget.setLayout(cacheGrid)
			cacheWidget.setSizePolicy(
				QSizePolicy.Policy.Preferred,
				QSizePolicy.Policy.Fixed,
			)
			form.addRow("Parâmetros de cache:", cacheWidget)
			self.vfsFastCheck = QCheckBox("vfs-fast-fingerprint")
			form.addRow(self.vfsFastCheck)
			self.excludeEdit = QLineEdit()
			self.excludeEdit.setPlaceholderText("/Cofre Pessoal/**")
			form.addRow("exclude:", self.excludeEdit)
			self.extraEdit = QLineEdit()
			self.extraEdit.setPlaceholderText("--transfers 8 --bwlimit off")
			form.addRow("Flags extras:", self.extraEdit)
			return box

		def _syncGroup(self) -> QGroupBox:
			box: QGroupBox = QGroupBox("Sincronização")
			form: QFormLayout = QFormLayout(box)
			form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
			self.bwEdit = QLineEdit()
			self.bwEdit.setPlaceholderText("sem limite  (ex.: 10M, ou 5M:20M para envio:descarga)")
			self.meteredCheck = QCheckBox("Pausar em rede limitada (dados móveis, hotspot)")
			self.pollSpin = QSpinBox()
			self.pollSpin.setRange(15, 3600)
			self.pollSpin.setSuffix(" s")
			hint: QLabel = QLabel(
				"As mudanças feitas noutros dispositivos aparecem em ~/OneDrive ao fim deste intervalo. "
				"A pausa (bandeja → Pausar) e a rede limitada travam envios e descargas de fundo; "
				"abrir um ficheiro continua a descarregá-lo."
			)
			hint.setWordWrap(True)
			form.addRow("Limite de banda", self.bwEdit)
			form.addRow("", self.meteredCheck)
			form.addRow("Verificar a nuvem a cada", self.pollSpin)
			form.addRow("", hint)
			return box

		def _loadSyncControl(self) -> None:
			from fastonedrive.synccontrol import load

			control = load()
			self.bwEdit.setText(control.bwLimit)
			self.meteredCheck.setChecked(control.pauseOnMetered)
			self.pollSpin.setValue(int(control.remotePollSeconds))

		def _saveSyncControl(self) -> None:
			from fastonedrive.synccontrol import load, save

			control = load()
			control.bwLimit = self.bwEdit.text().strip()
			control.pauseOnMetered = self.meteredCheck.isChecked()
			control.remotePollSeconds = int(self.pollSpin.value())
			save(control)

		def _pinGroup(self) -> QGroupBox:
			box: QGroupBox = QGroupBox("Manter localmente e abrir")
			form: QFormLayout = QFormLayout(box)
			form.setLabelAlignment(
				Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
			)
			self.openSpin = QSpinBox()
			self.openSpin.setRange(1, 10240)
			self.openSpin.setSuffix(" MB")
			skipBox: QWidget = QWidget()
			skipLayout: QVBoxLayout = QVBoxLayout(skipBox)
			skipLayout.setContentsMargins(0, 0, 0, 0)
			skipLayout.setSpacing(6)
			self.skipList = QListWidget()
			self.skipList.setMinimumHeight(110)
			self.skipList.itemDoubleClicked.connect(self._editSkipPin)
			self.skipText = QPlainTextEdit()
			self.skipText.setPlaceholderText("roms\n/Documentos/Backups\n**/Emuladores/**")
			self.skipText.setMinimumHeight(110)
			self.skipText.setVisible(False)
			buttons: QWidget = QWidget()
			buttonsLayout: QHBoxLayout = QHBoxLayout(buttons)
			buttonsLayout.setContentsMargins(0, 0, 0, 0)
			buttonsLayout.setSpacing(6)
			self.skipAddButton = QPushButton("Adicionar…")
			self.skipAddButton.setAutoDefault(False)
			self.skipAddButton.clicked.connect(self._addSkipPin)
			self.skipRemoveButton = QPushButton("Remover")
			self.skipRemoveButton.setAutoDefault(False)
			self.skipRemoveButton.clicked.connect(self._removeSkipPin)
			self.skipTextButton = QPushButton("Editar como texto")
			self.skipTextButton.setAutoDefault(False)
			self.skipTextButton.clicked.connect(self._toggleSkipTextMode)
			buttonsLayout.addWidget(self.skipAddButton)
			buttonsLayout.addWidget(self.skipRemoveButton)
			buttonsLayout.addWidget(self.skipTextButton)
			buttonsLayout.addStretch(1)
			hint: QLabel = QLabel(
				"Uma regra por linha: nome (roms), caminho no OneDrive "
				"(/Documentos/Backups) ou padrão (**/roms/**)."
			)
			hint.setWordWrap(True)
			skipLayout.addWidget(self.skipList)
			skipLayout.addWidget(self.skipText)
			skipLayout.addWidget(buttons)
			skipLayout.addWidget(hint)
			form.addRow("Descarregar por inteiro até", self.openSpin)
			form.addRow("Não manter localmente", skipBox)
			return box

		def _setSkipPinValues(self, values: list[str]) -> None:
			self.skipList.clear()
			lines: list[str] = []
			for value in values:
				text: str = value.strip()
				if text == "":
					continue
				self.skipList.addItem(text)
				lines.append(text)
			self.skipText.setPlainText("\n".join(lines))

		def _skipPinValues(self) -> list[str]:
			if self.skipTextMode:
				return [
					line.strip()
					for line in self.skipText.toPlainText().splitlines()
					if line.strip() != ""
				]
			found: list[str] = []
			for index in range(self.skipList.count()):
				item: QListWidgetItem | None = self.skipList.item(index)
				if item is None:
					continue
				text: str = item.text().strip()
				if text != "":
					found.append(text)
			return found

		def _toggleSkipTextMode(self) -> None:
			if self.skipTextMode:
				self._setSkipPinValues(self._skipPinValues())
				self.skipText.setVisible(False)
				self.skipList.setVisible(True)
				self.skipAddButton.setVisible(True)
				self.skipRemoveButton.setVisible(True)
				self.skipTextButton.setText("Editar como texto")
				self.skipTextMode = False
				return
			self.skipText.setPlainText("\n".join(self._skipPinValues()))
			self.skipList.setVisible(False)
			self.skipAddButton.setVisible(False)
			self.skipRemoveButton.setVisible(False)
			self.skipText.setVisible(True)
			self.skipTextButton.setText("Editar como lista")
			self.skipTextMode = True

		def _addSkipPin(self) -> None:
			text: str
			accepted: bool
			text, accepted = QInputDialog.getText(
				self,
				"Fast-OneDrive",
				"Nome, caminho no OneDrive ou padrão:",
			)
			if not accepted:
				return
			rule: str = text.strip()
			if rule == "":
				return
			key: str = rule.strip("/").lower()
			for existing in self._skipPinValues():
				if existing.strip("/").lower() == key:
					return
			self.skipList.addItem(rule)

		def _removeSkipPin(self) -> None:
			row: int = self.skipList.currentRow()
			if row >= 0:
				self.skipList.takeItem(row)

		def _editSkipPin(self, item: QListWidgetItem) -> None:
			text: str
			accepted: bool
			text, accepted = QInputDialog.getText(
				self,
				"Fast-OneDrive",
				"Nome, caminho no OneDrive ou padrão:",
				QLineEdit.EchoMode.Normal,
				item.text(),
			)
			if not accepted:
				return
			rule: str = text.strip()
			if rule != "":
				item.setText(rule)

		def _refreshRcloneStatus(self) -> None:
			fastMount: str = "activo" if fastMountActive() else "parado"
			legacy: str = "activo" if legacyMountActive() else "desligado"
			self.rcloneStatusLabel.setText(
				f"Estado: FUSE {fastMount} · antigo {legacy} · {rcloneVersion()}"
			)

		def _fastStatusRows(self) -> list[tuple[str, str]]:
			return [
				("índice", str(config.dbPath())),
				("pins", str(config.pinDir())),
				("cache", str(config.cacheDir())),
				("config", str(config.configPath())),
			]

		def _refreshIndexStatus(self) -> None:
			progress: dict[str, Any] = indexProgress()
			running: bool = bool(progress["running"])
			if running:
				self.indexStarting = False
			state: str = str(progress["state"])
			if self.indexStarting and not running:
				state = "starting"
			labels: dict[str, str] = {
				"running": "Em curso",
				"starting": "A iniciar…",
				"idle": "Parado",
				"done": "Concluída",
				"error": "Falhou",
				"empty": "Ainda sem índice",
			}
			self.indexStateLabel.setText(labels.get(state, state))
			self.indexRootLabel.setText(str(progress["root"]))
			mode: str = str(progress.get("mode") or "")
			modeLabels: dict[str, str] = {
				"incremental": "Incremental (Graph)",
				"completo": "Completo (Graph)",
				"rclone": "Completo (rclone)",
			}
			self.indexModeLabel.setText(modeLabels.get(mode, "—"))
			counted: int = int(progress["counted"])
			if running and counted == 0:
				counted = int(progress["files"]) + int(progress["dirs"])
			self.indexCountedLabel.setText(f"{counted} itens")
			if bool(progress["exists"]):
				self.indexSqliteLabel.setText(
					f"{progress['files']} arquivos, {progress['dirs']} pastas"
				)
			else:
				self.indexSqliteLabel.setText("índice ainda não criado")
			self.indexStartedLabel.setText(_formatStamp(progress["startedAt"]))
			self.indexUpdatedLabel.setText(
				_formatStamp(progress["updatedAt"] or progress["indexUpdatedAt"])
			)
			self.indexFinishedLabel.setText(_formatStamp(progress["finishedAt"]))
			error: str = str(progress["error"] or "")
			self.indexErrorLabel.setText(error if error != "" else "—")
			busy: bool = running or self.indexStarting
			self.indexBusy.setVisible(busy)
			self.indexStartButton.setEnabled(not busy)
			self.indexStartButton.setText("A indexar…" if busy else "Indexar agora")
			self._refreshRcloneStatus()

		def _refreshQuota(self) -> None:
			if self.quotaPayload is not None and time.monotonic() - self.quotaAt < 60.0:
				self._applyQuota(self.quotaPayload, None)
				return
			if self.quotaBusy:
				return
			profile: config.Profile | None = config.activeProfile()
			profileId: str = profile.id if profile is not None else ""
			root: str = config.remoteRoot()
			self.quotaBusy = True
			self.quotaLabel.setText("A obter quota…")

			def work() -> None:
				payload: dict[str, Any] | None = None
				errorText: str | None = None
				try:
					payload = remoteAbout(root)
				except FastOneDriveError as error:
					errorText = str(error)
				except Exception as error:
					errorText = str(error)
				self.quotaBridge.finished.emit(payload, errorText, profileId)

			threading.Thread(
				target=work,
				daemon=True,
				name="fod-quota",
			).start()

		def _onQuotaFinished(self, payload: object, errorText: object, profileId: str) -> None:
			self.quotaBusy = False
			current: config.Profile | None = config.activeProfile()
			currentId: str = current.id if current is not None else ""
			if profileId != currentId:
				self.quotaPayload = None
				self._refreshQuota()
				return
			error: str | None = errorText if isinstance(errorText, str) else None
			data: dict[str, Any] | None = payload if isinstance(payload, dict) else None
			self._applyQuota(data, error)

		def _applyQuota(self, payload: dict[str, Any] | None, errorText: str | None) -> None:
			if errorText is not None:
				self.quotaBar.setValue(0)
				self.quotaLabel.setText(errorText)
				return
			if payload is None:
				self.quotaBar.setValue(0)
				self.quotaLabel.setText("—")
				return
			self.quotaPayload = payload
			self.quotaAt = time.monotonic()
			used: int = int(payload.get("used") or 0)
			total: int = int(payload.get("total") or 0)
			free: int = int(payload.get("free") or 0)
			percent: int = 0
			if total > 0:
				percent = min(100, int(used * 100 / total))
			self.quotaBar.setValue(percent)
			self.quotaLabel.setText(
				f"{_formatSize(used)} usados de {_formatSize(total)} "
				f"({_formatSize(free)} livres)"
			)

		def _startIndex(self) -> None:
			progress: dict[str, Any] = indexProgress()
			if bool(progress["running"]):
				self._refreshIndexStatus()
				return
			started: bool = startBackgroundIndex(True)
			if not started and not bool(indexProgress()["running"]):
				QMessageBox.warning(
					self,
					"Fast-OneDrive",
					"Não foi possível iniciar a indexação.",
				)
				return
			self.indexStarting = True
			self._refreshIndexStatus()

		def _statusGroup(self, title: str, rows: list[tuple[str, str]]) -> QGroupBox:
			box: QGroupBox = QGroupBox(title)
			form: QFormLayout = QFormLayout(box)
			for label, value in rows:
				valueLabel: QLabel = QLabel(value)
				valueLabel.setTextInteractionFlags(
					Qt.TextInteractionFlag.TextSelectableByMouse
				)
				valueLabel.setWordWrap(True)
				form.addRow(label, valueLabel)
				self.fastStatusLabels.append(valueLabel)
			return box

		def _refreshFastStatus(self) -> None:
			rows: list[tuple[str, str]] = self._fastStatusRows()
			index: int
			for index, label in enumerate(self.fastStatusLabels):
				if index >= len(rows):
					break
				label.setText(rows[index][1])

		def _loadValues(self) -> None:
			self.rcloneConfigEdit.setText(config.rcloneConfigDisplay())
			self._fillRemotes(config.remoteName())
			flags: dict[str, Any] = config.rcloneFlagValues()
			profile: config.Profile | None = config.activeProfile()
			if profile is not None:
				self.mountCheck.setChecked(profile.mountEnabled)
				self.mountCheck.setText(
					f"Montar {profile.mountPath} com o FUSE do Fast-OneDrive"
				)
			else:
				self.mountCheck.setChecked(bool(flags["mountEnabled"]))
			mode: str = str(flags["vfsCacheMode"])
			modeIndex: int = self.vfsModeCombo.findText(mode)
			if modeIndex >= 0:
				self.vfsModeCombo.setCurrentIndex(modeIndex)
			self.vfsCacheMaxSizeEdit.setText(str(flags["vfsCacheMaxSize"]))
			self.vfsCacheMaxAgeEdit.setText(str(flags["vfsCacheMaxAge"]))
			self.vfsWriteBackEdit.setText(str(flags["vfsWriteBack"]))
			self.dirCacheTimeEdit.setText(str(flags["dirCacheTime"]))
			self.pollIntervalEdit.setText(str(flags["pollInterval"]))
			self.vfsFastCheck.setChecked(bool(flags["vfsFastFingerprint"]))
			self.excludeEdit.setText(", ".join(list(flags["rcloneExclude"])))
			self.extraEdit.setText(str(flags["rcloneExtra"]))
			openMb: int = config.bytesToMb(config.openMaxBytes())
			self.openSpin.setValue(openMb if openMb > 0 else 1)
			self._loadSyncControl()
			self._setSkipPinValues(list(config.skipPinSegments()))
			self._refreshFastStatus()
			self._refreshQuota()

		def _save(self) -> None:
			try:
				path: Path = self._persist(applyFuse=True)
			except FastOneDriveError as error:
				QMessageBox.warning(self, "Fast-OneDrive", str(error))
				return
			self._refreshRcloneStatus()
			QMessageBox.information(
				self,
				"Fast-OneDrive",
				f"Definições guardadas em {path}",
			)

		def _persist(self, applyFuse: bool = False) -> Path:
			self._saveSyncControl()
			skipPin: list[str] = self._skipPinValues()
			exclude: list[str] = [
				part.strip() for part in self.excludeEdit.text().split(",")
			]
			profile: config.Profile | None = config.activeProfile()
			mountEnabled: bool = self.mountCheck.isChecked()
			remoteValue: str = self.remoteCombo.currentText().strip()
			if profile is not None and not applyFuse:
				mountEnabled = profile.mountEnabled
			if profile is not None:
				remoteValue = config.storedRemoteName()
			path: Path = config.saveUserConfig(
				config.copyDestDisplay(),
				config.mbToBytes(int(self.openSpin.value())),
				skipPin,
				self.rcloneConfigEdit.text().strip(),
				remoteValue,
				{
					"vfsCacheMode": self.vfsModeCombo.currentText().strip(),
					"vfsCacheMaxSize": self.vfsCacheMaxSizeEdit.text().strip(),
					"vfsCacheMaxAge": self.vfsCacheMaxAgeEdit.text().strip(),
					"vfsWriteBack": self.vfsWriteBackEdit.text().strip(),
					"dirCacheTime": self.dirCacheTimeEdit.text().strip(),
					"pollInterval": self.pollIntervalEdit.text().strip(),
					"vfsFastFingerprint": self.vfsFastCheck.isChecked(),
					"rcloneExclude": exclude,
					"rcloneExtra": self.extraEdit.text().strip(),
					"mountEnabled": mountEnabled,
				},
			)
			if profile is not None:
				self._showProfileRemote(profile.remoteName)
			if profile is None:
				if applyFuse:
					applyMount()
				return path
			if not applyFuse:
				return path
			previous: bool = profile.mountEnabled
			profile.mountEnabled = self.mountCheck.isChecked()
			config.replaceProfile(profile)
			try:
				from fastonedrive.mount import applyProfileMount

				applyProfileMount(profile)
			except Exception:
				if self.mountCheck.isChecked() and not fastMountActive():
					profile.mountEnabled = previous
					config.replaceProfile(profile)
					self.mountCheck.setChecked(previous)
				raise
			return path

		def _showProfileRemote(self, remoteName: str) -> None:
			self.remoteCombo.blockSignals(True)
			index: int = self.remoteCombo.findText(remoteName)
			if index >= 0:
				self.remoteCombo.setCurrentIndex(index)
			else:
				self.remoteCombo.setEditText(remoteName)
			self.remoteCombo.blockSignals(False)

		def _fillRemotes(self, selected: str) -> None:
			self.remoteCombo.blockSignals(True)
			self.remoteCombo.clear()
			names: list[str]
			try:
				names = listRemoteNames()
			except FastOneDriveError:
				names = []
			if selected != "" and selected not in names:
				names = [selected, *names]
			for name in names:
				self.remoteCombo.addItem(name)
			if selected != "":
				index: int = self.remoteCombo.findText(selected)
				if index >= 0:
					self.remoteCombo.setCurrentIndex(index)
				else:
					self.remoteCombo.setEditText(selected)
			self.remoteCombo.blockSignals(False)

		def _pickRcloneConfig(self) -> None:
			start: str = str(config.rcloneConfigPath().parent)
			chosen: str
			chosen, _filter = QFileDialog.getOpenFileName(
				self,
				"rclone.conf do Fast-OneDrive",
				start,
				"rclone.conf (*.conf);;Todos (*)",
			)
			if chosen != "":
				self.rcloneConfigEdit.setText(_displayPath(Path(chosen)))

		def _importSystemRclone(self) -> None:
			try:
				self._persist()
				path: Path = config.importSystemRcloneConfig()
				self._fillRemotes(config.remoteName())
			except FastOneDriveError as error:
				QMessageBox.warning(self, "Fast-OneDrive", str(error))
				return
			QMessageBox.information(
				self,
				"Fast-OneDrive",
				f"Os remotes de {config.systemRcloneConfigPath()} que faltavam foram juntados a {path}. "
				"Os remotes que já existiam não foram alterados.",
			)

		def _refreshRemotes(self) -> None:
			try:
				self._persist()
			except FastOneDriveError as error:
				QMessageBox.warning(self, "Fast-OneDrive", str(error))
				return
			self._fillRemotes(self.remoteCombo.currentText().strip())

		def _testRclone(self) -> None:
			try:
				self._persist()
				message: str = probeRemote()
			except FastOneDriveError as error:
				QMessageBox.warning(self, "Fast-OneDrive", str(error))
				return
			QMessageBox.information(self, "Fast-OneDrive", message)

		def _configureRclone(self) -> None:
			self._addProfile()

		def _runSearch(self) -> None:
			needle: str = self.queryEdit.text().strip()
			self.results.clear()
			self._setResultActionsEnabled(False)
			if needle == "":
				return
			try:
				rows = search(needle, config.SEARCH_KIO_LIMIT)
			except FastOneDriveError as error:
				QMessageBox.warning(self, "Fast-OneDrive", str(error))
				return
			for row in rows:
				entry: dict[str, Any] = rowToDict(row)
				item: QTreeWidgetItem = QTreeWidgetItem(
					[
						str(entry["name"]),
						str(entry["path"]),
						"—" if bool(entry["isDir"]) else _formatSize(int(entry["size"])),
						"Local" if bool(entry["pinned"]) else "—",
					]
				)
				item.setData(0, Qt.ItemDataRole.UserRole, entry)
				self.results.addTopLevelItem(item)
			if self.results.topLevelItemCount() == 0:
				empty: QTreeWidgetItem = QTreeWidgetItem(
					["Nenhum resultado.", "", "", ""]
				)
				empty.setDisabled(True)
				self.results.addTopLevelItem(empty)

		def _onSelectionChanged(self) -> None:
			self._setResultActionsEnabled(len(self._selectedEntries()) > 0)

		def _setResultActionsEnabled(self, enabled: bool) -> None:
			self.openButton.setEnabled(enabled and not self.openBusy)
			self.locateButton.setEnabled(enabled)
			self.copyButton.setEnabled(enabled)

		def _selectedEntries(self) -> list[dict[str, Any]]:
			found: list[dict[str, Any]] = []
			for item in self.results.selectedItems():
				payload: object = item.data(0, Qt.ItemDataRole.UserRole)
				if isinstance(payload, dict):
					found.append(payload)
			return found

		def _openSelected(self, *_args: object) -> None:
			if self.openBusy:
				return
			entries: list[dict[str, Any]] = self._selectedEntries()
			if len(entries) == 0:
				return
			entry: dict[str, Any] = entries[0]
			try:
				if bool(entry["isDir"]):
					_spawnDolphin([_kioItemUrl(str(entry["path"]))])
					return
				localPath: str = str(entry.get("localPath") or "")
				if localPath != "" and Path(localPath).exists():
					_spawnOpen(Path(localPath))
					return
				if bool(entry.get("tooLargeToOpen")):
					if not fuseMountReady():
						raise FastOneDriveError(
							"Activa o FUSE do Fast-OneDrive para abrir este ficheiro."
						)
					_spawnOpen(config.mountPath() / str(entry["path"]))
					return
			except FastOneDriveError as error:
				QMessageBox.warning(self, "Fast-OneDrive", str(error))
				return
			self._setOpenBusy(True)
			self.searchStatus.setText(
				f"A descarregar {entry.get('name') or entry['path']}…"
			)
			remote: str = config.remotePath(str(entry["path"]))
			openedProfile: config.Profile | None = config.activeProfile()
			openedId: str = openedProfile.id if openedProfile is not None else ""

			def work() -> None:
				pathText: str | None = None
				errorText: str | None = None
				try:
					pathText = str(hydrateRemote(remote))
				except FastOneDriveError as error:
					errorText = str(error)
				except Exception as error:
					errorText = str(error)
				finally:
					if openedId != "" and config.profileById(openedId) is not None:
						try:
							config.activateProfile(openedId)
						except FastOneDriveError:
							pass
				self.openBridge.finished.emit(pathText, errorText)

			try:
				threading.Thread(
					target=work,
					daemon=True,
					name="fod-open",
				).start()
			except Exception:
				self._setOpenBusy(False)
				raise

		def _setOpenBusy(self, busy: bool) -> None:
			self.openBusy = busy
			self.profileCombo.setEnabled(not busy)
			self.profileAddButton.setEnabled(not busy)
			self.profileRenameButton.setEnabled(not busy)
			self.profileRemoveButton.setEnabled(not busy)
			self._setResultActionsEnabled(len(self._selectedEntries()) > 0)

		def _onOpenFinished(self, pathText: object, errorText: object) -> None:
			self._reloadProfileCombo()
			self._setOpenBusy(False)
			self.searchStatus.setText("")
			error: str | None = errorText if isinstance(errorText, str) else None
			if error is not None:
				QMessageBox.warning(self, "Fast-OneDrive", error)
				return
			if not isinstance(pathText, str) or pathText == "":
				return
			try:
				_spawnOpen(Path(pathText))
			except FastOneDriveError as error:
				QMessageBox.warning(self, "Fast-OneDrive", str(error))

		def _locateSelected(self) -> None:
			entries: list[dict[str, Any]] = self._selectedEntries()
			if len(entries) == 0:
				return
			try:
				_locateEntry(entries[0])
			except FastOneDriveError as error:
				QMessageBox.warning(self, "Fast-OneDrive", str(error))

		def _copySelected(self) -> None:
			entries: list[dict[str, Any]] = self._selectedEntries()
			if len(entries) == 0:
				return
			urls: list[QUrl] = []
			for entry in entries:
				urls.append(_clipboardUrl(entry))
			mime: QMimeData = QMimeData()
			mime.setUrls(urls)
			mime.setText("\n".join(url.toString() for url in urls))
			clipboard = QApplication.clipboard()
			if clipboard is None:
				QMessageBox.warning(
					self,
					"Fast-OneDrive",
					"Área de transferência indisponível.",
				)
				return
			clipboard.setMimeData(mime)
			QMessageBox.information(
				self,
				"Fast-OneDrive",
				"Copiado. Cola com Ctrl+V noutra janela do Dolphin.",
			)

	app: QApplication
	existing: object | None = QApplication.instance()
	if existing is None:
		app = QApplication(["fastonedrive"])
	else:
		app = cast(QApplication, existing)
	app.setQuitOnLastWindowClosed(False)
	app.setApplicationName("Fast-OneDrive")
	app.setDesktopFileName("fast-onedrive")
	app.setWindowIcon(_appIcon())
	if _activateRunningUi(not startHidden):
		return 0
	server: QLocalServer = QLocalServer()
	pendingClients: list[QLocalSocket] = []
	QLocalServer.removeServer(_UI_SOCKET_NAME)
	server.listen(_UI_SOCKET_NAME)
	dialog: SettingsDialog = SettingsDialog()
	if len(config.listProfiles()) == 0:
		from fastonedrive.wizard import runProfileWizard

		if runProfileWizard(dialog):
			dialog._reloadProfileCombo()
			dialog._loadValues()

	def showWindow() -> None:
		dialog.show()
		dialog.raise_()
		dialog.activateWindow()

	def dropClient(client: QLocalSocket) -> None:
		if client not in pendingClients:
			return
		pendingClients.remove(client)
		client.close()

	def onNewConnection() -> None:
		client: QLocalSocket | None = server.nextPendingConnection()
		if client is None:
			return
		pendingClients.append(client)

		def takeText() -> str:
			return bytes(client.readAll()).decode("utf-8", errors="replace")

		def onReady() -> None:
			if "show" in takeText():
				showWindow()
			dropClient(client)

		def onDisconnected() -> None:
			if "show" in takeText():
				showWindow()
			dropClient(client)

		client.readyRead.connect(onReady)
		client.disconnected.connect(onDisconnected)

	server.newConnection.connect(onNewConnection)
	tray: object | None = _installTray(app, dialog, showWindow)
	dialog.hideOnClose = tray is not None
	if not startHidden or tray is None:
		dialog.show()
	return int(app.exec())


def _activateRunningUi(showWindow: bool) -> bool:
	from PyQt6.QtNetwork import QLocalSocket

	socket: QLocalSocket = QLocalSocket()
	socket.connectToServer(_UI_SOCKET_NAME)
	if not socket.waitForConnected(200):
		return False
	if showWindow:
		socket.write(b"show\n")
		socket.waitForBytesWritten(200)
	socket.disconnectFromServer()
	return True


def _installTray(app: object, parent: object, showWindow: Callable[[], None]) -> object | None:
	from PyQt6.QtGui import QAction, QIcon
	from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

	qtApp: QApplication = cast(QApplication, app)
	if not QSystemTrayIcon.isSystemTrayAvailable():
		return None
	appIcon: QIcon = cast(QIcon, _loadAppIcon())
	tray: QSystemTrayIcon = QSystemTrayIcon(appIcon, qtApp)
	tray.setToolTip("Fast-OneDrive")
	menu: QMenu = QMenu(cast(QWidget, parent))
	# Como o menu do OneDrive no Windows: pasta, busca, web, actividade, pausa.
	folderAction: QAction | None = menu.addAction(appIcon, "Abrir pasta OneDrive")
	searchAction: QAction | None = menu.addAction(QIcon.fromTheme("search"), "Pesquisar no OneDrive…")
	webAction: QAction | None = menu.addAction(QIcon.fromTheme("internet-web-browser"), "Ver online")
	recycleAction: QAction | None = menu.addAction(QIcon.fromTheme("user-trash"), "Reciclagem do OneDrive")
	recentMenu: QMenu | None = menu.addMenu(QIcon.fromTheme("view-history"), "Actividade recente")
	menu.addSeparator()
	pauseMenu: QMenu | None = menu.addMenu(QIcon.fromTheme("media-playback-pause"), "Pausar sincronização")
	resumeAction: QAction | None = menu.addAction(QIcon.fromTheme("media-playback-start"), "Retomar sincronização")
	menu.addSeparator()
	openAction: QAction | None = menu.addAction(QIcon.fromTheme("configure"), "Definições")
	quitAction: QAction | None = menu.addAction(QIcon.fromTheme("application-exit"), "Sair")
	if openAction is not None:
		openAction.triggered.connect(showWindow)
	if quitAction is not None:
		quitAction.triggered.connect(qtApp.quit)
	if folderAction is not None:
		folderAction.triggered.connect(lambda: _trayRun(["open-folder"]))
	if searchAction is not None:
		searchAction.triggered.connect(lambda: _trayRun(["dolphin-search"]))
	if recycleAction is not None:
		recycleAction.triggered.connect(lambda: _trayRun(["open-recycle"]))
	if webAction is not None:
		webAction.triggered.connect(lambda: _trayRun(["open-web", "--notify", str(config.mountPath())]))
	if pauseMenu is not None:
		for label, duration in (("2 horas", "2h"), ("8 horas", "8h"), ("24 horas", "24h")):
			pauseItem: QAction | None = pauseMenu.addAction(label)
			if pauseItem is not None:
				pauseItem.triggered.connect(lambda _checked=False, value=duration: _trayRun(["pause", value]))
	if resumeAction is not None:
		resumeAction.triggered.connect(lambda: _trayRun(["resume"]))

	def fillRecent() -> None:
		if recentMenu is None:
			return
		recentMenu.clear()
		entries: list[dict[str, Any]] = readHistory(10)
		if len(entries) == 0:
			empty: QAction | None = recentMenu.addAction("Sem actividade recente")
			if empty is not None:
				empty.setEnabled(False)
			return
		icons: dict[str, str] = {"upload": "cloud-upload", "download": "cloud-download", "error": "emblem-error"}
		for entry in entries:
			detail: str = str(entry.get("detail") or "")
			kind: str = str(entry.get("kind") or "")
			when: str = datetime.fromtimestamp(float(entry.get("time") or 0)).strftime("%H:%M")
			name: str = detail.rsplit("/", 1)[-1] if kind != "error" else detail[:80]
			entryIcon: QIcon = QIcon.fromTheme(icons.get(kind, ""))
			if entryIcon.isNull():
				entryIcon = appIcon
			item: QAction | None = recentMenu.addAction(entryIcon, f"{when}  {name}")
			if item is not None and kind in ("upload", "download"):
				item.triggered.connect(lambda _checked=False, value=detail: _trayRun(["open-folder", value]))

	def syncResumeVisibility() -> None:
		from fastonedrive.synccontrol import load

		paused: bool = load().pausedUntil > time.time()
		if resumeAction is not None:
			resumeAction.setVisible(paused)
		pauseAction: QAction | None = pauseMenu.menuAction() if pauseMenu is not None else None
		if pauseAction is not None:
			pauseAction.setVisible(not paused)

	def beforeShow() -> None:
		fillRecent()
		syncResumeVisibility()

	menu.aboutToShow.connect(beforeShow)
	tray.setContextMenu(menu)

	def onActivated(reason: QSystemTrayIcon.ActivationReason) -> None:
		if reason == QSystemTrayIcon.ActivationReason.Trigger:
			showWindow()

	stateIcons: dict[str, str] = {
		"idle": "fastonedrive",
		"syncing": "folder-sync",
		"error": "dialog-warning",
		"paused": "media-playback-pause",
	}
	lastIcon: list[str] = [""]

	def refreshTip() -> None:
		from fastonedrive.synccontrol import pauseReason

		activity: dict[str, Any] = readActivity()
		state: str = str(activity.get("state") or "idle")
		detail: str = str(activity.get("detail") or "")
		paused: str = pauseReason()
		if paused != "" and state != "error":
			state = "paused"
			detail = paused
		labels: dict[str, str] = {
			"idle": "Actualizado",
			"syncing": "A sincronizar",
			"error": "Erro",
			"paused": "Em pausa",
		}
		tip: str = f"Fast-OneDrive — {labels.get(state, state)}"
		if detail != "":
			tip = f"{tip}\n{detail}"
		tray.setToolTip(tip)
		iconName: str = stateIcons.get(state, "fastonedrive")
		if iconName != lastIcon[0]:
			themed: QIcon = QIcon.fromTheme(iconName) if iconName != "fastonedrive" else appIcon
			if themed.isNull():
				themed = appIcon
			tray.setIcon(themed)
			lastIcon[0] = iconName

	from PyQt6.QtCore import QTimer

	timer: QTimer = QTimer(tray)
	timer.setInterval(2000)
	timer.timeout.connect(refreshTip)
	timer.start()
	refreshTip()
	tray.activated.connect(onActivated)
	tray.show()
	return tray


def _trayRun(args: list[str]) -> None:
	try:
		binary: str = config.cliBin()
	except FastOneDriveError:
		return
	profile: config.Profile | None = config.activeProfile()
	command: list[str] = [binary]
	if profile is not None and args[:1] not in (["pause"], ["resume"]):
		command.extend(["--profile", profile.id])
	subprocess.Popen(
		[*command, *args],
		start_new_session=True,
		stdin=subprocess.DEVNULL,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)


def _formatStamp(value: object) -> str:
	text: str = str(value or "").strip()
	if text == "":
		return "—"
	try:
		stamp: datetime = datetime.fromisoformat(text.replace("Z", "+00:00"))
	except ValueError:
		return text
	return stamp.astimezone().strftime("%d/%m/%Y %H:%M:%S")


def _displayPath(path: Path) -> str:
	home: Path = config.homeDir().resolve()
	try:
		relative: Path = path.expanduser().resolve(strict=False).relative_to(home)
	except ValueError:
		return str(path.expanduser())
	posix: str = relative.as_posix()
	if posix == ".":
		return "~"
	return f"~/{posix}"


def _formatSize(size: int) -> str:
	value: float = float(size)
	units: tuple[str, ...] = ("B", "KiB", "MiB", "GiB", "TiB")
	index: int = 0
	while value >= 1024 and index < len(units) - 1:
		value /= 1024
		index += 1
	if index == 0:
		return f"{int(value)} {units[index]}"
	return f"{value:.1f} {units[index]}"


def _pluginEnv() -> dict[str, str]:
	env: dict[str, str] = os.environ.copy()
	pluginRoot: str = str(Path.home() / ".local" / "lib" / "qt6" / "plugins")
	current: str = env.get("QT_PLUGIN_PATH", "")
	if pluginRoot not in current.split(":"):
		env["QT_PLUGIN_PATH"] = pluginRoot if current == "" else f"{pluginRoot}:{current}"
	env.setdefault("FASTONEDRIVE_BIN", str(Path.home() / ".local" / "bin" / "fastonedrive"))
	return env


def _kioItemUrl(itemPath: str) -> str:
	quoted: str = quote(itemPath, safe="/")
	profile: config.Profile | None = config.activeProfile()
	if profile is None:
		return f"{config.KIO_SCHEME}:/item/{quoted}"
	return f"{config.KIO_SCHEME}:/p/{profile.id}/item/{quoted}"


def _clipboardUrl(entry: dict[str, Any]) -> Any:
	from PyQt6.QtCore import QUrl

	localPath: str = str(entry.get("localPath") or "")
	if localPath != "" and Path(localPath).exists():
		return QUrl.fromLocalFile(localPath)
	return QUrl(_kioItemUrl(str(entry["path"])))


def _locateEntry(entry: dict[str, Any]) -> None:
	localPath: str = str(entry.get("localPath") or "")
	if localPath != "" and Path(localPath).exists():
		_spawnDolphin(["--select", localPath])
		return
	itemPath: str = str(entry["path"])
	if bool(entry["isDir"]):
		_spawnDolphin([_kioItemUrl(itemPath)])
		return
	_spawnDolphin(["--select", _kioItemUrl(itemPath)])


def _spawnDolphin(args: list[str]) -> None:
	binary: str | None = shutil.which("dolphin")
	if binary is None:
		raise FastOneDriveError("dolphin não encontrado")
	subprocess.Popen(
		[binary, *args],
		start_new_session=True,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		env=_pluginEnv(),
	)


def _spawnOpen(path: Path) -> None:
	binary: str | None = shutil.which("xdg-open")
	if binary is None:
		raise FastOneDriveError("xdg-open não encontrado")
	subprocess.Popen(
		[binary, str(path)],
		start_new_session=True,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
