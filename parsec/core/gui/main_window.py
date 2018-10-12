import os

from PyQt5.QtCore import QCoreApplication, Qt
from PyQt5.QtWidgets import QMainWindow, QMessageBox, QSystemTrayIcon, QMenu
from PyQt5.QtGui import QIcon, QFontDatabase

from parsec.core.devices_manager import DeviceLoadingError, DeviceConfigureBackendError

from parsec.core.gui import settings
from parsec.core.gui.core_call import core_call
from parsec.core.gui.login_widget import LoginWidget
from parsec.core.gui.files_widget import FilesWidget
from parsec.core.gui.users_widget import UsersWidget
from parsec.core.gui.settings_widget import SettingsWidget
from parsec.core.gui.devices_widget import DevicesWidget
from parsec.core.gui.ui.main_window import Ui_MainWindow


class MainWindow(QMainWindow, Ui_MainWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setupUi(self)
        self.close_requested = False
        QFontDatabase.addApplicationFont(":/fonts/fonts/ProximaNova.otf")
        self.files_widget = None
        self.settings_widget = None
        self.users_widget = None
        self.tray = None
        self.widget_menu.hide()
        self.login_widget = LoginWidget(parent=self.widget_main)
        for device_name in core_call().get_devices():
            self.login_widget.add_device(device_name)
        self.main_widget_layout.insertWidget(1, self.login_widget)
        self.users_widget = UsersWidget(parent=self.widget_main)
        self.main_widget_layout.insertWidget(1, self.users_widget)
        self.devices_widget = DevicesWidget(parent=self.widget_main)
        self.main_widget_layout.insertWidget(1, self.devices_widget)
        self.settings_widget = SettingsWidget(parent=self.widget_main)
        self.main_widget_layout.insertWidget(1, self.settings_widget)
        self.files_widget = FilesWidget(parent=self.widget_main)
        self.main_widget_layout.insertWidget(1, self.files_widget)
        self.show_login_widget()
        self.current_device = None
        self.add_tray_icon()
        self.connect_all()

    def add_tray_icon(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.action_put_in_tray.setDisabled(True)
            return
        self.tray = QSystemTrayIcon(self)
        menu = QMenu()
        action = menu.addAction(QCoreApplication.translate(self.__class__.__name__, "Show window"))
        action.triggered.connect(self.show)
        action = menu.addAction(QCoreApplication.translate(self.__class__.__name__, "Exit"))
        action.triggered.connect(self.close_app)
        self.tray.setContextMenu(menu)
        self.tray.setIcon(QIcon(":/icons/images/icons/parsec.png"))
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()

    def connect_all(self):
        self.button_files.clicked.connect(self.show_files_widget)
        self.button_users.clicked.connect(self.show_users_widget)
        self.button_settings.clicked.connect(self.show_settings_widget)
        self.button_devices.clicked.connect(self.show_devices_widget)
        self.login_widget.loginClicked.connect(self.login)
        self.login_widget.claimClicked.connect(self.claim_user)
        self.login_widget.configureDeviceClicked.connect(self.configure_device)
        self.button_logout.clicked.connect(self.logout)
        self.users_widget.registerUserClicked.connect(self.register_user)

    def tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show()
            self.raise_()

    def logout(self):
        self.files_widget.set_mountpoint("")
        if core_call().is_mounted():
            core_call().unmount()
        core_call().logout()
        self.login_widget.reset()
        self.users_widget.reset()
        self.files_widget.reset()
        self.show_login_widget()
        device = core_call().load_device("johndoe@test")
        core_call().login(device)
        self.current_device = device
        self.widget_menu.hide()

    def mount(self):
        base_mountpoint = settings.get_value("mountpoint")
        if not base_mountpoint:
            return None
        mountpoint = os.path.join(base_mountpoint, self.current_device.id)
        if core_call().is_mounted():
            core_call().unmount()
        try:
            core_call().mount(mountpoint)
            self.files_widget.set_mountpoint(mountpoint)
            return mountpoint
        except (RuntimeError, PermissionError) as exc:
            import traceback

            traceback.print_exc(exc)
            return None

    def remount(self):
        mountpoint = self.mount()
        if mountpoint is None:
            QMessageBox.warning(
                self,
                QCoreApplication.translate("MainWindow", "Error"),
                QCoreApplication.translate(
                    "MainWindow",
                    'Can not mount in "{}" (permissions problems ?). Go '
                    "to Settings/Global to a set mountpoint, then File/Remount to "
                    "mount it.",
                ).format(settings.get_value("mountpoint")),
            )
            self.show_settings_widget()
            return

        self.files_widget.reset()
        self.files_widget.set_mountpoint(mountpoint)
        self.show_files_widget()

    def perform_login(self, device_id, password):
        try:
            device = core_call().load_device(device_id, password)
            self.current_device = device
            core_call().logout()
            core_call().login(device)
            mountpoint = self.mount()
            if mountpoint is None:
                QMessageBox.warning(
                    self,
                    QCoreApplication.translate("MainWindow", "Error"),
                    QCoreApplication.translate(
                        "MainWindow",
                        'Can not mount in "{}" (permissions problems ?). Go '
                        "to Settings/Global to a set mountpoint, then File/Remount to "
                        "mount it.",
                    ).format(settings.get_value("mountpoint")),
                )
                self.show_settings_widget()
                return
            self.widget_menu.show()
            self.show_files_widget()
        except DeviceLoadingError:
            return QCoreApplication.translate(self.__class__.__name__, "Invalid password")

    def login(self, device_id, password):
        err = self.perform_login(device_id, password)
        if err:
            self.login_widget.set_login_error(err)

    def register_user(self, login):
        try:
            token = core_call().invite_user(login)
            self.users_widget.set_claim_infos(login, token)
        except DeviceConfigureBackendError:
            self.users_widget.set_error(
                QCoreApplication.translate("MainWindow", "Can not register the new user.")
            )

    def claim_user(self, user_id, password, device_name, token):
        try:
            privkey, signkey, manifest = core_call().claim_user(user_id, device_name, token)
            privkey = privkey.encode()
            signkey = signkey.encode()
            device_id = f"{user_id}@{device_name}"
            core_call().register_new_device(device_id, privkey, signkey, manifest, password)
            self.login_widget.add_device(device_id)
            err = self.perform_login(device_id, password)
            if err:
                self.login_widget.set_claim_error(err)
        except Exception as exc:
            # TODO: better error handling
            self.login_widget.set_claim_error(str(exc))

    def configure_device(self, user_id, password, device_name, token):
        try:
            device_id = f"{user_id}@{device_name}"
            try:
                core_call().configure_device(device_id, password, token)
            except Exception as exc:
                # TODO: better error handling
                self.login_widget.set_device_config_error(str(exc))
            self.login_widget.add_device(device_id)
            err = self.perform_login(device_id, password)
            if err:
                self.login_widget.set_device_config_error(err)
        except DeviceLoadingError:
            pass

    def close_app(self):
        self.close_requested = True
        self.close()

    def closeEvent(self, event):
        if (
            not QSystemTrayIcon.isSystemTrayAvailable()
            or self.close_requested
            or core_call().is_debug()
        ):
            result = QMessageBox.question(
                self,
                QCoreApplication.translate(self.__class__.__name__, "Confirmation"),
                QCoreApplication.translate("MainWindow", "Are you sure you want to quit ?"),
            )
            if result != QMessageBox.Yes:
                event.ignore()
                return
            event.accept()
            if core_call().is_mounted():
                core_call().unmount()
            core_call().logout()
            core_call().stop()
            if self.tray:
                self.tray.hide()
        else:
            if self.tray:
                self.tray.showMessage(
                    "Parsec",
                    QCoreApplication.translate(self.__class__.__name__, "Parsec is still running."),
                )
            event.ignore()
            self.hide()

    def show_files_widget(self):
        self._hide_all_central_widgets()
        self.button_files.setChecked(True)
        self.files_widget.show()

    def show_users_widget(self):
        self._hide_all_central_widgets()
        self.button_users.setChecked(True)
        self.users_widget.show()

    def show_devices_widget(self):
        self._hide_all_central_widgets()
        self.button_devices.setChecked(True)
        self.devices_widget.show()

    def show_settings_widget(self):
        self._hide_all_central_widgets()
        self.button_settings.setChecked(True)
        self.settings_widget.show()

    def show_login_widget(self):
        self._hide_all_central_widgets()
        self.login_widget.show()

    def _hide_all_central_widgets(self):
        self.files_widget.hide()
        self.users_widget.hide()
        self.settings_widget.hide()
        self.login_widget.hide()
        self.devices_widget.hide()
        self.button_files.setChecked(False)
        self.button_users.setChecked(False)
        self.button_settings.setChecked(False)
        self.button_devices.setChecked(False)
