"""应用入口：QApplication 初始化与主窗口启动。"""

import sys

from PySide6.QtCore import Qt, qInstallMessageHandler
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from .window import ChatWindow


def _quiet_directwrite_warnings(msg_type, context, message):
    """过滤 Windows 上 DirectWrite 无法加载遗留点阵字体的无害警告。

    MS Sans Serif / Fixedsys 等是 Win9x 时代的点阵字体，DirectWrite 无法解析，
    Qt 会退回 GDI 引擎渲染，不影响显示；仅在个别启动时序下偶发刷屏，故行过滤。
    """
    if "CreateFontFaceFromHDC() failed" in message:
        return
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def run():
    qInstallMessageHandler(_quiet_directwrite_warnings)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    # 界面为固定浅色设计：强制浅色调色板，避免系统深色模式下出现"白底白字"不可见
    if hasattr(Qt, "ColorScheme"):
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    font = QFont()
    if sys.platform == "win32":
        font.setFamily("Microsoft YaHei")
    elif sys.platform == "darwin":
        font.setFamily("PingFang SC")
    else:
        font.setFamily("Noto Sans CJK SC")
    font.setPointSize(10)
    app.setFont(font)

    window = ChatWindow()
    window.show()
    sys.exit(app.exec())
