"""消息展示组件：Markdown 渲染、代码块、工具调用卡片、思考过程卡片、消息气泡。

MessageWidget 支持两种内容形态：
- 纯文本（用户消息 / 旧版历史）
- 内容块列表（agent 消息流：思考/文本段与工具调用交错出现）
"""

import base64
import re
from typing import List, Optional

import mistune
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer, TextLexer
from pygments.util import ClassNotFound
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton,
    QTextBrowser, QVBoxLayout, QWidget,
)


# ==================== Markdown 解析器（mistune + Pygments）====================

class PygmentsRenderer(mistune.HTMLRenderer):
    """使用 Pygments 进行代码高亮的 mistune 渲染器"""

    def __init__(self, style='monokai', css_class='code-highlight', *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style = style
        self.css_class = css_class

    def block_code(self, code, info=None):
        if not code or not code.strip():
            return ''
        lexer = self._get_lexer(code, info)
        formatter = HtmlFormatter(
            style=self.style, cssclass=self.css_class, nowrap=False, linenos=False
        )
        return highlight(code, lexer, formatter)

    def _get_lexer(self, code, info):
        if not info:
            try:
                return guess_lexer(code)
            except ClassNotFound:
                return TextLexer()
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
        escaped = mistune.escape(text)
        return f'<code class="inline-code">{escaped}</code>'


class MarkdownParser:
    """Markdown 解析器封装 - 单例模式"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_parser()
        return cls._instance

    def _init_parser(self):
        self.renderer = PygmentsRenderer(style='monokai')
        self.markdown = mistune.create_markdown(
            renderer=self.renderer, plugins=['table', 'strikethrough', 'url']
        )
        self.token_parser = mistune.Markdown()
        from mistune.plugins.table import table as table_plugin
        table_plugin(self.token_parser)

    def parse_to_html(self, text):
        return self.markdown(text)

    def split_content(self, text):
        """将内容分割为代码块和普通文本片段"""
        tokens, _state = self.token_parser.parse(text)
        result = []
        for token in tokens:
            token_type = token.get('type', '')
            if token_type == 'blank_line':
                continue
            if token_type == 'block_code':
                attrs = token.get('attrs', {})
                lang = attrs.get('info', '').strip() if attrs else ''
                result.append({'type': 'code', 'language': lang or 'code',
                               'content': token.get('raw', '')})
            elif token_type == 'paragraph':
                text_content = self._extract_text(token.get('children', []))
                if text_content.strip():
                    result.append({'type': 'text', 'content': text_content})
            elif token_type == 'heading':
                text_content = self._extract_text(token.get('children', []))
                attrs = token.get('attrs', {})
                level = attrs.get('level', 1) if attrs else 1
                if text_content.strip():
                    result.append({'type': 'text',
                                   'content': f"{'#' * level} {text_content}"})
            elif token_type == 'block_html':
                raw = token.get('raw', '')
                if raw.strip():
                    result.append({'type': 'text', 'content': raw})
            elif token_type == 'list':
                list_text = ""
                for item in token.get('children', []):
                    item_text = self._extract_text(item.get('children', []))
                    list_text += f"• {item_text}\n"
                if list_text.strip():
                    result.append({'type': 'text', 'content': list_text.rstrip()})
            elif token_type == 'table':
                table_text = self._render_table(token)
                if table_text.strip():
                    result.append({'type': 'text', 'content': table_text})
            elif token_type == 'thematic_break':
                result.append({'type': 'text', 'content': '---'})
        return result

    def _extract_text(self, children):
        if not children:
            return ''
        parts = []
        for child in children:
            ctype = child.get('type', '')
            if ctype == 'text':
                parts.append(child.get('raw', ''))
            elif ctype == 'codespan':
                parts.append(f"`{child.get('raw', '')}`")
            elif ctype == 'strong':
                parts.append(f"**{self._extract_text(child.get('children', []))}**")
            elif ctype == 'emphasis':
                parts.append(f"*{self._extract_text(child.get('children', []))}*")
            elif ctype == 'link':
                inner = self._extract_text(child.get('children', []))
                parts.append(f"[{inner}]({child.get('url', '')})")
            elif ctype == 'image':
                parts.append(f"![{child.get('alt', '')}]({child.get('url', '')})")
            elif 'children' in child:
                parts.append(self._extract_text(child['children']))
        return ' '.join(parts)

    def _render_table(self, node):
        children = node.get('children', [])
        if not children:
            return ''
        result = []
        for child in children:
            if child.get('type') == 'table_head':
                cells = [self._extract_text(c.get('children', []))
                         for c in child.get('children', [])]
                result.append('| ' + ' | '.join(cells) + ' |')
                result.append('| ' + ' | '.join(['---'] * len(cells)) + ' |')
            elif child.get('type') == 'table_body':
                for row in child.get('children', []):
                    cells = [self._extract_text(c.get('children', []))
                             for c in row.get('children', [])]
                    result.append('| ' + ' | '.join(cells) + ' |')
        return '\n'.join(result)


def get_markdown_parser():
    return MarkdownParser()


# ==================== 代码块组件 ====================

class CodeBlockWidget(QWidget):
    """代码块控件 - Pygments 语法高亮 + 复制按钮 + 高度自适应"""

    HIGHLIGHT_CSS = """
    <style>
        body { background: transparent; margin: 0; padding: 0; }
        pre { margin: 0; white-space: pre-wrap; word-wrap: break-word; }
        .code-highlight { background: transparent; padding: 0; margin: 0; }
        .code-highlight .hll { background-color: #49483e }
        .code-highlight .c { color: #75715e; font-style: italic }
        .code-highlight .err { color: #960050; background-color: #1e0010 }
        .code-highlight .k { color: #66d9ef; font-weight: bold }
        .code-highlight .l { color: #ae81ff }
        .code-highlight .n { color: #f8f8f2 }
        .code-highlight .o { color: #f92672 }
        .code-highlight .p { color: #f8f8f2 }
        .code-highlight .ch { color: #75715e }
        .code-highlight .cm { color: #75715e }
        .code-highlight .cp { color: #75715e }
        .code-highlight .cpf { color: #75715e }
        .code-highlight .c1 { color: #75715e }
        .code-highlight .cs { color: #75715e }
        .code-highlight .gd { color: #f92672 }
        .code-highlight .ge { font-style: italic }
        .code-highlight .gi { color: #a6e22e }
        .code-highlight .gs { font-weight: bold }
        .code-highlight .gu { color: #75715e }
        .code-highlight .kc { color: #66d9ef; font-weight: bold }
        .code-highlight .kd { color: #66d9ef; font-weight: bold }
        .code-highlight .kn { color: #f92672 }
        .code-highlight .kp { color: #66d9ef }
        .code-highlight .kr { color: #66d9ef; font-weight: bold }
        .code-highlight .kt { color: #66d9ef; font-weight: bold }
        .code-highlight .ld { color: #e6db74 }
        .code-highlight .m { color: #ae81ff }
        .code-highlight .s { color: #e6db74 }
        .code-highlight .na { color: #a6e22e }
        .code-highlight .nb { color: #f8f8f2 }
        .code-highlight .nc { color: #a6e22e }
        .code-highlight .no { color: #66d9ef }
        .code-highlight .nd { color: #a6e22e }
        .code-highlight .ni { color: #f8f8f2 }
        .code-highlight .ne { color: #a6e22e }
        .code-highlight .nf { color: #a6e22e }
        .code-highlight .nl { color: #f8f8f2 }
        .code-highlight .nn { color: #f8f8f2 }
        .code-highlight .nx { color: #a6e22e }
        .code-highlight .py { color: #f8f8f2 }
        .code-highlight .nt { color: #f92672 }
        .code-highlight .nv { color: #f8f8f2 }
        .code-highlight .ow { color: #f92672 }
        .code-highlight .w { color: #f8f8f2 }
        .code-highlight .mb { color: #ae81ff }
        .code-highlight .mf { color: #ae81ff }
        .code-highlight .mh { color: #ae81ff }
        .code-highlight .mi { color: #ae81ff }
        .code-highlight .mo { color: #ae81ff }
        .code-highlight .sa { color: #e6db74 }
        .code-highlight .sb { color: #e6db74 }
        .code-highlight .sc { color: #e6db74 }
        .code-highlight .dl { color: #e6db74 }
        .code-highlight .sd { color: #e6db74 }
        .code-highlight .s2 { color: #e6db74 }
        .code-highlight .se { color: #ae81ff }
        .code-highlight .sh { color: #e6db74 }
        .code-highlight .si { color: #e6db74 }
        .code-highlight .sx { color: #e6db74 }
        .code-highlight .sr { color: #e6db74 }
        .code-highlight .s1 { color: #e6db74 }
        .code-highlight .ss { color: #e6db74 }
        .code-highlight .bp { color: #f8f8f2 }
        .code-highlight .fm { color: #a6e22e }
        .code-highlight .vc { color: #f8f8f2 }
        .code-highlight .vg { color: #f8f8f2 }
        .code-highlight .vi { color: #f8f8f2 }
        .code-highlight .vm { color: #f8f8f2 }
        .code-highlight .il { color: #ae81ff }
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
        lang_label.setStyleSheet(
            "color: #cbd5e0; font-size: 12px; background: transparent; font-family: monospace;")
        header_layout.addWidget(lang_label)
        header_layout.addStretch()

        copy_btn = QPushButton("📋 复制")
        copy_btn.setCursor(Qt.PointingHandCursor)
        copy_btn.setStyleSheet("""
            QPushButton {
                background: #4a5568; color: white; border: none;
                padding: 4px 14px; border-radius: 20px;
                font-size: 12px; font-weight: 500;
            }
            QPushButton:hover { background: #5a6578; }
            QPushButton:pressed { background: #3a4558; }
        """)
        copy_btn.clicked.connect(self.copy_code)
        header_layout.addWidget(copy_btn)
        layout.addWidget(header)

        self.code_display = QTextBrowser()
        self.code_display.setReadOnly(True)
        self.code_display.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
        )
        self.code_display.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.code_display.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
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
        self.code_display.setHtml(self._highlight_code())
        self.code_display.document().documentLayout().documentSizeChanged.connect(
            self._adjust_code_height
        )
        QTimer.singleShot(0, self._adjust_code_height)
        layout.addWidget(self.code_display)

    def _highlight_code(self):
        try:
            lexer = self._get_lexer()
            formatter = HtmlFormatter(style='monokai', cssclass='code-highlight', nowrap=False)
            highlighted = highlight(self.code, lexer, formatter)
            return f"{self.HIGHLIGHT_CSS}<body>{highlighted}</body>"
        except Exception:
            escaped = self.code.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            return f"<pre style='color: #e2e8f0; margin: 0;'>{escaped}</pre>"

    def _get_lexer(self):
        if self.language:
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
        try:
            return guess_lexer(self.code)
        except ClassNotFound:
            return TextLexer()

    def _adjust_code_height(self):
        try:
            doc = self.code_display.document()
            height = int(doc.size().height()) + 40
            self.code_display.setFixedHeight(max(height, 60))
        except RuntimeError:
            pass

    def copy_code(self):
        clipboard = QApplication.clipboard()
        clipboard.setText(self.code)
        btn = self.sender()
        btn.setText("✅ 已复制")
        QTimer.singleShot(1500, lambda: btn.setText("📋 复制"))


# ==================== 工具调用卡片 ====================

class ToolCallWidget(QFrame):
    """工具调用卡片：标题行显示工具名/参数摘要/状态，点击展开完整参数与结果"""

    def __init__(self, call_id: str, name: str, args_json: str, parent=None):
        super().__init__(parent)
        self.call_id = call_id
        self.name = name
        self.args_json = args_json
        self.result_text = None
        self.ok = None
        self.expanded = False
        self.parser = get_markdown_parser()

        self.setObjectName("toolCallCard")
        self.setStyleSheet("""
            QFrame#toolCallCard {
                background: #f1f5f9;
                border: 1px solid #e2e8f0;
                border-radius: 10px;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        # 标题行
        header = QHBoxLayout()
        header.setSpacing(8)
        self.toggle_label = QLabel("▸")
        self.toggle_label.setStyleSheet(
            "color: #718096; font-size: 12px; background: transparent; border: none;")
        header.addWidget(self.toggle_label)

        icon_by_state = {"running": "⏳", "ok": "🛠", "fail": "🛠"}
        self.icon_label = QLabel(icon_by_state["running"])
        self.icon_label.setStyleSheet("background: transparent; border: none;")
        header.addWidget(self.icon_label)

        self.name_label = QLabel(name)
        self.name_label.setStyleSheet("""
            QLabel {
                color: #2d3748; font-size: 13px; font-weight: 600;
                font-family: 'Consolas', 'Courier New', monospace;
                background: transparent; border: none;
            }
        """)
        header.addWidget(self.name_label)

        self.args_preview = QLabel(self._short_args())
        self.args_preview.setStyleSheet(
            "color: #718096; font-size: 12px; background: transparent; border: none;"
            "font-family: 'Consolas', 'Courier New', monospace;")
        header.addWidget(self.args_preview, 1)

        self.status_label = QLabel("执行中…")
        self.status_label.setStyleSheet(
            "color: #b7791f; font-size: 12px; background: transparent; border: none;")
        header.addWidget(self.status_label)
        layout.addLayout(header)

        # 结果摘要行
        self.preview_label = QLabel("")
        self.preview_label.setVisible(False)
        self.preview_label.setWordWrap(True)
        self.preview_label.setStyleSheet("""
            QLabel {
                color: #4a5568; font-size: 12px;
                background: transparent; border: none;
                font-family: 'Consolas', 'Courier New', monospace; padding-left: 20px;
            }
        """)
        layout.addWidget(self.preview_label)

        # 展开区：完整参数 + 完整结果
        self.detail = QWidget()
        self.detail.setVisible(False)
        detail_layout = QVBoxLayout(self.detail)
        detail_layout.setContentsMargins(20, 4, 4, 4)
        detail_layout.setSpacing(6)

        args_browser = self._mono_browser()
        args_browser.setHtml(f"<pre style='margin:0;'>{self._escape(args_json)}</pre>")
        detail_layout.addWidget(args_browser)

        self.result_browser = self._mono_browser()
        self.result_browser.setHtml("<pre style='margin:0;'></pre>")
        detail_layout.addWidget(self.result_browser)
        layout.addWidget(self.detail)

        # 点击标题行切换展开
        self.setCursor(Qt.PointingHandCursor)

    # ----- 状态 -----

    def set_result(self, result_text: str, ok: bool):
        self.result_text = result_text
        self.ok = ok
        escaped = self._escape(result_text)
        self.result_browser.setHtml(f"<pre style='margin:0;'>{escaped}</pre>")
        if ok:
            self.icon_label.setText("🛠")
            self.status_label.setText("完成")
            self.status_label.setStyleSheet(
                "color: #38a169; font-size: 12px; background: transparent; border: none;")
            self.preview_label.setText(f"⎿ {self._first_line(result_text)}")
        else:
            self.icon_label.setText("⚠️")
            self.status_label.setText("失败")
            self.status_label.setStyleSheet(
                "color: #e53e3e; font-size: 12px; background: transparent; border: none;")
            self.preview_label.setText(f"⎿ {self._first_line(result_text)}")
        self.preview_label.setVisible(True)

    # ----- 内部 -----

    @staticmethod
    def _escape(text: str) -> str:
        return (text or "").replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    def _short_args(self) -> str:
        try:
            compact = re.sub(r"\s+", " ", self.args_json).strip("{} ")
        except Exception:
            compact = self.args_json
        compact = compact[:90] + ("…" if len(compact) > 90 else "")
        return f"({compact})"

    @staticmethod
    def _first_line(text: str) -> str:
        lines = (text or "").splitlines()
        if not lines:
            return "(空)"
        first = lines[0][:80]
        if len(lines) > 1:
            return f"{first} … +{len(lines) - 1} 行"
        if len(lines[0]) > 80:
            return first + "…"
        return first

    @staticmethod
    def _mono_browser() -> QTextBrowser:
        browser = QTextBrowser()
        browser.setReadOnly(True)
        browser.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        browser.setOpenExternalLinks(False)
        browser.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        browser.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        browser.setStyleSheet("""
            QTextBrowser {
                background: #ffffff; color: #1a202c;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 12px;
                padding: 8px;
            }
        """)
        browser.setFixedHeight(90)
        return browser

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.expanded = not self.expanded
            self.detail.setVisible(self.expanded)
            self.toggle_label.setText("▾" if self.expanded else "▸")
        super().mouseReleaseEvent(event)

    def select_all(self):
        for browser in (self.result_browser,):
            try:
                browser.selectAll()
            except RuntimeError:
                pass


# ==================== 思考过程卡片 ====================

class ThinkingWidget(QFrame):
    """思考过程卡片：默认折叠，标题行实时预览，点击展开完整内容"""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.thinking_text = ""
        self.expanded = False
        self.setObjectName("thinkingCard")
        self.setStyleSheet("""
            QFrame#thinkingCard {
                background: #fafbfe;
                border: 1px dashed #cbd5e0;
                border-radius: 10px;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.toggle_label = QLabel("▸")
        self.toggle_label.setStyleSheet(
            "color: #718096; font-size: 12px; background: transparent; border: none;")
        header.addWidget(self.toggle_label)

        self.title_label = QLabel("💭 思考过程")
        self.title_label.setStyleSheet("""
            QLabel {
                color: #805ad5; font-size: 13px; font-weight: 600;
                background: transparent; border: none;
            }
        """)
        header.addWidget(self.title_label)

        self.preview_label = QLabel("")
        self.preview_label.setWordWrap(True)
        self.preview_label.setStyleSheet(
            "color: #a0aec0; font-size: 12px; font-style: italic;"
            "background: transparent; border: none;")
        self.preview_label.setVisible(False)
        header.addWidget(self.preview_label, 1)
        layout.addLayout(header)

        self.browser = QTextBrowser()
        self.browser.setReadOnly(True)
        self.browser.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.browser.setOpenExternalLinks(False)
        self.browser.setVisible(False)
        self.browser.setStyleSheet("""
            QTextBrowser {
                background: #ffffff; color: #4a5568;
                border: 1px solid #e2e8f0; border-radius: 8px;
                font-size: 13px; padding: 8px;
            }
        """)
        self.browser.setFixedHeight(200)
        layout.addWidget(self.browser)

        if text:
            self.thinking_text = text
            self._refresh_preview()
            self.finish()

        self.setCursor(Qt.PointingHandCursor)

    # ----- 流式 API -----

    def append(self, delta: str):
        """追加思考增量；展开状态下实时滚动"""
        self.thinking_text += delta
        self.title_label.setText("💭 思考中…")
        self._refresh_preview()
        if self.expanded:
            self._update_browser()
            bar = self.browser.verticalScrollBar()
            bar.setValue(bar.maximum())

    def finish(self):
        """思考结束：标题恢复常驻文案"""
        self.title_label.setText("💭 思考过程")
        self._refresh_preview()

    def select_all(self):
        try:
            self.browser.selectAll()
        except RuntimeError:
            pass

    # ----- 内部 -----

    def _refresh_preview(self):
        compact = " ".join(self.thinking_text.split())
        if not compact:
            self.preview_label.setVisible(False)
            return
        tail = compact[-60:]
        self.preview_label.setText(("…" if len(compact) > 60 else "") + tail)
        self.preview_label.setVisible(True)

    def _update_browser(self):
        escaped = (self.thinking_text
                   .replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
        self.browser.setHtml(
            f"<pre style='margin:0; white-space: pre-wrap; "
            f"word-wrap: break-word;'>{escaped}</pre>")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.expanded = not self.expanded
            if self.expanded:
                self._update_browser()
                bar = self.browser.verticalScrollBar()
                bar.setValue(bar.maximum())
            self.browser.setVisible(self.expanded)
            self.toggle_label.setText("▾" if self.expanded else "▸")
        super().mouseReleaseEvent(event)


# ==================== 消息气泡 ====================

class MessageWidget(QFrame):
    """消息控件：支持流式文本、图片、代码块与工具调用的交错展示"""

    def __init__(self, role: str, content, image_data_list: List[str] = None, parent=None):
        super().__init__(parent)
        self.role = role
        self.raw_content = content
        self.image_data_list = image_data_list if image_data_list else []

        self.text_container = None
        self.text_layout = None
        self.outer_layout = None

        self._cached_text_browser: Optional[QTextBrowser] = None
        self._stream_text = ""
        self._sealed_texts: List[str] = []
        self._cached_code_blocks: List[CodeBlockWidget] = []
        self._all_text_browsers: List[QTextBrowser] = []
        self._tool_widgets = {}  # call_id -> ToolCallWidget
        self._thinking_widgets: List[ThinkingWidget] = []
        self._active_thinking: Optional[ThinkingWidget] = None

        self.parser = get_markdown_parser()
        self.setFocusPolicy(Qt.StrongFocus)
        self.setup_ui()

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(24, 8, 24, 8)
        main_layout.setSpacing(4)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)
        if self.role == "user":
            header_layout.addStretch()
            role_label = QLabel("👤 我")
            role_label.setStyleSheet(
                "color: #667eea; font-size: 12px; font-weight: 600; background: transparent;")
            header_layout.addWidget(role_label)
        else:
            role_label = QLabel("🤖 AI")
            role_label.setStyleSheet(
                "color: #48bb78; font-size: 12px; font-weight: 600; background: transparent;")
            header_layout.addWidget(role_label)
            header_layout.addStretch()

        self.copy_all_btn = QPushButton("📋 复制全部")
        self.copy_all_btn.setCursor(Qt.PointingHandCursor)
        self.copy_all_btn.setStyleSheet("""
            QPushButton {
                background: transparent; color: #718096;
                border: 1px solid #e2e8f0; border-radius: 12px;
                padding: 2px 10px; font-size: 11px;
            }
            QPushButton:hover {
                background: #edf2f7; color: #4a5568; border-color: #cbd5e0;
            }
        """)
        self.copy_all_btn.clicked.connect(self.copy_all_content)
        header_layout.addWidget(self.copy_all_btn)
        main_layout.addLayout(header_layout)

        self.text_container = QWidget()
        self.text_layout = QVBoxLayout(self.text_container)
        self.text_layout.setContentsMargins(0, 0, 0, 0)
        self.text_layout.setSpacing(10)

        content_h_layout = QHBoxLayout()
        content_h_layout.setContentsMargins(0, 0, 0, 0)
        content_h_layout.setSpacing(0)

        if self.role == "user":
            content_h_layout.addStretch()
            if self.image_data_list:
                self.add_multiple_image_widgets(self.text_layout)
            text = self._plain_text()
            if text:
                self.parse_content(self.text_layout, text, user=True)
            content_h_layout.addWidget(self.text_container)
        else:
            self._render_assistant_content(content_h_layout)

        main_layout.addLayout(content_h_layout)
        self.outer_layout = QHBoxLayout()

    # ----- 静态渲染 -----

    def _plain_text(self) -> str:
        """提取消息中的纯文本（content 可能是 str 或块列表）"""
        if self._sealed_texts:
            return "\n\n".join(t for t in self._sealed_texts if t.strip())
        if isinstance(self.raw_content, str):
            return self.raw_content
        parts = []
        for block in self.raw_content or []:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n\n".join(p for p in parts if p.strip())

    def _render_assistant_content(self, content_h_layout):
        """渲染 AI 消息：思考/文本段与工具调用卡片按顺序交错"""
        content = self.raw_content
        if isinstance(content, list):
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if text.strip():
                        self.parse_content(self.text_layout, text, user=False)
                elif btype == "thinking":
                    text = block.get("thinking", "")
                    if text.strip():
                        self._append_thinking_widget(ThinkingWidget(text))
                elif btype == "redacted_thinking":
                    self._append_thinking_widget(
                        ThinkingWidget("🔒 此思考内容已加密，无法展示。"))
                elif btype == "tool_use":
                    self._append_tool_widget(
                        block.get("id", ""), block.get("name", ""),
                        self._pretty_json(block.get("input") or {}))
                # 忽略 tool_result（由 ChatWindow 挂到对应卡片）
        elif content:
            self.parse_content(self.text_layout, content, user=False)
        content_h_layout.addWidget(self.text_container)
        content_h_layout.addStretch()

    @staticmethod
    def _pretty_json(obj) -> str:
        try:
            import json as _json
            return _json.dumps(obj, ensure_ascii=False, indent=2)
        except Exception:
            return str(obj)

    def _append_tool_widget(self, call_id: str, name: str, args_json: str) -> ToolCallWidget:
        widget = ToolCallWidget(call_id, name, args_json)
        self.text_layout.addWidget(widget)
        if call_id:
            self._tool_widgets[call_id] = widget
        return widget

    def _append_thinking_widget(self, widget: ThinkingWidget) -> ThinkingWidget:
        self.text_layout.addWidget(widget)
        self._thinking_widgets.append(widget)
        return widget

    # ----- 流式 API（ChatWindow 在 agent 回合中调用）-----

    def stream_thinking(self, delta: str):
        """追加一段思考过程增量（默认折叠的 💭 卡片，独立于正文）"""
        if self._active_thinking is None:
            self._active_thinking = self._append_thinking_widget(ThinkingWidget())
        self._active_thinking.append(delta)

    def _seal_thinking(self):
        """结束当前思考段（正文/工具调用开始时调用）"""
        if self._active_thinking is not None:
            self._active_thinking.finish()
            self._active_thinking = None

    def stream_append(self, delta: str):
        """追加一段流式文本（在当前打开的文本段中）"""
        self._seal_thinking()
        self._stream_text += delta
        if self._cached_text_browser is None:
            text_browser = QTextBrowser()
            text_browser.setReadOnly(True)
            text_browser.setTextInteractionFlags(
                Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
            text_browser.setOpenExternalLinks(False)
            text_browser.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            text_browser.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            text_browser.setStyleSheet("""
                QTextBrowser {
                    background: transparent; border: none;
                    font-size: 15px; padding: 4px 0;
                }
            """)
            text_browser.setMinimumHeight(30)
            self.text_layout.addWidget(text_browser)
            self._cached_text_browser = text_browser
        html_text = self.process_inline_code(self._stream_text.strip(), user=False)
        self._cached_text_browser.setHtml(html_text)
        try:
            doc = self._cached_text_browser.document()
            height = int(doc.size().height()) + 20
            self._cached_text_browser.setFixedHeight(max(height, 30))
        except RuntimeError:
            pass

    def seal_stream(self):
        """封存当前流式文本段：转为最终 Markdown 渲染"""
        self._seal_thinking()
        if self._cached_text_browser is None:
            return
        text = self._stream_text
        browser = self._cached_text_browser
        self._cached_text_browser = None
        self._stream_text = ""
        self._sealed_texts.append(text)
        try:
            self.text_layout.removeWidget(browser)
            browser.deleteLater()
        except RuntimeError:
            pass
        if text.strip():
            if self._has_unclosed_code_block(text):
                text += "\n```"
            self.parse_content(self.text_layout, text, user=False)

    def add_tool_call(self, call_id: str, name: str, args_json: str) -> ToolCallWidget:
        self._seal_thinking()
        return self._append_tool_widget(call_id, name, args_json)

    def set_tool_result(self, call_id: str, result_text: str, ok: bool):
        widget = self._tool_widgets.get(call_id)
        if widget:
            widget.set_result(result_text, ok)

    # ----- 图片 -----

    def add_multiple_image_widgets(self, layout: QVBoxLayout):
        if not self.image_data_list:
            return
        if len(self.image_data_list) == 1:
            self._add_single_image_widget(layout, self.image_data_list[0])
            return
        images_container = QWidget()
        images_h_layout = QHBoxLayout(images_container)
        images_h_layout.setContentsMargins(0, 0, 0, 0)
        images_h_layout.setSpacing(8)
        for image_data in self.image_data_list:
            image_label = self._create_image_label(image_data)
            if image_label:
                images_h_layout.addWidget(image_label)
        images_h_layout.addStretch()
        layout.addWidget(images_container)

    def _add_single_image_widget(self, layout: QVBoxLayout, image_data: str):
        image_label = self._create_image_label(image_data)
        if image_label:
            layout.addWidget(image_label)

    def _create_image_label(self, image_data: str) -> Optional[QLabel]:
        image_label = QLabel()
        image_label.setStyleSheet("""
            QLabel { background: #f7fafc; border-radius: 12px; padding: 8px; }
        """)
        try:
            image_bytes = base64.b64decode(image_data)
            image = QImage()
            image.loadFromData(image_bytes)
            if not image.isNull():
                pixmap = QPixmap.fromImage(image)
                max_size = 200 if len(self.image_data_list) > 1 else 300
                pixmap = pixmap.scaled(max_size, max_size, Qt.KeepAspectRatio,
                                       Qt.SmoothTransformation)
                image_label.setPixmap(pixmap)
                return image_label
        except Exception:
            error_label = QLabel("[图片加载失败]")
            error_label.setStyleSheet("color: #e53e3e; font-size: 14px;")
            return error_label
        return None

    # ----- 文本渲染 -----

    def parse_content(self, layout: QVBoxLayout, content: str, user: bool):
        """解析 Markdown 内容，代码块用 CodeBlockWidget，其余渲染为富文本"""
        if not content:
            return
        segments = self.parser.split_content(content)
        for segment in segments:
            if segment['type'] == 'code':
                code = segment['content']
                lang = segment.get('language', 'code')
                if code.strip():
                    code_block = CodeBlockWidget(code, lang)
                    layout.addWidget(code_block)
                    if not user:
                        self._cached_code_blocks.append(code_block)
            else:
                text = segment['content']
                if text.strip():
                    self._add_text_segment(layout, text, user)

    def _add_text_segment(self, layout: QVBoxLayout, text: str, user: bool):
        html_text = self.process_inline_code(text, user)
        text_browser = QTextBrowser()
        text_browser.setHtml(html_text)
        text_browser.setReadOnly(True)
        text_browser.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        text_browser.setOpenExternalLinks(False)
        text_browser.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        text_browser.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        text_browser.setStyleSheet("""
            QTextBrowser {
                background: transparent; border: none;
                font-size: 15px; padding: 4px 0;
            }
            QTextBrowser::selection { background: #667eea; color: white; }
        """)
        layout.addWidget(text_browser)
        self._all_text_browsers.append(text_browser)

        def update_height(tb):
            try:
                doc = tb.document()
                if doc:
                    height = int(doc.size().height()) + 20
                    tb.setFixedHeight(max(height, 20))
            except RuntimeError:
                pass

        text_browser.document().documentLayout().documentSizeChanged.connect(
            lambda size, tb=text_browser: update_height(tb)
        )
        QTimer.singleShot(50, lambda tb=text_browser: update_height(tb))

    @staticmethod
    def _has_unclosed_code_block(content: str) -> bool:
        return content.count('```') % 2 == 1

    def process_inline_code(self, text: str, user: bool) -> str:
        """处理行内代码和格式，支持表格渲染"""
        color = "#1e40af" if user else "#1a202c"

        table_pattern = r'^\|.+\|\s*\n\|[-\s|:]+\|\s*\n(\|.+\|\s*\n?)+'
        if re.search(table_pattern, text, re.MULTILINE):
            html = self.parser.parse_to_html(text)
            styled_html = self._add_table_styles(html, color)
            return f'<div style="line-height: 1.7; color: {color};">{styled_html}</div>'

        lines = text.strip().split('\n')
        table_lines = [line for line in lines if '|' in line and line.strip().startswith('|')]
        if len(table_lines) >= 2:
            html = self.parser.parse_to_html(text)
            if '<table' in html:
                styled_html = self._add_table_styles(html, color)
                return f'<div style="line-height: 1.7; color: {color};">{styled_html}</div>'

        text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        text = text.replace(
            '```',
            '<span style="background:#f1f5f9; color:#718096; padding:2px 6px; '
            'border-radius:4px; font-family:monospace; font-size:0.85em;">```</span>')
        text = re.sub(
            r'(?<!`)`(?!`)([^`]+)`(?!`)',
            r'<span style="background:#f1f5f9; color:#e53e3e; padding:2px 8px; '
            r'border-radius:12px; font-family:monospace; font-size:0.9em;">\1</span>',
            text)
        text = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', text)
        text = re.sub(r'\*([^*]+)\*', r'<i>\1</i>', text)
        text = text.replace('\n', '<br>')
        return f'<div style="line-height: 1.7; color: {color};">{text}</div>'

    def _add_table_styles(self, html: str, text_color: str) -> str:
        table_style = (
            'border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 14px; '
            'background: #fafafa; border-radius: 8px; overflow: hidden; '
            'box-shadow: 0 1px 3px rgba(0,0,0,0.1);'
        )
        th_style = (
            'background: #667eea; color: white; padding: 12px 16px; '
            'text-align: left; font-weight: 600; border-bottom: 2px solid #5a67d8;'
        )
        td_style = (
            f'color: {text_color}; padding: 10px 16px; border-bottom: 1px solid #e2e8f0;'
        )
        tr_style = 'background: #ffffff;'
        html = re.sub(r'<table>', f'<table style="{table_style}">', html)
        html = re.sub(r'<th>', f'<th style="{th_style}">', html)
        html = re.sub(r'<td>', f'<td style="{td_style}">', html)
        html = re.sub(r'<tr>', f'<tr style="{tr_style}">', html)
        return html

    # ----- 复制 / 全选 -----

    def get_all_text(self) -> str:
        return self._plain_text()

    def copy_all_content(self):
        clipboard = QApplication.clipboard()
        clipboard.setText(self.get_all_text())
        self.copy_all_btn.setText("✅ 已复制")
        QTimer.singleShot(1500, lambda: self.copy_all_btn.setText("📋 复制全部"))

    def select_all_text(self):
        for text_browser in self._all_text_browsers:
            try:
                text_browser.selectAll()
            except RuntimeError:
                pass
        for code_block in self._cached_code_blocks:
            try:
                code_block.code_display.selectAll()
            except (RuntimeError, AttributeError):
                pass
        for tool_widget in self._tool_widgets.values():
            tool_widget.select_all()
        for thinking_widget in self._thinking_widgets:
            thinking_widget.select_all()
        if self._cached_text_browser:
            try:
                self._cached_text_browser.selectAll()
            except RuntimeError:
                pass

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_A and event.modifiers() == Qt.ControlModifier:
            self.select_all_text()
            event.accept()
        else:
            super().keyPressEvent(event)
