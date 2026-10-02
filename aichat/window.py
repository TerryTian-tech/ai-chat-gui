"""主窗口：会话管理、消息收发、agent 循环集成、桌宠与进程监控联动。"""

import base64
import os
import re
import sys
import threading
import uuid
from datetime import datetime
from typing import Dict, List
from urllib.parse import urlparse

from PySide6.QtCore import QBuffer, QByteArray, QEvent, QSettings, Signal, Qt, QThread, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPushButton, QScrollArea, QSplitter, QTextEdit, QToolButton, QVBoxLayout,
    QWidget,
)

from .agent import AgentWorker
from .api import (ANTHROPIC, FORMAT_LABELS, RESPONSES, THINKING_HIGH,
                  THINKING_LOW, THINKING_MEDIUM, THINKING_OFF,
                  normalize_history_message, normalize_thinking_effort)
from . import __version__ as _pkg_version
from .pet import PuppyWidget
from .widgets import MessageWidget

APP_VERSION = f"V{_pkg_version}"


# ==================== 设置对话框 ====================
class SettingsDialog(QDialog):
    """API / Agent 设置对话框"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setModal(True)
        self.resize(520, 560)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel("⚙️ API 配置")
        title.setStyleSheet("font-size: 20px; font-weight: 600; color: #1a202c;")
        layout.addWidget(title)

        form = QVBoxLayout()
        form.setSpacing(10)

        form.addWidget(QLabel("API 格式:"))
        self.format_combo = QComboBox()
        self.format_combo.addItem(FORMAT_LABELS[ANTHROPIC], ANTHROPIC)
        self.format_combo.addItem(FORMAT_LABELS[RESPONSES], RESPONSES)
        self._style_combo(self.format_combo)
        form.addWidget(self.format_combo)

        form.addWidget(QLabel("API Key:"))
        key_row = QHBoxLayout()
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setPlaceholderText("输入你的 API Key")
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_row.addWidget(self.api_key_edit)
        self.toggle_key_btn = QToolButton()
        self.toggle_key_btn.setText("👁")
        self.toggle_key_btn.setCheckable(True)
        self.toggle_key_btn.setCursor(Qt.PointingHandCursor)
        self.toggle_key_btn.clicked.connect(self.toggle_key_visibility)
        key_row.addWidget(self.toggle_key_btn)
        form.addLayout(key_row)

        form.addWidget(QLabel("Base URL:"))
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText(
            "Anthropic 如 https://api.anthropic.com ｜ Responses 如 https://api.openai.com/v1")
        form.addWidget(self.base_url_edit)

        form.addWidget(QLabel("模型:"))
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("例如 claude-sonnet-4-5 / gpt-5 / deepseek-chat")
        form.addWidget(self.model_edit)

        form.addWidget(QLabel("思考强度:"))
        self.thinking_combo = QComboBox()
        self.thinking_combo.addItem("关闭（不发送思考参数）", THINKING_OFF)
        self.thinking_combo.addItem("低", THINKING_LOW)
        self.thinking_combo.addItem("中", THINKING_MEDIUM)
        self.thinking_combo.addItem("高", THINKING_HIGH)
        self.thinking_combo.setToolTip(
            "推理模型的思考预算，同时作用于聊天与 Agent。\n"
            "Anthropic → thinking.budget_tokens（低 2048 / 中 8192 / 高 16384）；\n"
            "OpenAI Responses → reasoning.effort。\n"
            "模型不支持思考时自动去掉思考参数重试；输出上限不足时自动收紧 max_tokens。")
        self._style_combo(self.thinking_combo)
        form.addWidget(self.thinking_combo)

        self.vision_checkbox = QCheckBox("此模型支持图片输入（多模态）")
        self.vision_checkbox.setStyleSheet("font-size: 13px; color: #4a5568;")
        form.addWidget(self.vision_checkbox)

        agent_title = QLabel("🤖 Agent 设置")
        agent_title.setStyleSheet("font-size: 16px; font-weight: 600; color: #1a202c; margin-top: 8px;")
        form.addWidget(agent_title)

        form.addWidget(QLabel("工具工作目录:"))
        workdir_row = QHBoxLayout()
        self.workdir_edit = QLineEdit()
        self.workdir_edit.setPlaceholderText("Agent 工具（read/write/bash 等）的工作目录")
        workdir_row.addWidget(self.workdir_edit)
        browse_btn = QPushButton("浏览…")
        browse_btn.setCursor(Qt.PointingHandCursor)
        browse_btn.clicked.connect(self.browse_workdir)
        workdir_row.addWidget(browse_btn)
        form.addLayout(workdir_row)

        self.confirm_checkbox = QCheckBox("执行写入/编辑/命令类工具前需要我确认")
        self.confirm_checkbox.setStyleSheet("font-size: 13px; color: #4a5568;")
        form.addWidget(self.confirm_checkbox)

        layout.addLayout(form)

        help_label = QLabel(
            "💡 两种格式均为原生流式请求，无需任何 SDK：\n"
            "   Anthropic (Messages)：POST {Base URL}/v1/messages\n"
            "   OpenAI (Responses)：POST {Base URL}/responses\n"
            "💡 Agent 模式下模型可调用 read/write/edit/glob/grep/bash 工具。"
        )
        help_label.setStyleSheet(
            "color: #718096; font-size: 12px; padding: 12px; background: #edf2f7; "
            "border-radius: 10px; line-height: 1.6;")
        help_label.setWordWrap(True)
        layout.addWidget(help_label, 1)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_button = button_box.button(QDialogButtonBox.StandardButton.Ok)
        cancel_button = button_box.button(QDialogButtonBox.StandardButton.Cancel)
        ok_button.setText("保存设置")
        cancel_button.setText("取消")
        ok_button.setObjectName("okButton")
        cancel_button.setObjectName("cancelButton")
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        button_box.setStyleSheet("""
            QPushButton { padding: 10px 28px; border-radius: 30px; font-size: 14px; font-weight: 600; }
            QPushButton#okButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #667eea, stop:1 #764ba2);
                color: white; border: none;
            }
            QPushButton#okButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #5a6fd6, stop:1 #6a4190);
            }
            QPushButton#cancelButton { background: white; color: #4a5568; border: 1px solid #e2e8f0; }
            QPushButton#cancelButton:hover { background: #f7fafc; }
        """)
        layout.addWidget(button_box)

        self.setStyleSheet("""
            QDialog { background-color: #f9fafc; }
            QLabel { color: #2d3748; font-size: 13px; font-weight: 500; }
            QLineEdit, QComboBox {
                background-color: white; color: #2d3748;
                border: 1px solid #e2e8f0; border-radius: 8px;
                padding: 8px 12px; font-size: 13px;
            }
            QLineEdit:focus { border: 2px solid #667eea; padding: 7px 11px; }
        """)

        self.load_settings()

    @staticmethod
    def _style_combo(combo: QComboBox):
        combo.setCursor(Qt.PointingHandCursor)

    def browse_workdir(self):
        d = QFileDialog.getExistingDirectory(self, "选择 Agent 工作目录", self.workdir_edit.text() or os.getcwd())
        if d:
            self.workdir_edit.setText(d)

    def load_settings(self):
        settings = QSettings("MyChatApp", "Settings")
        fmt = settings.value("api_format", ANTHROPIC)
        idx = 0 if fmt != RESPONSES else 1
        self.format_combo.setCurrentIndex(idx)
        self.api_key_edit.setText(settings.value("api_key", ""))
        self.base_url_edit.setText(settings.value("base_url", ""))
        self.model_edit.setText(settings.value("model", ""))
        effort_idx = self.thinking_combo.findData(
            normalize_thinking_effort(settings.value("thinking_effort", THINKING_OFF)))
        self.thinking_combo.setCurrentIndex(max(effort_idx, 0))
        self.vision_checkbox.setChecked(settings.value("supports_vision", False, type=bool))
        self.workdir_edit.setText(settings.value("workdir", ""))
        self.confirm_checkbox.setChecked(settings.value("confirm_tools", True, type=bool))

    def save_settings(self):
        settings = QSettings("MyChatApp", "Settings")
        settings.setValue("api_format", self.get_settings()["api_format"])
        settings.setValue("api_key", self.api_key_edit.text())
        settings.setValue("base_url", self.base_url_edit.text())
        settings.setValue("model", self.model_edit.text())
        settings.setValue("thinking_effort", self.thinking_combo.currentData())
        settings.setValue("supports_vision", self.vision_checkbox.isChecked())
        settings.setValue("workdir", self.workdir_edit.text())
        settings.setValue("confirm_tools", self.confirm_checkbox.isChecked())

    def toggle_key_visibility(self):
        if self.toggle_key_btn.isChecked():
            self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self.toggle_key_btn.setText("🔒")
        else:
            self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.toggle_key_btn.setText("👁")

    def get_settings(self):
        return {
            "api_format": self.format_combo.currentData(),
            "api_key": self.api_key_edit.text(),
            "base_url": self.base_url_edit.text(),
            "model": self.model_edit.text(),
            "thinking_effort": self.thinking_combo.currentData(),
            "supports_vision": self.vision_checkbox.isChecked(),
            "workdir": self.workdir_edit.text(),
            "confirm_tools": self.confirm_checkbox.isChecked(),
        }


# ==================== 进程监控线程 ====================
class ProcessMonitor(QThread):
    """监测新启动的 Office/WPS/浏览器进程，触发桌宠奔跑"""
    running = Signal()
    sleeping = Signal()

    OFFICE_PROCS = {'WINWORD.EXE', 'EXCEL.EXE', 'POWERPNT.EXE'}
    WPS_PROCS = {'WPS.EXE', 'ET.EXE', 'WPP.EXE'}
    BROWSER_PROCS = {
        'CHROME.EXE', 'MSEDGE.EXE', 'FIREFOX.EXE',
        '360SE.EXE', 'LIEBAO.EXE', 'SOGOUEXPLORER.EXE'
    }

    def __init__(self):
        super().__init__()
        self._stop_event = threading.Event()
        self._baseline_procs = set()
        self._baseline_browser_pids = set()
        self._prev_new_browser_windows = False
        self._prev_office_wps_running = False
        self._ctypes = None
        if sys.platform == 'win32':
            import ctypes
            self._ctypes = ctypes

    def run(self):
        try:
            import psutil
        except ImportError:
            return  # 未安装 psutil 时静默停用进程监控
        self._establish_baseline(psutil)
        self._prev_new_browser_windows = False
        self.sleeping.emit()
        while not self._stop_event.is_set():
            self._check(psutil)
            self._stop_event.wait(1)

    def _establish_baseline(self, psutil):
        current = {p.info['name'].upper(): p.info.get('pid')
                   for p in psutil.process_iter(['name', 'pid'])}
        for name in self.OFFICE_PROCS | self.WPS_PROCS:
            if name in current:
                self._baseline_procs.add(name)
        for name in self.BROWSER_PROCS:
            if name in current:
                self._baseline_browser_pids.add(current[name])

    def _check(self, psutil):
        try:
            self._check_office_wps(psutil)
            self._check_browser_windows()
        except Exception as e:
            print(f"进程检查异常: {e}")

    def _check_office_wps(self, psutil):
        current = {p.info['name'].upper() for p in psutil.process_iter(['name'])}
        target = current & (self.OFFICE_PROCS | self.WPS_PROCS)
        has_new = bool(target - self._baseline_procs)
        if has_new and not self._prev_office_wps_running:
            self.running.emit()
        elif not has_new and self._prev_office_wps_running:
            self.sleeping.emit()
        self._prev_office_wps_running = has_new

    def _check_browser_windows(self):
        if not self._ctypes:
            return
        current_pids = set()
        try:
            import psutil
            for proc in psutil.process_iter(['name', 'pid']):
                if proc.info['name'].upper() in self.BROWSER_PROCS:
                    current_pids.add(proc.info['pid'])
        except Exception:
            return
        new_pids = current_pids - self._baseline_browser_pids
        has_new = bool(new_pids) and self._browser_has_window(new_pids)
        if has_new and not self._prev_new_browser_windows:
            self.running.emit()
        elif not has_new and self._prev_new_browser_windows:
            self.sleeping.emit()
        self._prev_new_browser_windows = has_new

    def _browser_has_window(self, browser_pids):
        try:
            import ctypes
            user32 = ctypes.windll.user32
            found = [False]

            def enum_callback(hwnd, _):
                if not user32.IsWindowVisible(hwnd):
                    return 1
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in browser_pids:
                    found[0] = True
                    return 0
                return 1

            EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
            user32.EnumWindows(EnumWindowsProc(enum_callback), None)
            return found[0]
        except Exception as e:
            print(f"窗口检查异常: {e}")
            return False

    def stop(self):
        self._stop_event.set()


# ==================== 主窗口 ====================
class ChatWindow(QMainWindow):
    HISTORY_DIR = os.path.join(os.path.expanduser("~"), ".aichat")
    HISTORY_FILE = os.path.join(HISTORY_DIR, "conversations.json")

    SEND_BUTTON_STYLE = """
        QPushButton {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #667eea, stop:1 #764ba2);
            color: white; border: none; border-radius: 30px; font-size: 16px; font-weight: 600;
        }
        QPushButton:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #5a6fd6, stop:1 #6a4190); }
        QPushButton:disabled { background: #cbd5e0; }
    """
    STOP_BUTTON_STYLE = """
        QPushButton {
            background: #e53e3e; color: white; border: none;
            border-radius: 30px; font-size: 15px; font-weight: 600;
        }
        QPushButton:hover { background: #c53030; }
        QPushButton:pressed { background: #9b2c2c; }
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"AI 聊天机器人 {APP_VERSION}")
        self.resize(1200, 800)
        self.setMinimumSize(1000, 600)

        self.conversations: Dict[str, Dict] = {}
        self.current_conversation_id = None
        self.api_worker = None
        self.current_image_data_list: List[str] = []
        self._save_timer = None

        self.current_ai_widget: MessageWidget = None
        self._request_conversation_id = None
        self._loading_assistant_widget: MessageWidget = None
        self._request_active = False
        self._confirm_box: QMessageBox = None
        # 已取消但线程尚未结束的 worker：QThread 运行中销毁会 qFatal 直接退出进程，
        # 必须暂存引用，等 finished 后再释放
        self._retiring_workers: List[AgentWorker] = []

        settings = QSettings("MyChatApp", "Settings")
        self.api_format = settings.value("api_format", ANTHROPIC)
        if self.api_format not in (ANTHROPIC, RESPONSES):
            self.api_format = ANTHROPIC
        self.api_key = settings.value("api_key", "")
        self.base_url = settings.value("base_url", "")
        self.model = settings.value("model", "")
        self.thinking_effort = normalize_thinking_effort(
            settings.value("thinking_effort", THINKING_OFF))
        self.supports_vision = settings.value("supports_vision", False, type=bool)
        self.workdir = settings.value("workdir", "")
        self.confirm_tools = settings.value("confirm_tools", True, type=bool)
        self.agent_mode = settings.value("agent_mode", False, type=bool)
        self.settings = settings

        icon_pixmap = QPixmap(32, 32)
        icon_pixmap.fill(Qt.transparent)
        painter = QPainter(icon_pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(102, 126, 234))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(0, 0, 32, 32, 8, 8)
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Arial", 18, QFont.Bold))
        painter.drawText(icon_pixmap.rect(), Qt.AlignCenter, "AI")
        painter.end()
        self.setWindowIcon(QIcon(icon_pixmap))

        self.setup_ui()

        # 桌宠小狗 + 进程监控联动
        self.puppy_widget = PuppyWidget()
        self.puppy_widget._main_window = self
        self.process_monitor = ProcessMonitor()
        self.process_monitor.running.connect(lambda: self.puppy_widget.set_run_flag("process", True))
        self.process_monitor.sleeping.connect(lambda: self.puppy_widget.set_run_flag("process", False))
        self.process_monitor.start()

        if not self.load_conversations():
            self.create_new_conversation()

    # ---------- UI ----------

    def setup_ui(self):
        self.setStyleSheet("""
            QMainWindow { background: #f7fafc; }
            QScrollBar:vertical { background: #edf2f7; width: 8px; border-radius: 4px; }
            QScrollBar::handle:vertical { background: #cbd5e0; border-radius: 4px; min-height: 30px; }
            QScrollBar::handle:vertical:hover { background: #a0aec0; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
            QListWidget { outline: none; }
        """)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setStyleSheet("QSplitter::handle { background: #e2e8f0; }")
        main_layout.addWidget(splitter)

        # 左侧面板
        left_widget = QWidget()
        left_widget.setFixedWidth(280)
        left_widget.setStyleSheet("background: #1a202c;")
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(16, 24, 16, 24)
        left_layout.setSpacing(16)

        header = QHBoxLayout()
        logo_label = QLabel("🐶")
        logo_label.setStyleSheet("font-size: 32px; background: transparent;")
        header.addWidget(logo_label)
        title_label = QLabel("AI Chat")
        title_label.setStyleSheet("color: white; font-size: 24px; font-weight: 600; background: transparent;")
        header.addWidget(title_label)
        header.addStretch()
        left_layout.addLayout(header)

        new_conv_btn = QPushButton("➕  新建对话")
        new_conv_btn.setCursor(Qt.PointingHandCursor)
        new_conv_btn.clicked.connect(self.create_new_conversation)
        new_conv_btn.setStyleSheet("""
            QPushButton {
                background: #2d3748; color: #e2e8f0;
                border: 2px dashed #4a5568; border-radius: 16px;
                padding: 14px; font-size: 16px; font-weight: 600;
            }
            QPushButton:hover { background: #3d4758; border-color: #718096; color: white; }
            QPushButton:pressed { background: #1e2a3a; }
        """)
        left_layout.addWidget(new_conv_btn)

        list_label = QLabel("对话历史")
        list_label.setStyleSheet(
            "color: #a0aec0; font-size: 12px; margin-top: 8px; background: transparent; letter-spacing: 0.5px;")
        left_layout.addWidget(list_label)

        self.conversation_list = QListWidget()
        self.conversation_list.setStyleSheet("""
            QListWidget { background: transparent; border: none; outline: none; font-size: 14px; }
            QListWidget::item { color: #e2e8f0; padding: 14px 16px; border-radius: 12px; margin: 2px 0; }
            QListWidget::item:hover { background: #2d3748; }
            QListWidget::item:selected { background: #4a5568; color: white; }
        """)
        self.conversation_list.currentItemChanged.connect(self.on_conversation_changed)
        self.conversation_list.itemDoubleClicked.connect(self.rename_conversation)
        self.conversation_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.conversation_list.customContextMenuRequested.connect(self.show_context_menu)
        left_layout.addWidget(self.conversation_list)

        settings_btn = QPushButton("⚙️  设置")
        settings_btn.setCursor(Qt.PointingHandCursor)
        settings_btn.clicked.connect(self.open_settings)
        settings_btn.setStyleSheet("""
            QPushButton { background: #2d3748; color: #a0aec0; border: none; border-radius: 12px; padding: 14px; font-size: 15px; }
            QPushButton:hover { background: #3d4758; color: white; }
        """)
        left_layout.addWidget(settings_btn)

        clear_btn = QPushButton("🗑️  清除所有历史")
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.clicked.connect(self.clear_all_history)
        clear_btn.setStyleSheet("""
            QPushButton { background: #2d3748; color: #a0aec0; border: none; border-radius: 12px; padding: 14px; font-size: 15px; }
            QPushButton:hover { background: #c53030; color: white; }
        """)
        left_layout.addWidget(clear_btn)

        splitter.addWidget(left_widget)

        # 右侧聊天区
        right_widget = QWidget()
        right_widget.setStyleSheet("background: #f9fafc;")
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        header_bar = QFrame()
        header_bar.setFixedHeight(70)
        header_bar.setStyleSheet("background: white; border-bottom: 1px solid #e2e8f0;")
        header_layout = QHBoxLayout(header_bar)
        header_layout.setContentsMargins(28, 0, 28, 0)
        self.conversation_title = QLabel("新对话")
        self.conversation_title.setStyleSheet("font-size: 20px; font-weight: 600; color: #1a202c;")
        header_layout.addWidget(self.conversation_title)
        header_layout.addStretch()
        self.status_label = QLabel("● 就绪")
        self.status_label.setStyleSheet("color: #48bb78; font-size: 14px; font-weight: 500;")
        header_layout.addWidget(self.status_label)
        right_layout.addWidget(header_bar)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("QScrollArea { border: none; background: #f9fafc; }")
        self.messages_container = QWidget()
        self.messages_layout = QVBoxLayout(self.messages_container)
        self.messages_layout.setContentsMargins(0, 20, 0, 20)
        self.messages_layout.setSpacing(8)
        self.messages_layout.addStretch()
        self.scroll_area.setWidget(self.messages_container)
        right_layout.addWidget(self.scroll_area)

        # 底部输入区
        input_container = QFrame()
        input_container.setFixedHeight(230)
        input_container.setStyleSheet("background: white; border-top: 1px solid #e2e8f0;")
        input_layout = QVBoxLayout(input_container)
        input_layout.setContentsMargins(28, 16, 28, 20)
        input_layout.setSpacing(8)

        self.image_preview_container = QWidget()
        self.image_preview_container.setVisible(False)
        self.image_preview_layout = QHBoxLayout(self.image_preview_container)
        self.image_preview_layout.setContentsMargins(0, 0, 0, 0)
        self.image_preview_layout.setSpacing(8)
        self.image_preview_layout.addStretch()
        input_layout.addWidget(self.image_preview_container)

        input_frame = QFrame()
        input_frame.setStyleSheet("""
            QFrame { background: #f7fafc; border: 2px solid #e2e8f0; border-radius: 24px; }
        """)
        input_frame_layout = QHBoxLayout(input_frame)
        input_frame_layout.setContentsMargins(20, 12, 12, 12)
        input_frame_layout.setSpacing(10)

        self.attach_btn = QPushButton("📎")
        self.attach_btn.setCursor(Qt.PointingHandCursor)
        self.attach_btn.setFixedSize(40, 40)
        self.attach_btn.clicked.connect(self.upload_file)
        self.attach_btn.setStyleSheet("""
            QPushButton { background: transparent; border: none; font-size: 20px; }
            QPushButton:hover { background: #e2e8f0; border-radius: 20px; }
        """)
        input_frame_layout.addWidget(self.attach_btn, 0, Qt.AlignBottom)

        self.input_edit = QTextEdit()
        self.input_edit.setPlaceholderText("输入消息... (Enter 发送，Shift+Enter 换行)")
        self.input_edit.setFixedHeight(76)
        self.input_edit.setStyleSheet("""
            QTextEdit {
                background: transparent; border: none; font-size: 16px;
                color: #2d3748; selection-background-color: #667eea; selection-color: white;
            }
        """)
        self.input_edit.textChanged.connect(self.auto_resize_input)
        input_frame_layout.addWidget(self.input_edit, 1)

        # Agent 模式开关
        self.agent_btn = QPushButton("🤖 Agent")
        self.agent_btn.setCheckable(True)
        self.agent_btn.setChecked(self.agent_mode)
        self.agent_btn.setCursor(Qt.PointingHandCursor)
        self.agent_btn.setFixedHeight(40)
        self.agent_btn.setToolTip(
            "开启后模型可调用 read/write/edit/glob/grep/bash 工具\n"
            f"工作目录：{self.workdir or os.getcwd()}")
        self.agent_btn.clicked.connect(self.toggle_agent_mode)
        self.agent_btn.setStyleSheet("""
            QPushButton {
                background: white; color: #718096; border: 1px solid #e2e8f0;
                border-radius: 20px; padding: 0 14px; font-size: 13px; font-weight: 600;
            }
            QPushButton:checked {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #38a169, stop:1 #2f855a);
                color: white; border: none;
            }
        """)
        input_frame_layout.addWidget(self.agent_btn, 0, Qt.AlignBottom)

        self.send_btn = QPushButton("发送")
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setFixedSize(90, 48)
        self.send_btn.clicked.connect(self.on_send_clicked)
        self.send_btn.setStyleSheet(self.SEND_BUTTON_STYLE)
        input_frame_layout.addWidget(self.send_btn, 0, Qt.AlignBottom)

        input_layout.addWidget(input_frame)

        hint_label = QLabel("AI 生成内容仅供参考。支持文本/图片上传；Agent 模式下可自动读写文件、执行命令。")
        hint_label.setStyleSheet("color: #a0aec0; font-size: 12px; padding-left: 8px; margin: 0;")
        input_layout.addWidget(hint_label)

        right_layout.addWidget(input_container)
        splitter.addWidget(right_widget)
        splitter.setSizes([280, 920])
        self.input_edit.installEventFilter(self)

    def toggle_agent_mode(self, checked: bool):
        self.agent_mode = checked
        self.settings.setValue("agent_mode", checked)

    def on_send_clicked(self):
        """发送按钮双态：空闲时发送，请求进行中变为停止"""
        if self._request_active:
            self._stop_generation()
        else:
            self.send_message()

    def _set_requesting_state(self, active: bool):
        self._request_active = active
        if active:
            self.send_btn.setText("⏹ 停止")
            self.send_btn.setToolTip("停止生成")
            self.send_btn.setStyleSheet(self.STOP_BUTTON_STYLE)
        else:
            self.send_btn.setText("发送")
            self.send_btn.setToolTip("")
            self.send_btn.setStyleSheet(self.SEND_BUTTON_STYLE)
        self.send_btn.setEnabled(True)

    def _stop_generation(self):
        """用户主动停止生成：取消 worker，封存已生成内容并回写已完成轮次"""
        if self.api_worker and self.api_worker.isRunning():
            self.api_worker.stop()  # 可能正卡在工具确认等待，先按拒绝唤醒
        self._cancel_current_request()
        self.save_conversations()

    # ---------- 设置与校验 ----------

    def validate_api_settings(self) -> tuple:
        if not self.api_key or not self.api_key.strip():
            return False, "API Key 不能为空"
        if not self.base_url or not self.base_url.strip():
            return False, "Base URL 不能为空"
        try:
            result = urlparse(self.base_url.strip())
            if not all([result.scheme, result.netloc]):
                return False, "Base URL 格式无效，请输入完整的URL（如 https://api.example.com/v1）"
        except Exception as e:
            return False, f"Base URL 格式无效: {str(e)}"
        if not self.model or not self.model.strip():
            return False, "模型名称不能为空"
        return True, ""

    def open_settings(self):
        dialog = SettingsDialog(self)
        if dialog.exec() == QDialog.Accepted:
            s = dialog.get_settings()
            self.api_format = s["api_format"]
            self.api_key = s["api_key"]
            self.base_url = s["base_url"]
            self.model = s["model"]
            self.thinking_effort = normalize_thinking_effort(s["thinking_effort"])
            self.supports_vision = s["supports_vision"]
            self.workdir = s["workdir"]
            self.confirm_tools = s["confirm_tools"]
            dialog.save_settings()
            self.agent_btn.setToolTip(
                "开启后模型可调用 read/write/edit/glob/grep/bash 工具\n"
                f"工作目录：{self.workdir or os.getcwd()}")
            QMessageBox.information(self, "设置已保存", "配置已更新。")

    # ---------- 文件上传 ----------

    def upload_file(self):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择文件（可多选图片）",
            "",
            "图片与文本(*.png *.jpg *.jpeg *.bmp *.gif *.webp *.txt *.py *.md *.json *.xml *.html *.css *.js);;"
            "图片文件(*.png *.jpg *.jpeg *.bmp *.gif *.webp);;"
            "文本文件(*.txt *.py *.md *.json *.xml *.html *.css *.js);;所有文件(*)"
        )
        if not file_paths:
            return

        for file_path in file_paths:
            ext = os.path.splitext(file_path)[1].lower()

            if ext in ['.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp']:
                try:
                    image = QImage(file_path)
                    if image.isNull():
                        QMessageBox.warning(self, "错误", f"无法加载图片: {file_path}")
                        continue
                    byte_array = QByteArray()
                    buffer = QBuffer(byte_array)
                    buffer.open(QBuffer.ReadWrite)
                    image.save(buffer, "PNG")
                    buffer.close()
                    image_base64 = base64.b64encode(bytes(byte_array)).decode('ascii')
                    self.current_image_data_list.append(image_base64)
                    self._add_image_preview(image, image_base64)
                except Exception as e:
                    QMessageBox.critical(self, "错误", f"读取图片失败: {e}")

            elif ext in ['.txt', '.py', '.md', '.json', '.html', '.css', '.js', '.xml']:
                try:
                    content = self._read_file_with_encoding(file_path)
                    lang_map = {
                        '.py': 'python', '.js': 'javascript', '.html': 'html',
                        '.css': 'css', '.json': 'json', '.xml': 'xml',
                        '.md': 'markdown', '.txt': ''
                    }
                    lang = lang_map.get(ext, '')
                    safe_content = content.replace('```', '`\u200B`\u200B`')
                    current_text = self.input_edit.toPlainText()
                    wrapped = f"\n[文件: {os.path.basename(file_path)}]\n```{lang}\n{safe_content}\n```\n"
                    self.input_edit.setPlainText(f"{current_text}{wrapped}")
                    self.auto_resize_input()
                except Exception as e:
                    QMessageBox.critical(self, "错误", f"读取文件失败: {e}")

    @staticmethod
    def _read_file_with_encoding(file_path: str) -> str:
        for encoding in ['utf-8', 'gbk', 'gb2312', 'gb18030', 'big5', 'shift_jis', 'latin-1']:
            try:
                with open(file_path, 'r', encoding=encoding) as f:
                    return f.read()
            except (UnicodeDecodeError, UnicodeError):
                continue
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()

    def _add_image_preview(self, image: QImage, image_base64: str):
        thumb_container = QWidget()
        thumb_layout = QVBoxLayout(thumb_container)
        thumb_layout.setContentsMargins(0, 0, 0, 0)
        thumb_layout.setSpacing(2)

        thumb_label = QLabel()
        pixmap = QPixmap.fromImage(image).scaled(80, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        thumb_label.setPixmap(pixmap)
        thumb_label.setStyleSheet("""
            QLabel { background: #f7fafc; border: 2px solid #e2e8f0; border-radius: 8px; padding: 4px; }
        """)
        thumb_layout.addWidget(thumb_label)

        remove_btn = QPushButton("✕")
        remove_btn.setFixedSize(24, 24)
        remove_btn.setCursor(Qt.PointingHandCursor)
        remove_btn.setStyleSheet("""
            QPushButton {
                background: #e53e3e; color: white; border: none;
                border-radius: 12px; font-size: 12px; font-weight: bold;
            }
            QPushButton:hover { background: #c53030; }
        """)
        remove_btn.clicked.connect(
            lambda checked, b64=image_base64, w=thumb_container: self._remove_image_preview(b64, w))
        thumb_layout.addWidget(remove_btn, 0, Qt.AlignCenter)

        self.image_preview_layout.insertWidget(self.image_preview_layout.count() - 1, thumb_container)
        self.image_preview_container.setVisible(True)

    def _remove_image_preview(self, image_base64: str, widget: QWidget):
        if image_base64 in self.current_image_data_list:
            self.current_image_data_list.remove(image_base64)
        widget.setVisible(False)
        widget.deleteLater()
        if not self.current_image_data_list:
            self.image_preview_container.setVisible(False)

    def _clear_image_previews(self):
        while self.image_preview_layout.count() > 1:
            item = self.image_preview_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.image_preview_container.setVisible(False)

    def auto_resize_input(self):
        doc = self.input_edit.document()
        height = min(doc.size().height() + 20, 160)
        self.input_edit.setFixedHeight(max(height, 56))

    def eventFilter(self, obj, event):
        if obj == self.input_edit and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key_Return:
                if event.modifiers() == Qt.ControlModifier:
                    self.input_edit.insertPlainText('\n')
                    return True
                elif event.modifiers() == Qt.NoModifier:
                    self.send_message()
                    return True
                elif event.modifiers() == Qt.ShiftModifier:
                    self.input_edit.insertPlainText('\n')
                    return True
        return super().eventFilter(obj, event)

    # ---------- 会话管理 ----------

    def _parse_timestamp_for_sort(self, timestamp_str: str) -> datetime:
        if not timestamp_str:
            return datetime.min
        try:
            return datetime.fromisoformat(timestamp_str)
        except ValueError:
            pass
        try:
            match = re.match(r'^(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})$', timestamp_str)
            if match:
                month, day, hour, minute = map(int, match.groups())
                now = datetime.now()
                dt = datetime(now.year, month, day, hour, minute)
                if dt > now:
                    dt = datetime(now.year - 1, month, day, hour, minute)
                return dt
        except (ValueError, TypeError):
            pass
        return datetime.min

    def _cancel_current_request(self):
        """取消进行中的请求（停止按钮 / 切换或删除会话时调用）"""
        if self.api_worker is not None:
            worker = self.api_worker
            cid = self._request_conversation_id
            self.api_worker = None
            if worker.isRunning():
                worker.stop()
                try:
                    worker.stream_chunk.disconnect()
                    worker.thinking_chunk.disconnect()
                    worker.tool_call_started.disconnect()
                    worker.tool_call_finished.disconnect()
                    worker.confirm_requested.disconnect()
                    worker.history_ready.disconnect()
                    worker.turn_finished.disconnect()
                    worker.error_occurred.disconnect()
                except (RuntimeError, TypeError):
                    pass
                # 保留最后一次历史回写：被截断的半轮内容也要落盘，
                # 否则屏幕上已显示的正文/工具卡在重新加载后消失
                worker.history_ready.connect(
                    lambda msgs, cid=cid: self._on_cancelled_history(cid, msgs))
                # stop() 只是异步置位，线程此刻往往仍阻塞在流读取/退避等待中；
                # 直接丢引用会销毁运行中的 QThread（qFatal 杀进程）。
                # 暂存到回收列表，等 finished 后再释放。
                self._retiring_workers.append(worker)
                worker.finished.connect(worker.deleteLater)
                worker.finished.connect(
                    lambda w=worker: self._retiring_workers.remove(w)
                    if w in self._retiring_workers else None)

        # 确认框还开着的话关掉（用户已放弃请求，视为拒绝）
        if self._confirm_box is not None:
            try:
                self._confirm_box.reject()
            except RuntimeError:
                pass
            self._confirm_box = None

        if self.current_ai_widget:
            try:
                self.current_ai_widget.seal_stream()
                self.current_ai_widget.mark_interrupted()
            except RuntimeError:
                pass

        self.current_ai_widget = None
        self._request_conversation_id = None
        self.puppy_widget.set_ai_state("idle")

        self.status_label.setText("● 就绪")
        self.status_label.setStyleSheet("color: #48bb78; font-size: 14px; font-weight: 500;")
        self._set_requesting_state(False)

    def _on_cancelled_history(self, cid, messages):
        """取消后 worker 的最后回写：把已生成的半轮内容落盘到原会话"""
        if cid and cid in self.conversations:
            self.conversations[cid]['messages'] = messages
            self.save_conversations()

    def create_new_conversation(self):
        self._cancel_current_request()
        conv_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()
        self.conversations[conv_id] = {
            'id': conv_id,
            'title': f'新对话 {len(self.conversations) + 1}',
            'messages': [],
            'created_at': timestamp
        }
        item = QListWidgetItem(f"💬 {self.conversations[conv_id]['title']}")
        item.setData(Qt.ItemDataRole.UserRole, conv_id)
        self.conversation_list.insertItem(0, item)
        self.conversation_list.setCurrentItem(item)
        self.current_conversation_id = conv_id
        self.update_conversation_title()
        self.save_conversations()

    def on_conversation_changed(self, current, previous):
        if current:
            self._cancel_current_request()
            conv_id = current.data(Qt.ItemDataRole.UserRole)
            self.current_conversation_id = conv_id
            self.update_conversation_title()
            self.load_conversation_messages()

    def update_conversation_title(self):
        if self.current_conversation_id and self.current_conversation_id in self.conversations:
            self.conversation_title.setText(self.conversations[self.current_conversation_id]['title'])

    def load_conversation_messages(self):
        self.clear_messages()
        if not self.current_conversation_id:
            return
        self._loading_assistant_widget = None
        messages = self.conversations[self.current_conversation_id]['messages']
        for raw_msg in messages:
            msg = normalize_history_message(raw_msg)
            content = msg.get('content')
            role = msg.get('role')

            if isinstance(content, list):
                block_types = {b.get('type') for b in content}
                # 仅含工具结果：挂到上一个 AI 气泡的工具卡片上
                if role == 'user' and block_types and block_types <= {'tool_result'}:
                    for b in content:
                        if self._loading_assistant_widget:
                            self._loading_assistant_widget.set_tool_result(
                                b.get('tool_use_id', ''),
                                str(b.get('content', '')),
                                not str(b.get('content', '')).startswith('error:'))
                    continue
                text = "\n\n".join(b.get('text', '') for b in content if b.get('type') == 'text')
                images = [b['source']['data'] for b in content
                          if b.get('type') == 'image' and b.get('source')]
                if role == 'user':
                    self.add_message_widget('user', text, images if images else None)
                else:
                    widget = self.add_message_widget('assistant', content)
                    self._loading_assistant_widget = widget
            else:
                widget = self.add_message_widget(role, content or '')
                if role != 'user':
                    self._loading_assistant_widget = widget
        self.scroll_to_bottom()

    def clear_messages(self):
        while self.messages_layout.count() > 1:
            item = self.messages_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def add_message_widget(self, role: str, content, image_data_list=None) -> MessageWidget:
        self.messages_layout.takeAt(self.messages_layout.count() - 1)  # 摘除尾部 stretch
        msg_widget = MessageWidget(role, content, image_data_list)
        self.messages_layout.addWidget(msg_widget)
        self.messages_layout.addStretch()
        return msg_widget

    def scroll_to_bottom(self):
        QTimer.singleShot(100, lambda: self.scroll_area.verticalScrollBar().setValue(
            self.scroll_area.verticalScrollBar().maximum()
        ))

    def _check_model_supports_vision(self) -> bool:
        if self.supports_vision:
            return True
        model_lower = (self.model or '').lower()
        vision_keywords = [
            'vision', 'vl', 'visual', 'multimodal', 'mm',
            '4o', 'gpt-4-turbo', 'gpt-4-vision', 'gpt-5',
            'claude-3', 'claude-3.5', 'claude-sonnet', 'claude-opus', 'claude-haiku',
            'gemini', 'qwen-vl', 'glm-4v', 'deepseek-vl',
            'llava', 'cogvlm', 'internvl', 'yi-vl'
        ]
        return any(kw in model_lower for kw in vision_keywords)

    # ---------- 发送与 agent 回合 ----------

    def send_message(self):
        # 请求进行中拒绝再次发送（Enter 键也走这里）：
        # 否则会覆盖 api_worker 引用，产生无人回收的野生线程 + 历史串写
        if self._request_active:
            return
        user_text = self.input_edit.toPlainText().strip()
        has_images = bool(self.current_image_data_list)

        is_valid, error_msg = self.validate_api_settings()
        if not is_valid:
            QMessageBox.warning(self, "配置缺失", f"请先在设置中填写正确的配置：{error_msg}")
            return

        if has_images and not self._check_model_supports_vision():
            skip_warning = self.settings.value("skip_vision_warning", False, type=bool)
            if not skip_warning:
                dialog = QDialog(self)
                dialog.setWindowTitle("模型可能不支持图片")
                dialog.setModal(True)
                dialog.setMinimumWidth(420)
                layout = QVBoxLayout(dialog)
                layout.setSpacing(16)
                layout.setContentsMargins(24, 24, 24, 24)

                info_label = QLabel(
                    f"⚠️ 当前模型「{self.model}」可能不支持图片输入。\n\n"
                    "如果您确定该模型支持多模态，可以直接发送。\n"
                    "API 会返回具体的错误信息，您可据此调整。\n\n"
                    "您也可以在设置中勾选「此模型支持图片输入」来跳过此检测。"
                )
                info_label.setWordWrap(True)
                layout.addWidget(info_label)

                skip_checkbox = QCheckBox("不再提示此警告")
                layout.addWidget(skip_checkbox)

                btn_layout = QHBoxLayout()
                btn_layout.addStretch()
                cancel_btn = QPushButton("取消")
                cancel_btn.clicked.connect(dialog.reject)
                btn_layout.addWidget(cancel_btn)
                send_btn = QPushButton("发送")
                send_btn.clicked.connect(dialog.accept)
                btn_layout.addWidget(send_btn)
                layout.addLayout(btn_layout)

                if dialog.exec() == QDialog.DialogCode.Rejected:
                    return
                if skip_checkbox.isChecked():
                    self.settings.setValue("skip_vision_warning", True)

        if not user_text and not has_images:
            return

        # 组装内部格式消息（Anthropic 风格内容块）
        if has_images:
            message_content = []
            if user_text:
                message_content.append({"type": "text", "text": user_text})
            for image_data in self.current_image_data_list:
                message_content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": image_data}
                })
        else:
            message_content = user_text

        image_count = len(self.current_image_data_list)
        display_text = user_text or (
            f"[发送了{image_count}张图片]" if image_count > 1 else "[发送了一张图片]")
        current_images_for_display = self.current_image_data_list.copy()

        self.input_edit.clear()
        self.current_image_data_list = []
        self._clear_image_previews()

        conversation = self.conversations[self.current_conversation_id]
        conversation['messages'].append({"role": "user", "content": message_content})
        self.add_message_widget("user", display_text, current_images_for_display)
        self.scroll_to_bottom()

        if len(conversation['messages']) == 1:
            new_title = display_text[:20] + ('...' if len(display_text) > 20 else '')
            conversation['title'] = new_title
            self.update_conversation_title()
            self.conversation_list.currentItem().setText(f"💬 {new_title}")

        status_text = "● Agent 工作中..." if self.agent_mode else "● AI 正在思考..."
        self.status_label.setText(status_text)
        self.status_label.setStyleSheet("color: #ed8936; font-size: 14px; font-weight: 500;")
        self._set_requesting_state(True)
        self.puppy_widget.set_ai_state("thinking")

        self.current_ai_widget = self.add_message_widget("assistant", "")
        self._request_conversation_id = self.current_conversation_id

        self.api_worker = AgentWorker(
            messages=conversation['messages'],
            fmt=self.api_format,
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            agent_mode=self.agent_mode,
            workdir=self.workdir or os.getcwd(),
            confirm_tools=self.confirm_tools,
            thinking_effort=self.thinking_effort,
        )
        self.api_worker.stream_chunk.connect(self.on_stream_chunk)
        self.api_worker.thinking_chunk.connect(self.on_thinking_chunk)
        self.api_worker.tool_call_started.connect(self.on_tool_call_started)
        self.api_worker.tool_call_finished.connect(self.on_tool_call_finished)
        self.api_worker.confirm_requested.connect(self.on_confirm_requested)
        self.api_worker.history_ready.connect(self.on_history_ready)
        self.api_worker.turn_finished.connect(self.on_stream_finished)
        self.api_worker.error_occurred.connect(self.on_api_error)
        self.api_worker.start()

    def _request_stale(self) -> bool:
        """请求所属会话与当前会话不一致（用户已切换）"""
        return self._request_conversation_id != self.current_conversation_id

    def on_stream_chunk(self, chunk: str):
        if self._request_stale():
            return
        if self.current_ai_widget:
            try:
                self.current_ai_widget.stream_append(chunk)
            except RuntimeError:
                self.current_ai_widget = None
                return
        self.scroll_to_bottom()

    def on_thinking_chunk(self, chunk: str):
        if self._request_stale():
            return
        if self.current_ai_widget:
            try:
                self.current_ai_widget.stream_thinking(chunk)
            except RuntimeError:
                self.current_ai_widget = None
                return
        self.scroll_to_bottom()

    def on_tool_call_started(self, call_id: str, name: str, args_json: str):
        if self._request_stale():
            return
        # 模型开始执行工具：小狗埋头干活
        self.puppy_widget.set_ai_state("working")
        if self.current_ai_widget:
            try:
                self.current_ai_widget.seal_stream()
                self.current_ai_widget.add_tool_call(call_id, name, args_json)
            except RuntimeError:
                pass
        self.scroll_to_bottom()

    def on_tool_call_finished(self, call_id: str, result: str, ok: bool):
        if self._request_stale():
            return
        # 工具执行完毕：小狗回到思考状态（消化结果、继续下一步）
        self.puppy_widget.set_ai_state("thinking")
        if self.current_ai_widget:
            try:
                self.current_ai_widget.set_tool_result(call_id, result, ok)
            except RuntimeError:
                pass
        self.scroll_to_bottom()

    def on_confirm_requested(self, name: str, args_display: str, approver):
        """GUI 线程弹出工具确认（非模态：等待期间仍可点「⏹ 停止」整体取消）"""
        preview = args_display if len(args_display) <= 800 else args_display[:800] + "…"
        box = QMessageBox(
            QMessageBox.Icon.Question, "确认执行工具",
            f"Agent 请求执行工具「{name}」：\n\n{preview}\n\n是否允许执行？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            self,
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        # QMessageBox 默认 ApplicationModal，会挡住主窗口的「⏹ 停止」；
        # 显式改为 NonModal：等待确认期间仍可停止生成/切换会话
        box.setWindowModality(Qt.NonModal)
        decided = []

        def decide_once(ok: bool):
            if not decided:
                decided.append(ok)
                approver.decide(ok)

        def on_finished(_result):
            # ESC / 关闭 / 停止取消等路径：尚未决定则一律按拒绝处理
            decide_once(False)
            if self._confirm_box is box:
                self._confirm_box = None

        # 注意：不能连按钮的 clicked——QMessageBox 内部先于自定义槽触发
        # accept()/reject()，会导致 finished 的兜底拒绝抢占 decided 名额；
        # accepted/rejected 语义正好覆盖"是/否/ESC/关闭"全部路径
        box.accepted.connect(lambda: decide_once(True))
        box.rejected.connect(lambda: decide_once(False))

        def on_finished(_result):
            if self._confirm_box is box:
                self._confirm_box = None

        box.finished.connect(on_finished)
        self._confirm_box = box
        box.show()

    def on_history_ready(self, messages):
        cid = self._request_conversation_id
        if cid and cid in self.conversations:
            self.conversations[cid]['messages'] = messages

    def on_stream_finished(self):
        if self._request_stale():
            self._request_conversation_id = None
            return

        if self.current_ai_widget:
            try:
                self.current_ai_widget.seal_stream()
            except RuntimeError:
                pass

        self.current_ai_widget = None
        self._request_conversation_id = None
        # 回合完成：小狗开心庆祝一下再回到平静状态
        self.puppy_widget.set_ai_state("happy")

        self.status_label.setText("● 就绪")
        self.status_label.setStyleSheet("color: #48bb78; font-size: 14px; font-weight: 500;")
        self._set_requesting_state(False)
        self.save_conversations()

    def on_api_error(self, error_msg: str):
        if self._request_stale():
            self._request_conversation_id = None
            return

        # 出错也要封存流式段：否则思考卡标题会停在「思考中…」
        if self.current_ai_widget:
            try:
                self.current_ai_widget.seal_stream()
            except RuntimeError:
                pass

        QMessageBox.critical(self, "API错误", f"请求失败：{error_msg}")
        self.status_label.setText("● 错误")
        self.status_label.setStyleSheet("color: #f56565; font-size: 14px; font-weight: 500;")
        self._set_requesting_state(False)
        self.puppy_widget.set_ai_state("sad")

        # 仅在气泡完全无内容（正文/工具卡片/思考卡）时移除；
        # 已显示的工具调用卡片要保留——历史已回写，重新加载能看到
        if self.current_ai_widget and self.current_ai_widget.is_empty():
            try:
                index = self.messages_layout.indexOf(self.current_ai_widget)
                if index >= 0:
                    self.messages_layout.takeAt(index)
                    self.current_ai_widget.deleteLater()
            except RuntimeError:
                pass

        self.current_ai_widget = None
        self._request_conversation_id = None
        self.save_conversations()

    # ---------- 历史持久化 ----------

    def show_context_menu(self, pos):
        item = self.conversation_list.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background: white; border: 1px solid #e2e8f0;
                border-radius: 12px; padding: 6px; font-size: 14px;
            }
            QMenu::item { padding: 10px 28px 10px 16px; border-radius: 8px; color: #2d3748; }
            QMenu::item:selected { background: #edf2f7; color: #1a202c; }
        """)
        rename_action = menu.addAction("✏️ 重命名")
        delete_action = menu.addAction("🗑️ 删除")
        action = menu.exec(self.conversation_list.mapToGlobal(pos))
        if action == rename_action:
            self.rename_conversation(item)
        elif action == delete_action:
            self.delete_conversation(item)

    def save_conversations(self, delay: bool = True):
        import json
        if delay:
            if self._save_timer is None:
                self._save_timer = QTimer()
                self._save_timer.setSingleShot(True)
                self._save_timer.timeout.connect(lambda: self.save_conversations(delay=False))
            self._save_timer.start(500)
            return
        try:
            os.makedirs(self.HISTORY_DIR, exist_ok=True)
            data = {
                "version": 2,
                "conversations": self.conversations,
                "last_updated": datetime.now().isoformat()
            }
            temp_file = self.HISTORY_FILE + ".tmp"
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            # os.replace 为原子替换：不存在"删了旧档还没写入新档"的丢档窗口
            os.replace(temp_file, self.HISTORY_FILE)
        except Exception as e:
            print(f"保存对话历史失败: {e}")

    def load_conversations(self) -> bool:
        import json
        try:
            if not os.path.exists(self.HISTORY_FILE):
                return False
            with open(self.HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)

            version = data.get("version", 0)
            if version not in (1, 2):
                print("对话历史版本不兼容，将创建新对话")
                return False

            self.conversations = data.get("conversations", {})
            if not self.conversations:
                return False

            # 旧版消息逐条规范化（图片块等结构转换）
            if version < 2:
                for conv in self.conversations.values():
                    conv['messages'] = [normalize_history_message(m) for m in conv.get('messages', [])]

            sorted_convs = sorted(
                self.conversations.items(),
                key=lambda x: self._parse_timestamp_for_sort(x[1].get('created_at', '')),
                reverse=True
            )
            self.conversation_list.clear()
            for conv_id, conv_data in sorted_convs:
                item = QListWidgetItem(f"💬 {conv_data.get('title', '未命名')}")
                item.setData(Qt.ItemDataRole.UserRole, conv_id)
                self.conversation_list.addItem(item)

            if self.conversation_list.count() > 0:
                self.conversation_list.setCurrentRow(0)
                return True
            return False
        except json.JSONDecodeError:
            print("对话历史文件损坏，将创建新对话")
            return False
        except Exception as e:
            print(f"加载对话历史失败: {e}")
            return False

    def clear_all_history(self):
        self._cancel_current_request()
        reply = QMessageBox.question(
            self, "确认清除",
            "确定要清除所有对话历史吗？此操作不可恢复！",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.conversations.clear()
            self.conversation_list.clear()
            self.create_new_conversation()
            try:
                if os.path.exists(self.HISTORY_FILE):
                    os.remove(self.HISTORY_FILE)
            except Exception as e:
                print(f"删除历史文件失败: {e}")

    def rename_conversation(self, item):
        conv_id = item.data(Qt.ItemDataRole.UserRole)
        current_title = self.conversations[conv_id]['title']
        new_title, ok = QInputDialog.getText(self, "重命名", "输入新名称:", QLineEdit.Normal, current_title)
        if ok and new_title.strip():
            self.conversations[conv_id]['title'] = new_title.strip()
            item.setText(f"💬 {new_title.strip()}")
            if conv_id == self.current_conversation_id:
                self.update_conversation_title()
            self.save_conversations()

    def delete_conversation(self, item):
        conv_id = item.data(Qt.ItemDataRole.UserRole)
        reply = QMessageBox.question(
            self, "确认删除", "确定要删除这个对话吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            del self.conversations[conv_id]
            self.conversation_list.takeItem(self.conversation_list.row(item))
            self.save_conversations()
            if conv_id == self.current_conversation_id:
                self._cancel_current_request()
                if self.conversation_list.count() > 0:
                    self.conversation_list.setCurrentRow(0)
                else:
                    self.create_new_conversation()

    # ---------- 窗口事件 ----------

    def changeEvent(self, event):
        if event.type() == QEvent.WindowStateChange and self.isMinimized():
            pass
        super().changeEvent(event)

    def request_real_exit(self):
        """请求真正退出程序（小狗菜单的退出项等程序化路径）"""
        self._force_quit = True
        self.close()

    def _do_exit_cleanup(self):
        if self._save_timer is not None:
            self._save_timer.stop()
            self._save_timer.deleteLater()
            self._save_timer = None

        self.save_conversations(delay=False)

        if self.api_worker and self.api_worker.isRunning():
            self.api_worker.stop()
            if not self.api_worker.wait(3000):
                self.api_worker.terminate()

        # 已取消但尚未结束的 worker 同样要等完/终止，避免退出时销毁运行中的线程
        for worker in list(self._retiring_workers):
            worker.stop()
            if not worker.wait(3000):
                worker.terminate()
        self._retiring_workers.clear()

        if hasattr(self, 'process_monitor') and self.process_monitor.isRunning():
            self.process_monitor.stop()
            if not self.process_monitor.wait(3000):
                self.process_monitor.terminate()

    def closeEvent(self, event):
        app = QApplication.instance()
        force_quit = getattr(self, "_force_quit", False)
        # 程序化退出（quit/小狗菜单）或系统会话结束 → 真正清理并退出；
        # 用户点击标题栏关闭（spontaneous）→ 最小化为桌宠小狗
        if force_quit or app.isSavingSession() or not event.spontaneous():
            if hasattr(self, 'puppy_widget') and self.puppy_widget.isVisible():
                self.puppy_widget.hide()
            self._do_exit_cleanup()
            event.accept()
            # 主窗口此时通常是隐藏状态（已最小化为桌宠），close() 一个隐藏窗口
            # 不会触发 Qt 的"最后窗口关闭即退出"机制，必须显式退出事件循环
            QApplication.quit()
            return

        if hasattr(self, 'puppy_widget'):
            self.hide()
            self.puppy_widget.show_at_bottom_right()
        event.ignore()
