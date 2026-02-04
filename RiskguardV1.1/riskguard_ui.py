from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6 import QtCore, QtGui, QtSvg, QtWidgets

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.txt"
CONFIG_EXAMPLE_PATH = ROOT / "config.example.txt"
TERMINAL_CFG_PATH = ROOT / ".rg_terminal.json"
ASSETS_DIR = ROOT / "UI Figma" / "Icons"
MAIN_PATH = ROOT / "main.py"
SETUP_SCRIPT = ROOT / "setup_riskguard.ps1"
UPDATE_SCRIPT = ROOT / "update_riskguard.py"
LOG_DIR = ROOT / "logger" / "logs"
LOCK_FILE = ROOT / ".rg_main.lock"
UPDATE_STATUS_PATH = ROOT / ".rg_update_status.json"

LOCALE_DOT = QtCore.QLocale(QtCore.QLocale.English, QtCore.QLocale.UnitedStates)


def _read_lines(path: Path) -> List[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        return []
    except Exception:
        return []


def _parse_config(lines: List[str]) -> Dict[str, str]:
    data: Dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        data[key] = value.strip()
    return data


def _update_config_lines(lines: List[str], updates: Dict[str, str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for raw in lines:
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";") or "=" not in line:
            out.append(raw)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}\n")
            seen.add(key)
        else:
            out.append(raw)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}\n")
    return out


def _read_terminal_path() -> str:
    try:
        if TERMINAL_CFG_PATH.exists():
            data = json.loads(TERMINAL_CFG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return str(data.get("terminal_path") or "").strip()
    except Exception:
        return ""
    return ""


def _write_terminal_path(path: str) -> None:
    try:
        TERMINAL_CFG_PATH.write_text(
            json.dumps({"terminal_path": path}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _scan_mt5_terminals(max_results: int = 25) -> List[str]:
    if os.name != "nt":
        return []

    found: List[str] = []
    seen = set()

    def add(path: str):
        if not path:
            return
        try:
            if not os.path.exists(path):
                return
        except Exception:
            return
        key = os.path.normcase(path)
        if key in seen:
            return
        seen.add(key)
        found.append(path)

    candidates = [
        r"C:\Program Files\MetaTrader 5\terminal64.exe",
        r"C:\Program Files\XM Global MT5\terminal64.exe",
        r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe",
    ]
    for p in candidates:
        add(p)
        if len(found) >= max_results:
            return found

    for env_name in ("APPDATA", "LOCALAPPDATA", "PROGRAMDATA"):
        base = os.environ.get(env_name)
        if not base:
            continue
        terminal_root = os.path.join(base, "MetaQuotes", "Terminal")
        if not os.path.isdir(terminal_root):
            continue
        try:
            for entry in os.scandir(terminal_root):
                if not entry.is_dir():
                    continue
                add(os.path.join(entry.path, "terminal64.exe"))
                if len(found) >= max_results:
                    return found
        except Exception:
            continue

    for env_name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env_name)
        if not root or not os.path.isdir(root):
            continue
        try:
            for entry in os.scandir(root):
                if not entry.is_dir():
                    continue
                add(os.path.join(entry.path, "terminal64.exe"))
                if len(found) >= max_results:
                    return found
        except Exception:
            continue

    return found


def _svg_pixmap(path: Path, size: QtCore.QSize, color: Optional[str] = None) -> QtGui.QPixmap:
    try:
        data = path.read_text(encoding="utf-8")
    except Exception:
        return QtGui.QPixmap()
    if color:
        data = data.replace("currentColor", color)
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(data.encode("utf-8")))
    pix = QtGui.QPixmap(size)
    pix.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pix)
    renderer.render(painter)
    painter.end()
    return pix


def _svg_icon(path: Path, size: QtCore.QSize, color: Optional[str] = None) -> QtGui.QIcon:
    pix = _svg_pixmap(path, size, color=color)
    return QtGui.QIcon(pix)


class ToggleSwitch(QtWidgets.QWidget):
    toggled = QtCore.Signal(bool)

    def __init__(
        self,
        label_on: str = "Ativado",
        label_off: str = "Desativado",
        checked: bool = False,
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._label_on = label_on
        self._label_off = label_off
        self._icon_on = _svg_icon(ASSETS_DIR / "Toggle On.svg", QtCore.QSize(52, 28))
        self._icon_off = _svg_icon(ASSETS_DIR / "Toggle Off.svg", QtCore.QSize(52, 28))

        self.button = QtWidgets.QPushButton()
        self.button.setCheckable(True)
        self.button.setChecked(checked)
        self.button.setCursor(QtCore.Qt.PointingHandCursor)
        self.button.setObjectName("toggleButton")
        self.button.setIconSize(QtCore.QSize(52, 28))
        self.button.setFixedSize(52, 28)

        self.label = QtWidgets.QLabel()
        self.label.setObjectName("toggleLabel")

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.button)
        layout.addWidget(self.label)

        self.button.toggled.connect(self._on_toggled)
        self._sync()

    def _sync(self) -> None:
        if self.button.isChecked():
            self.button.setIcon(self._icon_on)
            self.label.setText(self._label_on)
        else:
            self.button.setIcon(self._icon_off)
            self.label.setText(self._label_off)

    def _on_toggled(self, value: bool) -> None:
        self._sync()
        self.toggled.emit(value)

    def isChecked(self) -> bool:
        return self.button.isChecked()

    def setChecked(self, value: bool) -> None:
        self.button.setChecked(value)

    def setEnabled(self, enabled: bool) -> None:
        self.button.setEnabled(enabled)
        self.label.setEnabled(enabled)
        super().setEnabled(enabled)


class SectionCard(QtWidgets.QFrame):
    def __init__(
        self,
        title: str,
        icon_path: Optional[Path] = None,
        icon_color: Optional[str] = None,
        default_open: bool = True,
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("sectionCard")
        self._open = default_open

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = QtWidgets.QFrame()
        self.header.setObjectName("sectionHeader")
        self.header.setCursor(QtCore.Qt.PointingHandCursor)
        header_layout = QtWidgets.QHBoxLayout(self.header)
        header_layout.setContentsMargins(16, 12, 16, 12)
        header_layout.setSpacing(10)

        self.icon_label = QtWidgets.QLabel()
        self.icon_label.setFixedSize(20, 20)
        if icon_path and icon_path.exists():
            pix = _svg_pixmap(icon_path, QtCore.QSize(20, 20), color=icon_color)
            self.icon_label.setPixmap(pix)
            header_layout.addWidget(self.icon_label)

        self.title_label = QtWidgets.QLabel(title)
        self.title_label.setObjectName("sectionTitle")
        header_layout.addWidget(self.title_label)
        header_layout.addStretch(1)

        self.arrow_label = QtWidgets.QLabel()
        self.arrow_label.setObjectName("sectionArrow")
        header_layout.addWidget(self.arrow_label)
        self._update_arrow()

        self.header.mousePressEvent = self._on_header_click

        self.content = QtWidgets.QWidget()
        self.content.setObjectName("sectionContent")
        self.content_layout = QtWidgets.QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(16, 0, 16, 16)
        self.content_layout.setSpacing(12)
        self.content.setVisible(self._open)

        layout.addWidget(self.header)
        layout.addWidget(self.content)

    def _on_header_click(self, event: QtGui.QMouseEvent) -> None:
        self.toggle()

    def _update_arrow(self) -> None:
        self.arrow_label.setText("▴" if self._open else "▾")

    def toggle(self) -> None:
        self._open = not self._open
        self.content.setVisible(self._open)
        self._update_arrow()


class RiskGuardUI(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("RiskGuard | Configurações")
        self.setMinimumSize(920, 900)
        shield_icon = ASSETS_DIR / "shield.svg"
        if shield_icon.exists():
            self.setWindowIcon(_svg_icon(shield_icon, QtCore.QSize(24, 24), color="#10b981"))

        self._config_lines = _read_lines(CONFIG_PATH)
        if not self._config_lines:
            self._config_lines = _read_lines(CONFIG_EXAMPLE_PATH)
        self._config = _parse_config(self._config_lines)

        central = QtWidgets.QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)

        root_layout = QtWidgets.QVBoxLayout(central)
        root_layout.setContentsMargins(20, 20, 20, 20)
        root_layout.setSpacing(14)

        header = self._build_header()
        root_layout.addWidget(header)

        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("mainScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        root_layout.addWidget(scroll)

        content = QtWidgets.QWidget()
        content.setObjectName("scrollContent")
        scroll.setWidget(content)
        content_layout = QtWidgets.QVBoxLayout(content)
        content_layout.setSpacing(18)

        content_layout.addWidget(self._build_terminal_group())
        content_layout.addWidget(self._build_telegram_group())
        content_layout.addWidget(self._build_risk_group())
        content_layout.addWidget(self._build_dd_group())
        content_layout.addWidget(self._build_news_group())
        content_layout.addWidget(self._build_status_group())
        content_layout.addStretch(1)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setObjectName("statusLabel")
        root_layout.addWidget(self.status_label)

        self._apply_style()
        self._load_from_config()
        self._wire_logic()
        self._allow_close = False
        self._tray_notice_shown = False
        self._init_tray()
        self._rg_process: Optional[subprocess.Popen] = None
        self._run_timer = QtCore.QTimer(self)
        self._run_timer.setInterval(1500)
        self._run_timer.timeout.connect(self._refresh_run_state)
        self._run_timer.start()
        self._status_timer = QtCore.QTimer(self)
        self._status_timer.setInterval(2000)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start()
        self._update_run_state()
        self._refresh_status()
        self._show_update_status()

    def _build_header(self) -> QtWidgets.QWidget:
        header = QtWidgets.QFrame()
        header.setObjectName("headerBar")
        layout = QtWidgets.QHBoxLayout(header)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(14)

        left = QtWidgets.QHBoxLayout()
        left.setSpacing(12)

        icon_box = QtWidgets.QFrame()
        icon_box.setObjectName("headerIconBox")
        icon_box.setFixedSize(40, 40)
        icon_layout = QtWidgets.QVBoxLayout(icon_box)
        icon_layout.setContentsMargins(0, 0, 0, 0)
        icon_layout.setAlignment(QtCore.Qt.AlignCenter)

        shield_path = ASSETS_DIR / "shield.svg"
        shield_pix = _svg_pixmap(shield_path, QtCore.QSize(24, 24), color="#10b981")
        icon_label = QtWidgets.QLabel()
        icon_label.setPixmap(shield_pix)
        icon_layout.addWidget(icon_label)

        title_main = QtWidgets.QLabel("RiskGuard")
        title_main.setObjectName("headerTitleMain")
        title_sub = QtWidgets.QLabel("Configurações")
        title_sub.setObjectName("headerTitleSub")

        left.addWidget(icon_box)
        left.addWidget(title_main)
        left.addWidget(title_sub)
        left.addStretch(1)

        layout.addLayout(left, 1)

        self.save_btn = QtWidgets.QPushButton("Salvar Configurações")
        self.save_btn.setObjectName("saveButton")
        self.save_btn.clicked.connect(self._save)
        layout.addWidget(self.save_btn)

        self.update_btn = QtWidgets.QPushButton("Atualizar")
        self.update_btn.setObjectName("updateButton")
        self.update_btn.clicked.connect(self._update_riskguard)
        layout.addWidget(self.update_btn)

        self.run_btn = QtWidgets.QPushButton("Iniciar RiskGuard")
        self.run_btn.setObjectName("runButton")
        self.run_btn.clicked.connect(self._toggle_riskguard)
        layout.addWidget(self.run_btn)

        user_box = QtWidgets.QFrame()
        user_box.setObjectName("headerUserBox")
        user_layout = QtWidgets.QHBoxLayout(user_box)
        user_layout.setContentsMargins(10, 6, 10, 6)
        user_layout.setSpacing(6)
        user_icon = QtWidgets.QLabel()
        user_pix = _svg_pixmap(ASSETS_DIR / "user-round.svg", QtCore.QSize(18, 18), color="#cbd5e1")
        user_icon.setPixmap(user_pix)
        user_label = QtWidgets.QLabel("Admin")
        user_label.setObjectName("headerUserLabel")
        user_layout.addWidget(user_icon)
        user_layout.addWidget(user_label)
        layout.addWidget(user_box)

        return header

    def _init_tray(self) -> None:
        if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            return

        tray_icon = QtWidgets.QSystemTrayIcon(self)
        icon = self.windowIcon()
        if icon.isNull():
            icon = self.style().standardIcon(QtWidgets.QStyle.SP_ComputerIcon)
        tray_icon.setIcon(icon)
        tray_icon.setToolTip("RiskGuard")

        menu = QtWidgets.QMenu()
        action_show = menu.addAction("Abrir Configurações")
        action_show.triggered.connect(self._show_from_tray)
        menu.addSeparator()
        action_exit = menu.addAction("Sair")
        action_exit.triggered.connect(self._exit_app)
        tray_icon.setContextMenu(menu)
        tray_icon.activated.connect(self._tray_activated)
        tray_icon.show()

        QtWidgets.QApplication.instance().setQuitOnLastWindowClosed(False)
        self._tray_icon = tray_icon

    def _tray_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        if reason == QtWidgets.QSystemTrayIcon.Trigger:
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _exit_app(self) -> None:
        # Garante que o loop principal pare ao sair pela bandeja.
        try:
            self._stop_riskguard()
        except Exception:
            pass
        self._allow_close = True
        QtWidgets.QApplication.instance().quit()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        tray_icon = getattr(self, "_tray_icon", None)
        if tray_icon and not self._allow_close:
            event.ignore()
            self.hide()
            if not self._tray_notice_shown:
                tray_icon.showMessage(
                    "RiskGuard",
                    "O RiskGuard continua rodando em segundo plano. Abra pelo ícone da bandeja.",
                    QtWidgets.QSystemTrayIcon.Information,
                    2500,
                )
                self._tray_notice_shown = True
            return
        event.accept()

    def _riskguard_python(self) -> Path:
        venv_dir = ROOT / "venv" / "Scripts"
        for name in ("pythonw.exe", "python.exe"):
            cand = venv_dir / name
            if cand.exists():
                return cand
        return Path(sys.executable)

    def _toggle_riskguard(self) -> None:
        if self._rg_process and self._rg_process.poll() is None:
            self._stop_riskguard()
        else:
            self._start_riskguard()

    def _update_riskguard(self) -> None:
        reply = QtWidgets.QMessageBox.question(
            self,
            "Atualizacao",
            "O RiskGuard sera fechado e atualizado. O aplicativo sera reiniciado. Deseja continuar?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return

        try:
            self._stop_riskguard()
        except Exception:
            pass

        if not UPDATE_SCRIPT.exists():
            QtWidgets.QMessageBox.warning(
                self,
                "Atualizacao",
                "Arquivo update_riskguard.py nao encontrado.",
            )
            return

        python_exe = self._riskguard_python()
        args = [str(python_exe), str(UPDATE_SCRIPT), "--restart-ui"]
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            subprocess.Popen(args, cwd=str(ROOT), creationflags=flags)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Atualizacao", f"Falha ao iniciar atualizacao: {exc}")
            return

        self._allow_close = True
        QtWidgets.QApplication.instance().quit()

    def _start_riskguard(self) -> None:
        terminal = self.terminal_path.text().strip()
        if terminal and not os.path.exists(terminal):
            QtWidgets.QMessageBox.warning(
                self,
                "MetaTrader 5",
                "Caminho do terminal MT5 inválido. Verifique em MetaTrader 5.",
            )
            return
        if not terminal:
            QtWidgets.QMessageBox.warning(
                self,
                "MetaTrader 5",
                "Defina o caminho do terminal MT5 antes de iniciar o RiskGuard.",
            )
            return

        if _is_main_running_via_lock():
            QtWidgets.QMessageBox.information(
                self,
                "RiskGuard",
                "O RiskGuard já está rodando. Feche a instância atual antes de iniciar outra.",
            )
            return

        _write_terminal_path(terminal)

        if not _is_mt5_running():
            try:
                subprocess.Popen(
                    [terminal],
                    cwd=str(Path(terminal).parent),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                )
            except Exception:
                pass

        if not _ensure_venv_ready():
            QtWidgets.QMessageBox.warning(
                self,
                "RiskGuard",
                "Falha ao preparar o ambiente. Verifique setup_riskguard.ps1.",
            )
            return

        python_exe = self._riskguard_python()
        args = [str(python_exe), str(MAIN_PATH)]

        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        try:
            env = os.environ.copy()
            env["RG_NO_PROMPT"] = "1"
            env["RG_TERMINAL_PATH"] = terminal
            self._rg_process = subprocess.Popen(
                args,
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                creationflags=flags,
            )
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Erro", f"Falha ao iniciar RiskGuard: {exc}")
            self._rg_process = None
        self._update_run_state()

    def _stop_riskguard(self) -> None:
        pid = None
        if self._rg_process:
            try:
                pid = int(self._rg_process.pid or 0)
            except Exception:
                pid = None
            try:
                self._rg_process.terminate()
                self._rg_process.wait(timeout=5)
            except Exception:
                try:
                    self._rg_process.kill()
                except Exception:
                    pass
        # Mata a árvore (evita pythonw órfão).
        if pid:
            _taskkill_tree(pid)
        # Se existir lock, encerra também o PID registrado.
        lock_pid = _get_lock_pid()
        if lock_pid and (pid is None or lock_pid != pid):
            _taskkill_tree(lock_pid)
        self._rg_process = None
        self._update_run_state()

    def _refresh_run_state(self) -> None:
        if self._rg_process and self._rg_process.poll() is not None:
            self._rg_process = None
            self._update_run_state()

    def _update_run_state(self) -> None:
        running = bool(self._rg_process and self._rg_process.poll() is None)
        if hasattr(self, "run_btn"):
            self.run_btn.setProperty("running", running)
            self.run_btn.setText("Parar RiskGuard" if running else "Iniciar RiskGuard")
            self.run_btn.style().unpolish(self.run_btn)
            self.run_btn.style().polish(self.run_btn)
        if hasattr(self, "status_label"):
            self.status_label.setText("RiskGuard: Rodando" if running else "RiskGuard: Parado")

    def _refresh_status(self) -> None:
        running = bool(self._rg_process and self._rg_process.poll() is None)
        mt5_running = _is_mt5_running()
        last_error = _last_error_line()

        if hasattr(self, "status_rg"):
            self.status_rg.setText("Rodando" if running else "Parado")
        if hasattr(self, "status_mt5"):
            self.status_mt5.setText("Ativo" if mt5_running else "Desativado")
        if hasattr(self, "status_err"):
            self.status_err.setText(last_error or "Sem erros recentes")
        if hasattr(self, "status_updated"):
            self.status_updated.setText(QtCore.QDateTime.currentDateTime().toString("dd/MM/yyyy HH:mm:ss"))

    def _show_update_status(self) -> None:
        if not UPDATE_STATUS_PATH.exists():
            return
        data: Optional[Dict[str, Any]] = None
        try:
            data = json.loads(UPDATE_STATUS_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = None
        try:
            UPDATE_STATUS_PATH.unlink()
        except Exception:
            pass
        if not isinstance(data, dict):
            return
        ok = bool(data.get("ok"))
        msg = str(data.get("message") or "").strip()
        version = str(data.get("version") or "").strip()
        if version:
            msg = f"{msg} (versao: {version})" if msg else f"Versao: {version}"
        if not msg:
            msg = "Atualizacao concluida." if ok else "Falha na atualizacao."
        if ok:
            QtWidgets.QMessageBox.information(self, "Atualizacao", msg)
        else:
            QtWidgets.QMessageBox.warning(self, "Atualizacao", msg)

    def _add_row(self, layout: QtWidgets.QVBoxLayout, label_text: str, field: QtWidgets.QWidget) -> None:
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(12)
        label = QtWidgets.QLabel(label_text)
        label.setObjectName("fieldLabel")
        label.setMinimumWidth(220)
        row.addWidget(label)
        row.addStretch(1)
        row.addWidget(field)
        layout.addLayout(row)

    def _build_terminal_group(self) -> SectionCard:
        card = SectionCard("MetaTrader 5", icon_path=ASSETS_DIR / "shield.svg", icon_color="#10b981")

        self.terminal_path = QtWidgets.QLineEdit()
        self.terminal_path.setPlaceholderText("C:\\Program Files\\MetaTrader 5\\terminal64.exe")
        self.terminal_path.setFixedWidth(420)

        browse = QtWidgets.QPushButton("Procurar...")
        browse.clicked.connect(self._browse_terminal)
        detect = QtWidgets.QPushButton("Detectar")
        detect.clicked.connect(self._detect_terminal)

        row = QtWidgets.QWidget()
        row_layout = QtWidgets.QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)
        row_layout.addWidget(self.terminal_path)
        row_layout.addWidget(browse)
        row_layout.addWidget(detect)

        self._add_row(card.content_layout, "Terminal MT5:", row)
        return card

    def _build_status_group(self) -> SectionCard:
        card = SectionCard("Status do RiskGuard", icon_path=ASSETS_DIR / "shield.svg", icon_color="#38bdf8")
        self.status_rg = QtWidgets.QLabel("Parado")
        self.status_rg.setObjectName("statusValue")
        self.status_mt5 = QtWidgets.QLabel("Desativado")
        self.status_mt5.setObjectName("statusValue")
        self.status_err = QtWidgets.QLabel("Sem erros recentes")
        self.status_err.setObjectName("statusMuted")
        self.status_updated = QtWidgets.QLabel("-")
        self.status_updated.setObjectName("statusMuted")

        self._add_row(card.content_layout, "RiskGuard:", self.status_rg)
        self._add_row(card.content_layout, "MT5:", self.status_mt5)
        self._add_row(card.content_layout, "Último erro:", self.status_err)
        self._add_row(card.content_layout, "Atualizado:", self.status_updated)
        return card

    def _build_telegram_group(self) -> SectionCard:
        card = SectionCard("Telegram", icon_path=ASSETS_DIR / "send.svg", icon_color="#60a5fa")

        self.telegram_token = QtWidgets.QLineEdit()
        self.telegram_token.setPlaceholderText("BOT TOKEN")
        self.telegram_token.setFixedWidth(420)

        copy_btn = QtWidgets.QPushButton("⧉")
        copy_btn.setObjectName("copyButton")
        copy_btn.setFixedSize(32, 28)
        copy_btn.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(self.telegram_token.text()))

        token_row = QtWidgets.QWidget()
        token_layout = QtWidgets.QHBoxLayout(token_row)
        token_layout.setContentsMargins(0, 0, 0, 0)
        token_layout.setSpacing(6)
        token_layout.addWidget(self.telegram_token)
        token_layout.addWidget(copy_btn)

        self.telegram_chat_id = QtWidgets.QLineEdit()
        self.telegram_chat_id.setPlaceholderText("CHAT ID")
        self.telegram_chat_id.setFixedWidth(160)

        self.telegram_commands = ToggleSwitch(label_on="Ativado", label_off="Desativado")

        self.telegram_poll = QtWidgets.QSpinBox()
        self.telegram_poll.setRange(1, 3600)
        self.telegram_poll.setSuffix(" seg.")
        self.telegram_poll.setFixedWidth(90)

        self._add_row(card.content_layout, "Bot Token:", token_row)
        self._add_row(card.content_layout, "Chat ID:", self.telegram_chat_id)
        self._add_row(card.content_layout, "Comandos do Telegram:", self.telegram_commands)
        self._add_row(card.content_layout, "Intervalo de Comandos (seg):", self.telegram_poll)
        return card

    def _build_risk_group(self) -> SectionCard:
        card = SectionCard("Regras de Risco", icon_path=ASSETS_DIR / "triangle-alert.svg", icon_color="#f59e0b")

        self.pertrade_max = QtWidgets.QDoubleSpinBox()
        self.pertrade_max.setLocale(LOCALE_DOT)
        self.pertrade_max.setRange(0.01, 100.0)
        self.pertrade_max.setDecimals(2)
        self.pertrade_max.setSuffix(" %")
        self.pertrade_max.setFixedWidth(110)

        self.pertrade_interactive = ToggleSwitch(label_on="Ativado", label_off="Desativado")
        self.pertrade_timeout = QtWidgets.QSpinBox()
        self.pertrade_timeout.setRange(1, 240)
        self.pertrade_timeout.setSuffix(" min.")
        self.pertrade_timeout.setFixedWidth(90)

        interactive_row = QtWidgets.QWidget()
        interactive_layout = QtWidgets.QHBoxLayout(interactive_row)
        interactive_layout.setContentsMargins(0, 0, 0, 0)
        interactive_layout.setSpacing(10)
        interactive_layout.addWidget(self.pertrade_interactive)
        timeout_label = QtWidgets.QLabel("Timeout (min):")
        timeout_label.setObjectName("mutedLabel")
        interactive_layout.addWidget(timeout_label)
        interactive_layout.addWidget(self.pertrade_timeout)

        self.trade_notifications = ToggleSwitch(label_on="Ativado", label_off="Desativado")

        self.aggregate_max = QtWidgets.QDoubleSpinBox()
        self.aggregate_max.setLocale(LOCALE_DOT)
        self.aggregate_max.setRange(0.01, 100.0)
        self.aggregate_max.setDecimals(2)
        self.aggregate_max.setSuffix(" %")
        self.aggregate_max.setFixedWidth(110)

        self.aggregate_attempts = QtWidgets.QSpinBox()
        self.aggregate_attempts.setRange(1, 20)
        self.aggregate_attempts.setFixedWidth(70)

        self._add_row(card.content_layout, "Risco Máx. por Trade:", self.pertrade_max)
        self._add_row(card.content_layout, "Modo Interativo:", interactive_row)
        self._add_row(card.content_layout, "Notificações de Trade:", self.trade_notifications)
        self._add_row(card.content_layout, "Risco Máx. Agregado:", self.aggregate_max)
        self._add_row(card.content_layout, "Limite de Tentativas:", self.aggregate_attempts)
        return card

    def _build_dd_group(self) -> SectionCard:
        card = SectionCard("Drawdown", icon_path=ASSETS_DIR / "trending-down.svg", icon_color="#f87171")

        self.dd_enabled = ToggleSwitch(label_on="Ativado", label_off="Desativado")

        self.dd_limit = QtWidgets.QDoubleSpinBox()
        self.dd_limit.setLocale(LOCALE_DOT)
        self.dd_limit.setRange(0.1, 100.0)
        self.dd_limit.setDecimals(2)
        self.dd_limit.setSuffix(" %")
        self.dd_limit.setFixedWidth(110)

        self.dd_cooldown = QtWidgets.QSpinBox()
        self.dd_cooldown.setRange(1, 365)
        self.dd_cooldown.setSuffix(" dias")
        self.dd_cooldown.setFixedWidth(110)

        self._add_row(card.content_layout, "Drawdown:", self.dd_enabled)
        self._add_row(card.content_layout, "Limite de DD:", self.dd_limit)
        self._add_row(card.content_layout, "Cooldown (dias):", self.dd_cooldown)
        return card

    def _build_news_group(self) -> SectionCard:
        card = SectionCard("Janela de Notícias", icon_path=ASSETS_DIR / "newspaper.svg", icon_color="#60a5fa")

        self.news_enabled = ToggleSwitch(label_on="Ativado", label_off="Desativado")
        self.news_window = QtWidgets.QSpinBox()
        self.news_window.setRange(1, 240)
        self.news_window.setSuffix(" min.")
        self.news_window.setFixedWidth(100)
        self.news_recent = QtWidgets.QSpinBox()
        self.news_recent.setRange(10, 3600)
        self.news_recent.setSuffix(" seg.")
        self.news_recent.setFixedWidth(100)

        self._add_row(card.content_layout, "Janela de Notícias:", self.news_enabled)
        self._add_row(card.content_layout, "Duração da Janela (min):", self.news_window)
        self._add_row(card.content_layout, "Filtro Recentes (seg):", self.news_recent)
        return card

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow {
                background: #141824;
                color: #e2e8f0;
                font-family: "Segoe UI";
                font-size: 12pt;
            }
            QWidget#centralWidget {
                background: #141824;
            }
            QScrollArea#mainScroll {
                background: #141824;
            }
            QScrollArea#mainScroll > QWidget {
                background: #141824;
            }
            QWidget#scrollContent {
                background: #141824;
            }
            QFrame#headerBar {
                background: #1a1f2e;
                border-bottom: 1px solid #374151;
            }
            QFrame#headerIconBox {
                background: #2a3142;
                border: 1px solid rgba(16,185,129,0.3);
                border-radius: 8px;
            }
            QLabel#headerTitleMain {
                font-size: 18pt;
                font-weight: 600;
                color: #ffffff;
            }
            QLabel#headerTitleSub {
                color: #94a3b8;
            }
            QPushButton#saveButton {
                background: #10b981;
                color: #ffffff;
                border-radius: 6px;
                padding: 8px 18px;
                font-weight: 600;
            }
            QPushButton#saveButton:hover {
                background: #059669;
            }
            QPushButton#updateButton {
                background: #2563eb;
                color: #ffffff;
                border-radius: 6px;
                padding: 8px 14px;
                font-weight: 600;
            }
            QPushButton#updateButton:hover {
                background: #1d4ed8;
            }
            QPushButton#runButton {
                background: #10b981;
                color: #ffffff;
                border-radius: 6px;
                padding: 8px 14px;
                font-weight: 600;
            }
            QPushButton#runButton:hover {
                background: #059669;
            }
            QPushButton#runButton[running="true"] {
                background: #dc2626;
            }
            QPushButton#runButton[running="true"]:hover {
                background: #b91c1c;
            }
            QFrame#headerUserBox {
                background: #1f2433;
                border: 1px solid #374151;
                border-radius: 8px;
            }
            QLabel#headerUserLabel {
                color: #cbd5e1;
            }
            QFrame#sectionCard {
                background: #1e2433;
                border: 1px solid #374151;
                border-radius: 10px;
            }
            QFrame#sectionHeader {
                background: #1e2433;
            }
            QFrame#sectionHeader:hover {
                background: #252b3c;
            }
            QLabel#sectionTitle {
                color: #ffffff;
                font-weight: 600;
            }
            QLabel#sectionArrow {
                color: #94a3b8;
                font-size: 14pt;
            }
            QLabel#fieldLabel {
                color: #cbd5e1;
            }
            QLabel#mutedLabel {
                color: #94a3b8;
            }
            QLabel#statusValue {
                color: #e2e8f0;
                font-weight: 600;
            }
            QLabel#statusMuted {
                color: #94a3b8;
            }
            QLineEdit, QSpinBox, QDoubleSpinBox {
                background: #2a3142;
                border: 1px solid #4b5563;
                border-radius: 6px;
                padding: 4px 8px;
                color: #e2e8f0;
                min-height: 28px;
                qproperty-alignment: AlignCenter;
            }
            QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {
                border: 1px solid #10b981;
            }
            QPushButton {
                background: #2a3142;
                border: 1px solid #4b5563;
                border-radius: 6px;
                padding: 6px 10px;
                color: #e2e8f0;
            }
            QPushButton:hover {
                background: #343d52;
            }
            QPushButton#copyButton {
                font-weight: 600;
            }
            QPushButton#toggleButton {
                border: none;
                background: transparent;
            }
            QLabel#toggleLabel {
                color: #cbd5e1;
            }
            QLabel#statusLabel {
                color: #9fb3c8;
                padding: 6px;
            }
            QScrollBar:vertical {
                background: #141824;
                width: 10px;
                margin: 6px 2px 6px 2px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical {
                background: #2a3142;
                min-height: 24px;
                border-radius: 5px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            """
        )

    def _load_from_config(self) -> None:
        cfg = self._config

        self.telegram_token.setText(cfg.get("TELEGRAM_BOT_TOKEN", ""))
        self.telegram_chat_id.setText(cfg.get("TELEGRAM_CHAT_ID", ""))
        self.telegram_commands.setChecked(_as_bool(cfg.get("TELEGRAM_COMMANDS"), False))
        self.telegram_poll.setValue(_as_int(cfg.get("TELEGRAM_COMMANDS_POLL_SECONDS"), 2))

        self.pertrade_max.setValue(_as_float(cfg.get("PERTRADE_MAX_RISK"), 1.0))
        self.pertrade_interactive.setChecked(_as_bool(cfg.get("PERTRADE_INTERACTIVE"), False))
        self.pertrade_timeout.setValue(_as_int(cfg.get("PERTRADE_INTERACTIVE_TIMEOUT_MIN"), 15))
        self.trade_notifications.setChecked(_as_bool(cfg.get("TRADE_NOTIFICATIONS"), False))
        self.aggregate_max.setValue(_as_float(cfg.get("AGGREGATE_MAX_RISK"), 5.0))
        self.aggregate_attempts.setValue(_as_int(cfg.get("AGGREGATE_MAX_ATTEMPTS"), 3))

        dd_limit_raw = cfg.get("DD_LIMIT_PCT")
        dd_limit_val = _as_optional_float(dd_limit_raw, 20.0)
        self.dd_enabled.setChecked(dd_limit_val is not None)
        self.dd_limit.setValue(dd_limit_val if dd_limit_val is not None else 20.0)
        self.dd_cooldown.setValue(_as_int(cfg.get("DD_COOLDOWN_DAYS"), 30))

        self.news_enabled.setChecked(_as_bool(cfg.get("NEWS_WINDOW_ENABLED"), False))
        self.news_window.setValue(_as_int(cfg.get("NEWS_WINDOW_MINUTES"), 60))
        self.news_recent.setValue(_as_int(cfg.get("NEWS_RECENT_SECONDS"), 180))

        self.terminal_path.setText(_read_terminal_path())

    def _wire_logic(self) -> None:
        self.telegram_commands.toggled.connect(self.telegram_poll.setEnabled)
        self.telegram_poll.setEnabled(self.telegram_commands.isChecked())

        self.pertrade_interactive.toggled.connect(self.pertrade_timeout.setEnabled)
        self.pertrade_timeout.setEnabled(self.pertrade_interactive.isChecked())

        def _dd_toggle(state: bool):
            self.dd_limit.setEnabled(state)
        self.dd_enabled.toggled.connect(_dd_toggle)
        _dd_toggle(self.dd_enabled.isChecked())

        def _news_toggle(state: bool):
            self.news_window.setEnabled(state)
            self.news_recent.setEnabled(state)
        self.news_enabled.toggled.connect(_news_toggle)
        _news_toggle(self.news_enabled.isChecked())

    def _browse_terminal(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Selecionar terminal64.exe",
            str(ROOT),
            "terminal64.exe (terminal64.exe);;Executáveis (*.exe);;Todos os arquivos (*.*)",
        )
        if path:
            self.terminal_path.setText(path)

    def _detect_terminal(self) -> None:
        candidates = _scan_mt5_terminals()
        if not candidates:
            QtWidgets.QMessageBox.information(self, "Detectar MT5", "Nenhum terminal MT5 encontrado.")
            return
        if len(candidates) == 1:
            self.terminal_path.setText(candidates[0])
            return
        choice, ok = QtWidgets.QInputDialog.getItem(
            self,
            "Selecionar terminal MT5",
            "Encontramos estes terminais:",
            candidates,
            0,
            False,
        )
        if ok and choice:
            self.terminal_path.setText(choice)

    def _save(self) -> None:
        updates = {
            "TELEGRAM_BOT_TOKEN": self.telegram_token.text().strip(),
            "TELEGRAM_CHAT_ID": self.telegram_chat_id.text().strip(),
            "TELEGRAM_COMMANDS": _fmt_bool(self.telegram_commands.isChecked()),
            "TELEGRAM_COMMANDS_POLL_SECONDS": str(int(self.telegram_poll.value())),
            "PERTRADE_MAX_RISK": _fmt_float(self.pertrade_max.value()),
            "PERTRADE_INTERACTIVE": _fmt_bool(self.pertrade_interactive.isChecked()),
            "PERTRADE_INTERACTIVE_TIMEOUT_MIN": str(int(self.pertrade_timeout.value())),
            "TRADE_NOTIFICATIONS": _fmt_bool(self.trade_notifications.isChecked()),
            "AGGREGATE_MAX_RISK": _fmt_float(self.aggregate_max.value()),
            "AGGREGATE_MAX_ATTEMPTS": str(int(self.aggregate_attempts.value())),
            "DD_COOLDOWN_DAYS": str(int(self.dd_cooldown.value())),
            "NEWS_WINDOW_MINUTES": str(int(self.news_window.value())),
            "NEWS_RECENT_SECONDS": str(int(self.news_recent.value())),
            "NEWS_WINDOW_ENABLED": _fmt_bool(self.news_enabled.isChecked()),
        }

        if self.dd_enabled.isChecked():
            updates["DD_LIMIT_PCT"] = _fmt_float(self.dd_limit.value())
        else:
            updates["DD_LIMIT_PCT"] = "none"

        base_lines = _read_lines(CONFIG_PATH)
        if not base_lines:
            base_lines = _read_lines(CONFIG_EXAMPLE_PATH)
        updated_lines = _update_config_lines(base_lines, updates)
        try:
            CONFIG_PATH.write_text("".join(updated_lines), encoding="utf-8")
        except Exception:
            QtWidgets.QMessageBox.warning(self, "Erro", "Falha ao salvar config.txt.")
            return

        terminal = self.terminal_path.text().strip()
        if terminal:
            _write_terminal_path(terminal)

        self.status_label.setText("Configurações salvas com sucesso.")


def _as_bool(val: Optional[str], default: bool) -> bool:
    if val is None:
        return default
    v = str(val).strip().lower()
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    return default


def _as_int(val: Optional[str], default: int) -> int:
    try:
        if val is None or str(val).strip() == "":
            return default
        return int(float(str(val)))
    except Exception:
        return default


def _as_float(val: Optional[str], default: float) -> float:
    try:
        if val is None or str(val).strip() == "":
            return default
        return float(str(val))
    except Exception:
        return default


def _as_optional_float(val: Optional[str], default: float) -> Optional[float]:
    if val is None:
        return default
    v = str(val).strip().lower()
    if v in ("", "none", "null"):
        return None
    try:
        return float(v)
    except Exception:
        return default


def _fmt_bool(value: bool) -> str:
    return "true" if value else "false"


def _fmt_float(value: float) -> str:
    return f"{value:.4g}" if value < 1 else f"{value:.2f}".rstrip("0").rstrip(".")


def _is_mt5_running() -> bool:
    if os.name != "nt":
        return False
    try:
        output = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq terminal64.exe"],
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        ).decode(errors="ignore")
        return "terminal64.exe" in output.lower()
    except Exception:
        return False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        output = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}"],
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        ).decode(errors="ignore")
        return str(pid) in output
    except Exception:
        return False


def _is_main_running_via_lock() -> bool:
    try:
        if not LOCK_FILE.exists():
            return False
        data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        pid = int(data.get("pid") or 0)
        if pid and _pid_alive(pid):
            return True
    except Exception:
        pass
    # lock stale
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass
    return False


def _get_lock_pid() -> int:
    try:
        if not LOCK_FILE.exists():
            return 0
        data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        return int(data.get("pid") or 0)
    except Exception:
        return 0


def _taskkill_tree(pid: int) -> None:
    if pid <= 0:
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            timeout=6,
        )
    except Exception:
        pass


def _last_error_line() -> Optional[str]:
    if not LOG_DIR.exists():
        return None
    latest = _latest_log_file()
    if not latest:
        return None
    try:
        dq: deque[str] = deque(maxlen=1500)
        with latest.open("r", encoding="utf-8") as f:
            dq.extend(f)
        for raw in reversed(dq):
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            typ = str(entry.get("type") or "").upper()
            if typ in ("ERROR", "LOGGER_ERROR"):
                ts = entry.get("ts") or ""
                ctx = entry.get("context") or {}
                module = ctx.get("module") or ""
                payload = entry.get("payload") or {}
                err = payload.get("err") or entry.get("error") or ""
                parts = [p for p in (ts, module, err) if p]
                return " | ".join(parts)[:120]
    except Exception:
        return None
    return None


def _latest_log_file() -> Optional[Path]:
    try:
        files = sorted(LOG_DIR.glob("*-riskguard.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return None
    return files[0] if files else None


def _ensure_venv_ready() -> bool:
    venv_python = ROOT / "venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        try:
            res = subprocess.run(
                [str(venv_python), "-V"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                timeout=8,
            )
            if res.returncode == 0:
                return True
        except Exception:
            pass

    if not SETUP_SCRIPT.exists():
        return False

    try:
        res = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SETUP_SCRIPT),
            ],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
        return res.returncode == 0
    except Exception:
        return False


def main() -> int:
    app = QtWidgets.QApplication([])
    window = RiskGuardUI()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
