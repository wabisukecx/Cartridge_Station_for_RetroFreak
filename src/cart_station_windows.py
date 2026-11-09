"""Optional PyQt6 desktop frontend. No reader is opened until Connect is chosen."""
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, QTimer, QUrl, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    QAbstractItemView,
)

from cart_station_paths import ROOT
from cart_station_branding import PRODUCT_NAME, WINDOWS_EDITION
from cart_station_protocol import ProtocolError
from cart_station_environment import default_output_directory
from cart_station_service import Service, UserCancelled
from cart_station_presentation import profile_details, profile_columns


class ReaderWorker(QObject):
    succeeded = pyqtSignal(str, object)
    failed = pyqtSignal(str, str, bool)
    progress = pyqtSignal(str, int)

    def __init__(self, service_factory=Service):
        super().__init__()
        self.factory = service_factory
        self.service = None
        self.cancelled = threading.Event()

    def report(self, stage, done, total):
        if self.cancelled.is_set():
            raise UserCancelled('Stopped at a safe boundary. No new ROM saved.')
        self.progress.emit(stage, int(done*100/total) if total else 0)

    @pyqtSlot(str, object)
    def execute(self, task, values):
        try:
            if task == 'connect':
                self.service = self.factory(**values)
                self.service.identification_progress = self.report
                result = self.service.poll()
            elif task == 'disconnect':
                service, self.service = self.service, None
                if service is not None:
                    service.close()
                result = None
            elif self.service is None:
                raise ProtocolError('Connect the reader first')
            elif task == 'poll':
                result = self.service.poll()
            elif task == 'select':
                result = self.service.select_profile(values)
            elif task == 'capture':
                path, created = self.service.capture(self.report)
                result = (path, created, self.service.snapshot('Verified capture'))
            else:
                raise ValueError('Unknown reader operation')
            self.succeeded.emit(task, result)
        except Exception as error:
            if self.service is not None:
                try:
                    self.service.close()
                except Exception:
                    pass
                self.service = None
            self.failed.emit(task, str(error), isinstance(error, UserCancelled))


class CartStationWindow(QMainWindow):
    requested = pyqtSignal(str, object)

    def __init__(self, destination=None, port=None, db_path=None,
                 service_factory=Service):
        super().__init__()
        self.setWindowTitle(PRODUCT_NAME + ' — ' + WINDOWS_EDITION)
        self.resize(940, 780)
        self.setMinimumSize(760, 650)
        self.state = None
        self.connected = False
        self.task = ''
        self.confirming = False
        self.closing = False
        self.allow_close = False
        self.paths_config = None
        self.custom_destination = destination is not None
        self.saved_path = None
        self.pending_selection = None
        self.pending_disconnect = False
        self.identifying = False
        self._build_ui(destination or default_output_directory('pc'), db_path)
        self.refresh_ports()
        if port:
            index = self.port.findData(port)
            if index < 0:
                self.port.addItem(port, port)
                index = self.port.count() - 1
            self.port.setCurrentIndex(index)
        self.thread = QThread(self)
        self.worker = ReaderWorker(service_factory)
        self.worker.moveToThread(self.thread)
        self.requested.connect(self.worker.execute)
        self.worker.succeeded.connect(self._succeeded)
        self.worker.failed.connect(self._failed)
        self.worker.progress.connect(self._progress)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self._thread_stopped)
        self.thread.start()
        self.timer = QTimer(self)
        self.timer.setInterval(600)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self._enabled()

    def _build_ui(self, destination, db_path):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
        title = QLabel(PRODUCT_NAME)
        title.setObjectName('title')
        layout.addWidget(title)
        layout.addWidget(QLabel(WINDOWS_EDITION + '  /  FC · NES* · GB · GBC · GBA · MD · PCE · SGX · SFC cartridge import'))
        cartridge_note = QLabel(
            '* NES cartridges require an NES cartridge converter to connect to the RetroFreak cartridge adapter.')
        cartridge_note.setWordWrap(True)
        layout.addWidget(cartridge_note)
        connection = QGroupBox('Reader && destination')
        form = QFormLayout(connection)
        row = QHBoxLayout()
        self.port = QComboBox()
        self.connection_value = QLabel()
        self.connection_value.setWordWrap(True)
        self.refresh = QPushButton('Refresh ports')
        self.connect_button = QPushButton('Connect')
        row.addWidget(self.port, 1)
        row.addWidget(self.connection_value, 1)
        row.addWidget(self.refresh)
        row.addWidget(self.connect_button)
        form.addRow('Connection', row)
        self.destination = QLineEdit(str(destination))
        self.destination.setReadOnly(True)
        self.browse = QPushButton('Choose folder…')
        row = QHBoxLayout()
        row.addWidget(self.destination, 1)
        row.addWidget(self.browse)
        form.addRow('Save to', row)
        self.database = QLineEdit(str(db_path) if db_path else '')
        self.database.setPlaceholderText('Included NES database (automatic)')
        self.database.setReadOnly(True)
        self.database.setToolTip('No database download is required. A local nes_profiles.json overrides the included database.')
        self.database_value = QLabel()
        self.database_value.setWordWrap(True)
        self.db_browse = QPushButton('Custom DB…')
        self.db_reset = QPushButton('Use default DB')
        row = QHBoxLayout()
        row.addWidget(self.database, 1)
        row.addWidget(self.database_value, 1)
        row.addWidget(self.db_browse)
        row.addWidget(self.db_reset)
        form.addRow('Database', row)
        self.settings_hint = QLabel()
        self.settings_hint.setWordWrap(True)
        form.addRow(self.settings_hint)
        layout.addWidget(connection)
        self.status = QLabel('Connect your cartridge reader to begin.')
        self.status.setWordWrap(True)
        self.status.setObjectName('status')
        layout.addWidget(self.status)
        self.title = QLabel('No cartridge selected')
        self.title.setWordWrap(True)
        self.title.setObjectName('gameTitle')
        layout.addWidget(self.title)
        self.details = QLabel('Insert a cartridge after connecting. Reading starts only after confirmation.')
        self.details.setWordWrap(True)
        layout.addWidget(self.details)
        self.candidates = QTableWidget(0, 5)
        self.candidates.setHorizontalHeaderLabels(('Title', 'Mapper', 'PRG', 'CHR', 'Support'))
        self.candidates.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            self.candidates.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.candidates.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.candidates.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.candidates.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.candidates.verticalHeader().hide()
        self.candidates.itemSelectionChanged.connect(self.select_candidate)
        layout.addWidget(self.candidates, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        row = QHBoxLayout()
        self.dump = QPushButton('Read cartridge…')
        self.dump.setObjectName('primary')
        self.cancel = QPushButton('Cancel reading')
        self.open_folder = QPushButton('Open save folder')
        row.addWidget(self.dump)
        row.addWidget(self.cancel)
        row.addStretch()
        row.addWidget(self.open_folder)
        layout.addLayout(row)
        self.result = QLabel('Two independent reads and database hashes are checked before saving.')
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.result)
        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setMaximumBlockCount(200)
        self.activity.setMaximumHeight(95)
        self.activity.setPlaceholderText('Activity')
        layout.addWidget(self.activity)
        for label in root.findChildren(QLabel):
            label.setTextFormat(Qt.TextFormat.PlainText)
        self.refresh.clicked.connect(self.refresh_ports)
        self.connect_button.clicked.connect(self.toggle_connection)
        self.browse.clicked.connect(self.choose_destination)
        self.db_browse.clicked.connect(self.choose_database)
        self.db_reset.clicked.connect(self.database.clear)
        self.dump.clicked.connect(self.confirm_capture)
        self.cancel.clicked.connect(self.cancel_capture)
        self.open_folder.clicked.connect(self.show_folder)
        self.setStyleSheet('''
            QWidget { font-family: "Segoe UI"; font-size: 10pt; color: #253347; }
            QMainWindow { background: #f5f7fa; }
            QLabel#title { font-size: 24pt; font-weight: 650; color: #12263b; }
            QLabel#gameTitle { font-size: 16pt; font-weight: 600; }
            QLabel#status { background: #e8eff5; padding: 12px; border-radius: 6px; }
            QGroupBox { font-weight: 600; border: 1px solid #d5dee8;
                        border-radius: 6px; margin-top: 10px; padding: 14px 10px 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; }
            QLineEdit, QComboBox, QPlainTextEdit, QTableWidget {
                background: white; border: 1px solid #d5dee8; border-radius: 4px; padding: 5px; }
            QTableWidget { outline: 0; }
            QTableWidget:focus { border-color: #166d73; }
            QTableWidget::item { padding: 5px; border: none; }
            QTableWidget::item:selected { background: #dceff0; color: #143e44; }
            QTableWidget::item:selected:!active { background: #dceff0; color: #143e44; }
            QPushButton { background: white; border: 1px solid #c7d2df;
                          border-radius: 5px; padding: 8px 14px; }
            QPushButton:hover { background: #edf3f8; }
            QPushButton#primary { background: #166d73; color: white; border: none; }
            QPushButton:disabled, QPushButton#primary:disabled { background: #e9edf1; color: #8491a2; }
            QProgressBar { border: 1px solid #d5dee8; border-radius: 4px;
                           background: white; text-align: center; height: 19px; }
            QProgressBar::chunk { background: #248a8c; }
        ''')

    def refresh_ports(self):
        from serial.tools import list_ports
        previous = self.port.currentData()
        self.port.clear()
        self.port.addItem('Auto-detect RetroFreak reader', None)
        for port in list_ports.comports():
            self.port.addItem('%s — %s' % (port.device, port.description), port.device)
        index = self.port.findData(previous)
        self.port.setCurrentIndex(max(index, 0))

    def choose_destination(self):
        value = QFileDialog.getExistingDirectory(self, 'Save ROMs to', self.destination.text())
        if value:
            self.destination.setText(value)
            self.custom_destination = True

    def choose_database(self):
        value, _ = QFileDialog.getOpenFileName(self, 'Choose NES database', '', 'JSON files (*.json)')
        if value:
            self.database.setText(value)

    def _send(self, task, values=None):
        if self.task:
            return False
        self.task = task
        self.identifying = False
        if task in ('capture', 'connect'):
            self.worker.cancelled.clear()
            self.progress.setValue(0)
        self._enabled()
        self.requested.emit(task, values)
        return True

    def toggle_connection(self):
        if self.connected:
            if self.task in ('poll', 'select'):
                self.pending_disconnect = True
                if self.identifying:
                    self.worker.cancelled.set()
                self.status.setText('Disconnecting after the current reader check…')
                self._enabled()
            else:
                self._send('disconnect')
        else:
            selected = self.port.currentData()
            self._send('connect', dict(destination=Path(self.destination.text()),
                                      port=selected, usb=False, environment='pc',
                                      paths_config=self.paths_config, follow_system_paths=not self.custom_destination,
                                      db_path=Path(self.database.text()) if self.database.text() else None,
                                      metrics_path=ROOT / 'runtime/last_capture_metrics.json'))

    def poll(self):
        if self.connected and not self.task and not self.confirming and not self.closing:
            self._send('poll')

    def select_candidate(self):
        if self.connected and not self.closing and self.state:
            row = self.candidates.currentRow()
            if 0 <= row < len(self.state.candidates):
                identifier = self.state.candidates[row].id
                if self.task in ('poll', 'select'):
                    # Keep keyboard/mouse focus during read-only polling; apply
                    # the latest choice when the single I/O worker is free.
                    self.pending_selection = identifier
                elif not self.task:
                    self._send('select', identifier)

    def confirm_capture(self):
        if not self.state or not (self.state.supported or self.state.auto) or self.task:
            return
        name = ('Auto-detect among %d candidates' % self.state.auto if self.state.auto
                else self.state.profile.title)
        self.confirming = True
        try:
            answer = QMessageBox.question(
                self, 'Read cartridge?', '%s\n\nRead twice, verify, and save to:\n%s' %
                (name, self.destination.text()),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
        finally:
            self.confirming = False
        if answer == QMessageBox.StandardButton.Yes:
            self._send('capture')

    def cancel_capture(self):
        self.worker.cancelled.set()
        self.status.setText('Stopping at a safe boundary. A save already in progress will finish first.')
        self.cancel.setEnabled(False)

    def show_folder(self):
        directory = Path(self.destination.text())
        if directory.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory.resolve())))
        else:
            self.result.setText('The save folder will be created when the first ROM is saved.')

    def _apply_state(self, state):
        self.state = state
        if getattr(state, 'destination', None) is not None:
            self.destination.setText(str(state.destination))
        self.status.setText(state.cartridge)
        if state.auto:
            self.title.setText('Auto-detect among %d candidates' % state.auto)
            self.details.setText('The complete ROM hashes will determine the title after reading.')
        elif state.profile and state.candidates and not state.selection_pending:
            profile = state.profile
            self.title.setText(profile.title)
            self.details.setText(profile.support() or profile_details(profile))
        else:
            self.title.setText('Select the cartridge you inserted' if state.candidates else 'No cartridge selected')
            self.details.setText('Select a DB candidate below.' if state.candidates else 'Waiting for a DB match.')
        ids = tuple(profile.id for profile in state.candidates)
        old_ids = getattr(self, '_candidate_ids', ())
        self.candidates.blockSignals(True)
        try:
            records = tuple((p.id, p.title, getattr(p, 'raw_sha1', '')) for p in state.candidates)
            if ids != old_ids or records != getattr(self, '_candidate_records', ()):
                self._candidate_records = records
                is_gb = bool(state.candidates and getattr(state.candidates[0], 'system', 'NES') != 'NES')
                self.candidates.setHorizontalHeaderLabels(('Title', 'Controller', 'ROM', 'System', 'Support') if is_gb else ('Title', 'Mapper', 'PRG', 'CHR', 'Support'))
                self.candidates.setRowCount(len(ids))
                for row, profile in enumerate(state.candidates):
                    values = (profile.title, *profile_columns(profile),
                              'Supported' if profile.support() is None else 'Not supported')
                    for column, value in enumerate(values):
                        item = QTableWidgetItem(value)
                        item.setToolTip(profile.support() or profile.title)
                        self.candidates.setItem(row, column, item)
                self._candidate_ids = ids
            selected = self.pending_selection
            if selected not in ids:
                selected = state.profile.id if state.profile and not state.auto else None
            if selected in ids:
                row = ids.index(selected)
                if (self.candidates.currentRow() != row
                        or not self.candidates.selectionModel().hasSelection()):
                    self.candidates.selectRow(row)
            elif self.candidates.selectionModel().hasSelection():
                self.candidates.clearSelection()
        finally:
            self.candidates.blockSignals(False)

    @pyqtSlot(str, object)
    def _succeeded(self, task, result):
        self.task = ''
        was_identifying, self.identifying = self.identifying, False
        if task == 'disconnect':
            self.pending_selection = None
            self.pending_disconnect = False
            self.connected = False
            self.state = None
            self.candidates.setRowCount(0)
            self._candidate_ids = ()
            self.title.setText('No cartridge selected')
            self.details.setText('Connect your reader to continue.')
            self.status.setText('Disconnected')
        elif task == 'capture':
            path, created, state = result
            self.saved_path = path
            self._apply_state(state)
            self.progress.setValue(100)
            message = ('Saved and verified: ' if created else 'Already saved; identical ROM verified: ') + str(path)
            if '.verified-' in path.name:
                message += '\nThe existing file was preserved in place.'
            self.result.setText(message)
            self.activity.appendPlainText(message)
        else:
            self.connected = True
            self._apply_state(result)
            if was_identifying:
                self.progress.setValue(0)
                self.result.setText('Title identified. No new ROM saved. Choose Read cartridge to import.')
            if task == 'connect':
                self.activity.appendPlainText('Reader connected. Waiting for a stable cartridge state.')
        self._enabled()
        if self.closing:
            if task == 'disconnect':
                self.thread.quit()
            else:
                self._send('disconnect')
        elif self.pending_disconnect:
            self.pending_disconnect = False
            self.pending_selection = None
            self._send('disconnect')
        elif self.pending_selection is not None:
            identifier, self.pending_selection = self.pending_selection, None
            if (self.connected and self.state and identifier in self._candidate_ids
                    and (not self.state.profile or self.state.profile.id != identifier)):
                self._send('select', identifier)

    @pyqtSlot(str, str, bool)
    def _failed(self, task, message, cancelled):
        self.task = ''
        self.identifying = False
        self.connected = False
        self.pending_selection = None
        self.pending_disconnect = False
        self.state = None
        self.status.setText(
            'Stopped safely — click Connect to continue.' if cancelled else
            'Reader session closed — check cartridge/USB connection, then click Connect.'
        )
        self.result.setText(message)
        self.activity.appendPlainText(message)
        self._enabled()
        if self.closing:
            self.thread.quit()

    @pyqtSlot(str, int)
    def _progress(self, stage, percent):
        if self.task in ('poll', 'connect'):
            self.identifying = True
            if self.closing or self.pending_disconnect:
                self.worker.cancelled.set()
            self._enabled()
        self.status.setText(stage)
        self.progress.setValue(max(0, min(100, percent)))

    def _enabled(self):
        idle = not self.task and not self.closing
        self.connect_button.setText('Disconnect' if self.connected else 'Connect')
        self.connect_button.setEnabled(not self.closing and not self.pending_disconnect
                                       and self.task in ('', 'poll', 'select'))
        self.port.setVisible(not self.connected)
        self.connection_value.setVisible(self.connected)
        self.connection_value.setText(self.port.currentText())
        self.refresh.setVisible(not self.connected)
        self.database.setVisible(not self.connected)
        self.database_value.setVisible(self.connected)
        self.database_value.setText(self.database.text() or 'Standard database (automatic)')
        for widget in (self.browse, self.db_browse, self.db_reset):
            widget.setVisible(not self.connected)
        self.settings_hint.setText('Disconnect to change the connection, save folder, or database.'
                                   if self.connected else
                                   'Use the included database, or choose Custom DB before connecting.')
        for widget in (self.port, self.refresh, self.browse, self.db_browse, self.db_reset):
            widget.setEnabled(idle and not self.connected)
        self.candidates.setEnabled(self.connected and not self.closing
                                   and self.task in ('', 'poll', 'select'))
        self.dump.setEnabled(bool(idle and self.connected and self.state and
                                  (self.state.supported or self.state.auto)))
        self.cancel.setEnabled((self.task == 'capture' or self.identifying) and not self.worker.cancelled.is_set())
        self.open_folder.setEnabled(idle)

    @pyqtSlot()
    def _thread_stopped(self):
        self.allow_close = True
        self.close()

    def closeEvent(self, event):
        if self.allow_close:
            event.accept()
            return
        event.ignore()
        if self.closing:
            return
        if self.task == 'capture':
            answer = QMessageBox.question(
                self, 'Close while reading?', 'Stop at a safe boundary and close?\nA save already in progress will finish first.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.cancel_capture()
        self.closing = True
        if self.identifying:
            self.worker.cancelled.set()
        self.timer.stop()
        self._enabled()
        if not self.task:
            self._send('disconnect')


def run_windows(args):
    application = QApplication.instance() or QApplication([sys.argv[0]])
    application.setApplicationName(PRODUCT_NAME)
    destination = getattr(args, 'output_dir', None) or default_output_directory(
        'pc', config_path=getattr(args, 'paths_config', None))
    window = CartStationWindow(destination=destination, port=args.port,
                               db_path=getattr(args, 'db', None))
    window.paths_config = getattr(args, 'paths_config', None)
    window.custom_destination = getattr(args, 'output_dir', None) is not None
    window.show()
    return application.exec()
