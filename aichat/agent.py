"""Agent 能力层：工具集与 agentic 循环（参考 nanocode.py 的设计）。

- 工具：read / write / edit / glob / grep / bash，全部围绕一个工作目录执行
- AgentWorker 在后台线程内循环：请求模型 → 执行工具 → 回传结果 → 直到模型不再调用工具
- 危险工具（write/edit/bash）可通过确认机制交由用户批准
"""

import copy
import glob as globlib
import json
import os
import re
import subprocess
import threading

from PySide6.QtCore import QThread, Signal

from .api import make_client

MAX_ITERATIONS = 20          # 单轮用户消息最多循环次数，防止失控
MAX_TOOL_OUTPUT = 20000      # 单个工具结果截断长度（保护上下文）
BASH_TIMEOUT = 30            # bash 工具超时（秒）
CONFIRM_TIMEOUT = 300        # 等待用户确认工具执行的超时（秒）
CONFIRM_TOOLS = {"write", "edit", "bash"}  # 需要用户确认的工具

SYSTEM_PROMPT = """You are a very strong reasoner and planner. Use these critical instructions to structure your plans, thoughts, and responses.
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
        7.2.2) You may need to consult the user to even know whether an option is applicable. Do not assume it is not applicable without checking.
    7.3) Review applicable sources of information from #5 to confirm which are relevant to the current state.
8.Persistence and patience: Do not give up unless all reasoning above is exhausted.
    8.1) Don't be dissuaded by time taken or user frustration.
    8.2) This persistence must be intelligent: On transient errors (e.g. please try again), you must retry unless an explicit retry limit (e.g., max x tries) has been reached. If such a limit is hit, you must stop. On other errors, you must change your strategy or arguments, not repeat the same failed call.
9.Inhibit your response: only take an action after all the above reasoning is completed. Once you've taken an action, you cannot take it back.
"""

AGENT_SYSTEM_SUFFIX = """

## Tool use
You can call tools: read, write, edit, glob, grep, bash.
All tools operate relative to the working directory: {workdir} (absolute paths are also allowed).
When a task involves files, code, or commands, use tools to inspect and verify instead of guessing.
Read only the ranges you need, keep edits surgical, and verify results after changes.
Finish with a concise summary in the user's language; do not expose raw tool output unless asked.
"""


# ==================== 工具实现 ====================

def _resolve(workdir: str, path: str) -> str:
    """相对路径以工作目录为基准解析为绝对路径"""
    path = (path or "").strip()
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(workdir or ".", path))


def _truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    """超长工具结果截断，保留头尾"""
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.85)]
    tail = text[-int(limit * 0.10):]
    return f"{head}\n... (已截断，共 {len(text)} 字符) ...\n{tail}"


def _read_text_file(path: str):
    """多编码尝试读取文本文件，返回 (文本, 实际命中编码)；OSError 时返回 (错误消息, None)。

    newline='' 保留原始换行符；编码随文本一并返回：edit 写回时必须沿用，
    否则会把 GBK 等文件静默转成 UTF-8、把 LF/CRLF 统一成本机换行符。"""
    for enc in ("utf-8", "gbk", "gb18030", "big5", "latin-1"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                return f.read(), enc
        except UnicodeDecodeError:
            continue
        except OSError as e:
            return f"error: {e}", None
    with open(path, "r", encoding="utf-8", newline="", errors="ignore") as f:
        return f.read(), "utf-8"


def tool_read(args, workdir):
    """读文件（带行号，支持 offset/limit 分页）"""
    path = _resolve(workdir, args["path"])
    if not os.path.isfile(path):
        return f"error: file not found: {path}"
    text, _enc = _read_text_file(path)
    lines = text.splitlines(keepends=True)
    offset = max(int(args.get("offset", 0) or 0), 0)
    limit = args.get("limit")
    limit = int(limit) if limit is not None else len(lines)
    selected = lines[offset: offset + limit]
    return "".join(
        f"{offset + i + 1:4}| {line}" for i, line in enumerate(selected)
    ) or "(empty file)"


def tool_write(args, workdir):
    """整文件写入（覆盖）；newline='' 使模型给出的换行符原样落盘"""
    path = _resolve(workdir, args["path"])
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(args["content"])
    return "ok"


def tool_edit(args, workdir):
    """基于字符串替换的编辑（old 需唯一，除非 all=true）；写回沿用文件原编码与换行符"""
    path = _resolve(workdir, args["path"])
    text, enc = _read_text_file(path)
    if enc is None:
        return text  # "error: ..."（文件不可读）
    # CRLF 文件先规范化为 LF 参与匹配（模型给的 old/new 通常用 LF），写回时还原
    crlf = "\r\n" in text
    if crlf:
        text = text.replace("\r\n", "\n")
    old, new = args["old"], args["new"]
    if crlf:
        old = old.replace("\r\n", "\n")
        new = new.replace("\r\n", "\n")
    if old not in text:
        return "error: old_string not found"
    count = text.count(old)
    if not args.get("all") and count > 1:
        return f"error: old_string appears {count} times, must be unique (use all=true)"
    replacement = text.replace(old, new) if args.get("all") else text.replace(old, new, 1)
    if crlf:
        replacement = replacement.replace("\n", "\r\n")
    with open(path, "w", encoding=enc, newline="") as f:
        f.write(replacement)
    return "ok"


_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode"}


def tool_glob(args, workdir):
    """按通配符查找文件（按修改时间倒序）"""
    base = args.get("path") or "."
    pattern = _resolve(workdir, os.path.join(base, args["pat"]))
    files = [f for f in globlib.glob(pattern, recursive=True) if os.path.isfile(f)]
    # 排序期间文件可能被删除，取不到 mtime 的排最前
    files.sort(key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0, reverse=True)
    return "\n".join(files[:200]) or "none"


def tool_grep(args, workdir):
    """跨文件正则搜索（最多返回 50 条命中；多编码探测，与 read 策略一致）"""
    try:
        pattern = re.compile(args["pat"])
    except re.error as e:
        return f"error: invalid regex: {e}"
    root = _resolve(workdir, args.get("path") or ".")
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            try:
                if os.path.getsize(fpath) > 2_000_000:
                    continue
            except OSError:
                continue
            text, enc = _read_text_file(fpath)
            if enc is None or "\x00" in text:  # 不可读或疑似二进制文件
                continue
            for num, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{fpath}:{num}:{line.rstrip()}")
                    if len(hits) >= 50:
                        return "\n".join(hits)
    return "\n".join(hits) or "none"


def tool_bash(args, workdir):
    """执行 shell 命令（工作目录内，30 秒超时）"""
    try:
        proc = subprocess.run(
            args["cmd"],
            shell=True,
            cwd=workdir or None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=BASH_TIMEOUT,
        )
        output = proc.stdout or ""
        suffix = "" if proc.returncode == 0 else f"\n(exit code {proc.returncode})"
        return (output.strip() + suffix).strip() or "(empty)"
    except subprocess.TimeoutExpired:
        return f"error: timed out after {BASH_TIMEOUT}s"
    except OSError as e:
        return f"error: {e}"


# 工具注册表：名称 → (描述, 参数表, 实现函数)。"?" 后缀表示可选参数
_TOOLS = {
    "read": (
        "Read file content with line numbers (supports offset/limit pagination)",
        {"path": "string", "offset": "number?", "limit": "number?"},
        tool_read,
    ),
    "write": (
        "Write content to file (overwrites; parent dirs are created)",
        {"path": "string", "content": "string"},
        tool_write,
    ),
    "edit": (
        "Replace old with new in file (old must be unique unless all=true)",
        {"path": "string", "old": "string", "new": "string", "all": "boolean?"},
        tool_edit,
    ),
    "glob": (
        "Find files by wildcard pattern, sorted by mtime (newest first)",
        {"pat": "string", "path": "string?"},
        tool_glob,
    ),
    "grep": (
        "Search file contents for a regex pattern under a directory",
        {"pat": "string", "path": "string?"},
        tool_grep,
    ),
    "bash": (
        "Run a shell command in the working directory",
        {"cmd": "string"},
        tool_bash,
    ),
}


def make_tool_schema() -> list:
    """生成 Anthropic 形态的工具定义（Responses 形态由 api 层转换）"""
    schema = []
    for name, (description, params, _fn) in _TOOLS.items():
        properties, required = {}, []
        for pname, ptype in params.items():
            optional = ptype.endswith("?")
            base = ptype.rstrip("?")
            properties[pname] = {"type": "integer" if base == "number" else base}
            if not optional:
                required.append(pname)
        schema.append(
            {
                "name": name,
                "description": description,
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            }
        )
    return schema


def run_tool(name: str, args: dict, workdir: str) -> str:
    """执行工具，异常统一转为错误字符串返回给模型"""
    try:
        return _truncate(str(_TOOLS[name][2](args or {}, workdir)))
    except KeyError:
        return f"error: unknown tool: {name}"
    except Exception as err:
        return f"error: {err}"


class ToolApprover:
    """跨线程的工具执行批准器：工作线程阻塞等待，GUI 线程回填决定"""

    def __init__(self):
        self._event = threading.Event()
        self.approved = False

    def decide(self, approved: bool):
        self.approved = approved
        self._event.set()

    def wait(self, timeout: float = CONFIRM_TIMEOUT) -> bool:
        self._event.wait(timeout)
        return self.approved


class AgentWorker(QThread):
    """后台 agent 循环线程（也承担无工具的普通聊天请求）"""

    stream_chunk = Signal(str)            # 文本增量
    thinking_chunk = Signal(str)          # 思考过程增量（独立于正文）
    tool_call_started = Signal(str, str, str)   # call_id, name, args_json
    tool_call_finished = Signal(str, str, bool)  # call_id, result, ok
    confirm_requested = Signal(str, str, object)  # name, args_display, ToolApprover
    history_ready = Signal(object)        # 完整的内部格式消息列表
    turn_finished = Signal()
    error_occurred = Signal(str)

    def __init__(self, messages, fmt, api_key, base_url, model,
                 agent_mode=False, workdir=".", confirm_tools=True,
                 thinking_effort="off", system_prompt=SYSTEM_PROMPT):
        super().__init__()
        self.messages = copy.deepcopy(messages)
        self.fmt = fmt
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.agent_mode = agent_mode
        self.workdir = workdir
        self.confirm_tools = confirm_tools
        self.thinking_effort = thinking_effort
        self.system_prompt = system_prompt
        self._running = True
        self._pending_approver = None  # 正在等待用户确认的工具批准器

    def stop(self):
        self._running = False
        # 若工作线程正卡在工具确认等待，主动按拒绝唤醒，避免悬挂到 300s 超时
        approver = self._pending_approver
        if approver is not None:
            approver.decide(False)

    def run(self):
        try:
            self._run_loop()
        except Exception as e:
            if self._running:
                # 中途失败也要把已完成轮次回写历史：界面上已显示的工具调用
                # 不至于在重新加载会话后凭空消失
                self.history_ready.emit(self._completed_history())
                self.error_occurred.emit(str(e))

    def _completed_history(self):
        """失败时的安全历史：尾部 assistant 的 tool_use 若无 tool_result 跟随
        （失败发生在工具结果回传前），剥离工具块，避免下次请求被 API 拒绝"""
        msgs = self.messages
        if msgs and msgs[-1].get("role") == "assistant":
            content = msgs[-1].get("content")
            if isinstance(content, list) and any(
                    b.get("type") == "tool_use" for b in content):
                kept = [b for b in content if b.get("type") != "tool_use"]
                if kept:
                    return msgs[:-1] + [{"role": "assistant", "content": kept}]
                return msgs[:-1]
        return msgs

    def _run_loop(self):
        client = make_client(self.fmt, self.api_key, self.base_url, self.model,
                             self.thinking_effort)
        system = self.system_prompt
        tools = make_tool_schema() if self.agent_mode else None
        if self.agent_mode:
            system += AGENT_SYSTEM_SUFFIX.format(workdir=os.path.abspath(self.workdir))

        for _ in range(MAX_ITERATIONS):
            if not self._running:
                return

            result = client.stream(
                self.messages,
                system,
                tools=tools,
                on_text=lambda d: self.stream_chunk.emit(d),
                on_thinking=lambda d: self.thinking_chunk.emit(d),
                is_cancelled=lambda: not self._running,
            )
            # 保留思考块：Anthropic 在思考 + 工具调用时要求下一轮原样回传
            # thinking 块（含 signature），剥掉会直接 400
            blocks = [b for b in result["blocks"]
                      if b.get("type") in ("text", "tool_use",
                                           "thinking", "redacted_thinking")]
            # 思考块必须排在 assistant content 数组最前
            blocks.sort(key=lambda b: 0 if b.get("type") in ("thinking",
                                                             "redacted_thinking") else 1)
            self.messages.append({"role": "assistant", "content": blocks})

            tool_uses = [b for b in blocks if b["type"] == "tool_use"]
            if not tool_uses or not self.agent_mode:
                break
            if not self._running:
                return

            results = []
            for tu in tool_uses:
                if not self._running:
                    return
                args_json = json.dumps(tu.get("input") or {}, ensure_ascii=False, indent=2)
                self.tool_call_started.emit(tu["id"], tu["name"], args_json)

                if self.confirm_tools and tu["name"] in CONFIRM_TOOLS:
                    approver = ToolApprover()
                    # 先登记再发信号：stop() 在信号被 GUI 处理前后都能唤醒
                    self._pending_approver = approver
                    self.confirm_requested.emit(tu["name"], args_json, approver)
                    approved = approver.wait()
                    self._pending_approver = None
                    if not approved:
                        output = "error: user declined this tool call"
                        self.tool_call_finished.emit(tu["id"], output, False)
                        results.append(
                            {"type": "tool_result", "tool_use_id": tu["id"], "content": output}
                        )
                        continue

                output = run_tool(tu["name"], tu.get("input") or {}, self.workdir)
                ok = not output.startswith("error:")
                self.tool_call_finished.emit(tu["id"], output, ok)
                results.append(
                    {"type": "tool_result", "tool_use_id": tu["id"], "content": output}
                )

            self.messages.append({"role": "user", "content": results})
            # 每完成一轮工具回传就回写一次历史（快照）：中途失败/取消时已完成轮次不丢失
            self.history_ready.emit(list(self.messages))

        if not self._running:
            return
        self.history_ready.emit(self.messages)
        self.turn_finished.emit()
