import sys
import re
import json
import base64
import threading
from typing import List, Dict, Optional
from urllib.parse import urlparse

from PySide6.QtCore import Qt, QThread, Signal, QSettings, QTimer, QByteArray, QBuffer, Property, QEvent
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QListWidget, QListWidgetItem, QPushButton, QTextEdit,
    QScrollArea, QFrame, QLabel, QDialog, QLineEdit, QDialogButtonBox,
    QMessageBox, QTextBrowser, QToolButton, QMenu, QInputDialog, QFileDialog,
    QCheckBox
)
from PySide6.QtGui import QFont, QIcon, QPixmap, QPainter, QColor, QImage, QPainterPath
import openai
import uuid
from datetime import datetime
import mistune
from pygments import highlight
from pygments.lexers import get_lexer_by_name, guess_lexer, TextLexer
from pygments.formatters import HtmlFormatter
from pygments.util import ClassNotFound
import os

# ==================== Markdown 解析器（使用 mistune + Pygments）====================

class PygmentsRenderer(mistune.HTMLRenderer):
    """
    使用 Pygments 进行代码高亮的 mistune 渲染器
    """
    
    def __init__(self, style='monokai', css_class='code-highlight', *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style = style
        self.css_class = css_class
    
    def block_code(self, code, info=None):
        """渲染代码块"""
        if not code or not code.strip():
            return ''
        
        lexer = self._get_lexer(code, info)
        formatter = HtmlFormatter(
            style=self.style,
            cssclass=self.css_class,
            nowrap=False,
            linenos=False
        )
        
        return highlight(code, lexer, formatter)
    
    def _get_lexer(self, code, info):
        """获取合适的词法分析器"""
        if not info:
            try:
                return guess_lexer(code)
            except ClassNotFound:
                return TextLexer()
        
        # 语言别名映射
        aliases = {
            'js': 'javascript', 'ts': 'typescript', 'py': 'python',
            'rb': 'ruby', 'sh': 'bash', 'shell': 'bash', 'zsh': 'bash',
            'yml': 'yaml', 'md': 'markdown', 'cs': 'csharp',
            'c++': 'cpp', 'h++': 'cpp', 'hpp': 'cpp',
        }
        
        lang = aliases.get(info.lower().strip(), info.lower().strip())
        
        try:
            return get_lexer_by_name(lang, stripall=True)
        except ClassNotFound:
            try:
                return guess_lexer(code)
            except ClassNotFound:
                return TextLexer()
    
    def codespan(self, text):
        """渲染行内代码"""
        escaped = mistune.escape(text)
        return f'<code class="inline-code">{escaped}</code>'


class MarkdownParser:
    """
    Markdown 解析器封装 - 单例模式
    使用 mistune 解析 Markdown，生成结构化的 AST
    """
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_parser()
        return cls._instance
    
    def _init_parser(self):
        """初始化解析器"""
        self.renderer = PygmentsRenderer(style='monokai')
        self.markdown = mistune.create_markdown(
            renderer=self.renderer,
            plugins=['table', 'strikethrough', 'url']
        )
        # Token 解析器（用于分离内容片段）- mistune 3.x 使用 Markdown 类
        # 必须手动添加表格插件，否则表格会被解析为普通段落
        self.token_parser = mistune.Markdown()
        # 添加表格插件支持
        from mistune.plugins.table import table as table_plugin
        table_plugin(self.token_parser)
    
    def parse_to_html(self, text):
        """将 Markdown 转换为 HTML"""
        return self.markdown(text)
    
    def parse_to_tokens(self, text):
        """将 Markdown 解析为 tokens（抽象语法树）
        mistune 3.x: parse() 返回 (tokens, state) 元组
        """
        tokens, state = self.token_parser.parse(text)
        return tokens
    
    def split_content(self, text):
        """
        将内容分割为代码块和普通文本片段
        返回: list[{'type': 'code'|'text', 'language': str, 'content': str}, ...]
        """
        return self._split_by_tokens(text)
    
    def _split_by_tokens(self, text):
        """使用 mistune 3.x tokens 解析分割内容"""
        tokens, state = self.token_parser.parse(text)
        result = []
        
        for token in tokens:
            token_type = token.get('type', '')
            
            # 跳过空白行
            if token_type == 'blank_line':
                continue
            
            if token_type == 'block_code':
                # 获取语言信息 - mistune 3.x 使用 attrs.info
                attrs = token.get('attrs', {})
                lang = attrs.get('info', '').strip() if attrs else ''
                result.append({
                    'type': 'code',
                    'language': lang or 'code',
                    'content': token.get('raw', '')
                })
            elif token_type == 'paragraph':
                text_content = self._extract_text(token.get('children', []))
                if text_content.strip():
                    result.append({
                        'type': 'text',
                        'content': text_content
                    })
            elif token_type == 'heading':
                text_content = self._extract_text(token.get('children', []))
                attrs = token.get('attrs', {})
                level = attrs.get('level', 1) if attrs else 1
                if text_content.strip():
                    result.append({
                        'type': 'text',
                        'content': f"{'#' * level} {text_content}"
                    })
            elif token_type == 'block_html':
                raw = token.get('raw', '')
                if raw.strip():
                    result.append({
                        'type': 'text',
                        'content': raw
                    })
            elif token_type == 'list':
                items = token.get('children', [])
                list_text = ""
                for item in items:
                    item_text = self._extract_text(item.get('children', []))
                    list_text += f"• {item_text}\n"
                if list_text.strip():
                    result.append({
                        'type': 'text',
                        'content': list_text.rstrip()
                    })
            elif token_type == 'table':
                # 表格作为原始 Markdown 保留
                table_text = self._render_table(token)
                if table_text.strip():
                    result.append({
                        'type': 'text',
                        'content': table_text
                    })
            elif token_type == 'thematic_break':
                result.append({
                    'type': 'text',
                    'content': '---'
                })
        
        return result
    
    def _extract_text(self, children):
        """从 AST 节点中递归提取文本"""
        if not children:
            return ''
        
        text_parts = []
        for child in children:
            child_type = child.get('type', '')
            
            if child_type == 'text':
                text_parts.append(child.get('raw', ''))
            elif child_type == 'codespan':
                text_parts.append(f"`{child.get('raw', '')}`")
            elif child_type == 'strong':
                inner = self._extract_text(child.get('children', []))
                text_parts.append(f"**{inner}**")
            elif child_type == 'emphasis':
                inner = self._extract_text(child.get('children', []))
                text_parts.append(f"*{inner}*")
            elif child_type == 'link':
                inner = self._extract_text(child.get('children', []))
                url = child.get('url', '')
                text_parts.append(f"[{inner}]({url})")
            elif child_type == 'image':
                alt = child.get('alt', '')
                url = child.get('url', '')
                text_parts.append(f"![{alt}]({url})")
            elif 'children' in child:
                text_parts.append(self._extract_text(child['children']))
        
        return ' '.join(text_parts)
    
    def _render_table(self, node):
        """渲染表格为文本表示"""
        children = node.get('children', [])
        if not children:
            return ''
        
        result = []
        for child in children:
            if child.get('type') == 'table_head':
                cells = []
                for cell in child.get('children', []):
                    cell_text = self._extract_text(cell.get('children', []))
                    cells.append(cell_text)
                result.append('| ' + ' | '.join(cells) + ' |')
                result.append('| ' + ' | '.join(['---'] * len(cells)) + ' |')
            elif child.get('type') == 'table_body':
                for row in child.get('children', []):
                    cells = []
                    for cell in row.get('children', []):
                        cell_text = self._extract_text(cell.get('children', []))
                        cells.append(cell_text)
                    result.append('| ' + ' | '.join(cells) + ' |')
        
        return '\n'.join(result)


# 获取全局解析器实例
def get_markdown_parser():
    """获取 Markdown 解析器单例"""
    return MarkdownParser()


# ==================== 配置对话框 ====================
class SettingsDialog(QDialog):
    """设置对话框（美化版）"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("API 设置")
        self.setModal(True)
        self.resize(480, 340)
        self.setStyleSheet("""
            QDialog {
                background-color: #f9fafc;
                border-radius: 16px;
            }
            QLabel {
                color: #2d3748;
                font-size: 14px;
                font-weight: 500;
            }
            QLineEdit {
                background-color: white;
                color: #2d3748; 
                border: 1px solid #e2e8f0;
                border-radius: 10px;
                padding: 10px 14px;
                font-size: 14px;
                selection-background-color: #667eea;
            }
            QLineEdit:focus {
                border: 2px solid #667eea;
                padding: 9px 13px;
            }
            QToolButton {
                background: transparent;
                border: none;
                font-size: 16px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(20)
        layout.setContentsMargins(24, 24, 24, 24)

        # 标题
        title = QLabel("⚙️ API 配置")
        title.setStyleSheet("font-size: 22px; font-weight: 600; color: #1a202c; margin-bottom: 8px;")
        layout.addWidget(title)

        # 表单
        form_layout = QVBoxLayout()
        form_layout.setSpacing(16)

        # API Key
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setPlaceholderText("输入你的API Key")
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form_layout.addWidget(QLabel("API Key:"))
        key_layout = QHBoxLayout()
        key_layout.addWidget(self.api_key_edit)
        self.toggle_key_btn = QToolButton()
        self.toggle_key_btn.setText("👁")
        self.toggle_key_btn.setCheckable(True)
        self.toggle_key_btn.setCursor(Qt.PointingHandCursor)
        self.toggle_key_btn.clicked.connect(self.toggle_key_visibility)
        key_layout.addWidget(self.toggle_key_btn)
        form_layout.addLayout(key_layout)

        # Base URL
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("例如：https://api.deepseek.com/v1")
        form_layout.addWidget(QLabel("Base URL:"))
        form_layout.addWidget(self.base_url_edit)

        # 模型
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("例如：deepseek-chat")
        form_layout.addWidget(QLabel("模型:"))
        form_layout.addWidget(self.model_edit)

        # 多模态模型选项（新增）
        self.vision_checkbox = QCheckBox("此模型支持图片输入（多模态）")
        self.vision_checkbox.setStyleSheet("font-size: 13px; color: #4a5568;")
        form_layout.addWidget(self.vision_checkbox)

        layout.addLayout(form_layout)

        # 帮助提示
        help_label = QLabel(
            "💡 提示：支持任何兼容OpenAI格式的API服务。\n"
            "💡 请查看各AI模型厂商官方页面的接口文档，获取正确的Base URL和模型名称。"
        )
        help_label.setStyleSheet("color: #718096; font-size: 13px; padding: 14px; "
                                 "background: #edf2f7; border-radius: 12px; line-height: 1.5;")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)

        # 按钮
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        OK_button = button_box.button(QDialogButtonBox.StandardButton.Ok)
        Cancel_button = button_box.button(QDialogButtonBox.StandardButton.Cancel)
        OK_button.setText("保存设置")
        Cancel_button.setText("取消")
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        
        # 使用 objectName 来设置样式
        OK_button.setObjectName("okButton")
        Cancel_button.setObjectName("cancelButton")
        
        button_box.setStyleSheet("""
            QPushButton {
                padding: 10px 28px;
                border-radius: 30px;
                font-size: 14px;
                font-weight: 600;
            }
            QPushButton#okButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #667eea, stop:1 #764ba2);
                color: white;
                border: none;
            }
            QPushButton#okButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #5a6fd6, stop:1 #6a4190);
            }
            QPushButton#cancelButton {
                background: white;
                color: #4a5568;
                border: 1px solid #e2e8f0;
            }
            QPushButton#cancelButton:hover {
                background: #f7fafc;
            }
        """)
        layout.addWidget(button_box)

        # 加载保存的配置
        self.load_settings()

    def load_settings(self):
        settings = QSettings("MyChatApp", "Settings")
        self.api_key_edit.setText(settings.value("api_key", ""))
        self.base_url_edit.setText(settings.value("base_url", ""))
        self.model_edit.setText(settings.value("model", "deepseek-chat"))
        # 加载多模态选项（新增）
        self.vision_checkbox.setChecked(settings.value("supports_vision", False, type=bool))

    def save_settings(self):
        settings = QSettings("MyChatApp", "Settings")
        settings.setValue("api_key", self.api_key_edit.text())
        settings.setValue("base_url", self.base_url_edit.text())
        settings.setValue("model", self.model_edit.text())
        # 保存多模态选项（新增）
        settings.setValue("supports_vision", self.vision_checkbox.isChecked())

    def toggle_key_visibility(self):
        if self.toggle_key_btn.isChecked():
            self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self.toggle_key_btn.setText("🔒")
        else:
            self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.toggle_key_btn.setText("👁")

    def get_settings(self):
        return {
            "api_key": self.api_key_edit.text(),
            "base_url": self.base_url_edit.text(),
            "model": self.model_edit.text(),
            "supports_vision": self.vision_checkbox.isChecked(),
        }


class TigerWidget(QWidget):
    """
    桌面小老虎组件
    点击关闭按钮时显示在桌面右下角，带奔跑动画
    支持根据进程监控状态切换奔跑/睡眠模式
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._frame = 0
        self._frames = []
        self._is_running = True  # True=奔跑, False=睡眠
        self._frame_sleeping = None  # 睡眠帧

        # 窗口标志：置顶、无边框、不显示在任务栏
        self.setWindowFlags(
            Qt.Tool |
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(120, 80)

        # 生成奔跑老虎的帧
        self._generate_tiger_frames()
        # 生成睡眠老虎的帧
        self._generate_sleep_frame()

        # 动画计时器
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._next_frame)
        self._timer.start(80)  # 每80ms切换一帧，约12fps

    def set_running_mode(self, is_running: bool):
        """切换奔跑/睡眠模式"""
        if self._is_running == is_running:
            return
        self._is_running = is_running
        if is_running:
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _generate_tiger_frames(self):
        """生成奔跑老虎的简化动画帧"""
        self._frames = []
        # 简化的老虎轮廓帧 - 使用多个帧模拟奔跑动作
        for i in range(4):
            self._frames.append(self._draw_tiger_frame(i))

    def _generate_sleep_frame(self):
        """生成睡眠老虎的静态帧"""
        self._frame_sleeping = self._draw_sleep_tiger()

    def _draw_tiger_frame(self, frame_idx):
        """绘制单帧老虎图像"""
        from PySide6.QtGui import QPainter, QColor, QFont, QPen

        # 创建120x80的图像
        image = QImage(120, 80, QImage.Format_ARGB32)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing)

        # 老虎颜色
        orange = QColor(245, 140, 50)
        black = QColor(30, 30, 30)
        white = QColor(255, 255, 255)

        # 简单的站立/奔跑姿态变换
        # 身体矩形位置随帧变化
        body_shifts = [(-3, 0), (0, 0), (3, 0), (0, 0)]
        leg_offsets = [(-5, 0), (0, 5), (5, 0), (0, -5)]

        shift_x, shift_y = body_shifts[frame_idx]
        leg_off = leg_offsets[frame_idx]

        # 身体
        painter.setBrush(orange)
        painter.setPen(Qt.NoPen)
        body_x, body_y = 45 + shift_x, 25 + shift_y
        body_w, body_h = 45, 30
        painter.drawRoundedRect(body_x, body_y, body_w, body_h, 12, 12)

        # 头部
        head_x, head_y = 25 + shift_x, 15 + shift_y
        painter.drawEllipse(head_x, head_y, 28, 25)

        # 耳朵
        painter.drawEllipse(head_x + 2, head_y - 5, 8, 10)
        painter.drawEllipse(head_x + 18, head_y - 5, 8, 10)

        # 眼睛 - 睁开
        painter.setBrush(black)
        eye_x = 8
        painter.drawEllipse(head_x + eye_x, head_y + 8, 5, 5)
        painter.drawEllipse(head_x + eye_x + 12, head_y + 8, 5, 5)

        # 鼻子
        painter.setBrush(black)
        painter.drawEllipse(head_x + 10, head_y + 16, 6, 4)

        # 嘴巴
        painter.setBrush(QColor(200, 80, 80))
        mouth_y = head_y + 20 if frame_idx % 2 == 0 else head_y + 22
        painter.drawEllipse(head_x + 8, mouth_y, 12, 4)

        # 条纹
        painter.setBrush(black)
        for i in range(3):
            stripe_x = body_x + 8 + i * 12
            painter.drawRect(stripe_x, body_y + 3, 4, 12)

        # 尾巴
        tail_base_x = body_x + body_w
        tail_base_y = body_y + 10
        tail_end_x = tail_base_x + 15 + (leg_off[0] if frame_idx % 2 == 0 else -leg_off[0])
        tail_end_y = tail_base_y - 10 + (leg_off[1] if frame_idx % 2 == 0 else -leg_off[1])
        pen = QPen(orange, 6)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawLine(tail_base_x, tail_base_y, tail_end_x, tail_end_y)
        # 尾巴条纹
        painter.setBrush(black)
        painter.drawEllipse(tail_end_x - 3, tail_end_y - 3, 6, 6)

        # 腿（前腿和后腿）
        painter.setBrush(orange)
        painter.setPen(Qt.NoPen)
        # 前腿
        front_leg_x = body_x + 8
        front_leg_y = body_y + body_h - 5
        painter.drawRect(front_leg_x, front_leg_y + leg_off[1], 8, 15 - leg_off[1])
        painter.drawRect(front_leg_x + 10, front_leg_y + leg_off[1] if frame_idx < 2 else 0, 8, 15)

        # 后腿
        back_leg_x = body_x + body_w - 15
        back_leg_y = body_y + body_h - 5
        painter.drawRect(back_leg_x, back_leg_y - leg_off[1], 8, 15 + leg_off[1])
        painter.drawRect(back_leg_x + 10, back_leg_y - leg_off[1] if frame_idx >= 2 else 0, 8, 15)

        # 爪子
        painter.setBrush(white)
        painter.drawRect(front_leg_x, front_leg_y + 12 + leg_off[1], 8, 4)
        painter.drawRect(back_leg_x, back_leg_y + 12 - leg_off[1], 8, 4)

        painter.end()
        return image

    def _draw_sleep_tiger(self):
        """绘制睡眠老虎的静态图像"""
        from PySide6.QtGui import QPainter, QColor, QPen

        image = QImage(120, 80, QImage.Format_ARGB32)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing)

        orange = QColor(245, 140, 50)
        black = QColor(30, 30, 30)
        white = QColor(255, 255, 255)

        # 睡眠姿态：身体压低趴下
        # 身体 - 更宽更矮
        painter.setBrush(orange)
        painter.setPen(Qt.NoPen)
        body_x, body_y = 40, 40
        body_w, body_h = 55, 25
        painter.drawRoundedRect(body_x, body_y, body_w, body_h, 10, 10)

        # 头部 - 更低
        head_x, head_y = 20, 35
        painter.drawEllipse(head_x, head_y, 30, 22)

        # 耳朵
        painter.drawEllipse(head_x + 3, head_y - 3, 8, 9)
        painter.drawEllipse(head_x + 19, head_y - 3, 8, 9)

        # 眼睛 - 闭眼（两条短线）
        pen = QPen(black, 2)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        # 左眼
        painter.drawLine(head_x + 8, head_y + 10, head_x + 14, head_y + 10)
        # 右眼
        painter.drawLine(head_x + 16, head_y + 10, head_x + 22, head_y + 10)

        # 鼻子
        painter.setBrush(black)
        painter.drawEllipse(head_x + 11, head_y + 14, 6, 4)

        # 嘴巴 - 微笑
        painter.setBrush(QColor(200, 80, 80))
        painter.drawEllipse(head_x + 9, head_y + 18, 10, 3)

        # 条纹
        painter.setBrush(black)
        for i in range(3):
            stripe_x = body_x + 10 + i * 14
            painter.drawRect(stripe_x, body_y + 2, 4, 10)

        # 尾巴 - 下垂
        tail_base_x = body_x + body_w
        tail_base_y = body_y + 8
        pen = QPen(orange, 5)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawLine(tail_base_x, tail_base_y, tail_base_x + 12, tail_base_y + 15)
        # 尾巴头
        painter.setBrush(black)
        painter.drawEllipse(tail_base_x + 10, tail_base_y + 13, 5, 5)

        # 腿 - 趴下姿势
        painter.setBrush(orange)
        painter.setPen(Qt.NoPen)
        # 前腿
        painter.drawRect(body_x + 5, body_y + body_h - 3, 10, 8)
        # 后腿
        painter.drawRect(body_x + body_w - 15, body_y + body_h - 3, 10, 8)

        # 爪子
        painter.setBrush(white)
        painter.drawRect(body_x + 5, body_y + body_h + 3, 10, 4)
        painter.drawRect(body_x + body_w - 15, body_y + body_h + 3, 10, 4)

        painter.end()
        return image

    def _next_frame(self):
        """切换到下一帧"""
        self._frame = (self._frame + 1) % len(self._frames)
        self.update()

    def paintEvent(self, event):
        """绘制当前帧"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        if self._is_running and self._frames:
            painter.drawImage(self.rect(), self._frames[self._frame])
        elif self._frame_sleeping:
            painter.drawImage(self.rect(), self._frame_sleeping)

    def mousePressEvent(self, event):
        """点击恢复主窗口"""
        if event.button() == Qt.LeftButton:
            self.hide()
            if hasattr(self, '_main_window'):
                self._main_window.showNormal()
                self._main_window.raise_()
                self._main_window.activateWindow()

    def contextMenuEvent(self, event):
        """右键菜单，退出程序"""
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background: white;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                padding: 4px;
            }
            QMenu::item {
                padding: 8px 20px;
                border-radius: 4px;
                color: #2d3748;
            }
            QMenu::item:selected {
                background: #edf2f7;
            }
        """)
        exit_action = menu.addAction("🐯 退出程序")
        action = menu.exec(self.mapToGlobal(event.pos()))
        if action == exit_action:
            if hasattr(self, '_main_window'):
                self._main_window.close()
            QApplication.instance().quit()

    def show_at_bottom_right(self):
        """显示在屏幕右下角"""
        screen = self._get_desktop_screen()
        if screen:
            geo = screen.geometry()
            x = geo.right() - self.width() - 20
            y = geo.bottom() - self.height() - 80
            self.move(x, y)
        self.show()

    def _get_desktop_screen(self):
        """获取主屏幕"""
        from PySide6.QtWidgets import QApplication
        screens = QApplication.instance().screens()
        return screens[0] if screens else None


# ==================== 进程监控线程 ====================
class ProcessMonitor(QThread):
    """
    进程监控线程
    - WPS/Office：监测进程启动/退出
    - 浏览器：监测窗口是否存在（新标签页会创建/关闭窗口）
    只有新启动的进程/窗口（非基线）才会触发奔跑状态
    """
    running = Signal()   # 有新进程或窗口启动
    sleeping = Signal()  # 所有新进程/窗口退出

    # 目标进程列表（统一大写）
    OFFICE_PROCS = {'WINWORD.EXE', 'EXCEL.EXE', 'POWERPNT.EXE'}
    WPS_PROCS = {'WPS.EXE', 'ET.EXE', 'WPP.EXE'}
    BROWSER_PROCS = {
        'CHROME.EXE', 'MSEDGE.EXE', 'FIREFOX.EXE',
        '360SE.EXE', 'LIEBAO.EXE', 'SOGOUEXPLORER.EXE'
    }

    def __init__(self):
        super().__init__()
        self._stop_event = threading.Event()

        # 进程基线
        self._baseline_procs = set()       # 启动时已存在的非浏览器进程
        self._baseline_browser_pids = set()  # 启动时已存在的浏览器进程PID
        self._prev_new_browser_windows = False

        # ctypes 用于 Windows API
        self._ctypes = None
        if sys.platform == 'win32':
            import ctypes
            self._ctypes = ctypes

    def run(self):
        """主循环：每秒检查一次"""
        import psutil

        # 建立基线
        self._establish_baseline(psutil)

        # 初始状态：睡眠
        self._prev_new_browser_windows = False
        self.sleeping.emit()

        # 主循环
        while not self._stop_event.is_set():
            self._check(psutil)
            self._stop_event.wait(1)

    def _establish_baseline(self, psutil):
        """建立基线：记录启动时已有的进程"""
        current_procs = {p.info['name'].upper(): p.info.get('pid') for p in psutil.process_iter(['name', 'pid'])}

        # WPS/Office 基线进程
        for name in self.OFFICE_PROCS | self.WPS_PROCS:
            if name in current_procs:
                self._baseline_procs.add(name)

        # 浏览器基线进程PID
        for name in self.BROWSER_PROCS:
            if name in current_procs:
                self._baseline_browser_pids.add(current_procs[name])

    def _check(self, psutil):
        """检查进程和窗口变化"""
        try:
            # 检查 WPS/Office 新增进程
            self._check_office_wps(psutil)

            # 检查浏览器窗口
            self._check_browser_windows()
        except Exception as e:
            print(f"进程检查异常: {e}")

    def _check_office_wps(self, psutil):
        """检查 WPS/Office 新增进程"""
        current_procs = {p.info['name'].upper() for p in psutil.process_iter(['name'])}
        target_procs = current_procs & (self.OFFICE_PROCS | self.WPS_PROCS)
        new_procs = target_procs - self._baseline_procs

        if new_procs:
            self.running.emit()

    def _check_browser_windows(self):
        """检查浏览器窗口是否存在（通过窗口标题监测新标签页）"""
        if not self._ctypes:
            return

        current_browser_pids = self._get_browser_pids()
        new_browser_pids = current_browser_pids - self._baseline_browser_pids

        # 检查新增浏览器进程是否有窗口
        has_new_browser_window = False
        if new_browser_pids:
            has_new_browser_window = self._browser_has_window(new_browser_pids)

        # 检测状态变化
        if has_new_browser_window and not self._prev_new_browser_windows:
            self.running.emit()
        elif not has_new_browser_window and self._prev_new_browser_windows:
            self.sleeping.emit()

        self._prev_new_browser_windows = has_new_browser_window

    def _get_browser_pids(self):
        """获取当前运行的浏览器进程PID"""
        import psutil
        pids = set()
        for proc in psutil.process_iter(['name', 'pid']):
            if proc.info['name'].upper() in self.BROWSER_PROCS:
                pids.add(proc.info['pid'])
        return pids

    def _browser_has_window(self, browser_pids):
        """检查指定浏览器进程是否有窗口"""
        try:
            import ctypes

            user32 = ctypes.windll.user32

            # 回调函数：检查窗口是否属于目标进程
            found = [False]
            def enum_callback(hwnd, _):
                if not user32.IsWindowVisible(hwnd):
                    return 1  # 继续枚举
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in browser_pids:
                    found[0] = True
                    return 0  # 停止枚举
                return 1  # 继续枚举

            # 枚举所有窗口
            EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
            user32.EnumWindows(EnumWindowsProc(enum_callback), None)

            return found[0]
        except Exception as e:
            print(f"窗口检查异常: {e}")
            return False

    def stop(self):
        """停止监控"""
        self._stop_event.set()


# ==================== AI 请求线程 ====================
class AIRequestThread(QThread):
    error_occurred = Signal(str)
    stream_chunk = Signal(str)
    finished_stream = Signal()

    def __init__(self, messages, api_key, base_url, model,
                 system_prompt="""
You are a very strong reasoner and planner. Use these critical instructions to structure your plans, thoughts, and responses.
Before taking any action (either tool calls or responses to the user), you must proactively, methodically, and independently plan and reason about:
1.Logical dependencies and constraints: Analyze the intended action against the following factors. Resolve conflicts in order of importance:
    1.1) Policy-based rules, mandatory prerequisites, and constraints.
    1.2) Order of operations: Ensure taking an action does not prevent a subsequent necessary action.
        1.2.1) The user may request actions in a random order, but you may need to reorder operations to maximize successful completion of the task.
    1.3) Other prerequisites (information and/or actions needed).
    1.4) Explicit user constraints or preferences.
2.Risk assessment: What are the consequences of taking the action? Will the new state cause any future issues?
    2.1) For exploratory tasks (like searches), missing optional parameters is a LOW risk. Prefer calling the tool with the available information over asking the user, unless your Rule 1 (Logical Dependencies) reasoning determines that optional information is required for a later step in your plan.
3.Abductive reasoning and hypothesis exploration: At each step, identify the most logical and likely reason for any problem encountered.
    3.1) Look beyond immediate or obvious causes. The most likely reason may not be the simplest and may require deeper inference.
    3.2) Hypotheses may require additional research. Each hypothesis may take multiple steps to test.
    3.3) Prioritize hypotheses based on likelihood, but do not discard less likely ones prematurely. A low-probability event may still be the root cause.
4.Outcome evaluation and adaptability: Does the previous observation require any changes to your plan?
    4.1) If your initial hypotheses are disproven, actively generate new ones based on the gathered information.
5.Information availability: Incorporate all applicable and alternative sources of information, including:
    5.1) Using available tools and their capabilities
    5.2) All policies, rules, checklists, and constraints
    5.3) Previous observations and conversation history
    5.4) Information only available by asking the user
6.Precision and Grounding: Ensure your reasoning is extremely precise and relevant to each exact ongoing situation.
    6.1) Verify your claims by quoting the exact applicable information (including policies) when referring to them.
7.Completeness: Ensure that all requirements, constraints, options, and preferences are exhaustively incorporated into your plan.
    7.1) Resolve conflicts using the order of importance in #1.
    7.2) Avoid premature conclusions: There may be multiple relevant options for a given situation.      
        7.2.1) To check for whether an option is relevant, reason about all information sources from #5.      
        7.2.2) You may need to consult the user to even know whether something is applicable. Do not assume it is not applicable without checking.
    7.3) Review applicable sources of information from #5 to confirm which are relevant to the current state.
8.Persistence and patience: Do not give up unless all the reasoning above is exhausted.
    8.1) Don't be dissuaded by time taken or user frustration.
    8.2) This persistence must be intelligent: On transient errors (e.g. please try again), you must retry unless an explicit retry limit (e.g., max x tries) has been reached. If such a limit is hit, you must stop. On other errors, you must change your strategy or arguments, not repeat the same failed call.
9.Inhibit your response: only take an action after all the above reasoning is completed. Once you've taken an action, you cannot take it back.
"""):
        super().__init__()
        self.user_messages = messages
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.system_prompt = system_prompt
        self._is_running = True

    def run(self):
        client = None
        try:
            # 设置超时：连接超时10秒，读取超时60秒（流式响应需要较长等待）
            client = openai.OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=openai.Timeout(
                    connect=10.0,    # 连接超时
                    read=60.0,       # 读取超时（流式响应每个数据块）
                    write=10.0,      # 写入超时
                    pool=10.0        # 连接池超时
                )
            )
            full_messages = [{"role": "system", "content": self.system_prompt}] + self.user_messages
            stream = client.chat.completions.create(
                model=self.model,
                messages=full_messages,
                stream=True
            )
            full_content = ""
            for chunk in stream:
                if not self._is_running:
                    break
                if chunk.choices and chunk.choices[0].delta.content:
                    content = chunk.choices[0].delta.content
                    full_content += content
                    self.stream_chunk.emit(content)
            self.finished_stream.emit()
        except Exception as e:
            self.error_occurred.emit(str(e))
        finally:
            # 修复问题12：确保关闭连接，避免资源泄漏
            if client:
                try:
                    client.close()
                except Exception:
                    pass

    def stop(self):
        self._is_running = False


# ==================== 消息组件（支持代码块复制，美化版，高度自适应，优化版）====================
class CodeBlockWidget(QWidget):
    """
    代码块控件 - 支持语法高亮
    使用 Pygments 进行代码高亮，使用 mistune 解析语言
    """
    
    # Monokai 风格的 CSS 样式
    HIGHLIGHT_CSS = """
    <style>
        body { background: transparent; margin: 0; padding: 0; }
        pre { margin: 0; white-space: pre-wrap; word-wrap: break-word; }
        .code-highlight { background: transparent; padding: 0; margin: 0; }
        .code-highlight .hll { background-color: #49483e }
        .code-highlight .c { color: #75715e; font-style: italic } /* Comment */
        .code-highlight .err { color: #960050; background-color: #1e0010 } /* Error */
        .code-highlight .k { color: #66d9ef; font-weight: bold } /* Keyword */
        .code-highlight .l { color: #ae81ff } /* Literal */
        .code-highlight .n { color: #f8f8f2 } /* Name */
        .code-highlight .o { color: #f92672 } /* Operator */
        .code-highlight .p { color: #f8f8f2 } /* Punctuation */
        .code-highlight .ch { color: #75715e } /* Comment.Hashbang */
        .code-highlight .cm { color: #75715e } /* Comment.Multiline */
        .code-highlight .cp { color: #75715e } /* Comment.Preproc */
        .code-highlight .cpf { color: #75715e } /* Comment.PreprocFile */
        .code-highlight .c1 { color: #75715e } /* Comment.Single */
        .code-highlight .cs { color: #75715e } /* Comment.Special */
        .code-highlight .gd { color: #f92672 } /* Generic.Deleted */
        .code-highlight .ge { font-style: italic } /* Generic.Emph */
        .code-highlight .gi { color: #a6e22e } /* Generic.Inserted */
        .code-highlight .gs { font-weight: bold } /* Generic.Strong */
        .code-highlight .gu { color: #75715e } /* Generic.Subheading */
        .code-highlight .kc { color: #66d9ef } /* Keyword.Constant */
        .code-highlight .kd { color: #66d9ef } /* Keyword.Declaration */
        .code-highlight .kn { color: #f92672 } /* Keyword.Namespace */
        .code-highlight .kp { color: #66d9ef } /* Keyword.Pseudo */
        .code-highlight .kr { color: #66d9ef } /* Keyword.Reserved */
        .code-highlight .kt { color: #66d9ef } /* Keyword.Type */
        .code-highlight .ld { color: #e6db74 } /* Literal.Date */
        .code-highlight .m { color: #ae81ff } /* Literal.Number */
        .code-highlight .s { color: #e6db74 } /* Literal.String */
        .code-highlight .na { color: #a6e22e } /* Name.Attribute */
        .code-highlight .nb { color: #f8f8f2 } /* Name.Builtin */
        .code-highlight .nc { color: #a6e22e } /* Name.Class */
        .code-highlight .no { color: #66d9ef } /* Name.Constant */
        .code-highlight .nd { color: #a6e22e } /* Name.Decorator */
        .code-highlight .ni { color: #f8f8f2 } /* Name.Entity */
        .code-highlight .ne { color: #a6e22e } /* Name.Exception */
        .code-highlight .nf { color: #a6e22e } /* Name.Function */
        .code-highlight .nl { color: #f8f8f2 } /* Name.Label */
        .code-highlight .nn { color: #f8f8f2 } /* Name.Namespace */
        .code-highlight .nx { color: #a6e22e } /* Name.Other */
        .code-highlight .py { color: #f8f8f2 } /* Name.Property */
        .code-highlight .nt { color: #f92672 } /* Name.Tag */
        .code-highlight .nv { color: #f8f8f2 } /* Name.Variable */
        .code-highlight .ow { color: #f92672 } /* Operator.Word */
        .code-highlight .w { color: #f8f8f2 } /* Text.Whitespace */
        .code-highlight .mb { color: #ae81ff } /* Literal.Number.Bin */
        .code-highlight .mf { color: #ae81ff } /* Literal.Number.Float */
        .code-highlight .mh { color: #ae81ff } /* Literal.Number.Hex */
        .code-highlight .mi { color: #ae81ff } /* Literal.Number.Integer */
        .code-highlight .mo { color: #ae81ff } /* Literal.Number.Oct */
        .code-highlight .sa { color: #e6db74 } /* Literal.String.Affix */
        .code-highlight .sb { color: #e6db74 } /* Literal.String.Backtick */
        .code-highlight .sc { color: #e6db74 } /* Literal.String.Char */
        .code-highlight .dl { color: #e6db74 } /* Literal.String.Delimiter */
        .code-highlight .sd { color: #e6db74 } /* Literal.String.Doc */
        .code-highlight .s2 { color: #e6db74 } /* Literal.String.Double */
        .code-highlight .se { color: #ae81ff } /* Literal.String.Escape */
        .code-highlight .sh { color: #e6db74 } /* Literal.String.Heredoc */
        .code-highlight .si { color: #e6db74 } /* Literal.String.Interpol */
        .code-highlight .sx { color: #e6db74 } /* Literal.String.Other */
        .code-highlight .sr { color: #e6db74 } /* Literal.String.Regex */
        .code-highlight .s1 { color: #e6db74 } /* Literal.String.Single */
        .code-highlight .ss { color: #e6db74 } /* Literal.String.Symbol */
        .code-highlight .bp { color: #f8f8f2 } /* Name.Builtin.Pseudo */
        .code-highlight .fm { color: #a6e22e } /* Name.Function.Magic */
        .code-highlight .vc { color: #f8f8f2 } /* Name.Variable.Class */
        .code-highlight .vg { color: #f8f8f2 } /* Name.Variable.Global */
        .code-highlight .vi { color: #f8f8f2 } /* Name.Variable.Instance */
        .code-highlight .vm { color: #f8f8f2 } /* Name.Variable.Magic */
        .code-highlight .il { color: #ae81ff } /* Literal.Number.Integer.Long */
    </style>
    """
    
    def __init__(self, code: str, language: str = ""):
        super().__init__()
        self.code = code
        self.language = language
        self.parser = get_markdown_parser()
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题栏
        header = QFrame()
        header.setStyleSheet("""
            QFrame {
                background: #2d3748;
                border-top-left-radius: 12px;
                border-top-right-radius: 12px;
            }
        """)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 8, 16, 8)

        lang_label = QLabel(f"📄 {self.language}" if self.language else "📄 代码")
        lang_label.setStyleSheet("color: #cbd5e0; font-size: 12px; background: transparent; font-family: monospace;")
        header_layout.addWidget(lang_label)

        header_layout.addStretch()

        copy_btn = QPushButton("📋 复制")
        copy_btn.setCursor(Qt.PointingHandCursor)
        copy_btn.setStyleSheet("""
            QPushButton {
                background: #4a5568;
                color: white;
                border: none;
                padding: 4px 14px;
                border-radius: 20px;
                font-size: 12px;
                font-weight: 500;
            }
            QPushButton:hover {
                background: #5a6578;
            }
            QPushButton:pressed {
                background: #3a4558;
            }
        """)
        copy_btn.clicked.connect(self.copy_code)
        header_layout.addWidget(copy_btn)

        layout.addWidget(header)

        # 代码区域 - 使用 QTextBrowser 显示高亮后的 HTML
        self.code_display = QTextBrowser()
        self.code_display.setReadOnly(True)
        self.code_display.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
        )
        self.code_display.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.code_display.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        
        # 设置样式
        self.code_display.setStyleSheet("""
            QTextBrowser {
                background: #1e1e2e;
                color: #e2e8f0;
                font-family: 'SF Mono', 'Consolas', 'Courier New', monospace;
                font-size: 13px;
                border: none;
                border-bottom-left-radius: 12px;
                border-bottom-right-radius: 12px;
                padding: 16px;
            }
        """)
        
        # 使用 Pygments 高亮代码
        highlighted_html = self._highlight_code()
        self.code_display.setHtml(highlighted_html)
        
        # 高度自适应
        self.code_display.document().documentLayout().documentSizeChanged.connect(
            self._adjust_code_height
        )
        # 立即设置初始高度
        QTimer.singleShot(0, self._adjust_code_height)
        layout.addWidget(self.code_display)

    def _highlight_code(self):
        """使用 Pygments 生成高亮的 HTML"""
        try:
            lexer = self._get_lexer()
            formatter = HtmlFormatter(style='monokai', cssclass='code-highlight', nowrap=False)
            highlighted = highlight(self.code, lexer, formatter)
            return f"{self.HIGHLIGHT_CSS}<body>{highlighted}</body>"
        except Exception:
            # 出错时降级为普通文本
            escaped_code = self.code.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            return f"<pre style='color: #e2e8f0; margin: 0;'>{escaped_code}</pre>"
    
    def _get_lexer(self):
        """获取适合的词法分析器"""
        if self.language:
            # 语言别名映射
            aliases = {
                'js': 'javascript', 'ts': 'typescript', 'py': 'python',
                'sh': 'bash', 'shell': 'bash', 'yml': 'yaml',
                'rb': 'ruby', 'cs': 'csharp', 'c++': 'cpp',
            }
            lang = aliases.get(self.language.lower(), self.language.lower())
            try:
                return get_lexer_by_name(lang, stripall=True)
            except ClassNotFound:
                pass
        
        # 尝试自动检测
        try:
            return guess_lexer(self.code)
        except ClassNotFound:
            return TextLexer()

    def _adjust_code_height(self):
        """调整代码块高度以适应内容"""
        try:
            doc = self.code_display.document()
            height = int(doc.size().height()) + 40  # 额外空间避免截断
            self.code_display.setFixedHeight(max(height, 60))  # 最小高度60px
        except RuntimeError:
            pass

    def copy_code(self):
        clipboard = QApplication.clipboard()
        clipboard.setText(self.code)
        btn = self.sender()
        btn.setText("✅ 已复制")
        QTimer.singleShot(1500, lambda: btn.setText("📋 复制"))


class MessageWidget(QFrame):
    """消息控件 - 使用 mistune 解析 Markdown，支持代码块语法高亮"""
    
    def __init__(self, role: str, content: str, image_data_list: List[str] = None, parent=None):
        super().__init__(parent)
        self.role = role  # 'user' 或 'assistant'
        self.content = content
        self.image_data_list = image_data_list if image_data_list else []
        
        # 保存布局引用
        self.text_container = None
        self.text_layout = None
        self.outer_layout = None
        
        # 缓存文本浏览器引用（用于流式输出）
        self._cached_text_browser: Optional[QTextBrowser] = None
        self._cached_code_blocks: List[CodeBlockWidget] = []
        # 缓存所有文本浏览器引用（用于全选）
        self._all_text_browsers: List[QTextBrowser] = []
        
        # Markdown 解析器
        self.parser = get_markdown_parser()
        
        # 启用焦点策略以支持键盘事件
        self.setFocusPolicy(Qt.StrongFocus)
        
        self.setup_ui()

    def setup_ui(self):
        # 使用垂直布局作为主布局（包含标题栏和内容）
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(24, 8, 24, 8)
        main_layout.setSpacing(4)
        
        # 标题栏（包含角色标识和复制按钮）
        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)
        
        if self.role == "user":
            # 用户消息：标题栏靠右
            header_layout.addStretch()
            role_label = QLabel("👤 我")
            role_label.setStyleSheet("color: #667eea; font-size: 12px; font-weight: 600; background: transparent;")
            header_layout.addWidget(role_label)
        else:
            # AI消息：标题栏靠左
            role_label = QLabel("🤖 AI")
            role_label.setStyleSheet("color: #48bb78; font-size: 12px; font-weight: 600; background: transparent;")
            header_layout.addWidget(role_label)
            header_layout.addStretch()
        
        # 复制全部按钮
        self.copy_all_btn = QPushButton("📋 复制全部")
        self.copy_all_btn.setCursor(Qt.PointingHandCursor)
        self.copy_all_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #718096;
                border: 1px solid #e2e8f0;
                border-radius: 12px;
                padding: 2px 10px;
                font-size: 11px;
            }
            QPushButton:hover {
                background: #edf2f7;
                color: #4a5568;
                border-color: #cbd5e0;
            }
        """)
        self.copy_all_btn.clicked.connect(self.copy_all_content)
        header_layout.addWidget(self.copy_all_btn)
        
        main_layout.addLayout(header_layout)
        
        # 内容区域
        self.text_container = QWidget()
        self.text_layout = QVBoxLayout(self.text_container)
        self.text_layout.setContentsMargins(0, 0, 0, 0)
        self.text_layout.setSpacing(10)
        self.text_container.setStyleSheet("")
        
        # 水平布局用于对齐
        content_h_layout = QHBoxLayout()
        content_h_layout.setContentsMargins(0, 0, 0, 0)
        content_h_layout.setSpacing(0)
        
        if self.role == "user":
            content_h_layout.addStretch()
            # 先添加所有图片（如果有）
            if self.image_data_list:
                self.add_multiple_image_widgets(self.text_layout)
            self.parse_content(self.text_layout, self.content, user=True)
            content_h_layout.addWidget(self.text_container)  # 右对齐
        else:
            # AI 消息
            self.parse_content(self.text_layout, self.content, user=False)
            content_h_layout.addWidget(self.text_container)  # 左对齐
            content_h_layout.addStretch()
        
        main_layout.addLayout(content_h_layout)
        
        # 保存外部布局引用（兼容旧代码）
        self.outer_layout = QHBoxLayout()

    def update_content(self, new_content: str):
        """
        流式输出时更新消息内容
        简化策略：始终作为普通文本显示，等待 finalize_content 最终渲染
        """
        self.content = new_content
        
        # 流式输出期间，始终作为普通文本显示
        # 避免代码块未闭合时的渲染问题
        self._update_text_only(new_content)

    def _update_text_only(self, content: str):
        """快速更新纯文本内容 - 流式输出期间使用"""
        if self._cached_text_browser is None:
            # 首次创建 - 创建一个简单的文本浏览器用于流式输出
            self._clear_layout(self.text_layout)
            text_browser = QTextBrowser()
            text_browser.setReadOnly(True)
            text_browser.setTextInteractionFlags(
                Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
            )
            text_browser.setOpenExternalLinks(False)
            text_browser.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            text_browser.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            text_browser.setStyleSheet("""
                QTextBrowser {
                    background: transparent;
                    border: none;
                    font-size: 15px;
                    padding: 4px 0;
                }
            """)
            
            # 设置初始高度
            text_browser.setMinimumHeight(100)
            
            self.text_layout.addWidget(text_browser)
            self._cached_text_browser = text_browser
        
        # 更新内容
        html_text = self.process_inline_code(content.strip(), user=False)
        self._cached_text_browser.setHtml(html_text)
        
        # 更新高度
        try:
            doc = self._cached_text_browser.document()
            height = int(doc.size().height()) + 20
            self._cached_text_browser.setFixedHeight(max(height, 50))
        except RuntimeError:
            pass

    def _has_unclosed_code_block(self, content: str) -> bool:
        """检查是否存在未闭合的代码块"""
        # 统计 ``` 出现的次数
        count = content.count('```')
        # 如果是奇数，说明有未闭合的代码块
        return count % 2 == 1

    def finalize_content(self):
        """
        AI 回复完成后调用，重新解析并渲染最终内容
        此时所有代码块都应该已闭合
        """
        if not self.content:
            return
        
        # 检查是否仍有未闭合的代码块（可能 AI 输出不完整）
        if self._has_unclosed_code_block(self.content):
            # 尝试自动闭合
            self.content = self.content + '\n```'
        
        # 完全重新解析内容
        self._clear_layout(self.text_layout)
        self._cached_code_blocks.clear()
        self._cached_text_browser = None
        self.parse_content(self.text_layout, self.content, user=False)

    def _clear_layout(self, layout: QVBoxLayout):
        """清理布局中的所有子控件"""
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                # 递归清理子布局
                self._clear_layout(item.layout())
        
        # 清空缓存引用
        self._cached_text_browser = None
        self._cached_code_blocks.clear()
        self._all_text_browsers.clear()

    def add_multiple_image_widgets(self, layout: QVBoxLayout):
        """添加多张图片显示组件"""
        if not self.image_data_list:
            return
        
        # 如果只有一张图片，直接添加
        if len(self.image_data_list) == 1:
            self._add_single_image_widget(layout, self.image_data_list[0])
            return
        
        # 多张图片：创建水平布局的图片容器
        images_container = QWidget()
        images_h_layout = QHBoxLayout(images_container)
        images_h_layout.setContentsMargins(0, 0, 0, 0)
        images_h_layout.setSpacing(8)
        
        for image_data in self.image_data_list:
            image_label = self._create_image_label(image_data)
            if image_label:
                images_h_layout.addWidget(image_label)
        
        images_h_layout.addStretch()  # 图片靠左对齐
        layout.addWidget(images_container)

    def _add_single_image_widget(self, layout: QVBoxLayout, image_data: str):
        """添加单张图片显示组件"""
        image_label = self._create_image_label(image_data)
        if image_label:
            layout.addWidget(image_label)

    def _create_image_label(self, image_data: str) -> Optional[QLabel]:
        """创建图片标签控件"""
        image_label = QLabel()
        image_label.setStyleSheet("""
            QLabel {
                background: #f7fafc;
                border-radius: 12px;
                padding: 8px;
            }
        """)
        try:
            # 解码 Base64 图片
            image_bytes = base64.b64decode(image_data)
            image = QImage()
            image.loadFromData(image_bytes)
            if not image.isNull():
                # 缩放到合适大小，保持宽高比
                pixmap = QPixmap.fromImage(image)
                # 多图时使用较小尺寸
                max_size = 200 if len(self.image_data_list) > 1 else 300
                pixmap = pixmap.scaled(max_size, max_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                image_label.setPixmap(pixmap)
                return image_label
        except Exception:
            # 图片解码失败时显示提示
            error_label = QLabel("[图片加载失败]")
            error_label.setStyleSheet("color: #e53e3e; font-size: 14px;")
            return error_label
        return None

    def parse_content(self, layout: QVBoxLayout, content: str, user: bool):
        """
        解析内容，支持代码块和普通文本
        使用 mistune AST 解析替代正则表达式
        """
        if not content:
            return
        
        # 使用 MarkdownParser 分割内容
        segments = self.parser.split_content(content)
        
        for segment in segments:
            if segment['type'] == 'code':
                # 创建代码块控件（支持语法高亮）
                code = segment['content']
                lang = segment.get('language', 'code')
                
                if code.strip():
                    code_block = CodeBlockWidget(code, lang)
                    layout.addWidget(code_block)
                    # 缓存代码块引用
                    if not user:
                        self._cached_code_blocks.append(code_block)
            else:
                # 普通文本
                text = segment['content']
                if text.strip():
                    self._add_text_segment(layout, text, user)
    
    def _add_text_segment(self, layout: QVBoxLayout, text: str, user: bool):
        """添加一个文本片段到布局中"""
        html_text = self.process_inline_code(text, user)
        
        # 创建 QTextBrowser
        text_browser = QTextBrowser()
        text_browser.setHtml(html_text)
        text_browser.setReadOnly(True)
        text_browser.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
        )
        text_browser.setOpenExternalLinks(False)
        text_browser.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        text_browser.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        text_browser.setStyleSheet("""
            QTextBrowser {
                background: transparent;
                border: none;
                font-size: 15px;
                padding: 4px 0;
            }
            QTextBrowser::selection {
                background: #667eea;
                color: white;
            }
        """)
        
        # 添加到布局
        layout.addWidget(text_browser)
        
        # 缓存文本浏览器引用（用于全选）
        self._all_text_browsers.append(text_browser)
        
        # 高度自适应 - 使用信号连接确保文档渲染完成后更新高度
        def update_height(tb):
            try:
                doc = tb.document()
                if doc:
                    height = int(doc.size().height()) + 20
                    tb.setFixedHeight(max(height, 20))
            except RuntimeError:
                pass
        
        # 连接文档大小变化信号
        text_browser.document().documentLayout().documentSizeChanged.connect(
            lambda size, tb=text_browser: update_height(tb)
        )
        
        # 使用更长的延迟确保文档已渲染
        QTimer.singleShot(50, lambda tb=text_browser: update_height(tb))

    def process_inline_code(self, text: str, user: bool) -> str:
        """处理行内代码和格式，支持表格渲染"""
        color = "#1e40af" if user else "#1a202c"
        
        # 检测是否包含表格格式（至少两行且包含 | 分隔符）
        # 表格格式：| col1 | col2 | 后跟 |---|---| 分隔行
        table_pattern = r'^\|.+\|\s*\n\|[-\s|:]+\|\s*\n(\|.+\|\s*\n?)+'
        if re.search(table_pattern, text, re.MULTILINE):
            # 包含表格，使用完整的 Markdown 解析器
            html = self.parser.parse_to_html(text)
            # 为表格添加样式
            styled_html = self._add_table_styles(html, color)
            return f'<div style="line-height: 1.7; color: {color};">{styled_html}</div>'
        
        # 检测简化的表格格式（仅包含多行 | 分隔的内容）
        lines = text.strip().split('\n')
        table_lines = [line for line in lines if '|' in line and line.strip().startswith('|')]
        if len(table_lines) >= 2:
            # 可能是表格，尝试用 Markdown 解析
            html = self.parser.parse_to_html(text)
            if '<table' in html:
                styled_html = self._add_table_styles(html, color)
                return f'<div style="line-height: 1.7; color: {color};">{styled_html}</div>'
        
        # 普通文本处理
        # 先转义 HTML 特殊字符
        text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        
        # 处理三个反引号（代码块标记）：显示为代码样式的文本
        # 注意：流式输出时未闭合的代码块标记会显示为普通文本
        text = text.replace('```', '<span style="background:#f1f5f9; color:#718096; padding:2px 6px; border-radius:4px; font-family:monospace; font-size:0.85em;">```</span>')
        
        # 修复问题2：简化行内代码正则表达式（移除冗余的后顾断言）
        text = re.sub(r'(?<!`)`(?!`)([^`]+)`(?!`)', r'<span style="background:#f1f5f9; color:#e53e3e; padding:2px 8px; border-radius:12px; font-family:monospace; font-size:0.9em;">\1</span>', text)
        
        # 处理加粗和斜体
        text = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', text)
        text = re.sub(r'\*([^*]+)\*', r'<i>\1</i>', text)
        text = text.replace('\n', '<br>')
        return f'<div style="line-height: 1.7; color: {color};">{text}</div>'
    
    def _add_table_styles(self, html: str, text_color: str) -> str:
        """为表格 HTML 添加样式"""
        # 表格样式
        table_style = (
            'border-collapse: collapse; '
            'width: 100%; '
            'margin: 10px 0; '
            'font-size: 14px; '
            'background: #fafafa; '
            'border-radius: 8px; '
            'overflow: hidden; '
            'box-shadow: 0 1px 3px rgba(0,0,0,0.1);'
        )
        
        # 表头样式
        th_style = (
            'background: #667eea; '
            'color: white; '
            'padding: 12px 16px; '
            'text-align: left; '
            'font-weight: 600; '
            'border-bottom: 2px solid #5a67d8;'
        )
        
        # 单元格样式
        td_style = (
            f'color: {text_color}; '
            'padding: 10px 16px; '
            'border-bottom: 1px solid #e2e8f0;'
        )
        
        # 行样式
        tr_style = 'background: #ffffff;'
        
        # 替换 <table>
        html = re.sub(r'<table>', f'<table style="{table_style}">', html)
        
        # 替换 <th> - 在标签名后插入 style
        html = re.sub(r'<th>', f'<th style="{th_style}">', html)
        
        # 替换 <td> - 在标签名后插入 style
        html = re.sub(r'<td>', f'<td style="{td_style}">', html)
        
        # 替换 <tr>
        html = re.sub(r'<tr>', f'<tr style="{tr_style}">', html)
        
        return html
    
    def get_all_text(self) -> str:
        """获取消息的所有文本内容（纯文本格式）"""
        # 直接返回原始内容，因为 self.content 保存了原始 markdown 文本
        return self.content
    
    def copy_all_content(self):
        """复制消息的全部内容到剪贴板"""
        clipboard = QApplication.clipboard()
        all_text = self.get_all_text()
        clipboard.setText(all_text)
        
        # 更新按钮状态显示
        self.copy_all_btn.setText("✅ 已复制")
        QTimer.singleShot(1500, lambda: self.copy_all_btn.setText("📋 复制全部"))
    
    def select_all_text(self):
        """选中消息内的所有文本（用于 CTRL+A）"""
        # 选中所有文本浏览器中的内容
        for text_browser in self._all_text_browsers:
            try:
                text_browser.selectAll()
            except RuntimeError:
                pass
        
        # 选中所有代码块中的内容
        for code_block in self._cached_code_blocks:
            try:
                code_browser = code_block.code_display
                code_browser.selectAll()
            except (RuntimeError, AttributeError):
                pass
        
        # 如果有缓存的文本浏览器（流式输出时），也选中
        if self._cached_text_browser:
            try:
                self._cached_text_browser.selectAll()
            except RuntimeError:
                pass
    
    def keyPressEvent(self, event):
        """处理键盘事件，实现 CTRL+A 全选"""
        if event.key() == Qt.Key_A and event.modifiers() == Qt.ControlModifier:
            # CTRL+A: 全选当前消息的所有内容
            self.select_all_text()
            event.accept()
        else:
            super().keyPressEvent(event)


# ==================== 主窗口 ====================
class ChatWindow(QMainWindow):
    # 历史文件存储路径
    HISTORY_DIR = os.path.join(os.path.expanduser("~"), ".aichat")
    HISTORY_FILE = os.path.join(HISTORY_DIR, "conversations.json")
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI 聊天机器人 V0.4.4")
        self.resize(1200, 800)
        self.setMinimumSize(1000, 600)

        # 对话历史
        self.messages_history: List[Dict[str, str]] = []
        self.conversations: Dict[str, Dict] = {}
        self.current_conversation_id = None
        self.api_worker = None
        self.current_image_data_list = []  # 存储当前待发送的多张图片Base64列表
        self._save_timer = None  # 延迟保存计时器
        
        # 修复问题4：规范化属性初始化
        self.current_ai_content = ""
        self.current_ai_widget = None
        # 新增：用于追踪当前请求所属的会话ID，防止切换会话后数据错乱
        self._request_conversation_id = None

        # 加载配置
        self.settings = QSettings("MyChatApp", "Settings")
        self.api_key = self.settings.value("api_key", "")
        self.base_url = self.settings.value("base_url", "")
        self.model = self.settings.value("model", "deepseek-chat")
        self.supports_vision = self.settings.value("supports_vision", False, type=bool)

        # 设置窗口图标
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

        # 创建小老虎组件
        self.tiger_widget = TigerWidget()
        self.tiger_widget._main_window = self

        # 创建进程监控器
        self.process_monitor = ProcessMonitor()
        self.process_monitor.running.connect(lambda: self.tiger_widget.set_running_mode(True))
        self.process_monitor.sleeping.connect(lambda: self.tiger_widget.set_running_mode(False))
        self.process_monitor.start()

        # 加载历史对话
        if not self.load_conversations():
            # 如果没有历史，创建新对话
            self.create_new_conversation()

    def setup_ui(self):
        # 全局样式
        self.setStyleSheet("""
            QMainWindow {
                background: #f7fafc;
            }
            QScrollBar:vertical {
                background: #edf2f7;
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #cbd5e0;
                border-radius: 4px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover {
                background: #a0aec0;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QListWidget {
                outline: none;
            }
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

        # Logo
        header = QHBoxLayout()
        logo_label = QLabel("🤖")
        logo_label.setStyleSheet("font-size: 32px; background: transparent;")
        header.addWidget(logo_label)
        title_label = QLabel("AI Chat")
        title_label.setStyleSheet("color: white; font-size: 24px; font-weight: 600; background: transparent;")
        header.addWidget(title_label)
        header.addStretch()
        left_layout.addLayout(header)

        # 新建对话按钮
        new_conv_btn = QPushButton("➕  新建对话")
        new_conv_btn.setCursor(Qt.PointingHandCursor)
        new_conv_btn.clicked.connect(self.create_new_conversation)
        new_conv_btn.setStyleSheet("""
            QPushButton {
                background: #2d3748;
                color: #e2e8f0;
                border: 2px dashed #4a5568;
                border-radius: 16px;
                padding: 16px;
                font-size: 16px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #3d4758;
                border-color: #718096;
                color: white;
            }
            QPushButton:pressed {
                background: #1e2a3a;
            }
        """)
        left_layout.addWidget(new_conv_btn)

        list_label = QLabel("对话历史")
        list_label.setStyleSheet("color: #a0aec0; font-size: 12px; margin-top: 8px; background: transparent; letter-spacing: 0.5px;")
        left_layout.addWidget(list_label)

        # 对话列表
        self.conversation_list = QListWidget()
        self.conversation_list.setStyleSheet("""
            QListWidget {
                background: transparent;
                border: none;
                outline: none;
                font-size: 14px;
            }
            QListWidget::item {
                color: #e2e8f0;
                padding: 14px 16px;
                border-radius: 12px;
                margin: 2px 0;
            }
            QListWidget::item:hover {
                background: #2d3748;
            }
            QListWidget::item:selected {
                background: #4a5568;
                color: white;
            }
        """)
        self.conversation_list.currentItemChanged.connect(self.on_conversation_changed)
        self.conversation_list.itemDoubleClicked.connect(self.rename_conversation)
        self.conversation_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.conversation_list.customContextMenuRequested.connect(self.show_context_menu)
        left_layout.addWidget(self.conversation_list)

        settings_btn = QPushButton("⚙️  API 设置")
        settings_btn.setCursor(Qt.PointingHandCursor)
        settings_btn.clicked.connect(self.open_settings)
        settings_btn.setStyleSheet("""
            QPushButton { background: #2d3748; color: #a0aec0; border: none; border-radius: 12px; padding: 14px; font-size: 15px; }
            QPushButton:hover { background: #3d4758; color: white; }
        """)
        left_layout.addWidget(settings_btn)

        # 清除历史按钮
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

        # 顶部标题栏
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

        # 消息显示区域
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

        # 底部输入区域
        input_container = QFrame()
        input_container.setFixedHeight(220)
        input_container.setStyleSheet("background: white; border-top: 1px solid #e2e8f0;")
        input_layout = QVBoxLayout(input_container)
        input_layout.setContentsMargins(28, 20, 28, 24)
        input_layout.setSpacing(10)

        # 图片预览区（支持多图）
        self.image_preview_container = QWidget()
        self.image_preview_container.setVisible(False)
        self.image_preview_layout = QHBoxLayout(self.image_preview_container)
        self.image_preview_layout.setContentsMargins(0, 0, 0, 0)
        self.image_preview_layout.setSpacing(8)
        self.image_preview_layout.addStretch()  # 左侧弹簧，让图片靠右对齐
        input_layout.addWidget(self.image_preview_container)

        input_frame = QFrame()
        input_frame.setStyleSheet("""
            QFrame {
                background: #f7fafc;
                border: 2px solid #e2e8f0;
                border-radius: 24px;
            }
        """)
        input_frame_layout = QHBoxLayout(input_frame)
        input_frame_layout.setContentsMargins(20, 12, 12, 12)
        input_frame_layout.setSpacing(10)

        # 附件按钮
        self.attach_btn = QPushButton("📎")
        self.attach_btn.setCursor(Qt.PointingHandCursor)
        self.attach_btn.setFixedSize(40, 40)
        self.attach_btn.clicked.connect(self.upload_file)
        self.attach_btn.setStyleSheet("""
            QPushButton {
                background: transparent; border: none; font-size: 20px;
            }
            QPushButton:hover {
                background: #e2e8f0; border-radius: 20px;
            }
        """)
        input_frame_layout.addWidget(self.attach_btn, 0, Qt.AlignBottom)

        self.input_edit = QTextEdit()
        self.input_edit.setPlaceholderText("输入消息... (Enter 发送，Shift+Enter 换行)")
        self.input_edit.setFixedHeight(80)
        self.input_edit.setStyleSheet("""
            QTextEdit {
                background: transparent;
                border: none;
                font-size: 16px;
                color: #2d3748;
                selection-background-color: #667eea;
                selection-color: white;
            }
        """)
        self.input_edit.textChanged.connect(self.auto_resize_input)
        input_frame_layout.addWidget(self.input_edit, 1)

        self.send_btn = QPushButton("发送")
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setFixedSize(90, 48)
        self.send_btn.clicked.connect(self.send_message)
        self.send_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #667eea, stop:1 #764ba2);
                color: white; border: none; border-radius: 30px; font-size: 16px; font-weight: 600;
            }
            QPushButton:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #5a6fd6, stop:1 #6a4190); }
            QPushButton:disabled { background: #cbd5e0; }
        """)
        input_frame_layout.addWidget(self.send_btn, 0, Qt.AlignBottom)

        input_layout.addWidget(input_frame)
        
        hint_label = QLabel("AI 生成内容仅供参考，请仔细甄别。支持文本/图片/代码文件上传。")
        hint_label.setStyleSheet("color: #a0aec0; font-size: 13px; padding-left: 8px; margin: 0;")
        input_layout.addWidget(hint_label)

        right_layout.addWidget(input_container)
        splitter.addWidget(right_widget)
        splitter.setSizes([280, 920])
        self.input_edit.installEventFilter(self)

    # 修复问题11：添加API设置验证
    def validate_api_settings(self) -> tuple:
        """验证API设置是否有效"""
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

    def upload_file(self):
        """处理文件上传（支持多选图片）"""
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, 
            "选择文件（可多选图片）", 
            "", 
            "图片与文本(*.png *.jpg *.jpeg *.bmp *.gif *.webp *.txt *.py *.md *.json *.xml *.html *.css *.js);;图片文件(*.png *.jpg *.jpeg *.bmp *.gif *.webp);;文本文件(*.txt *.py *.md *.json *.xml *.html *.css *.js);;所有文件(*)"
        )
        if not file_paths:
            return

        for file_path in file_paths:
            ext = os.path.splitext(file_path)[1].lower()
            
            # 处理图片
            if ext in ['.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp']:
                try:
                    image = QImage(file_path)
                    if image.isNull():
                        QMessageBox.warning(self, "错误", f"无法加载图片: {file_path}")
                        continue
                    
                    # 转换为 Base64
                    byte_array = QByteArray()
                    buffer = QBuffer(byte_array)
                    buffer.open(QBuffer.ReadWrite)
                    image.save(buffer, "PNG")
                    buffer.close()
                    image_base64 = base64.b64encode(byte_array).decode('utf-8')
                    
                    # 添加到图片列表
                    self.current_image_data_list.append(image_base64)
                    
                    # 添加预览缩略图
                    self._add_image_preview(image, image_base64)
                    
                except Exception as e:
                    QMessageBox.critical(self, "错误", f"读取图片失败: {e}")

            # 处理文本文件
            elif ext in ['.txt', '.py', '.md', '.json', '.html', '.css', '.js', '.xml']:
                try:
                    # 修复问题6：添加文件编码检测和多种编码尝试
                    content = self._read_file_with_encoding(file_path)
                    
                    lang_map = {
                        '.py': 'python',
                        '.js': 'javascript',
                        '.html': 'html',
                        '.css': 'css',
                        '.json': 'json',
                        '.xml': 'xml',
                        '.md': 'markdown',
                        '.txt': ''
                    }
                    lang = lang_map.get(ext, '')
                    
                    # 使用零宽空格转义，防止文件内容中的 ``` 干扰消息格式
                    # 显示时会恢复正常的外观
                    safe_content = content.replace('```', '`\u200B`\u200B`')
                    
                    current_text = self.input_edit.toPlainText()
                    wrapped_content = f"\n[文件: {os.path.basename(file_path)}]\n```{lang}\n{safe_content}\n```\n"
                    self.input_edit.setPlainText(f"{current_text}{wrapped_content}")
                    self.auto_resize_input()
                except Exception as e:
                    QMessageBox.critical(self, "错误", f"读取文件失败: {e}")

    def _read_file_with_encoding(self, file_path: str) -> str:
        """使用多种编码尝试读取文件"""
        encodings = ['utf-8', 'gbk', 'gb2312', 'gb18030', 'big5', 'shift_jis', 'latin-1']
        
        for encoding in encodings:
            try:
                with open(file_path, 'r', encoding=encoding) as f:
                    return f.read()
            except (UnicodeDecodeError, UnicodeError):
                continue
        
        # 如果所有编码都失败，尝试以二进制模式读取并忽略错误
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read()
        except Exception as e:
            raise Exception(f"无法解码文件: {str(e)}")

    def _add_image_preview(self, image: QImage, image_base64: str):
        """添加图片预览缩略图（带删除按钮）"""
        thumb_container = QWidget()
        thumb_layout = QVBoxLayout(thumb_container)
        thumb_layout.setContentsMargins(0, 0, 0, 0)
        thumb_layout.setSpacing(2)
        
        thumb_label = QLabel()
        pixmap = QPixmap.fromImage(image).scaled(80, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        thumb_label.setPixmap(pixmap)
        thumb_label.setStyleSheet("""
            QLabel {
                background: #f7fafc;
                border: 2px solid #e2e8f0;
                border-radius: 8px;
                padding: 4px;
            }
        """)
        thumb_layout.addWidget(thumb_label)
        
        remove_btn = QPushButton("✕")
        remove_btn.setFixedSize(24, 24)
        remove_btn.setCursor(Qt.PointingHandCursor)
        remove_btn.setStyleSheet("""
            QPushButton {
                background: #e53e3e;
                color: white;
                border: none;
                border-radius: 12px;
                font-size: 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: #c53030;
            }
        """)
        remove_btn.clicked.connect(lambda checked, b64=image_base64, w=thumb_container: self._remove_image_preview(b64, w))
        thumb_layout.addWidget(remove_btn, 0, Qt.AlignCenter)
        
        self.image_preview_layout.insertWidget(self.image_preview_layout.count() - 1, thumb_container)
        self.image_preview_container.setVisible(True)

    def _remove_image_preview(self, image_base64: str, widget: QWidget):
        """修复问题8：移除图片预览，确保同步更新状态"""
        if image_base64 in self.current_image_data_list:
            self.current_image_data_list.remove(image_base64)
        
        # 立即隐藏并标记删除
        widget.setVisible(False)
        widget.deleteLater()
        
        if not self.current_image_data_list:
            self.image_preview_container.setVisible(False)

    def _clear_image_previews(self):
        """清除所有图片预览"""
        while self.image_preview_layout.count() > 1:
            item = self.image_preview_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        self.image_preview_container.setVisible(False)

    def auto_resize_input(self):
        doc = self.input_edit.document()
        height = min(doc.size().height() + 20, 180)
        self.input_edit.setFixedHeight(max(height, 60))

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

    def open_settings(self):
        dialog = SettingsDialog(self)
        if dialog.exec() == QDialog.Accepted:
            settings = dialog.get_settings()
            self.api_key = settings["api_key"]
            self.base_url = settings["base_url"]
            self.model = settings["model"]
            self.supports_vision = settings["supports_vision"]
            dialog.save_settings()
            QMessageBox.information(self, "设置已保存", "API配置已更新。")

    def _format_timestamp(self, timestamp_str: str) -> str:
        if not timestamp_str:
            return ""
        
        try:
            dt = datetime.fromisoformat(timestamp_str)
            return dt.strftime("%m/%d %H:%M")
        except ValueError:
            return timestamp_str

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
        """
        取消当前正在进行的AI请求，清理相关状态
        修复：切换会话时防止崩溃和数据错乱
        """
        # 停止请求线程
        if self.api_worker and self.api_worker.isRunning():
            self.api_worker.stop()
            # 不等待线程完成，直接断开信号连接
            try:
                self.api_worker.stream_chunk.disconnect()
                self.api_worker.finished_stream.disconnect()
                self.api_worker.error_occurred.disconnect()
            except (RuntimeError, TypeError):
                pass  # 信号可能已断开
            self.api_worker = None
        
        # 清理AI消息状态
        self.current_ai_content = ""
        self.current_ai_widget = None
        self._request_conversation_id = None
        
        # 恢复UI状态
        self.status_label.setText("● 就绪")
        self.status_label.setStyleSheet("color: #48bb78; font-size: 14px; font-weight: 500;")
        self.send_btn.setEnabled(True)

    def create_new_conversation(self):
        # 取消当前请求（如果有）
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
            # 修复：切换会话前取消当前请求，防止崩溃和数据错乱
            self._cancel_current_request()
            
            conv_id = current.data(Qt.ItemDataRole.UserRole)
            self.current_conversation_id = conv_id
            self.update_conversation_title()
            self.load_conversation_messages()

    def update_conversation_title(self):
        if self.current_conversation_id:
            title = self.conversations[self.current_conversation_id]['title']
            self.conversation_title.setText(title)

    def load_conversation_messages(self):
        self.clear_messages()
        if self.current_conversation_id:
            messages = self.conversations[self.current_conversation_id]['messages']
            for msg in messages:
                content = msg['content']
                image_data_list = []
                
                if isinstance(content, list):
                    text_part = next((item.get('text', '') for item in content if item.get('type') == 'text'), "")
                    
                    for item in content:
                        if item.get('type') == 'image_url':
                            image_url = item.get('image_url', {})
                            url = image_url.get('url', '')
                            if url.startswith('data:image') and ';base64,' in url:
                                image_data_list.append(url.split(';base64,')[1])
                    
                    self.add_message_widget(msg['role'], text_part, image_data_list if image_data_list else None)
                else:
                    self.add_message_widget(msg['role'], msg['content'])
            self.scroll_to_bottom()

    def clear_messages(self):
        while self.messages_layout.count() > 1:
            item = self.messages_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def add_message_widget(self, role: str, content: str, image_data_list: List[str] = None):
        stretch_item = self.messages_layout.takeAt(self.messages_layout.count() - 1)
        msg_widget = MessageWidget(role, content, image_data_list)
        self.messages_layout.addWidget(msg_widget)
        self.messages_layout.addStretch()
        return msg_widget

    def scroll_to_bottom(self):
        QTimer.singleShot(100, lambda: self.scroll_area.verticalScrollBar().setValue(
            self.scroll_area.verticalScrollBar().maximum()
        ))

    def _check_model_supports_vision(self) -> bool:
        """修复问题7：改进视觉模型检测，优先使用用户设置"""
        # 首先检查用户手动设置
        if self.supports_vision:
            return True
        
        # 自动检测关键词
        model_lower = self.model.lower()
        
        vision_keywords = [
            'vision', 'vl', 'visual', 'multimodal', 'mm',
            '4o', 'gpt-4-turbo', 'gpt-4-vision',
            'claude-3', 'claude-3.5',
            'gemini', 'qwen-vl', 'glm-4v', 'deepseek-vl',
            'llava', 'cogvlm', 'internvl', 'yi-vl'
        ]
        
        return any(kw in model_lower for kw in vision_keywords)

    def send_message(self):
        user_text = self.input_edit.toPlainText().strip()
        has_images = bool(self.current_image_data_list)
        
        # 修复问题11：使用验证函数检查API设置
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
                dialog.setStyleSheet("""
                    QDialog { background: white; }
                    QLabel { color: #2d3748; }
                    QPushButton { padding: 8px 24px; border-radius: 8px; font-weight: 500; }
                """)
                
                layout = QVBoxLayout(dialog)
                layout.setSpacing(16)
                layout.setContentsMargins(24, 24, 24, 24)
                
                info_label = QLabel(
                    f"⚠️ 当前模型「{self.model}」可能不支持图片输入。\n\n"
                    "如果您确定该模型支持多模态，可以直接发送。\n"
                    "API 会返回具体的错误信息，您可据此调整。\n\n"
                    "您也可以在API设置中勾选「此模型支持图片输入」来跳过此检测。"
                )
                info_label.setWordWrap(True)
                info_label.setStyleSheet("font-size: 14px; line-height: 1.6;")
                layout.addWidget(info_label)
                
                skip_checkbox = QCheckBox("不再提示此警告")
                skip_checkbox.setStyleSheet("font-size: 13px; color: #718096;")
                layout.addWidget(skip_checkbox)
                
                btn_layout = QHBoxLayout()
                btn_layout.addStretch()
                
                cancel_btn = QPushButton("取消")
                cancel_btn.setStyleSheet(
                    "background: #f7fafc; color: #4a5568; border: 1px solid #e2e8f0;"
                )
                cancel_btn.clicked.connect(dialog.reject)
                btn_layout.addWidget(cancel_btn)
                
                send_btn = QPushButton("发送")
                send_btn.setStyleSheet(
                    "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #667eea, stop:1 #764ba2);"
                    "color: white; border: none;"
                )
                send_btn.clicked.connect(dialog.accept)
                btn_layout.addWidget(send_btn)
                
                layout.addLayout(btn_layout)
                
                if dialog.exec() == QDialog.DialogCode.Rejected:
                    return
                
                if skip_checkbox.isChecked():
                    self.settings.setValue("skip_vision_warning", True)
        
        if not user_text and not has_images:
            return

        if has_images:
            message_content = []
            if user_text:
                message_content.append({"type": "text", "text": user_text})
            for image_data in self.current_image_data_list:
                message_content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{image_data}"
                    }
                })
        else:
            message_content = user_text
        
        image_count = len(self.current_image_data_list)
        if user_text:
            display_text = user_text
        elif has_images:
            display_text = f"[发送了{image_count}张图片]" if image_count > 1 else "[发送了一张图片]"
        else:
            display_text = ""
        
        current_images_for_display = self.current_image_data_list.copy()

        self.input_edit.clear()
        self.current_image_data_list = []
        self._clear_image_previews()
            
        self.conversations[self.current_conversation_id]['messages'].append({"role": "user", "content": message_content})
        self.add_message_widget("user", display_text, current_images_for_display)
        self.scroll_to_bottom()

        if len(self.conversations[self.current_conversation_id]['messages']) == 1:
            new_title = display_text[:20] + ('...' if len(display_text) > 20 else '')
            self.conversations[self.current_conversation_id]['title'] = new_title
            self.update_conversation_title()
            self.conversation_list.currentItem().setText(f"💬 {new_title}")

        self.status_label.setText("● AI 正在思考...")
        self.status_label.setStyleSheet("color: #ed8936; font-size: 14px; font-weight: 500;")
        self.send_btn.setEnabled(False)

        # 修复问题4：重置状态变量
        self.current_ai_content = ""
        self.current_ai_widget = self.add_message_widget("assistant", "")
        # 记录当前请求所属的会话ID
        self._request_conversation_id = self.current_conversation_id

        messages = self.conversations[self.current_conversation_id]['messages']
        self.api_worker = AIRequestThread(messages, self.api_key, self.base_url, self.model)
        self.api_worker.stream_chunk.connect(self.on_stream_chunk)
        self.api_worker.finished_stream.connect(self.on_stream_finished)
        self.api_worker.error_occurred.connect(self.on_api_error)
        self.api_worker.start()

    def on_stream_chunk(self, chunk: str):
        # 修复：检查会话ID是否匹配，防止切换会话后更新错误的控件
        if self._request_conversation_id != self.current_conversation_id:
            return  # 已切换到其他会话，忽略此回调
        
        self.current_ai_content += chunk
            
        if self.current_ai_widget:
            try:
                self.current_ai_widget.update_content(self.current_ai_content)
            except RuntimeError:
                # 控件已被删除，忽略此更新
                self.current_ai_widget = None
                return
        else:
            self.current_ai_widget = self.add_message_widget("assistant", self.current_ai_content)
        
        self.scroll_to_bottom()

    def on_stream_finished(self):
        # 修复：检查会话ID是否匹配，防止将AI回答追加到错误的会话
        if self._request_conversation_id != self.current_conversation_id:
            # 已切换到其他会话，丢弃此结果
            self._request_conversation_id = None
            return
        
        if self.current_ai_content:
            self.conversations[self.current_conversation_id]['messages'].append(
                {"role": "assistant", "content": self.current_ai_content}
            )
            
            # 流式输出完成，重新解析并渲染最终内容
            if self.current_ai_widget:
                try:
                    self.current_ai_widget.finalize_content()
                except RuntimeError:
                    # 控件已被删除，忽略
                    pass
        
        self.current_ai_content = ""
        self.current_ai_widget = None
        self._request_conversation_id = None
        
        self.status_label.setText("● 就绪")
        self.status_label.setStyleSheet("color: #48bb78; font-size: 14px; font-weight: 500;")
        self.send_btn.setEnabled(True)
        self.save_conversations()

    def on_api_error(self, error_msg: str):
        # 修复：检查会话ID是否匹配
        if self._request_conversation_id != self.current_conversation_id:
            # 已切换到其他会话，不显示错误提示
            self._request_conversation_id = None
            return
        
        QMessageBox.critical(self, "API错误", f"请求失败：{error_msg}")
        self.status_label.setText("● 错误")
        self.status_label.setStyleSheet("color: #f56565; font-size: 14px; font-weight: 500;")
        self.send_btn.setEnabled(True)
        
        # 修复问题5：API错误时清理空的AI消息控件
        if self.current_ai_widget and not self.current_ai_content:
            try:
                # 移除空的AI消息控件
                index = self.messages_layout.indexOf(self.current_ai_widget)
                if index >= 0:
                    self.messages_layout.takeAt(index)
                    self.current_ai_widget.deleteLater()
            except RuntimeError:
                # 控件已被删除，忽略
                pass
        
        self.current_ai_content = ""
        self.current_ai_widget = None
        self._request_conversation_id = None

    def show_context_menu(self, pos):
        item = self.conversation_list.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background: white;
                border: 1px solid #e2e8f0;
                border-radius: 12px;
                padding: 6px;
                font-size: 14px;
            }
            QMenu::item {
                padding: 10px 28px 10px 16px;
                border-radius: 8px;
            }
            QMenu::item:selected {
                background: #edf2f7;
                color: #1a202c;
            }
        """)
        rename_action = menu.addAction("✏️ 重命名")
        delete_action = menu.addAction("🗑️ 删除")
        action = menu.exec(self.conversation_list.mapToGlobal(pos))
        if action == rename_action:
            self.rename_conversation(item)
        elif action == delete_action:
            self.delete_conversation(item)

    def save_conversations(self, delay: bool = True):
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
                "version": 1,
                "conversations": self.conversations,
                "last_updated": datetime.now().isoformat()
            }
            
            temp_file = self.HISTORY_FILE + ".tmp"
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            if os.path.exists(self.HISTORY_FILE):
                os.remove(self.HISTORY_FILE)
            os.rename(temp_file, self.HISTORY_FILE)
            
        except Exception as e:
            print(f"保存对话历史失败: {e}")

    def load_conversations(self) -> bool:
        try:
            if not os.path.exists(self.HISTORY_FILE):
                return False
            
            with open(self.HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            if data.get("version", 0) != 1:
                print("对话历史版本不兼容，将创建新对话")
                return False
            
            self.conversations = data.get("conversations", {})
            
            if not self.conversations:
                return False
            
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
        reply = QMessageBox.question(self, "确认删除", "确定要删除这个对话吗？",
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

    def changeEvent(self, event):
        """检测窗口最小化事件"""
        if event.type() == QEvent.WindowStateChange and self.isMinimized():
            # 最小化时正常最小化到任务栏，不显示小老虎
            pass
        super().changeEvent(event)

    def closeEvent(self, event):
        # 检查是否是从小老虎图标触发的退出
        # 如果小老虎正在显示，说明用户点击了关闭按钮，需要最小化到小老虎
        if hasattr(self, 'tiger_widget') and self.tiger_widget.isVisible():
            # 小老虎正在显示，说明这是第二次close（真正退出）
            # 执行正常清理
            self.tiger_widget.hide()
        else:
            # 第一次关闭，最小化到小老虎
            if hasattr(self, 'tiger_widget'):
                self.hide()  # 隐藏主窗口
                self.tiger_widget.show_at_bottom_right()
            event.ignore()  # 阻止真正关闭
            return

        if self._save_timer is not None:
            self._save_timer.stop()
            self._save_timer.deleteLater()
            self._save_timer = None

        self.save_conversations(delay=False)

        if self.api_worker and self.api_worker.isRunning():
            self.api_worker.stop()
            if not self.api_worker.wait(3000):
                self.api_worker.terminate()

        # 关闭进程监控线程
        if hasattr(self, 'process_monitor') and self.process_monitor.isRunning():
            self.process_monitor.stop()
            if not self.process_monitor.wait(3000):
                self.process_monitor.terminate()

        event.accept()


# ==================== 程序入口 ====================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
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