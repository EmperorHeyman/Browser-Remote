"""
Seamless browser remote - Desktop Launcher (PyQt6)
A GUI to configure, launch, and manage the browser remote server.
Features: system tray, QR code pairing, %appdata% config, browser restart.
"""

import json
import os
import signal
import socket
import subprocess
import sys
from io import BytesIO
from pathlib import Path

from PyQt6.QtCore import Qt, QProcess, QTimer, QSettings, QSize
from PyQt6.QtGui import (
    QFont, QColor, QIcon, QPalette, QPixmap, QPainter, QImage, QAction,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QSpinBox, QLineEdit, QGroupBox,
    QTextEdit, QFormLayout, QMessageBox, QSystemTrayIcon, QMenu,
    QFrame, QSplitter, QStackedWidget, QSpacerItem, QSizePolicy,
    QCheckBox, QFileDialog, QDoubleSpinBox, QDialog, QScrollArea,
)

import config  # our %appdata% config module

# ---------------------------------------------------------------------------
# Detect installed browsers
# ---------------------------------------------------------------------------
BROWSER_CANDIDATES = {
    "Brave": [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    ],
    "Chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "Edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
}

USER_DATA_DIRS = {
    "Brave": os.path.join(os.environ.get("LOCALAPPDATA", ""), "BraveSoftware", "Brave-Browser", "User Data"),
    "Chrome": os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data"),
    "Edge": os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "User Data"),
}


def find_installed_browsers() -> dict[str, str]:
    found = {}
    for name, paths in BROWSER_CANDIDATES.items():
        for p in paths:
            if os.path.isfile(p):
                found[name] = p
                break
    return found


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def detect_monitors() -> list[dict]:
    """Detect monitors via ctypes (Windows)."""
    monitors = []
    try:
        import ctypes
        import ctypes.wintypes

        MONITOR_ENUM_PROC = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.POINTER(ctypes.wintypes.RECT), ctypes.c_double,
        )

        def callback(hMonitor, hdcMonitor, lprcMonitor, dwData):
            r = lprcMonitor.contents
            monitors.append({
                "left": r.left, "top": r.top,
                "width": r.right - r.left, "height": r.bottom - r.top,
            })
            return 1

        ctypes.windll.user32.EnumDisplayMonitors(
            None, None, MONITOR_ENUM_PROC(callback), 0
        )
    except Exception:
        pass
    return monitors


# ---------------------------------------------------------------------------
# QR Code generator (pure Python, no Pillow needed)
# ---------------------------------------------------------------------------
def _ensure_qrcode():
    """Install qrcode package if not available."""
    try:
        import qrcode  # noqa: F401
    except ImportError:
        if getattr(sys, 'frozen', False):
            return  # Frozen exe — qrcode must be bundled at build time
        import subprocess as _sp
        _sp.check_call([sys.executable, "-m", "pip", "install", "--quiet", "qrcode[pil]"])

_ensure_qrcode()

# ---------------------------------------------------------------------------
# Simple i18n
# ---------------------------------------------------------------------------
TRANSLATIONS = {
    "en": {
        "title": "browser remote",
        "subtitle": "Control your projector browser from your phone",
        "browser": "Browser", "path": "Path", "profile": "Profile",
        "url": "URL", "cdp_port": "CDP Port", "server_port": "Server Port",
        "browser_display": "Browser Display", "identify": "Identify",
        "server_only": "Server only (don't launch browser)",
        "autostart": "Start with Windows",
        "minimize_tray": "Minimize to tray on start",
        "start_server": "Start Server", "stop_server": "Stop Server",
        "show_logs": "Show Logs", "hide_logs": "Hide Logs",
        "export_logs": "Export", "server_running": "Server Running",
        "advanced": "Advanced Settings",
        "cursor_delay": "Cursor Hide Delay (s)",
        "cursor_sens": "Cursor Sensitivity",
        "scroll_speed": "Scroll Speed",
        "startup_urls": "Startup URLs (one per line)",
        "import_cfg": "Import Config", "export_cfg": "Export Config",
        "welcome": "Welcome! Configure your browser and settings, then click Start.",
    },
    "cs": {
        "title": "browser remote",
        "subtitle": "Ovládejte prohlížeč projektoru z telefonu",
        "browser": "Prohlížeč", "path": "Cesta", "profile": "Profil",
        "url": "URL", "cdp_port": "CDP Port", "server_port": "Port serveru",
        "browser_display": "Displej prohlížeče", "identify": "Identifikovat",
        "server_only": "Pouze server (nespouštět prohlížeč)",
        "autostart": "Spustit s Windows",
        "minimize_tray": "Minimalizovat do lišty",
        "start_server": "Spustit server", "stop_server": "Zastavit server",
        "show_logs": "Zobrazit logy", "hide_logs": "Skrýt logy",
        "export_logs": "Export", "server_running": "Server běží",
        "advanced": "Pokročilé nastavení",
        "cursor_delay": "Prodleva skrytí kurzoru (s)",
        "cursor_sens": "Citlivost kurzoru",
        "scroll_speed": "Rychlost posuvu",
        "startup_urls": "URL při startu (jedno na řádek)",
        "import_cfg": "Importovat nastavení", "export_cfg": "Exportovat nastavení",
        "welcome": "Vítejte! Nastavte prohlížeč a klikněte na Start.",
    },
}

def _t(key: str) -> str:
    lang = config.load().get("language", "en")
    return TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(key, TRANSLATIONS["en"].get(key, key))


def _base_dir() -> str:
    """Return the base directory for user-facing files (SSL certs, etc.).
    In a PyInstaller exe this is the folder containing the .exe;
    during development it is the source directory."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _find_ssl_files() -> tuple[str | None, str | None]:
    """Locate key.pem and cert.pem, checking multiple directories.

    Search order (first match wins):
      1. Next to the .exe  (user can drop custom certs beside the exe)
      2. sys._MEIPASS      (bundled by PyInstaller into _internal/)
      3. Source directory   (development mode)
    Returns (key_path, cert_path) or (None, None) if not found.
    """
    search_dirs = []
    if getattr(sys, 'frozen', False):
        search_dirs.append(os.path.dirname(sys.executable))      # next to exe
        search_dirs.append(getattr(sys, '_MEIPASS', ''))          # _internal/
    else:
        search_dirs.append(os.path.dirname(os.path.abspath(__file__)))  # source dir

    for d in search_dirs:
        if not d:
            continue
        key = os.path.join(d, 'key.pem')
        cert = os.path.join(d, 'cert.pem')
        if os.path.isfile(key) and os.path.isfile(cert):
            return key, cert
    return None, None


# ---------------------------------------------------------------------------
# Windows auto-start helpers (Startup folder shortcut)
# ---------------------------------------------------------------------------
_STARTUP_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
)
_SHORTCUT_NAME = "BrowserRemote.lnk"


def _shortcut_path() -> str:
    return os.path.join(_STARTUP_DIR, _SHORTCUT_NAME)


def _is_autostart_enabled() -> bool:
    return os.path.isfile(_shortcut_path())


def _set_autostart(enable: bool):
    """Create or remove a .lnk shortcut in the Windows Startup folder."""
    link = _shortcut_path()
    if enable:
        try:
            import winreg  # noqa: F401 – just to confirm Windows
            # Use PowerShell to create the shortcut (avoids pywin32 dependency)
            if getattr(sys, 'frozen', False):
                target = sys.executable  # the compiled .exe
            else:
                target = sys.executable  # python.exe
            args_str = "" if getattr(sys, 'frozen', False) else f'"{os.path.abspath(__file__)}"'
            work_dir = os.path.dirname(target)

            ps_script = (
                f'$ws = New-Object -ComObject WScript.Shell; '
                f'$sc = $ws.CreateShortcut("{link}"); '
                f'$sc.TargetPath = "{target}"; '
                f'$sc.Arguments = "{args_str}"; '
                f'$sc.WorkingDirectory = "{work_dir}"; '
                f'$sc.Description = "Browser Remote"; '
                f'$sc.Save()'
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=10,
            )
        except Exception as e:
            print(f"Failed to create startup shortcut: {e}")
    else:
        try:
            if os.path.isfile(link):
                os.remove(link)
        except Exception as e:
            print(f"Failed to remove startup shortcut: {e}")


def generate_qr_pixmap(data_str: str, size: int = 180) -> QPixmap:
    """Generate a QR code as a QPixmap. Falls back to text if qrcode is missing."""
    try:
        import qrcode
        qr = qrcode.QRCode(box_size=1, border=2)
        qr.add_data(data_str)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        rows = len(matrix)
        cols = len(matrix[0]) if matrix else 0
        img = QImage(cols, rows, QImage.Format.Format_Mono)
        img.fill(1)  # white
        for y, row in enumerate(matrix):
            for x, val in enumerate(row):
                if val:
                    img.setPixel(x, y, 0)  # black
        return QPixmap.fromImage(img).scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
    except ImportError:
        # Fallback: plain text label
        pixmap = QPixmap(size, size)
        pixmap.fill(QColor("#1e1e35"))
        painter = QPainter(pixmap)
        painter.setPen(QColor("#888"))
        painter.setFont(QFont("Segoe UI", 9))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter,
                         f"Install 'qrcode' for\nQR pairing\n\n{data_str}")
        painter.end()
        return pixmap


# ---------------------------------------------------------------------------
# Monitor Identification Overlays
# ---------------------------------------------------------------------------
class MonitorOverlay(QWidget):
    """Temporary fullscreen overlay showing a monitor number."""

    def __init__(self, monitor_index: int, geometry: dict, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.monitor_index = monitor_index
        self.setGeometry(
            geometry["left"], geometry["top"],
            geometry["width"], geometry["height"],
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Semi-transparent dark background
        painter.fillRect(self.rect(), QColor(10, 10, 10, 180))
        # Large circle in center
        cx, cy = self.width() // 2, self.height() // 2
        radius = min(self.width(), self.height()) // 5
        painter.setBrush(QColor("#e94560"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(cx - radius, cy - radius, radius * 2, radius * 2)
        # Monitor number
        font = QFont("Segoe UI", radius)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, str(self.monitor_index))
        # Label below
        small_font = QFont("Segoe UI", max(16, radius // 4))
        painter.setFont(small_font)
        painter.setPen(QColor("#4ecca3"))
        label_rect = self.rect().adjusted(0, radius + 20, 0, 0)
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                         f"Monitor {self.monitor_index}")
        painter.end()


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------
class LauncherWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("browser remote")
        self.setMinimumSize(640, 700)
        self.resize(720, 860)

        self.settings = QSettings("ProjectorRemote", "Launcher")
        self.browsers = find_installed_browsers()
        self.monitors = detect_monitors()
        self.server_proc = None
        self.running = False
        self.first_launch = not os.path.isfile(config.CONFIG_PATH)

        self._build_ui()
        self._load_settings()
        self._setup_tray()

        # Status timer
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._update_status)
        self.status_timer.start(2000)

        # First launch: show welcome
        if self.first_launch:
            QTimer.singleShot(200, self._show_first_launch)

    # ----- System Tray -----
    def _setup_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self._make_tray_icon())
        self.tray_icon.setToolTip("browser remote")

        tray_menu = QMenu()
        show_act = QAction("Show Window", self)
        show_act.triggered.connect(self._show_from_tray)
        tray_menu.addAction(show_act)
        tray_menu.addSeparator()

        # Quick-launch sites submenu
        sites_menu = QMenu("Quick Launch", tray_menu)
        cfg = config.load()
        for name, url in cfg.get("sites", {}).items():
            act = QAction(name.capitalize(), self)
            act.setData(url)
            act.triggered.connect(lambda checked, u=url: self._tray_open_site(u))
            sites_menu.addAction(act)
        if cfg.get("favorites"):
            sites_menu.addSeparator()
            for fav in cfg.get("favorites", []):
                act = QAction(fav.get("name", fav.get("url", "")), self)
                act.triggered.connect(lambda checked, u=fav["url"]: self._tray_open_site(u))
                sites_menu.addAction(act)
        tray_menu.addMenu(sites_menu)

        restart_act = QAction("Restart Browser", self)
        restart_act.triggered.connect(self._restart_browser)
        tray_menu.addAction(restart_act)
        tray_menu.addSeparator()
        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(self._quit_app)
        tray_menu.addAction(quit_act)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _make_tray_icon(self) -> QIcon:
        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#e94560"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(2, 2, 28, 28)
        painter.setBrush(QColor("#4ecca3"))
        painter.drawEllipse(10, 10, 12, 12)
        painter.end()
        return QIcon(pixmap)

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_from_tray()

    def _show_from_tray(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _restart_browser(self):
        """Restart browser via server's /api/start endpoint."""
        if not self.running:
            self._log("Server not running — start it first.", "#e94560")
            return
        import urllib.request
        port = self.server_port_spin.value()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/start", method="POST",
                headers={"Content-Type": "application/json"}, data=b"",
            )
            urllib.request.urlopen(req, timeout=10)
            self._log("Browser restart requested.", "#4ecca3")
            self.tray_icon.showMessage(
                "browser remote", "Browser restarting...",
                QSystemTrayIcon.MessageIcon.Information, 2000,
            )
        except Exception as e:
            self._log(f"Restart failed: {e}", "#e94560")

    def _quit_app(self):
        self._stop()
        QApplication.instance().quit()

    # ----- Build UI -----
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # --- 1. Header ---
        header_widget = QWidget()
        header_layout = QVBoxLayout(header_widget)
        header_layout.setContentsMargins(20, 16, 20, 10)
        header_layout.setSpacing(2)
        
        title = QLabel("browser remote")
        title.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color: #e94560;")
        header_layout.addWidget(title)

        subtitle = QLabel("Control your projector browser from your phone")
        subtitle.setFont(QFont("Segoe UI", 11))
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet("color: #888888;")
        header_layout.addWidget(subtitle)
        
        main_layout.addWidget(header_widget)

        # --- 2. Core Stack ---
        self.stack = QStackedWidget()
        main_layout.addWidget(self.stack, 1) # stretch=1

        # Page 1: Setup State (scrollable)
        self.page_setup = QWidget()
        setup_outer_layout = QVBoxLayout(self.page_setup)
        setup_outer_layout.setContentsMargins(0, 0, 0, 0)

        setup_scroll = QScrollArea()
        setup_scroll.setWidgetResizable(True)
        setup_scroll.setFrameShape(QFrame.Shape.NoFrame)
        setup_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        setup_scroll_content = QWidget()
        setup_layout = QVBoxLayout(setup_scroll_content)
        setup_layout.setContentsMargins(30, 10, 30, 20)
        
        setup_card = QFrame()
        setup_card.setObjectName("Card")
        setup_card_layout = QVBoxLayout(setup_card)
        setup_card_layout.setContentsMargins(24, 20, 24, 20)
        setup_card_layout.setSpacing(12)

        # Helper to create labeled inputs
        def make_field(label_text, widget):
            container = QWidget()
            l = QVBoxLayout(container)
            l.setContentsMargins(0, 0, 0, 0)
            l.setSpacing(4)
            lbl = QLabel(label_text.upper())
            lbl.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            lbl.setStyleSheet("color: #777777; letter-spacing: 1px;")
            l.addWidget(lbl)
            l.addWidget(widget)
            return container

        self.browser_combo = QComboBox()
        for name in self.browsers:
            self.browser_combo.addItem(name, self.browsers[name])
        if not self.browsers:
            self.browser_combo.addItem("(No browser found)")
        self.browser_combo.currentIndexChanged.connect(self._on_browser_changed)
        setup_card_layout.addWidget(make_field("Browser", self.browser_combo))

        self.browser_path_edit = QLineEdit()
        self.browser_path_edit.setPlaceholderText("Path to browser executable")
        if self.browsers:
            self.browser_path_edit.setText(list(self.browsers.values())[0])
        setup_card_layout.addWidget(make_field("Path", self.browser_path_edit))

        self.profile_combo = QComboBox()
        self.profile_combo.addItem("Default (browser's own profile)")
        self.profile_combo.addItem("Separate (isolated profile)")
        setup_card_layout.addWidget(make_field("Profile", self.profile_combo))

        self.default_url_edit = QLineEdit("https://www.youtube.com/tv")
        setup_card_layout.addWidget(make_field("URL", self.default_url_edit))

        ports_widget = QWidget()
        ports_layout = QHBoxLayout(ports_widget)
        ports_layout.setContentsMargins(0, 0, 0, 0)
        ports_layout.setSpacing(12)
        
        self.cdp_port_spin = QSpinBox()
        self.cdp_port_spin.setRange(1024, 65535)
        self.cdp_port_spin.setValue(9222)
        ports_layout.addWidget(make_field("CDP Port", self.cdp_port_spin))

        self.server_port_spin = QSpinBox()
        self.server_port_spin.setRange(1024, 65535)
        self.server_port_spin.setValue(5000)
        ports_layout.addWidget(make_field("Server Port", self.server_port_spin))
        
        setup_card_layout.addWidget(ports_widget)

        self.monitor_combo = QComboBox()
        self._populate_monitors()

        monitor_row = QWidget()
        monitor_row_layout = QHBoxLayout(monitor_row)
        monitor_row_layout.setContentsMargins(0, 0, 0, 0)
        monitor_row_layout.setSpacing(8)
        monitor_row_layout.addWidget(make_field("Browser Display", self.monitor_combo), 1)

        self.identify_btn = QPushButton("Identify")
        self.identify_btn.setFixedHeight(38)
        self.identify_btn.setFixedWidth(90)
        self.identify_btn.setObjectName("IdentifyBtn")
        self.identify_btn.setToolTip("Show monitor numbers on each display")
        self.identify_btn.clicked.connect(self._identify_monitors)
        # Align button to bottom of the field
        id_container = QWidget()
        id_layout = QVBoxLayout(id_container)
        id_layout.setContentsMargins(0, 0, 0, 0)
        id_layout.addStretch()
        id_layout.addWidget(self.identify_btn)
        monitor_row_layout.addWidget(id_container)

        setup_card_layout.addWidget(monitor_row)

        # --- Options row ---
        options_widget = QWidget()
        options_layout = QHBoxLayout(options_widget)
        options_layout.setContentsMargins(0, 4, 0, 0)
        options_layout.setSpacing(20)

        self.server_only_check = QCheckBox("Server only (don't launch browser)")
        self.server_only_check.setStyleSheet("color: #cccccc; font-size: 12px;")
        self.server_only_check.setToolTip(
            "Start the server without opening the browser.\n"
            "You can launch the browser later from your phone."
        )
        options_layout.addWidget(self.server_only_check)

        self.autostart_check = QCheckBox("Start with Windows")
        self.autostart_check.setStyleSheet("color: #cccccc; font-size: 12px;")
        self.autostart_check.setToolTip(
            "Automatically start the server when you log in to Windows."
        )
        self.autostart_check.toggled.connect(self._on_autostart_toggled)
        options_layout.addWidget(self.autostart_check)

        self.minimize_tray_check = QCheckBox("Minimize to tray on start")
        self.minimize_tray_check.setStyleSheet("color: #cccccc; font-size: 12px;")
        self.minimize_tray_check.setToolTip(
            "Automatically hide the window to the system tray\n"
            "after starting the server."
        )
        options_layout.addWidget(self.minimize_tray_check)

        options_layout.addStretch()
        setup_card_layout.addWidget(options_widget)

        # --- Advanced Settings (collapsible) ---
        self.adv_toggle_btn = QPushButton("▸ Advanced Settings")
        self.adv_toggle_btn.setObjectName("AdvToggleBtn")
        self.adv_toggle_btn.setFixedHeight(30)
        self.adv_toggle_btn.clicked.connect(self._toggle_advanced)
        setup_card_layout.addWidget(self.adv_toggle_btn)

        self.adv_widget = QWidget()
        self.adv_widget.setVisible(False)
        adv_layout = QVBoxLayout(self.adv_widget)
        adv_layout.setContentsMargins(0, 4, 0, 0)
        adv_layout.setSpacing(10)

        adv_grid = QWidget()
        adv_grid_layout = QHBoxLayout(adv_grid)
        adv_grid_layout.setContentsMargins(0, 0, 0, 0)
        adv_grid_layout.setSpacing(12)

        self.cursor_delay_spin = QDoubleSpinBox()
        self.cursor_delay_spin.setRange(0.5, 10.0)
        self.cursor_delay_spin.setSingleStep(0.5)
        self.cursor_delay_spin.setValue(2.0)
        self.cursor_delay_spin.setSuffix("s")
        adv_grid_layout.addWidget(make_field("Cursor Hide Delay", self.cursor_delay_spin))

        self.cursor_sens_spin = QDoubleSpinBox()
        self.cursor_sens_spin.setRange(0.1, 5.0)
        self.cursor_sens_spin.setSingleStep(0.1)
        self.cursor_sens_spin.setValue(1.0)
        self.cursor_sens_spin.setSuffix("x")
        adv_grid_layout.addWidget(make_field("Cursor Sensitivity", self.cursor_sens_spin))

        self.scroll_speed_spin = QDoubleSpinBox()
        self.scroll_speed_spin.setRange(0.1, 5.0)
        self.scroll_speed_spin.setSingleStep(0.1)
        self.scroll_speed_spin.setValue(1.0)
        self.scroll_speed_spin.setSuffix("x")
        adv_grid_layout.addWidget(make_field("Scroll Speed", self.scroll_speed_spin))

        adv_layout.addWidget(adv_grid)

        self.startup_urls_edit = QTextEdit()
        self.startup_urls_edit.setPlaceholderText("https://example.com\nhttps://another.com")
        self.startup_urls_edit.setFixedHeight(60)
        self.startup_urls_edit.setStyleSheet("""
            QTextEdit {
                background: #0d0d1a; color: #eeeeee; border: 2px solid #1e2a4a;
                border-radius: 8px; padding: 6px; font-size: 11px;
            }
        """)
        adv_layout.addWidget(make_field("Startup URLs (one per line)", self.startup_urls_edit))

        # Language selector
        lang_row = QWidget()
        lang_row_layout = QHBoxLayout(lang_row)
        lang_row_layout.setContentsMargins(0, 0, 0, 0)
        lang_row_layout.setSpacing(12)

        self.language_combo = QComboBox()
        self.language_combo.addItem("English", "en")
        self.language_combo.addItem("Čeština", "cs")
        lang_row_layout.addWidget(make_field("Language", self.language_combo))

        # Config import/export buttons
        ie_widget = QWidget()
        ie_layout = QHBoxLayout(ie_widget)
        ie_layout.setContentsMargins(0, 0, 0, 0)
        ie_layout.setSpacing(8)
        import_btn = QPushButton("Import Config")
        import_btn.setFixedHeight(34)
        import_btn.setObjectName("IdentifyBtn")
        import_btn.clicked.connect(self._import_config)
        ie_layout.addWidget(import_btn)
        export_btn = QPushButton("Export Config")
        export_btn.setFixedHeight(34)
        export_btn.setObjectName("IdentifyBtn")
        export_btn.clicked.connect(self._export_config)
        ie_layout.addWidget(export_btn)

        ie_container = QWidget()
        ie_container_layout = QVBoxLayout(ie_container)
        ie_container_layout.setContentsMargins(0, 0, 0, 0)
        ie_container_layout.addStretch()
        ie_container_layout.addWidget(ie_widget)
        lang_row_layout.addWidget(ie_container)

        lang_row_layout.addStretch()
        adv_layout.addWidget(lang_row)

        setup_card_layout.addWidget(self.adv_widget)

        self.start_btn = QPushButton("Start Server")
        self.start_btn.setFixedHeight(50)
        self.start_btn.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        self.start_btn.setObjectName("StartBtn")
        self.start_btn.clicked.connect(self._toggle_start)
        setup_card_layout.addWidget(self.start_btn)

        setup_layout.addWidget(setup_card)
        setup_scroll.setWidget(setup_scroll_content)
        setup_outer_layout.addWidget(setup_scroll)
        self.stack.addWidget(self.page_setup)

        # Page 2: Active State
        self.page_active = QWidget()
        active_layout = QVBoxLayout(self.page_active)
        active_layout.setContentsMargins(30, 10, 30, 20)

        active_card = QFrame()
        active_card.setObjectName("Card")
        active_card_layout = QVBoxLayout(active_card)
        active_card_layout.setContentsMargins(24, 32, 24, 24)
        active_card_layout.setSpacing(20)
        active_card_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setFixedSize(220, 220)
        self.qr_label.setStyleSheet(
            "background: #ffffff; border-radius: 16px; padding: 10px;"
            "border: 2px solid #1e2a4a;"
        )
        active_card_layout.addWidget(self.qr_label, 0, Qt.AlignmentFlag.AlignHCenter)

        self.url_label = QLabel()
        self.url_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.url_label.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self.url_label.setStyleSheet("color: #4ecca3;")
        self.url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        active_card_layout.addWidget(self.url_label)

        # Dashboard info
        self.dashboard_label = QLabel("")
        self.dashboard_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dashboard_label.setFont(QFont("Segoe UI", 10))
        self.dashboard_label.setStyleSheet("color: #888888;")
        self.dashboard_label.setWordWrap(True)
        active_card_layout.addWidget(self.dashboard_label)

        active_card_layout.addStretch()

        bottom_row = QWidget()
        bottom_row_layout = QHBoxLayout(bottom_row)
        bottom_row_layout.setContentsMargins(0, 0, 0, 0)
        
        self.status_indicator = QLabel("● Server Running")
        self.status_indicator.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        self.status_indicator.setStyleSheet("color: #4ecca3;")
        bottom_row_layout.addWidget(self.status_indicator)

        bottom_row_layout.addStretch()

        self.stop_btn = QPushButton("Stop Server")
        self.stop_btn.setFixedHeight(40)
        self.stop_btn.setFixedWidth(140)
        self.stop_btn.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.stop_btn.setObjectName("StopBtn")
        self.stop_btn.clicked.connect(self._stop)
        bottom_row_layout.addWidget(self.stop_btn)

        active_card_layout.addWidget(bottom_row)
        active_layout.addWidget(active_card)
        self.stack.addWidget(self.page_active)

        # --- 3. Log Drawer ---
        drawer_widget = QWidget()
        drawer_layout = QVBoxLayout(drawer_widget)
        drawer_layout.setContentsMargins(20, 0, 20, 20)
        drawer_layout.setSpacing(10)

        toggle_layout = QHBoxLayout()
        toggle_layout.addStretch()
        self.log_toggle_btn = QPushButton("Show Logs")
        self.log_toggle_btn.setFixedSize(100, 28)
        self.log_toggle_btn.setObjectName("LogToggleBtn")
        self.log_toggle_btn.clicked.connect(self._toggle_logs)
        toggle_layout.addWidget(self.log_toggle_btn)

        self.log_export_btn = QPushButton("Export")
        self.log_export_btn.setFixedSize(70, 28)
        self.log_export_btn.setObjectName("LogToggleBtn")
        self.log_export_btn.clicked.connect(self._export_logs)
        toggle_layout.addWidget(self.log_export_btn)

        toggle_layout.addStretch()
        drawer_layout.addLayout(toggle_layout)

        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setFixedHeight(0) # Hidden by default
        self.log_area.setStyleSheet("""
            QTextEdit {
                background: #0a0a0a; color: #aaaaaa; border: none;
                border-radius: 8px; font-family: 'Consolas', 'Courier New', monospace;
                font-size: 11px; padding: 10px;
            }
        """)
        drawer_layout.addWidget(self.log_area)
        
        main_layout.addWidget(drawer_widget)

        # Update QR code
        self._update_qr()
        self.server_port_spin.valueChanged.connect(self._update_qr)

        # Styling — matched to web app theme vars
        self.setStyleSheet("""
            QMainWindow { background: #0a0a0a; }
            QFrame#Card {
                background: #161625;
                border: 1px solid #1e2a4a;
                border-radius: 16px;
            }
            QComboBox, QSpinBox, QLineEdit {
                background: #0d0d1a; color: #eeeeee; border: 2px solid #1e2a4a;
                border-radius: 8px; padding: 8px 12px; font-size: 12px;
            }
            QComboBox:focus, QSpinBox:focus, QLineEdit:focus {
                border: 2px solid #e94560;
            }
            QComboBox::drop-down { border: none; width: 24px; }
            QComboBox QAbstractItemView {
                background: #161625; color: #eeeeee; selection-background-color: #e94560;
                border: 1px solid #1e2a4a; border-radius: 8px;
            }
            QCheckBox { color: #cccccc; font-size: 12px; }
            QCheckBox::indicator {
                width: 16px; height: 16px; border-radius: 4px;
                border: 2px solid #1e2a4a; background: #0d0d1a;
            }
            QCheckBox::indicator:checked {
                background: #e94560; border-color: #e94560;
            }
            QPushButton#StartBtn {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #e94560, stop:1 #d63050);
                color: #ffffff; border: none; border-radius: 12px;
            }
            QPushButton#StartBtn:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #ff5670, stop:1 #e94560);
            }
            QPushButton#StartBtn:pressed {
                background: #c72340;
            }
            QPushButton#StopBtn {
                background: #e94560; color: white; border: none;
                border-radius: 8px;
            }
            QPushButton#StopBtn:hover { background: #ff5670; }
            QPushButton#StopBtn:pressed { background: #c72340; }
            QPushButton#IdentifyBtn {
                background: #0f3460; color: #eee; border: none;
                border-radius: 8px; font-size: 11px; font-weight: bold;
            }
            QPushButton#IdentifyBtn:hover { background: #1a4a80; }
            QPushButton#IdentifyBtn:pressed { background: #0a2540; }
            QPushButton#LogToggleBtn {
                background: #161625; color: #888888; border: 1px solid #1e2a4a;
                border-radius: 14px; font-size: 11px; font-weight: bold;
            }
            QPushButton#LogToggleBtn:hover { background: #1e2a4a; color: #ffffff; }
            QPushButton#AdvToggleBtn {
                background: none; color: #888888; border: none;
                text-align: left; font-size: 12px; font-weight: bold;
                padding: 4px 0;
            }
            QPushButton#AdvToggleBtn:hover { color: #e94560; }
        """)

    def _toggle_logs(self):
        if self.log_area.height() == 0:
            self.log_area.setFixedHeight(150)
            self.log_toggle_btn.setText("Hide Logs")
        else:
            self.log_area.setFixedHeight(0)
            self.log_toggle_btn.setText("Show Logs")

    def _toggle_advanced(self):
        vis = not self.adv_widget.isVisible()
        self.adv_widget.setVisible(vis)
        self.adv_toggle_btn.setText(("▾ " if vis else "▸ ") + "Advanced Settings")

    def _export_logs(self):
        text = self.log_area.toPlainText()
        if not text.strip():
            self._log("No logs to export.", "#e94560")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Logs", "browser_remote_logs.txt", "Text Files (*.txt)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                self._log(f"Logs exported to {path}", "#4ecca3")
            except Exception as e:
                self._log(f"Export failed: {e}", "#e94560")

    def _import_config(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Config", "", "JSON Files (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("Invalid config format")
            config.save(data)
            self._load_settings()
            self._log(f"Config imported from {path}", "#4ecca3")
        except Exception as e:
            self._log(f"Import failed: {e}", "#e94560")

    def _export_config(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Config", "browser_remote_config.json", "JSON Files (*.json)")
        if not path:
            return
        try:
            cfg = config.load()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            self._log(f"Config exported to {path}", "#4ecca3")
        except Exception as e:
            self._log(f"Export failed: {e}", "#e94560")

    def _tray_open_site(self, url: str):
        """Open a URL via the server's navigate API."""
        if not self.running:
            return
        import urllib.request
        port = self.server_port_spin.value()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/navigate", method="POST",
                headers={"Content-Type": "application/json"},
                data=json.dumps({"url": url}).encode(),
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

    def _has_ssl(self):
        key, cert = _find_ssl_files()
        return key is not None

    def _update_qr(self):
        ip = get_local_ip()
        port = self.server_port_spin.value()
        scheme = "https" if self._has_ssl() else "http"
        url = f"{scheme}://{ip}:{port}"
        pixmap = generate_qr_pixmap(url, 170)
        self.qr_label.setPixmap(pixmap)
        self.url_label.setText(url)

    def _populate_monitors(self):
        self.monitor_combo.clear()
        if self.monitors:
            for i, m in enumerate(self.monitors):
                label = f"Monitor {i}: {m['width']}x{m['height']} at ({m['left']},{m['top']})"
                self.monitor_combo.addItem(label, i)
        else:
            self.monitor_combo.addItem("Default (primary)", -1)

    def _identify_monitors(self):
        """Show numbered overlays on each monitor for 3 seconds."""
        if not self.monitors:
            self._log("No monitors detected.", "#e94560")
            return
        self._overlays = []
        for i, m in enumerate(self.monitors):
            overlay = MonitorOverlay(i, m)
            overlay.show()
            self._overlays.append(overlay)
        QTimer.singleShot(3000, self._close_overlays)

    def _close_overlays(self):
        for overlay in getattr(self, '_overlays', []):
            overlay.close()
        self._overlays = []

    def _on_browser_changed(self, idx):
        name = self.browser_combo.currentText()
        path = self.browsers.get(name, "")
        self.browser_path_edit.setText(path)

    def _on_autostart_toggled(self, checked):
        _set_autostart(checked)
        if checked:
            self._log("Added to Windows Startup.", "#4ecca3")
        else:
            self._log("Removed from Windows Startup.", "#888")

    # ----- Settings persistence -----
    def _load_settings(self):
        # Load from QSettings (window state) and config.json (app config)
        cfg = config.load()

        # Find matching browser
        browser_name = self.settings.value("browser", "")
        if browser_name:
            idx = self.browser_combo.findText(browser_name)
            if idx >= 0:
                self.browser_combo.setCurrentIndex(idx)

        self.browser_path_edit.setText(cfg.get("brave_path", self.browser_path_edit.text()))
        self.cdp_port_spin.setValue(cfg.get("cdp_port", 9222))
        self.server_port_spin.setValue(cfg.get("port", 5000))
        self.default_url_edit.setText(cfg.get("default_url", "https://www.youtube.com/tv"))

        mon = cfg.get("projector_monitor", 0)
        if 0 <= mon < self.monitor_combo.count():
            self.monitor_combo.setCurrentIndex(mon)

        profile = self.settings.value("profile", 0, type=int)
        if 0 <= profile < self.profile_combo.count():
            self.profile_combo.setCurrentIndex(profile)

        self.server_only_check.setChecked(cfg.get("no_browser", False))
        self.autostart_check.setChecked(_is_autostart_enabled())
        self.minimize_tray_check.setChecked(
            self.settings.value("minimize_to_tray", False, type=bool)
        )

        # Advanced settings
        self.cursor_delay_spin.setValue(cfg.get("cursor_hide_delay", 2.0))
        self.cursor_sens_spin.setValue(cfg.get("cursor_sensitivity", 1.0))
        self.scroll_speed_spin.setValue(cfg.get("scroll_speed", 1.0))
        urls = cfg.get("startup_urls", [])
        self.startup_urls_edit.setPlainText("\n".join(urls) if urls else "")
        lang = cfg.get("language", "en")
        lang_idx = self.language_combo.findData(lang)
        if lang_idx >= 0:
            self.language_combo.setCurrentIndex(lang_idx)

    def _save_settings(self):
        """Save to both QSettings (launcher prefs) and config.json (app config)."""
        self.settings.setValue("browser", self.browser_combo.currentText())
        self.settings.setValue("profile", self.profile_combo.currentIndex())
        self.settings.setValue("minimize_to_tray", self.minimize_tray_check.isChecked())

        browser_name = self.browser_combo.currentText()
        browser_path = self.browser_path_edit.text().strip()

        # Determine user data dir
        if self.profile_combo.currentIndex() == 0:
            user_data = USER_DATA_DIRS.get(browser_name, "")
        else:
            user_data = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "browser_profile"
            )

        # Build monitors dict for config
        monitors_dict = {}
        for i, m in enumerate(self.monitors):
            monitors_dict[str(i)] = m

        config.save({
            "brave_path": browser_path,
            "cdp_port": self.cdp_port_spin.value(),
            "user_data_dir": user_data,
            "host": "0.0.0.0",
            "port": self.server_port_spin.value(),
            "projector_monitor": self.monitor_combo.currentData() or 0,
            "monitors": monitors_dict,
            "default_url": self.default_url_edit.text().strip() or "https://www.youtube.com/tv",
            "no_browser": self.server_only_check.isChecked(),
            "cursor_hide_delay": self.cursor_delay_spin.value(),
            "cursor_sensitivity": self.cursor_sens_spin.value(),
            "scroll_speed": self.scroll_speed_spin.value(),
            "startup_urls": [u.strip() for u in self.startup_urls_edit.toPlainText().split("\n") if u.strip()],
            "language": self.language_combo.currentData() or "en",
        })

    def _show_first_launch(self):
        """Show a first-run wizard dialog."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Welcome — browser remote")
        dlg.setFixedSize(480, 360)
        dlg.setStyleSheet("QDialog { background: #0a0a0a; }")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(32, 32, 32, 24)
        layout.setSpacing(16)

        title = QLabel("👋 Welcome to browser remote!")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        title.setStyleSheet("color: #e94560;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        steps = [
            "1. Select your browser above (Brave / Chrome / Edge)",
            "2. Pick which monitor the browser should appear on",
            "3. Click 'Start Server' to launch",
            "4. Scan the QR code with your phone to connect",
            "5. Explore Advanced Settings for sensitivity, WOL, and more",
        ]
        for step in steps:
            lbl = QLabel(step)
            lbl.setFont(QFont("Segoe UI", 11))
            lbl.setStyleSheet("color: #cccccc;")
            layout.addWidget(lbl)

        layout.addStretch()

        tip = QLabel(f"Config saved to: {config.CONFIG_PATH}")
        tip.setFont(QFont("Segoe UI", 9))
        tip.setStyleSheet("color: #555;")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        ok_btn = QPushButton("Got it!")
        ok_btn.setFixedHeight(40)
        ok_btn.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        ok_btn.setStyleSheet(
            "QPushButton { background: #e94560; color: white; border: none; border-radius: 8px; }"
            "QPushButton:hover { background: #ff5670; }"
        )
        ok_btn.clicked.connect(dlg.accept)
        layout.addWidget(ok_btn)

        dlg.exec()

    def _log(self, msg: str, color: str = "#aaa"):
        self.log_area.append(f'<span style="color:{color}">{msg}</span>')
        sb = self.log_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ----- Start / Stop -----
    def _toggle_start(self):
        if self.running:
            return
        self._save_settings()
        self._start()

    def _start(self):
        browser_path = self.browser_path_edit.text().strip()
        if not browser_path or not os.path.isfile(browser_path):
            QMessageBox.warning(self, "Error", f"Browser not found: {browser_path}")
            return

        self._log("Starting server (server will launch browser)...", "#4ecca3")

        server_port = self.server_port_spin.value()

        self.server_proc = QProcess(self)
        self.server_proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.server_proc.readyReadStandardOutput.connect(self._on_server_output)
        self.server_proc.finished.connect(self._on_server_finished)

        bd = _base_dir()
        key_file, cert_file = _find_ssl_files()
        has_ssl = key_file is not None

        if has_ssl:
            self._log(f"SSL certs found: {key_file}", "#4ecca3")
        else:
            self._log("No SSL certs found — starting in HTTP mode.", "#e9a045")

        no_browser = self.server_only_check.isChecked()
        if no_browser:
            self._log("Server-only mode — browser will NOT be launched.", "#e9a045")

        if getattr(sys, 'frozen', False):
            # PyInstaller exe — re-launch ourselves with --server flag
            uvicorn_args = [
                "--server",
                "--host", "0.0.0.0",
                "--port", str(server_port),
                "--log-level", "info",
            ]
            if has_ssl:
                uvicorn_args += ["--ssl-keyfile", key_file, "--ssl-certfile", cert_file]
            if no_browser:
                uvicorn_args.append("--no-browser")
            self.server_proc.setWorkingDirectory(bd)
            self.server_proc.start(sys.executable, uvicorn_args)
        else:
            # Development — use python -m uvicorn
            python = sys.executable
            self.server_proc.setWorkingDirectory(os.path.dirname(os.path.abspath(__file__)))
            uvicorn_args = [
                "-m", "uvicorn", "server:app",
                "--host", "0.0.0.0",
                "--port", str(server_port),
                "--log-level", "info",
            ]
            if has_ssl:
                uvicorn_args += ["--ssl-keyfile", key_file, "--ssl-certfile", cert_file]
            self.server_proc.start(python, uvicorn_args)

        ip = get_local_ip()
        scheme = "https" if has_ssl else "http"
        self._log(f"Server starting on {scheme}://{ip}:{server_port}", "#4ecca3")

        self.running = True
        self.stack.setCurrentWidget(self.page_active)
        self._update_status_label("Starting...", "#e9a045")
        self._update_qr()

        # Auto-minimize to tray if configured
        if self.minimize_tray_check.isChecked():
            QTimer.singleShot(1500, self._hide_to_tray)

        # Window stays visible so user can see QR / IP

    def _hide_to_tray(self):
        if self.running:
            self.hide()
            self.tray_icon.showMessage(
                "browser remote",
                "Running in background. Right-click tray icon for options.",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )

    def _on_server_output(self):
        if self.server_proc is None:
            return
        data = self.server_proc.readAllStandardOutput().data().decode("utf-8", errors="replace")
        for line in data.strip().split("\n"):
            if line.strip():
                color = "#aaa"
                ll = line.lower()
                if "error" in ll or "failed" in ll:
                    color = "#e94560"
                elif "started" in ll or "connected" in ll or "running" in ll or "launched" in ll:
                    color = "#4ecca3"
                elif "warning" in ll:
                    color = "#e9a045"
                self._log(line, color)

    def _on_server_finished(self, exit_code, status):
        self._log(f"Server process ended (exit code {exit_code})", "#e94560")
        self.running = False
        self.stack.setCurrentWidget(self.page_setup)
        self._update_status_label("Stopped", "#888")

    def _stop(self):
        self._log("Stopping...", "#e9a045")
        if self.server_proc and self.server_proc.state() != QProcess.ProcessState.NotRunning:
            self.server_proc.terminate()
            if not self.server_proc.waitForFinished(3000):
                self.server_proc.kill()
        self.server_proc = None
        self.running = False
        self.stack.setCurrentWidget(self.page_setup)
        self._update_status_label("Stopped", "#888")
        self._log("Stopped.", "#888")

    def _update_status(self):
        if not self.running:
            return
        ip = get_local_ip()
        port = self.server_port_spin.value()
        try:
            s = socket.create_connection((ip, port), timeout=1)
            s.close()
            scheme = "https" if self._has_ssl() else "http"
            self._update_status_label(f"Running  |  {scheme}://{ip}:{port}", "#4ecca3")
            self.tray_icon.setToolTip(f"browser remote — {scheme}://{ip}:{port}")
            # Fetch dashboard info
            self._fetch_dashboard(port)
        except Exception:
            if self.server_proc and self.server_proc.state() == QProcess.ProcessState.Running:
                self._update_status_label("Starting...", "#e9a045")
            else:
                self._update_status_label("Not responding", "#e94560")

    def _fetch_dashboard(self, port: int):
        """Fetch dashboard info from the server and update the label."""
        import urllib.request
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/dashboard", method="GET")
            resp = urllib.request.urlopen(req, timeout=2)
            data = json.loads(resp.read().decode())
            parts = []
            if data.get("browser_title"):
                parts.append(f"📺 {data['browser_title'][:40]}")
            if data.get("server_uptime"):
                parts.append(f"⏱ Uptime: {data['server_uptime']}")
            if data.get("active_clients") is not None:
                parts.append(f"📱 Clients: {data['active_clients']}")
            mode = "PC Mode" if data.get("pc_mode") else "Browser Mode"
            parts.append(f"🔧 {mode}")
            self.dashboard_label.setText("  •  ".join(parts) if parts else "")
        except Exception:
            pass

    def _update_status_label(self, text, color):
        self.status_indicator.setText(f"● {text}")
        self.status_indicator.setStyleSheet(f"color: {color};")

    def closeEvent(self, event):
        # If running, hide to tray instead of quitting
        if self.running:
            event.ignore()
            self.hide()
            return
        self._save_settings()
        self.tray_icon.hide()
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    # ── Server mode (PyInstaller exe re-launches itself with --server) ──
    if "--server" in sys.argv:
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("--server", action="store_true")
        p.add_argument("--host", default="0.0.0.0")
        p.add_argument("--port", type=int, default=5000)
        p.add_argument("--log-level", default="info")
        p.add_argument("--ssl-keyfile", default=None)
        p.add_argument("--ssl-certfile", default=None)
        p.add_argument("--no-browser", action="store_true")
        args = p.parse_args()

        # Tell the server module to skip browser launch
        if args.no_browser:
            os.environ["BR_NO_BROWSER"] = "1"

        import uvicorn
        uvicorn.run(
            "server:app",
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            ssl_keyfile=args.ssl_keyfile,
            ssl_certfile=args.ssl_certfile,
        )
        return

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Dark palette — matched to web app theme
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#0a0a0a"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#eee"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#0a0a0a"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#161625"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#eee"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#161625"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#eee"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#e94560"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#fff"))
    app.setPalette(palette)

    window = LauncherWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
