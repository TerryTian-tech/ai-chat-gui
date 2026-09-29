"""离线端到端测试：本地 SSE 模拟服务器验证双格式 API 客户端与 Agent 循环。

运行：QT_QPA_PLATFORM=offscreen python tests/offline_test.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✓ {name}")
    else:
        FAIL.append(name)
        print(f"  ✗ {name}  {detail}")


# ==================== SSE 片段构造 ====================

def anth_events(blocks):
    """生成 Anthropic 流式事件序列（blocks: [('text', str) | ('tool_use', id, name, json_str)]）"""
    lines = [
        "event: message_start",
        'data: {"type":"message_start","message":{"id":"msg_t","role":"assistant","content":[]}}',
        "",
    ]
    idx = 0
    for block in blocks:
        if block[0] == "text":
            text = block[1]
            lines += ["event: content_block_start",
                      "data: " + json.dumps({"type": "content_block_start", "index": idx,
                                  "content_block": {"type": "text", "text": ""}}), ""]
            for i in range(0, len(text), 5):
                chunk = text[i:i + 5]
                lines += ["event: content_block_delta",
                          "data: " + json.dumps({"type": "content_block_delta", "index": idx,
                                      "delta": {"type": "text_delta", "text": chunk}}), ""]
            lines += ["event: content_block_stop",
                      "data: " + json.dumps({"type": "content_block_stop", "index": idx}), ""]
        else:
            _, tid, name, args_json = block
            lines += ["event: content_block_start",
                      "data: " + json.dumps({"type": "content_block_start", "index": idx,
                                  "content_block": {"type": "tool_use", "id": tid,
                                                    "name": name, "input": {}}}), ""]
            for i in range(0, len(args_json), 7):
                lines += ["event: content_block_delta",
                          "data: " + json.dumps({"type": "content_block_delta", "index": idx,
                                      "delta": {"type": "input_json_delta",
                                                "partial_json": args_json[i:i + 7]}}), ""]
            lines += ["event: content_block_stop",
                      "data: " + json.dumps({"type": "content_block_stop", "index": idx}), ""]
        idx += 1
    lines += ["event: message_delta",
              'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}', "",
              "event: message_stop", 'data: {"type":"message_stop"}', ""]
    return "\r\n".join(lines) + "\r\n"


def resp_events(items):
    """生成 Responses 流式事件序列（items: [('text', str) | ('call', call_id, name, args_json)]）"""
    lines = ['event: response.created',
             'data: {"type":"response.created","response":{"id":"resp_t"}}', ""]
    final_output = []
    for i, item in enumerate(items):
        if item[0] == "text":
            text = item[1]
            lines += ["event: response.output_item.added",
                      "data: " + json.dumps({"type": "response.output_item.added", "output_index": i,
                                  "item": {"type": "message", "id": f"msg_{i}", "role": "assistant",
                                           "content": []}}), ""]
            for j in range(0, len(text), 5):
                lines += ["event: response.output_text.delta",
                          "data: " + json.dumps({"type": "response.output_text.delta", "output_index": i,
                                      "content_index": 0, "delta": text[j:j + 5]}), ""]
            done_item = {"type": "message", "id": f"msg_{i}", "role": "assistant",
                         "content": [{"type": "output_text", "text": text}]}
            lines += ["event: response.output_item.done",
                      "data: " + json.dumps({"type": "response.output_item.done", "output_index": i,
                                  "item": done_item}), ""]
            final_output.append(done_item)
        else:
            _, cid, name, args = item
            lines += ["event: response.output_item.added",
                      "data: " + json.dumps({"type": "response.output_item.added", "output_index": i,
                                  "item": {"type": "function_call", "call_id": cid, "name": name,
                                           "arguments": ""}}), ""]
            for j in range(0, len(args), 7):
                lines += ["event: response.function_call_arguments.delta",
                          "data: " + json.dumps({"type": "response.function_call_arguments.delta",
                                      "output_index": i, "delta": args[j:j + 7]}), ""]
            done_item = {"type": "function_call", "call_id": cid, "name": name, "arguments": args}
            lines += ["event: response.output_item.done",
                      "data: " + json.dumps({"type": "response.output_item.done", "output_index": i,
                                  "item": done_item}), ""]
            final_output.append(done_item)
    lines += ["event: response.completed",
              "data: " + json.dumps({"type": "response.completed",
                          "response": {"id": "resp_t", "output": final_output}}), ""]
    return "\r\n".join(lines) + "\r\n"


# ==================== 模拟服务器 ====================

RECEIVED = []  # 每个请求的 {"path", "headers", "body"}


class FakeAPIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        RECEIVED.append({"path": self.path,
                         "headers": {k.lower(): v for k, v in self.headers.items()},
                         "body": body})

        if self.path.endswith("/messages"):
            sse = self._anthropic_sse(body)
        else:
            sse = self._responses_sse(body)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        payload = sse.encode("utf-8")
        for i in range(0, len(payload), 64):
            piece = payload[i:i + 64]
            self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            self.wfile.flush()
            time.sleep(0.001)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    @staticmethod
    def _has_tool_result(body):
        for msg in body.get("messages", []):
            content = msg.get("content")
            if isinstance(content, list) and any(
                    b.get("type") in ("tool_result", "function_call_output") for b in content):
                return True
        for item in body.get("input", []):
            if item.get("type") == "function_call_output":
                return True
        return False

    def _anthropic_sse(self, body):
        if self._has_tool_result(body):
            return anth_events([("text", "文件已写入，小狗汪汪！")])
        return anth_events([
            ("text", "我来创建文件。"),
            ("tool_use", "toolu_01", "write",
             json.dumps({"path": "hello.txt", "content": "小狗汪汪"})),
            ("tool_use", "toolu_02", "read", json.dumps({"path": "hello.txt"})),
        ])

    def _responses_sse(self, body):
        if self._has_tool_result(body):
            return resp_events([("text", "Done, woof!")])
        return resp_events([
            ("text", "Creating file now."),
            ("call", "call_01", "write",
             json.dumps({"path": "hello.txt", "content": "puppy woof"})),
            ("call", "call_02", "bash", json.dumps({"cmd": "echo hi-dog"})),
        ])


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ==================== 测试用例 ====================

def test_api_clients(base):
    print("\n[1] API 客户端直连（双格式流式 + 工具调用组装）")
    from aichat.api import AnthropicClient, ResponsesClient, ApiError

    # --- Anthropic ---
    texts = []
    client = AnthropicClient("sk-test", base, "test-model")
    result = client.stream(
        messages=[{"role": "user", "content": "请创建文件"}],
        system="你是助手",
        tools=[{"name": "write", "description": "d",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                                 "required": ["path"]}}],
        on_text=texts.append,
    )
    check("anthropic: 文本增量流式回调", "".join(texts) == "我来创建文件。", repr(texts))
    check("anthropic: 响应包含 3 个块", len(result["blocks"]) == 3,
          str([b.get("type") for b in result["blocks"]]))
    tu1, tu2 = result["blocks"][1], result["blocks"][2]
    check("anthropic: tool_use 参数 JSON 正确组装",
          tu1["id"] == "toolu_01" and tu1["name"] == "write"
          and tu1["input"] == {"path": "hello.txt", "content": "小狗汪汪"}, str(tu1))
    check("anthropic: 第二个 tool_use", tu2["name"] == "read" and tu2["input"] == {"path": "hello.txt"})

    req = RECEIVED[-1]
    check("anthropic: 请求头 x-api-key + anthropic-version",
          req["headers"].get("x-api-key") == "sk-test"
          and req["headers"].get("anthropic-version") == "2023-06-01")
    check("anthropic: system 字段 + tools 以 input_schema 形态发送",
          req["body"]["system"] == "你是助手"
          and req["body"]["tools"][0]["input_schema"]["required"] == ["path"])

    # --- Responses ---
    texts2 = []
    client2 = ResponsesClient("sk-test2", base, "test-model")
    internal_msgs = [
        {"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                          "data": "AAAA"}},
        ]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "好的"},
            {"type": "tool_use", "id": "call_x", "name": "write",
             "input": {"path": "a.txt", "content": "x"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_x", "content": "ok"},
        ]},
    ]
    from aichat.agent import make_tool_schema
    result2 = client2.stream(messages=internal_msgs, system="sys",
                             tools=make_tool_schema(), on_text=texts2.append)
    # 历史已含 tool_result → 服务器脚本返回最终文本
    check("responses: 文本增量流式回调", "".join(texts2) == "Done, woof!", repr(texts2))
    check("responses: 输出块为单个文本块",
          len(result2["blocks"]) == 1 and result2["blocks"][0]["text"] == "Done, woof!",
          str(result2["blocks"]))

    req2 = RECEIVED[-1]
    check("responses: 请求头 Bearer",
          req2["headers"].get("authorization") == "Bearer sk-test2")
    items = req2["body"]["input"]
    kinds = [i.get("type") for i in items]
    check("responses: 内部消息正确展开为 input items",
          "function_call" in kinds and "function_call_output" in kinds, str(kinds))
    fc = next(i for i in items if i.get("type") == "function_call")
    check("responses: 历史 tool_use → function_call(call_id/arguments)",
          fc["call_id"] == "call_x" and json.loads(fc["arguments"])["path"] == "a.txt")
    fco = next(i for i in items if i.get("type") == "function_call_output")
    check("responses: 历史 tool_result → function_call_output", fco["output"] == "ok")
    user_item = next(i for i in items if i.get("role") == "user" and isinstance(i.get("content"), list))
    img_part = next(p for p in user_item["content"] if p["type"] == "input_image")
    check("responses: 图片块 → input_image(data URL)",
          img_part["image_url"] == "data:image/png;base64,AAAA", str(img_part))
    check("responses: instructions + 工具以 parameters 形态发送",
          req2["body"]["instructions"] == "sys"
          and req2["body"]["tools"][0]["type"] == "function"
          and "parameters" in req2["body"]["tools"][0])

    # --- 错误处理 ---
    err_server = ThreadingHTTPServer(("127.0.0.1", 0), ErrHandler)
    threading.Thread(target=err_server.serve_forever, daemon=True).start()
    err_base = f"http://127.0.0.1:{err_server.server_address[1]}"
    try:
        AnthropicClient("k", err_base, "m").stream(
            [{"role": "user", "content": "hi"}], "s")
        check("anthropic: HTTP 错误转为 ApiError", False)
    except ApiError as e:
        check("anthropic: HTTP 错误转为 ApiError", "401" in str(e) and "bad key" in str(e), str(e))
    try:
        ResponsesClient("k", err_base, "m").stream(
            [{"role": "user", "content": "hi"}], "s")
        check("responses: HTTP 错误转为 ApiError", False)
    except ApiError as e:
        check("responses: HTTP 错误转为 ApiError", "429" in str(e), str(e))
    err_server.shutdown()


class ErrHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)
        if "messages" in self.path:
            code, msg = 401, "bad key"
        else:
            code, msg = 429, "rate limited"
        body = json.dumps({"error": {"message": msg}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_agent_worker(base, workdir):
    print("\n[2] AgentWorker 端到端（工具真实执行 + agentic 循环）")
    from aichat.agent import AgentWorker

    events = {"chunks": [], "tools": [], "results": [], "history": None, "done": False, "error": None}

    worker = AgentWorker(
        messages=[{"role": "user", "content": "创建 hello.txt 并读回来"}],
        fmt="anthropic", api_key="sk-test", base_url=base, model="test-model",
        agent_mode=True, workdir=workdir, confirm_tools=False,
    )
    worker.stream_chunk.connect(lambda c: events["chunks"].append(c))
    worker.tool_call_started.connect(lambda cid, n, a: events["tools"].append(n))
    worker.tool_call_finished.connect(lambda cid, r, ok: events["results"].append((r, ok)))
    worker.history_ready.connect(lambda h: events.update(history=h))
    worker.turn_finished.connect(lambda: events.update(done=True))
    worker.error_occurred.connect(lambda e: events.update(error=e))
    worker.run()  # 同步执行（测试内在当前线程跑完整个循环）

    check("agent: 无错误完成", events["error"] is None and events["done"], str(events["error"]))
    all_text = "".join(events["chunks"])
    check("agent: 两轮文本都流式到达",
          all_text.startswith("我来创建文件。") and "文件已写入" in all_text, repr(all_text))
    check("agent: 工具调用事件顺序正确",
          events["tools"] == ["write", "read"], str(events["tools"]))
    check("agent: 工具真实执行（文件已写入）",
          os.path.isfile(os.path.join(workdir, "hello.txt"))
          and open(os.path.join(workdir, "hello.txt"), encoding="utf-8").read() == "小狗汪汪")
    read_result = next(r for r, ok in events["results"] if "小狗汪汪" in r)
    check("agent: read 工具返回带行号内容", "1| 小狗汪汪" in read_result, read_result)

    history = events["history"]
    check("agent: 历史含 2 条 assistant + 1 条 tool_result user 消息",
          sum(1 for m in history if m["role"] == "assistant") == 2
          and sum(1 for m in history if m["role"] == "user") == 2
          and len(history) == 4, str(len(history or [])))
    tr_msg = [m for m in history if m["role"] == "user" and isinstance(m["content"], list)][-1]
    check("agent: tool_result 与 tool_use id 配对",
          {b["tool_use_id"] for b in tr_msg["content"]} == {"toolu_01", "toolu_02"})

    # Responses 格式的 agent 循环（bash 工具）
    events2 = {"chunks": [], "history": None, "error": None}
    workdir2 = tempfile.mkdtemp(prefix="aichat_t2_")
    worker2 = AgentWorker(
        messages=[{"role": "user", "content": "run echo"}],
        fmt="responses", api_key="sk", base_url=base, model="m",
        agent_mode=True, workdir=workdir2, confirm_tools=False,
    )
    worker2.stream_chunk.connect(events2["chunks"].append)
    worker2.history_ready.connect(lambda h: events2.update(history=h))
    worker2.error_occurred.connect(lambda e: events2.update(error=e))
    worker2.run()
    check("agent(responses): 无错误", events2["error"] is None, str(events2["error"]))
    check("agent(responses): bash 工具输出回传模型",
          events2["history"] is not None and any(
              "hi-dog" in str(b) for m in events2["history"] for b in
              (m["content"] if isinstance(m["content"], list) else [])))
    shutil.rmtree(workdir2, ignore_errors=True)

    # 无 agent 模式：不应带 tools 字段
    RECEIVED.clear()
    worker3 = AgentWorker(
        messages=[{"role": "user", "content": "hi"}],
        fmt="anthropic", api_key="k", base_url=base, model="m", agent_mode=False,
    )
    chunks3 = []
    worker3.stream_chunk.connect(chunks3.append)
    worker3.run()
    check("普通聊天: 请求不含 tools 字段", "tools" not in RECEIVED[0]["body"])
    check("普通聊天: 文本正常流式", "".join(chunks3).startswith("我来创建文件。"))


def test_tools(workdir):
    print("\n[3] 工具函数单元测试")
    from aichat import agent as A

    with open(os.path.join(workdir, "a.txt"), "w", encoding="utf-8") as f:
        f.write("line1\nline2\nline3\n")
    os.makedirs(os.path.join(workdir, "sub"), exist_ok=True)
    with open(os.path.join(workdir, "sub", "b.py"), "w", encoding="utf-8") as f:
        f.write("print('dog')\n")

    check("read: 带行号", "  1| line1" in A.run_tool("read", {"path": "a.txt"}, workdir))
    check("read: offset/limit", "  3| line3" in A.run_tool(
        "read", {"path": "a.txt", "offset": 2, "limit": 5}, workdir))
    check("read: 不存在的文件报错", A.run_tool(
        "read", {"path": "nope.txt"}, workdir).startswith("error:"))

    check("write: 创建含父目录的文件", A.run_tool(
        "write", {"path": "x/y.txt", "content": "hi"}, workdir) == "ok"
        and os.path.isfile(os.path.join(workdir, "x", "y.txt")))

    check("edit: 唯一匹配替换", A.run_tool(
        "edit", {"path": "a.txt", "old": "line2", "new": "LINE2"}, workdir) == "ok")
    check("edit: 多处匹配拒绝", A.run_tool(
        "edit", {"path": "a.txt", "old": "line", "new": "x"}, workdir).startswith("error:"))
    check("edit: all=true 全部替换", A.run_tool(
        "edit", {"path": "a.txt", "old": "line", "new": "LINE", "all": True}, workdir) == "ok")

    glob_out = A.run_tool("glob", {"pat": "**/*.txt"}, workdir)
    check("glob: 递归匹配", "a.txt" in glob_out and "x" + os.sep + "y.txt" in glob_out, glob_out)
    check("grep: 正则搜索", "b.py" in A.run_tool("grep", {"pat": "dog"}, workdir))
    check("grep: 非法正则报错", A.run_tool("grep", {"pat": "("}, workdir).startswith("error:"))

    if sys.platform == "win32":
        bash_out = A.run_tool("bash", {"cmd": "echo puppy"}, workdir)
    else:
        bash_out = A.run_tool("bash", {"cmd": "echo puppy"}, workdir)
    check("bash: 执行命令", "puppy" in bash_out, bash_out)
    check("bash: 在工作目录执行", "a.txt" in A.run_tool(
        "bash", {"cmd": "dir" if sys.platform == "win32" else "ls"}, workdir))
    check("工具schema: 6 个工具且为 Anthropic 形态",
          len(A.make_tool_schema()) == 6
          and all("input_schema" in t for t in A.make_tool_schema()))


def test_normalize():
    print("\n[4] 历史消息规范化（旧版 OpenAI 格式 → 内部格式）")
    from aichat.api import normalize_history_message

    legacy = {"role": "user", "content": [
        {"type": "text", "text": "看"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
    ]}
    m = normalize_history_message(legacy)
    check("旧图片块转换", m["content"][1] == {
        "type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}})
    check("纯文本消息保持不变", normalize_history_message(
        {"role": "assistant", "content": "hi"}) == {"role": "assistant", "content": "hi"})


def test_api_block_start_input():
    print("\n[5] content_block_start 自带完整 input 时不被覆盖")
    import urllib.request
    from aichat.api import AnthropicClient

    events = [
        "event: message_start",
        'data: {"type":"message_start","message":{"id":"m","role":"assistant","content":[]}}',
        "",
        "event: content_block_start",
        'data: {"type":"content_block_start","index":0,"content_block":'
        '{"type":"tool_use","id":"t1","name":"bash","input":{"cmd":"echo hi"}}}',
        "",
        "event: content_block_stop",
        'data: {"type":"content_block_stop","index":0}',
        "",
        "event: message_delta",
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}',
        "",
    ]

    class FakeResp:
        def __init__(self, lines):
            self._lines = [l.encode() for l in lines]
        def __iter__(self):
            return iter(self._lines)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=None: FakeResp(events)
    try:
        result = AnthropicClient("k", "http://x", "m").stream(
            [{"role": "user", "content": "hi"}], "s")
    finally:
        urllib.request.urlopen = orig
    tu = result["blocks"][0]
    check("tool input 保留（部分网关不增量发参）",
          tu.get("input") == {"cmd": "echo hi"}, str(tu))

    # 正常增量路径不受影响（有 input_json_delta 时按增量组装）
    events[4] = ('data: {"type":"content_block_start","index":0,'
                 '"content_block":{"type":"tool_use","id":"t1","name":"bash","input":{}}}')
    events[6:6] = ["event: content_block_delta",
                   'data: {"type":"content_block_delta","index":0,'
                   '"delta":{"type":"input_json_delta","partial_json":"{\\"cmd\\": \\"echo hi\\"}"}}',
                   ""]
    urllib.request.urlopen = lambda req, timeout=None: FakeResp(events)
    try:
        result2 = AnthropicClient("k", "http://x", "m").stream(
            [{"role": "user", "content": "hi"}], "s")
    finally:
        urllib.request.urlopen = orig
    check("input_json_delta 增量组装不受影响",
          result2["blocks"][0].get("input") == {"cmd": "echo hi"},
          str(result2["blocks"][0]))


def test_ui(workdir):
    print("\n[6] UI 离屏冒烟测试（主窗口 + 小狗 + 消息组件）")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])

    from aichat.window import ChatWindow
    import tempfile
    hist_dir = tempfile.mkdtemp(prefix="aichat_hist_")
    ChatWindow.HISTORY_DIR = hist_dir
    ChatWindow.HISTORY_FILE = os.path.join(hist_dir, "conversations.json")

    win = ChatWindow()
    win.resize(1200, 800)
    check("主窗口创建成功", win.current_conversation_id is not None)

    # 模拟一轮 agent 对话的历史渲染
    conv = win.conversations[win.current_conversation_id]
    conv["messages"] = [
        {"role": "user", "content": "创建文件"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "好的，**马上**创建。"},
            {"type": "tool_use", "id": "t1", "name": "write",
             "input": {"path": "hello.txt", "content": "小狗汪汪"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        ]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "完成！\n```python\nprint('hi')\n```"},
        ]},
    ]
    win.load_conversation_messages()
    win.grab().save(os.path.join(hist_dir, "render.png"))
    check("含工具调用历史渲染无崩溃", True)

    # 流式 API 冒烟
    w = win.add_message_widget("assistant", "")
    w.stream_append("正在")
    w.stream_append("思考…")
    w.seal_stream()
    tw = w.add_tool_call("t9", "bash", '{"cmd": "echo hi"}')
    w.set_tool_result("t9", "hi\n", True)
    check("MessageWidget 流式 + 工具卡片 API", tw.result_text == "hi\n")

    # 小狗各姿态渲染 + AI 状态联动
    win.puppy_widget._t = 1.2
    for pose in ("sit", "walk", "run", "drowsy", "sleep", "stretch",
                 "think", "work", "happy", "sad"):
        win.puppy_widget._enter_pose(pose, 5)
        win.puppy_widget.repaint()
    check("小狗十种姿态渲染无崩溃", True)
    pw = win.puppy_widget
    for state in ("thinking", "working", "happy", "sad", "idle"):
        pw.set_ai_state(state)
        pw.repaint()
    check("小狗 AI 状态联动（thinking/working/happy/sad/idle）",
          pw._ai_state == "idle" and pw._pose == "stretch", f"{pw._ai_state}/{pw._pose}")
    pw.set_ai_state("working")
    pw.set_run_flag("process", True)   # AI 忙碌时进程奔跑让位
    check("AI 忙碌时进程触发让位", pw._pose == "work", pw._pose)
    pw.set_ai_state("idle")
    check("AI 空闲后恢复进程奔跑", pw._pose == "run", pw._pose)
    pw.set_run_flag("process", False)
    check("进程结束回到平静", pw._pose == "stretch", pw._pose)

    # 保存/加载往返
    win.save_conversations(delay=False)
    win2 = ChatWindow()
    check("历史保存/加载往返", len(win2.conversations) >= 1
          and any("tool_use" in str(m) for c in win2.conversations.values()
                  for m in c["messages"]))
    for w in (win, win2):
        w.process_monitor.stop()
        w.process_monitor.wait(2000)
    shutil.rmtree(hist_dir, ignore_errors=True)


def main():
    server = start_server()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    workdir = tempfile.mkdtemp(prefix="aichat_w_")
    try:
        test_ui(workdir)          # 先建 QApplication，供后续 QThread 使用
        test_api_clients(base)
        test_api_block_start_input()
        test_agent_worker(base, workdir)
        test_tools(workdir)
        test_normalize()
    finally:
        server.shutdown()
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n{'=' * 46}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：", FAIL)
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
