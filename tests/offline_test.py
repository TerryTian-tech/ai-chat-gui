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
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 中文 Windows 上输出重定向到管道时 stdout 可能是 GBK，✓/✗ 符号会炸编码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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
    """生成 Anthropic 流式事件序列（blocks: [('thinking', str, sig) | ('text', str)
    | ('tool_use', id, name, json_str)]）"""
    lines = [
        "event: message_start",
        'data: {"type":"message_start","message":{"id":"msg_t","role":"assistant","content":[]}}',
        "",
    ]
    idx = 0
    for block in blocks:
        if block[0] == "thinking":
            _, think_text, sig = block
            lines += ["event: content_block_start",
                      "data: " + json.dumps({"type": "content_block_start", "index": idx,
                                  "content_block": {"type": "thinking", "thinking": ""}}), ""]
            for i in range(0, len(think_text), 5):
                lines += ["event: content_block_delta",
                          "data: " + json.dumps({"type": "content_block_delta", "index": idx,
                                      "delta": {"type": "thinking_delta",
                                                "thinking": think_text[i:i + 5]}}), ""]
            if sig:
                lines += ["event: content_block_delta",
                          "data: " + json.dumps({"type": "content_block_delta", "index": idx,
                                      "delta": {"type": "signature_delta", "signature": sig}}), ""]
            lines += ["event: content_block_stop",
                      "data: " + json.dumps({"type": "content_block_stop", "index": idx}), ""]
        elif block[0] == "text":
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
    """生成 Responses 流式事件序列（items: [('reasoning', str) | ('text', str)
    | ('call', call_id, name, args_json)]）"""
    lines = ['event: response.created',
             'data: {"type":"response.created","response":{"id":"resp_t"}}', ""]
    final_output = []
    for i, item in enumerate(items):
        if item[0] == "reasoning":
            _, rtext = item
            lines += ["event: response.output_item.added",
                      "data: " + json.dumps({"type": "response.output_item.added", "output_index": i,
                                  "item": {"type": "reasoning", "id": f"rs_{i}", "summary": []}}), ""]
            for j in range(0, len(rtext), 5):
                lines += ["event: response.reasoning_summary_text.delta",
                          "data: " + json.dumps({"type": "response.reasoning_summary_text.delta",
                                      "output_index": i, "summary_index": 0,
                                      "delta": rtext[j:j + 5]}), ""]
            done_item = {"type": "reasoning", "id": f"rs_{i}",
                         "summary": [{"type": "summary_text", "text": rtext}]}
            lines += ["event: response.output_item.done",
                      "data: " + json.dumps({"type": "response.output_item.done", "output_index": i,
                                  "item": done_item}), ""]
            final_output.append(done_item)
        elif item[0] == "text":
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
FLAGS = {"anth_thinking": False, "resp_reasoning": False}  # 思考脚本开关


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
        blocks = []
        if FLAGS["anth_thinking"]:
            blocks.append(("thinking", "我先想一下……", "sigSIGsig"))
        if self._has_tool_result(body):
            blocks.append(("text", "文件已写入，小狗汪汪！"))
            return anth_events(blocks)
        blocks += [
            ("text", "我来创建文件。"),
            ("tool_use", "toolu_01", "write",
             json.dumps({"path": "hello.txt", "content": "小狗汪汪"})),
            ("tool_use", "toolu_02", "read", json.dumps({"path": "hello.txt"})),
        ]
        return anth_events(blocks)

    def _responses_sse(self, body):
        items = []
        if FLAGS["resp_reasoning"]:
            items.append(("reasoning", "先想想再答。"))
        if self._has_tool_result(body):
            items.append(("text", "Done, woof!"))
            return resp_events(items)
        items += [
            ("text", "Creating file now."),
            ("call", "call_01", "write",
             json.dumps({"path": "hello.txt", "content": "puppy woof"})),
            ("call", "call_02", "bash", json.dumps({"cmd": "echo hi-dog"})),
        ]
        return resp_events(items)


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ==================== 测试用例 ====================

def test_stop_generation():
    print("\n[3.11] 停止生成（取消后 worker 不得在运行中被销毁）")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from aichat.window import ChatWindow

    class SlowHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            if length:
                self.rfile.read(length)
            time.sleep(1.0)  # 让 worker 停留在阻塞的请求中
            sse = anth_events([("text", "慢慢回复")])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            data = sse.encode("utf-8")
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    hist_dir = tempfile.mkdtemp(prefix="aichat_hist2_")
    ChatWindow.HISTORY_DIR = hist_dir
    ChatWindow.HISTORY_FILE = os.path.join(hist_dir, "conversations.json")
    win = ChatWindow()
    try:
        win.api_format, win.api_key = "anthropic", "sk-test"
        win.base_url, win.model = f"http://127.0.0.1:{server.server_address[1]}", "m"
        win.thinking_effort = "off"
        win.input_edit.setPlainText("你好")
        win.send_message()
        check("停止: 请求进行中按钮变为停止态",
              win._request_active and win.api_worker is not None
              and win.send_btn.text() == "⏹ 停止", win.send_btn.text())
        time.sleep(0.2)  # 此刻 worker 正阻塞在服务器的 1s 延迟上
        win._stop_generation()
        check("停止: 点击后恢复发送态",
              win.api_worker is None and not win._request_active
              and win.send_btn.text() == "发送", win.send_btn.text())
        check("停止: worker 移入回收列表而非被直接销毁",
              len(win._retiring_workers) == 1, str(len(win._retiring_workers)))
        worker = win._retiring_workers[0]
        ok = worker.wait(8000)
        for _ in range(100):
            app.processEvents()
            if not win._retiring_workers:
                break
            time.sleep(0.05)
        check("停止: worker 自然结束后回收、进程存活",
              ok and not win._retiring_workers, f"wait={ok}")
    finally:
        win.process_monitor.stop()
        win.process_monitor.wait(2000)
        server.shutdown()
        shutil.rmtree(hist_dir, ignore_errors=True)


def test_empty_blocks_guard():
    print("\n[3.12] 空响应不写入空 content（防下一轮 400）")
    import urllib.request
    from aichat.agent import AgentWorker

    events = [
        "event: message_start",
        'data: {"type":"message_start","message":{"id":"m","role":"assistant","content":[]}}',
        "",
        "event: content_block_start",
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        "",
        "event: content_block_delta",
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"   "}}',
        "",
        "event: content_block_stop",
        'data: {"type":"content_block_stop","index":0}',
        "",
        "event: message_delta",
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
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
        worker = AgentWorker(messages=[{"role": "user", "content": "hi"}],
                             fmt="anthropic", api_key="k", base_url="http://x", model="m")
        history = []
        worker.history_ready.connect(lambda h: history.append(h))
        worker.run()
    finally:
        urllib.request.urlopen = orig
    h = history[0]
    check("空响应: 历史不含空 assistant 消息",
          len(h) == 1 and h[0]["role"] == "user", str(h))


def test_sse_flush():
    print("\n[3.13] SSE 末尾无空行结尾时不丢事件")
    import urllib.request
    from aichat.api import AnthropicClient

    events = [
        "event: message_start",
        'data: {"type":"message_start","message":{"id":"m","role":"assistant","content":[]}}',
        "",
        "event: content_block_start",
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        "",
        "event: content_block_delta",
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"结尾"}}',
        "",
        "event: content_block_stop",
        'data: {"type":"content_block_stop","index":0}',
        "",
        "event: message_delta",
        # 以下 data 行之后没有空行，流直接结束——此前该事件会被丢弃
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
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
    check("SSE: 无空行结尾的末尾事件被保留",
          result["blocks"][0]["text"] == "结尾" and result["stop_reason"] == "end_turn",
          str(result))


def test_interruptible_backoff():
    print("\n[3.14] 退避等待可被取消打断（Retry-After: 30 不再卡死）")
    import threading as _threading
    import time as _time
    from aichat.api import AnthropicClient, ApiError

    class Retry30Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        calls = []

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            if length:
                self.rfile.read(length)
            type(self).calls.append(self.path)
            if len(type(self).calls) == 1:
                payload = b'{"error":{"message":"rate limited"}}'
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", "30")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            sse = anth_events([("text", "ok")])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            data = sse.encode("utf-8")
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Retry30Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        cancel_event = _threading.Event()
        outcome = {}

        def run():
            try:
                AnthropicClient("k", f"http://127.0.0.1:{server.server_address[1]}", "m").stream(
                    [{"role": "user", "content": "hi"}], "s",
                    is_cancelled=cancel_event.is_set)
                outcome["ok"] = True
            except ApiError as e:
                outcome["err"] = str(e)

        t0 = _time.time()
        thread = _threading.Thread(target=run, daemon=True)
        thread.start()
        _time.sleep(0.5)  # 此刻 worker 正处于 30s 的退避等待中
        cancel_event.set()
        thread.join(5)
        elapsed = _time.time() - t0
        check("退避: 取消后快速退出（不再等满 30s）",
              not thread.is_alive() and elapsed < 5 and "取消" in outcome.get("err", ""),
              f"elapsed={elapsed:.1f}s err={outcome.get('err')}")
    finally:
        server.shutdown()


def test_interrupted_half_round():
    print("\n[3.15] 停止生成时半轮内容落盘")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from aichat.window import ChatWindow

    class PartialStreamHandler(BaseHTTPRequestHandler):
        """先吐一段文本增量，停顿 1.5s 再发剩余——模拟"停止时正在流式输出\""""
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            if length:
                self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            part1 = "\r\n".join([
                "event: message_start",
                'data: {"type":"message_start","message":{"id":"m","role":"assistant","content":[]}}',
                "",
                "event: content_block_start",
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                "",
                "event: content_block_delta",
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"半轮内容"}}',
                "",
            ]) + "\r\n"
            piece = part1.encode("utf-8")
            self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            self.wfile.flush()
            time.sleep(1.5)
            part2 = "\r\n".join([
                "event: content_block_stop",
                'data: {"type":"content_block_stop","index":0}',
                "",
                "event: message_delta",
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
                "",
                "event: message_stop",
                'data: {"type":"message_stop"}',
                "",
            ])
            piece = part2.encode("utf-8")
            self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), PartialStreamHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    hist_dir = tempfile.mkdtemp(prefix="aichat_hist3_")
    ChatWindow.HISTORY_DIR = hist_dir
    ChatWindow.HISTORY_FILE = os.path.join(hist_dir, "conversations.json")
    win = ChatWindow()
    try:
        win.api_format, win.api_key = "anthropic", "sk-test"
        win.base_url, win.model = f"http://127.0.0.1:{server.server_address[1]}", "m"
        win.thinking_effort = "off"
        win.input_edit.setPlainText("讲个故事")
        win.send_message()
        time.sleep(0.5)  # part1 已被消费：气泡正在显示"半轮内容"
        win._stop_generation()
        worker = win._retiring_workers[0]
        check("停止: worker 线程自然结束", worker.wait(8000))
        for _ in range(100):
            app.processEvents()
            if not win._retiring_workers:
                break
            time.sleep(0.05)
        msgs = win.conversations[win.current_conversation_id]['messages']
        partial = (len(msgs) == 2 and isinstance(msgs[-1].get("content"), list)
                   and any(b.get("type") == "text" and "半轮内容" in b.get("text", "")
                           for b in msgs[-1]["content"]))
        check("停止: 半轮正文已落盘到会话历史", partial, str(msgs)[:200])
    finally:
        win.process_monitor.stop()
        win.process_monitor.wait(2000)
        server.shutdown()
        shutil.rmtree(hist_dir, ignore_errors=True)


def test_output_cap_cache():
    print("\n[3.17] 输出上限按模型缓存（不再每次白挨 400）")
    from aichat.api import _OUTPUT_CAPS, AnthropicClient
    _OUTPUT_CAPS.clear()

    class CapHandler(BaseHTTPRequestHandler):
        """max_tokens 超过 64000 就拒绝（模拟小输出上限模型）"""
        protocol_version = "HTTP/1.1"
        calls = []

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            type(self).calls.append((self.path, body))
            if body.get("max_tokens", 0) > 64000:
                payload = json.dumps({"error": {"message":
                    "max_tokens: 393216 > 64000, which is the maximum allowed "
                    "number of output tokens"}}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            sse = anth_events([("text", "ok")])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            data = sse.encode("utf-8")
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), CapHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        CapHandler.calls.clear()
        AnthropicClient("k", base, "model-a", "off").stream(
            [{"role": "user", "content": "hi"}], "s")
        AnthropicClient("k", base, "model-a", "off").stream(
            [{"role": "user", "content": "hi"}], "s")
        caps = [b.get("max_tokens") for _, b in CapHandler.calls]
        check("cap 缓存: 第二次请求直接带上收紧值（3 次请求而非 4 次）",
              len(CapHandler.calls) == 3 and caps == [393216, 64000, 64000], str(caps))
        check("cap 缓存: 按 (base_url, model) 记录",
              _OUTPUT_CAPS.get((base, "model-a")) == 64000)
        CapHandler.calls.clear()
        AnthropicClient("k", base, "model-b", "off").stream(
            [{"role": "user", "content": "hi"}], "s")
        caps = [b.get("max_tokens") for _, b in CapHandler.calls]
        check("cap 缓存: 换模型重新探测", len(caps) == 2 and caps == [393216, 64000], str(caps))
    finally:
        server.shutdown()
        _OUTPUT_CAPS.clear()


def test_parse_output_cap_patterns():
    print("\n[3.18] 输出上限报错解析（措辞覆盖 + 防误判）")
    from aichat.api import ApiError, _parse_output_cap
    sent = 384 * 1024
    cases = [
        ("max_tokens: 393216 > 64000, which is the maximum allowed number of output tokens",
         64000),
        ("max_tokens is too large: 393216. This model supports at most 4096", 4096),
        ("max_tokens must be less than or equal to 4096", 4096),
        ("Maximum allowed output tokens is 8192", 8192),
        # 上下文长度不是输出上限，绝不能误判
        ("This model's maximum context length is 128000 tokens, max_tokens too large", None),
        ("max_tokens must be less than or equal to 1", None),  # 数值不可信（< 1024）
        ("rate limited", None),                                # 与输出无关
    ]
    for msg, want in cases:
        got = _parse_output_cap(ApiError(msg), sent)
        check(f"解析: {msg[:38]}… → {want}", got == want, f"got {got}")


def test_prompt_too_long_halving():
    print("\n[3.19] prompt 超长自动减半上下文窗口重试")
    from aichat.api import AnthropicClient

    class PromptTooLongHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        calls = []

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            type(self).calls.append((self.path, body))
            if len(type(self).calls) == 1:
                payload = json.dumps({"error": {"message":
                    "prompt is too long: 3000 tokens > 2000 maximum"}}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            sse = anth_events([("text", "窗口收敛")])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            data = sse.encode("utf-8")
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), PromptTooLongHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        PromptTooLongHandler.calls.clear()
        msgs = []
        for i in range(6):
            msgs.append({"role": "user", "content": f"问题{i}" + "字" * 190})
            msgs.append({"role": "assistant", "content": f"回答{i}" + "字" * 190})
        client = AnthropicClient("k", f"http://127.0.0.1:{server.server_address[1]}",
                                 "m", "off", context_chars=2000)
        texts = []
        client.stream(msgs, "s", on_text=texts.append)
        bodies = [b for _, b in PromptTooLongHandler.calls]
        check("prompt 超长: 首请求按 2000 字符预算裁剪",
              len(bodies[0]["messages"]) < len(msgs),
              f"{len(bodies[0]['messages'])}/{len(msgs)}")
        check("prompt 超长: 减半后窗口更小且请求成功",
              len(bodies[1]["messages"]) < len(bodies[0]["messages"])
              and "".join(texts) == "窗口收敛",
              f"{len(bodies[0]['messages'])} → {len(bodies[1]['messages'])}")
    finally:
        server.shutdown()


def test_cancel_writeback_race():
    print("\n[3.20] 取消回写不覆盖新请求的历史")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from aichat.window import ChatWindow

    class SlowFirstHandler(BaseHTTPRequestHandler):
        """第 1 个 POST：先吐半轮文本再停顿 2.5s；之后的 POST：立即完整回复"""
        protocol_version = "HTTP/1.1"
        calls = []

        def log_message(self, *args):
            pass

        def _write_chunk(self, text):
            piece = text.encode("utf-8")
            self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            self.wfile.flush()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            if length:
                self.rfile.read(length)
            type(self).calls.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if len(type(self).calls) == 1:
                part1 = "\r\n".join([
                    "event: message_start",
                    'data: {"type":"message_start","message":{"id":"m","role":"assistant","content":[]}}',
                    "",
                    "event: content_block_start",
                    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                    "",
                    "event: content_block_delta",
                    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"第一条的一半"}}',
                    "",
                ]) + "\r\n"
                self._write_chunk(part1)
                time.sleep(2.5)
                part2 = "\r\n".join([
                    "event: content_block_stop",
                    'data: {"type":"content_block_stop","index":0}',
                    "",
                    "event: message_delta",
                    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
                    "",
                    "event: message_stop",
                    'data: {"type":"message_stop"}',
                    "",
                ])
                self._write_chunk(part2)
            else:
                self._write_chunk(anth_events([("text", "第二条的回答")]))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowFirstHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    SlowFirstHandler.calls.clear()

    hist_dir = tempfile.mkdtemp(prefix="aichat_hist4_")
    ChatWindow.HISTORY_DIR = hist_dir
    ChatWindow.HISTORY_FILE = os.path.join(hist_dir, "conversations.json")
    win = ChatWindow()
    try:
        win.api_format, win.api_key = "anthropic", "sk-test"
        win.base_url, win.model = f"http://127.0.0.1:{server.server_address[1]}", "m"
        win.thinking_effort = "off"
        cid = win.current_conversation_id

        win.input_edit.setPlainText("第一条")
        win.send_message()
        time.sleep(0.4)          # 半轮已流式显示
        win._stop_generation()   # worker1 取消（其回写将在 ~2.5s 后晚到）
        win.input_edit.setPlainText("第二条")
        win.send_message()       # 立即发起新请求

        deadline = time.time() + 8
        while time.time() < deadline:
            app.processEvents()
            if len(SlowFirstHandler.calls) >= 2 and not win._retiring_workers:
                break
            time.sleep(0.05)
        time.sleep(3.0)  # 确保 worker1 的晚到回写已经发生并被拒收
        for _ in range(30):
            app.processEvents()
            time.sleep(0.05)
        result = win.conversations[cid]['messages']
        check("竞态: 新请求的历史不被旧回写覆盖",
              len(result) == 3
              and result[0]["content"] == "第一条"
              and result[1]["content"] == "第二条"
              and isinstance(result[2].get("content"), list)
              and any(b.get("type") == "text" and "第二条的回答" in b.get("text", "")
                      for b in result[2]["content"]),
              str(result)[:300])
    finally:
        win.process_monitor.stop()
        win.process_monitor.wait(2000)
        server.shutdown()
        shutil.rmtree(hist_dir, ignore_errors=True)


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


class FailFirstHandler(BaseHTTPRequestHandler):
    """前 fail_count 次请求返回 error_code（消息可配置），之后返回正常 SSE。

    用于验证：思考参数被拒时自动降级重试；max_tokens 超限时按服务端上报的
    上限收紧重试；429/5xx 连接阶段指数退避重试。
    """
    protocol_version = "HTTP/1.1"
    calls = []  # [(path, body)]
    error_code = 400
    error_messages = []  # 按失败次序取用的消息，超出后回落到 error_message
    error_message = "thinking is not supported by this model"
    fail_count = 1

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).calls.append((self.path, body))
        if len(type(self).calls) <= type(self).fail_count:
            idx = len(type(self).calls) - 1
            msgs = type(self).error_messages
            msg = msgs[idx] if idx < len(msgs) else type(self).error_message
            payload = json.dumps({"error": {"message": msg}}).encode()
            self.send_response(type(self).error_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.endswith("/messages"):
            sse = anth_events([("text", "降级成功")])
        else:
            sse = resp_events([("text", "degraded ok")])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        data = sse.encode("utf-8")
        for i in range(0, len(data), 64):
            piece = data[i:i + 64]
            self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")


class FailNthHandler(BaseHTTPRequestHandler):
    """序号在 fail_on 内的请求返回 500，其余返回正常 Anthropic SSE。

    用于验证：agent 循环中途失败时，已完成轮次的历史仍会回写。
    """
    protocol_version = "HTTP/1.1"
    calls = []
    fail_on = {2, 3, 4, 5, 6, 7, 8}  # 默认第 2 个请求起持续失败（含退避重试）

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).calls.append((self.path, body))
        if len(type(self).calls) in type(self).fail_on:
            payload = json.dumps({"error": {"message": "server exploded"}}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if FakeAPIHandler._has_tool_result(body):
            sse = anth_events([("text", "文件已写入")])
        else:
            sse = anth_events([
                ("text", "我来创建文件。"),
                ("tool_use", "toolu_01", "write",
                 json.dumps({"path": "err_history.txt", "content": "hi"})),
            ])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        data = sse.encode("utf-8")
        for i in range(0, len(data), 64):
            piece = data[i:i + 64]
            self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")


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


def test_thinking_support(base):
    print("\n[3] 思考强度端到端（payload 注入 + 思考流解析 + 历史回传）")
    from aichat.agent import AgentWorker
    from aichat.api import (AnthropicClient, ResponsesClient,
                            message_to_responses_items)

    # --- Anthropic：开启 → payload 注入 thinking + max_tokens 抬高 ---
    from aichat.api import MAX_TOKENS
    AnthropicClient("sk-test", base, "test-model", "medium").stream(
        [{"role": "user", "content": "hi"}], "s")
    req = RECEIVED[-1]
    check("anthropic: thinking 参数按档位注入",
          req["body"]["thinking"] == {"type": "enabled", "budget_tokens": 8192},
          str(req["body"].get("thinking")))
    check("anthropic: max_tokens 取默认上限与预算余量的较大者",
          req["body"]["max_tokens"] == MAX_TOKENS, str(req["body"]["max_tokens"]))

    # --- Anthropic：关闭 → 不注入，且历史思考块被剥离 ---
    AnthropicClient("sk-test", base, "test-model", "off").stream(
        [{"role": "user", "content": "hi"},
         {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "x", "signature": "sg"},
             {"type": "text", "text": "done"}]}], "s")
    req = RECEIVED[-1]
    check("anthropic: 关闭时不含 thinking 参数", "thinking" not in req["body"])
    check("anthropic: 关闭时历史思考块被剥离",
          all(b.get("type") != "thinking"
              for m in req["body"]["messages"] if isinstance(m.get("content"), list)
              for b in m["content"]))
    check("anthropic: 非法档位回退为 off",
          AnthropicClient("k", base, "m", "extreme").thinking_effort == "off")

    FLAGS["anth_thinking"] = True
    FLAGS["resp_reasoning"] = True
    try:
        # --- Anthropic：思考流解析（独立回调 + 签名完整）---
        texts, thinks = [], []
        result = AnthropicClient("k", base, "m", "high").stream(
            [{"role": "user", "content": "hi"}], "s",
            on_text=texts.append, on_thinking=thinks.append)
        check("anthropic: 思考增量走独立回调", "".join(thinks) == "我先想一下……", repr(thinks))
        check("anthropic: 正文增量不受思考污染", "".join(texts) == "我来创建文件。", repr(texts))
        tb = result["blocks"][0]
        check("anthropic: 思考块排最前且签名完整",
              tb["type"] == "thinking" and tb["thinking"] == "我先想一下……"
              and tb["signature"] == "sigSIGsig", str(tb))
        check("anthropic: 高档位 max_tokens 保持默认上限",
              RECEIVED[-1]["body"]["max_tokens"] == MAX_TOKENS,
              str(RECEIVED[-1]["body"]["max_tokens"]))

        # --- Responses：reasoning.effort + 摘要增量 + reasoning item 转换 ---
        thinks2 = []
        result2 = ResponsesClient("k", base, "m", "high").stream(
            [{"role": "user", "content": "hi"}], "s", on_thinking=thinks2.append)
        req2 = RECEIVED[-1]
        check("responses: reasoning.effort 注入",
              req2["body"]["reasoning"] == {"effort": "high"}, str(req2["body"].get("reasoning")))
        check("responses: 思考摘要增量回调", "".join(thinks2) == "先想想再答。", repr(thinks2))
        check("responses: reasoning item → thinking 块",
              result2["blocks"][0].get("type") == "thinking"
              and result2["blocks"][0]["thinking"] == "先想想再答。", str(result2["blocks"]))

        items = message_to_responses_items({"role": "assistant", "content": [
            {"type": "thinking", "thinking": "嗯", "signature": "s"},
            {"type": "text", "text": "hi"}]})
        check("responses: 历史思考块不回传",
              len(items) == 1 and items[0]["content"]["text"] == "hi", str(items))

        # --- Agent 循环：思考块保留进历史并原样回传 ---
        workdir3 = tempfile.mkdtemp(prefix="aichat_t3_")
        events3 = {"chunks": [], "thinks": [], "history": None, "error": None}
        worker = AgentWorker(
            messages=[{"role": "user", "content": "创建 hello2.txt"}],
            fmt="anthropic", api_key="sk", base_url=base, model="m",
            agent_mode=True, workdir=workdir3, confirm_tools=False,
            thinking_effort="high")
        worker.stream_chunk.connect(events3["chunks"].append)
        worker.thinking_chunk.connect(events3["thinks"].append)
        worker.history_ready.connect(lambda h: events3.update(history=h))
        worker.error_occurred.connect(lambda e: events3.update(error=e))
        worker.run()
        check("agent(thinking): 无错误完成", events3["error"] is None, str(events3["error"]))
        check("agent(thinking): 思考增量信号", "我先想一下……" in "".join(events3["thinks"]))
        history = events3["history"]
        a1 = next(m for m in history if m["role"] == "assistant")
        check("agent(thinking): 思考块排最前且签名保留",
              a1["content"][0]["type"] == "thinking"
              and a1["content"][0]["signature"] == "sigSIGsig", str(a1["content"][0]))
        tool_reqs = [r for r in RECEIVED if r["path"].endswith("/messages")
                     and any(isinstance(m.get("content"), list)
                             and any(b.get("type") == "tool_result" for b in m["content"])
                             for m in r["body"].get("messages", []))]
        check("agent(thinking): 下一轮请求原样回传思考块",
              bool(tool_reqs) and tool_reqs[0]["body"]["messages"][1]["content"][0]
              .get("signature") == "sigSIGsig",
              str(tool_reqs[0]["body"]["messages"][1] if tool_reqs else None))
        shutil.rmtree(workdir3, ignore_errors=True)
    finally:
        FLAGS["anth_thinking"] = False
        FLAGS["resp_reasoning"] = False


def test_thinking_degrade():
    print("\n[3.5] 思考参数被拒的降级保护（空 content 占位 + 自动重试一次）")
    from aichat.api import _OUTPUT_CAPS, MAX_TOKENS, AnthropicClient, ResponsesClient, ApiError
    _OUTPUT_CAPS.clear()  # 与其他降级用例隔离缓存

    # 空 content 占位：思考阶段被截断的回合（只有 thinking 块），关档位剥离后不再产生空数组
    stripped = AnthropicClient._strip_thinking({"role": "assistant", "content": [
        {"type": "thinking", "thinking": "被截断的思考", "signature": "sig"}]})
    check("strip thinking-only: 空 content 换占位文本块",
          stripped["content"] == [{"type": "text", "text": "[thinking omitted]"}],
          str(stripped["content"]))
    mixed = AnthropicClient._strip_thinking({"role": "assistant", "content": [
        {"type": "thinking", "thinking": "x", "signature": "s"},
        {"type": "text", "text": "答案"}]})
    check("strip: 混合块正常保留正文",
          [b["type"] for b in mixed["content"]] == ["text"]
          and mixed["content"][0]["text"] == "答案", str(mixed["content"]))

    server = ThreadingHTTPServer(("127.0.0.1", 0), FailFirstHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    dbase = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        # --- Anthropic：纯思考拒绝消息 → 400 → 去掉思考参数重试一次 ---
        # （消息不含 max_tokens/输出字样，不走收紧路径，专测思考降级）
        FailFirstHandler.calls.clear()
        FailFirstHandler.error_message = "thinking is not supported by this model"
        texts = []
        AnthropicClient("k", dbase, "m", "high").stream(
            [{"role": "user", "content": "hi"}], "s", on_text=texts.append)
        _, first = FailFirstHandler.calls[0]
        _, second = FailFirstHandler.calls[1]
        check("anthropic: 首请求带 thinking + 默认 max_tokens 上限",
              first.get("thinking") == {"type": "enabled", "budget_tokens": 16384}
              and first["max_tokens"] == MAX_TOKENS,
              f"{first.get('thinking')} / {first['max_tokens']}")
        check("anthropic: 重试请求已去 thinking 且 max_tokens 复位",
              "thinking" not in second and second["max_tokens"] == MAX_TOKENS,
              f"{second.get('thinking')} / {second['max_tokens']}")
        check("anthropic: 降级后正文正常到达", "".join(texts) == "降级成功", repr(texts))

        # --- Responses：reasoning 参数被拒 → 降级 ---
        FailFirstHandler.calls.clear()
        texts2 = []
        ResponsesClient("k", dbase, "m", "high").stream(
            [{"role": "user", "content": "hi"}], "s", on_text=texts2.append)
        _, rfirst = FailFirstHandler.calls[0]
        _, rsecond = FailFirstHandler.calls[1]
        check("responses: 首请求带 reasoning.effort，重试已去掉",
              rfirst.get("reasoning") == {"effort": "high"}
              and "reasoning" not in rsecond,
              f"{rfirst.get('reasoning')} / {rsecond.get('reasoning')}")
        check("responses: 降级后正文正常到达", "".join(texts2) == "degraded ok", repr(texts2))

        # --- 错误与思考参数无关 → 不重试，原样抛出 ---
        FailFirstHandler.calls.clear()
        FailFirstHandler.error_message = "invalid x-api-key"
        try:
            AnthropicClient("k", dbase, "m", "high").stream(
                [{"role": "user", "content": "hi"}], "s")
            check("anthropic: 无关 400 不触发重试", False)
        except ApiError as e:
            check("anthropic: 无关 400 不触发重试",
                  "invalid" in str(e) and len(FailFirstHandler.calls) == 1, str(e))

        # --- 档位关闭 → 即使错误提到 thinking 也不重试（参数本就没发）---
        FailFirstHandler.calls.clear()
        FailFirstHandler.error_message = "thinking is not supported"
        try:
            AnthropicClient("k", dbase, "m", "off").stream(
                [{"role": "user", "content": "hi"}], "s")
            check("anthropic: off 档位不触发重试", False)
        except ApiError:
            check("anthropic: off 档位不触发重试",
                  len(FailFirstHandler.calls) == 1, str(len(FailFirstHandler.calls)))

        # --- 关闭档位 + 思考截断历史 → 请求体带占位文本块而非空 content ---
        FailFirstHandler.calls.clear()
        FailFirstHandler.fail_count = 0  # 不再拒绝，放行所有请求
        AnthropicClient("k", dbase, "m", "off").stream(
            [{"role": "user", "content": "hi"},
             {"role": "assistant", "content": [
                 {"type": "thinking", "thinking": "只思考了", "signature": "s"}]}], "s")
        _, body = FailFirstHandler.calls[0]
        a_content = body["messages"][1]["content"]
        check("anthropic: 思考截断历史 → 占位文本块而非空 content",
              a_content == [{"type": "text", "text": "[thinking omitted]"}], str(a_content))
    finally:
        _OUTPUT_CAPS.clear()
        FailFirstHandler.error_message = "thinking is not supported by this model"
        FailFirstHandler.fail_count = 1
        server.shutdown()


def test_error_history_writeback():
    print("\n[3.6] 中途失败的历史回写（已完成轮次不丢失）")
    from aichat.agent import AgentWorker

    server = ThreadingHTTPServer(("127.0.0.1", 0), FailNthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    workdir = tempfile.mkdtemp(prefix="aichat_t4_")
    try:
        FailNthHandler.calls.clear()
        events = {"chunks": [], "history": None, "error": None, "done": False}
        worker = AgentWorker(
            messages=[{"role": "user", "content": "创建文件"}],
            fmt="anthropic", api_key="sk", base_url=base, model="m",
            agent_mode=True, workdir=workdir, confirm_tools=False)
        worker.stream_chunk.connect(events["chunks"].append)
        worker.history_ready.connect(lambda h: events.update(history=h))
        worker.turn_finished.connect(lambda: events.update(done=True))
        worker.error_occurred.connect(lambda e: events.update(error=e))
        worker.run()
        check("agent 中途 500: 报错且未正常结束",
              events["error"] is not None and not events["done"], str(events["error"]))
        check("agent 中途 500: 第一轮文本与工具事件已发生",
              "".join(events["chunks"]) == "我来创建文件。", repr(events["chunks"]))
        history = events["history"]
        check("agent 中途 500: 已完成轮次回写历史",
              history is not None and len(history) == 3
              and history[1]["role"] == "assistant"
              and any(b.get("type") == "tool_use" for b in history[1]["content"])
              and history[2]["role"] == "user"
              and history[2]["content"][0].get("type") == "tool_result",
              str(history)[:300] if history else "None")

        # 普通聊天首轮即失败（持续失败盖过连接级重试）：历史也回写（仅 user 消息）
        FailNthHandler.calls.clear()
        FailNthHandler.fail_on = {1, 2, 3}
        events2 = {"history": None, "error": None}
        worker2 = AgentWorker(
            messages=[{"role": "user", "content": "hi"}],
            fmt="anthropic", api_key="sk", base_url=base, model="m")
        worker2.history_ready.connect(lambda h: events2.update(history=h))
        worker2.error_occurred.connect(lambda e: events2.update(error=e))
        worker2.run()
        check("普通聊天失败: 历史仍回写",
              events2["error"] is not None and events2["history"] is not None
              and len(events2["history"]) == 1,
              f"{events2['error']} / {events2['history']}")
    finally:
        FailNthHandler.fail_on = {2, 3, 4, 5, 6, 7, 8}
        server.shutdown()
        shutil.rmtree(workdir, ignore_errors=True)


def test_retry_backoff():
    print("\n[3.7] 连接阶段重试退避（429 → 指数退避后成功）")
    from aichat.api import AnthropicClient, ApiError

    server = ThreadingHTTPServer(("127.0.0.1", 0), FailFirstHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        FailFirstHandler.calls.clear()
        FailFirstHandler.error_code = 429
        FailFirstHandler.error_message = "rate limited"
        FailFirstHandler.fail_count = 2
        texts = []
        AnthropicClient("k", base, "m").stream(
            [{"role": "user", "content": "hi"}], "s", on_text=texts.append)
        check("anthropic: 429 两次后第三次成功", len(FailFirstHandler.calls) == 3,
              str(len(FailFirstHandler.calls)))
        check("anthropic: 重试成功后正文正常", "".join(texts) == "降级成功", repr(texts))

        # 400 不属于可重试状态码：连接阶段只发一次（降级逻辑另行处理）
        FailFirstHandler.calls.clear()
        FailFirstHandler.error_code = 400
        FailFirstHandler.error_message = "bad request"
        FailFirstHandler.fail_count = 3
        try:
            AnthropicClient("k", base, "m").stream([{"role": "user", "content": "hi"}], "s")
            check("anthropic: 400 不做连接级重试", False)
        except ApiError as e:
            check("anthropic: 400 不做连接级重试",
                  len(FailFirstHandler.calls) == 1 and "bad request" in str(e),
                  f"{len(FailFirstHandler.calls)} / {e}")
    finally:
        FailFirstHandler.error_code = 400
        FailFirstHandler.error_message = "thinking is not supported by this model"
        FailFirstHandler.fail_count = 1
        server.shutdown()


def test_output_limit_clamp():
    print("\n[3.7b] max_tokens 超限自动收紧（按服务端上报的上限重试）")
    from aichat.api import _OUTPUT_CAPS, AnthropicClient, ApiError

    server = ThreadingHTTPServer(("127.0.0.1", 0), FailFirstHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        # 上限装得下思考预算：收紧 max_tokens、保留思考参数
        _OUTPUT_CAPS.clear()  # 四个用例共用 (base, "m") 缓存键，必须逐用例隔离
        FailFirstHandler.calls.clear()
        FailFirstHandler.error_messages = [
            "max_tokens: 393216 > 64000, which is the maximum allowed number of output tokens"]
        FailFirstHandler.fail_count = 1
        texts = []
        AnthropicClient("k", base, "m", "high").stream(
            [{"role": "user", "content": "hi"}], "s", on_text=texts.append)
        _, second = FailFirstHandler.calls[1]
        check("anthropic: 超限后按服务端上限收紧并保留思考",
              len(FailFirstHandler.calls) == 2 and second["max_tokens"] == 64000
              and second.get("thinking") == {"type": "enabled", "budget_tokens": 16384}
              and "".join(texts) == "降级成功",
              f"{second['max_tokens']} / {second.get('thinking')}")

        # 上限装不下思考预算（高档 budget+4096=20480 > 20000）：连思考一起去掉
        _OUTPUT_CAPS.clear()
        FailFirstHandler.calls.clear()
        FailFirstHandler.error_messages = [
            "max_tokens: 393216 > 20000, which is the maximum allowed number of output tokens"]
        FailFirstHandler.fail_count = 1
        AnthropicClient("k", base, "m", "high").stream(
            [{"role": "user", "content": "hi"}], "s")
        _, second = FailFirstHandler.calls[1]
        check("anthropic: 上限装不下思考预算时连思考一起去掉",
              "thinking" not in second and second["max_tokens"] == 20000,
              f"{second['max_tokens']} / {second.get('thinking')}")

        # 两段式：先超限、收紧后思考仍被拒 → 再去掉思考
        _OUTPUT_CAPS.clear()
        FailFirstHandler.calls.clear()
        FailFirstHandler.error_messages = [
            "max_tokens: 393216 > 64000, which is the maximum allowed number of output tokens",
            "thinking is not supported by this model",
        ]
        FailFirstHandler.fail_count = 2
        AnthropicClient("k", base, "m", "high").stream(
            [{"role": "user", "content": "hi"}], "s")
        _, second = FailFirstHandler.calls[1]
        _, third = FailFirstHandler.calls[2]
        check("anthropic: 收紧上限与思考降级可串联",
              len(FailFirstHandler.calls) == 3
              and second["max_tokens"] == 64000 and "thinking" in second
              and third["max_tokens"] == 64000 and "thinking" not in third,
              str([(b.get("max_tokens"), b.get("thinking")) for _, b in FailFirstHandler.calls]))

        # 与 max_tokens 无关的 400 不触发收紧
        _OUTPUT_CAPS.clear()
        FailFirstHandler.calls.clear()
        FailFirstHandler.error_messages = ["invalid x-api-key"]
        FailFirstHandler.fail_count = 1
        try:
            AnthropicClient("k", base, "m", "high").stream(
                [{"role": "user", "content": "hi"}], "s")
            check("anthropic: 无关 400 不触发收紧重试", False)
        except ApiError as e:
            check("anthropic: 无关 400 不触发收紧重试",
                  len(FailFirstHandler.calls) == 1 and "invalid" in str(e), str(e))
    finally:
        _OUTPUT_CAPS.clear()
        FailFirstHandler.error_messages = []
        FailFirstHandler.fail_count = 1
        server.shutdown()


def test_context_window():
    print("\n[3.8] 请求级上下文窗口（超预算裁旧轮次，配对完整）")
    import aichat.api as api_mod
    from aichat.api import estimate_message_chars, window_messages

    small = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    check("窗口: 预算内历史原样返回", window_messages(small) == small)

    # 默认预算按 1M 上下文档校准（80 万字符）：中等规模历史不应被裁
    medium = [{"role": "user", "content": "字" * 100_000},
              {"role": "assistant", "content": "字" * 100_000}]
    check("窗口: 默认 1M 档预算不裁 20 万字符历史", window_messages(medium) == medium)

    orig_limit = api_mod.CONTEXT_CHAR_LIMIT
    try:
        api_mod.CONTEXT_CHAR_LIMIT = 6000
        # 多任务轮：裁中间、留首尾
        msgs = []
        for i in range(4):
            msgs.append({"role": "user", "content": f"问题{i}" + "字" * 4000})
            msgs.append({"role": "assistant", "content": f"回答{i}" + "字" * 4000})
        windowed = window_messages(msgs)
        check("窗口: 首轮与最新轮保留、中间轮被裁",
              len(windowed) == 4
              and windowed[0]["content"].startswith("问题0")
              and windowed[1]["content"].startswith("回答0")
              and windowed[2]["content"].startswith("问题3")
              and windowed[3]["content"].startswith("回答3"),
              str([m["content"][:6] for m in windowed]))

        # 回归：thinking=off（默认档）此前在 else 分支用未裁剪全量覆盖了裁剪结果
        from aichat.api import AnthropicClient
        payload_off = AnthropicClient("k", "http://x", "m", "off")._build_payload(
            msgs, "s", None, True)
        check("窗口: thinking=off 同样走裁剪",
              len(payload_off["messages"]) == len(windowed),
              f"payload={len(payload_off['messages'])} windowed={len(windowed)}")
        payload_high = AnthropicClient("k", "http://x", "m", "high")._build_payload(
            msgs, "s", None, True)
        check("窗口: thinking=high 裁剪不回退",
              len(payload_high["messages"]) == len(windowed))
        est = sum(estimate_message_chars(m) for m in windowed)
        check("窗口: 裁剪后估算量下降", est < sum(estimate_message_chars(m) for m in msgs))

        # 单任务长 agent 历史：按工具交换对从头裁，配对不被拆散
        agent_msgs = [{"role": "user", "content": "task"}]
        for i in range(5):
            agent_msgs.append({"role": "assistant", "content": [
                {"type": "tool_use", "id": f"t{i}", "name": "bash",
                 "input": {"cmd": "x" * 2500}}]})
            agent_msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t{i}", "content": "y" * 2500}]})
        agent_msgs.append({"role": "assistant", "content": "完成"})
        w2 = window_messages(agent_msgs)
        pairing_ok = True
        for idx, m in enumerate(w2):
            if m["role"] == "assistant" and isinstance(m["content"], list):
                for b in m["content"]:
                    if b.get("type") != "tool_use":
                        continue
                    nxt = w2[idx + 1] if idx + 1 < len(w2) else None
                    if not (isinstance(nxt, dict) and nxt.get("role") == "user"
                            and isinstance(nxt.get("content"), list)
                            and any(r.get("type") == "tool_result"
                                    and r.get("tool_use_id") == b["id"]
                                    for r in nxt["content"])):
                        pairing_ok = False
        check("窗口: agent 历史裁剪后 tool_use/tool_result 仍配对", pairing_ok)
        check("窗口: agent 历史保留任务锚点与最终回答",
              w2[0]["content"] == "task" and w2[-1]["content"] == "完成",
              str([m["role"] for m in w2]))
        est2 = sum(estimate_message_chars(m) for m in w2)
        check("窗口: agent 历史裁剪后估算量受限",
              est2 <= api_mod.CONTEXT_CHAR_LIMIT + 10, str(est2))
    finally:
        api_mod.CONTEXT_CHAR_LIMIT = orig_limit


def test_encoding_tools(workdir):
    print("\n[3.9] 编码保持（edit 不再静默转码；grep 多编码可搜）")
    from aichat import agent as A

    gbk_path = os.path.join(workdir, "gbk.txt")
    gbk_content = "第一行中文\nline2\n第三行\n"
    with open(gbk_path, "wb") as f:
        f.write(gbk_content.encode("gbk"))

    check("edit: GBK 文件编辑成功", A.run_tool(
        "edit", {"path": "gbk.txt", "old": "line2", "new": "LINE2"}, workdir) == "ok")
    with open(gbk_path, "rb") as f:
        raw = f.read()
    check("edit: GBK 编码保持不变（未被转成 UTF-8）",
          raw.decode("gbk") == "第一行中文\nLINE2\n第三行\n"
          and raw != gbk_content.replace("line2", "LINE2").encode("utf-8"), repr(raw[:30]))

    check("grep: GBK 中文文件可搜", "gbk.txt" in A.run_tool("grep", {"pat": "第三行"}, workdir))
    with open(os.path.join(workdir, "bin.dat"), "wb") as f:
        f.write(bytes([0, 1, 2, 0, 3]) * 100)
    check("grep: 二进制文件被跳过不误报", A.run_tool("grep", {"pat": "\x00\x01"}, workdir) == "none"
          or "bin.dat" not in A.run_tool("grep", {"pat": "\x00\x01"}, workdir))

    check("read: GBK 文件读取带行号", "第一行中文" in A.run_tool("read", {"path": "gbk.txt"}, workdir))


def test_office_read(workdir):
    print("\n[3.9b] read 支持 Office 文件（docx/xlsx/pptx 文本提取）")
    from aichat import agent as A

    # 手工构造最小 OOXML：只包含提取器实际读取的部件
    docx_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:r><w:t>标题段落</w:t></w:r></w:p>
<w:tbl>
<w:tr><w:tc><w:p><w:r><w:t>A1</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>B1</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:p><w:r><w:t>A2</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>B2</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl>
<w:p><w:r><w:t>结束段落</w:t></w:r></w:p>
</w:body></w:document>'''
    with zipfile.ZipFile(os.path.join(workdir, "demo.docx"), "w") as z:
        z.writestr("word/document.xml", docx_xml)

    m = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with zipfile.ZipFile(os.path.join(workdir, "demo.xlsx"), "w") as z:
        z.writestr("xl/workbook.xml", f'''<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="{m}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="数据表" sheetId="1" r:id="rId1"/></sheets></workbook>''')
        z.writestr("xl/_rels/workbook.xml.rels", '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>''')
        z.writestr("xl/sharedStrings.xml", f'''<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="{m}"><si><t>名称</t></si><si><t>值</t></si></sst>''')
        z.writestr("xl/worksheets/sheet1.xml", f'''<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="{m}"><sheetData>
<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>1</v></c></row>
<row r="2"><c r="A2"><v>3.14</v></c><c r="B2" t="b"><v>1</v></c></row>
<row r="3"><c r="A3" t="str"><v>=SUM(1,2)</v></c></row>
</sheetData></worksheet>''')

    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    p = "http://schemas.openxmlformats.org/presentationml/2006/main"
    with zipfile.ZipFile(os.path.join(workdir, "demo.pptx"), "w") as z:
        for num, text in ((1, "第一页标题"), (2, "第二页内容"), (10, "第十页内容")):
            z.writestr(f"ppt/slides/slide{num}.xml", f'''<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="{p}" xmlns:a="{a}"><p:cSld><p:spTree>
<p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>
</p:spTree></p:cSld></p:sld>''')

    out = A.run_tool("read", {"path": "demo.docx"}, workdir)
    check("read: docx 段落提取", "标题段落" in out and "结束段落" in out, out)
    check("read: docx 表格渲染为行", "A1 | B1" in out and "A2 | B2" in out, out)
    check("read: docx 提取结果带行号", "  1| 标题段落" in out, repr(out[:80]))

    out = A.run_tool("read", {"path": "demo.xlsx"}, workdir)
    check("read: xlsx 工作表名", "===== 数据表 =====" in out, out)
    check("read: xlsx 共享字符串与空列补位", "名称\t\t值" in out, repr(out))
    check("read: xlsx 数字/布尔/公式串", "3.14\tTRUE" in out and "=SUM(1,2)" in out, repr(out))

    out = A.run_tool("read", {"path": "demo.pptx"}, workdir)
    check("read: pptx 按页码数字排序",
          0 <= out.index("第一页标题") < out.index("第二页内容") < out.index("第十页内容"), out)

    out = A.run_tool("read", {"path": "demo.docx", "offset": 1, "limit": 1}, workdir)
    check("read: docx 提取结果支持分页", "A1 | B1" in out and "标题段落" not in out, out)

    with open(os.path.join(workdir, "old.doc"), "wb") as f:
        f.write(b"\xd0\xcf\x11\xe0legacy-binary")
    out = A.run_tool("read", {"path": "old.doc"}, workdir)
    check("read: 旧版二进制格式友好报错", out.startswith("error:") and ".docx" in out, out)

    with open(os.path.join(workdir, "broken.docx"), "w", encoding="utf-8") as f:
        f.write("not a zip file")
    check("read: 损坏 docx 报错不崩溃", A.run_tool(
        "read", {"path": "broken.docx"}, workdir).startswith("error:"))


def test_responses_done_fallback():
    print("\n[3.10] Responses 网关兜底（只发 output_item.done 也能出块）")
    import urllib.request
    from aichat.api import ResponsesClient

    text = "网关兜底文本"
    done_item = {"type": "message", "id": "msg_0", "role": "assistant",
                 "content": [{"type": "output_text", "text": text}]}
    events = [
        "event: response.created",
        'data: {"type":"response.created","response":{"id":"resp_t"}}',
        "",
        "event: response.output_item.added",
        "data: " + json.dumps({"type": "response.output_item.added", "output_index": 0,
                               "item": {"type": "message", "id": "msg_0", "role": "assistant",
                                        "content": []}}),
        "",
        "event: response.output_text.delta",
        "data: " + json.dumps({"type": "response.output_text.delta", "output_index": 0,
                               "delta": text}),
        "",
        "event: response.output_item.done",
        "data: " + json.dumps({"type": "response.output_item.done", "output_index": 0,
                               "item": done_item}),
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

    texts = []
    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=None: FakeResp(events)
    try:
        result = ResponsesClient("k", "http://x", "m").stream(
            [{"role": "user", "content": "hi"}], "s", on_text=texts.append)
    finally:
        urllib.request.urlopen = orig
    check("responses: 无 response.completed 时用 output_item.done 兜底",
          result["blocks"] == [{"type": "text", "text": text}]
          and "".join(texts) == text, str(result["blocks"]))


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
    print("\n[6] UI 离屏冒烟测试（主窗口 + 玄猫 + 消息组件）")
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

    # 思考卡片：流式增量 → 独立折叠卡片，不计入正文
    wt = win.add_message_widget("assistant", "")
    wt.stream_thinking("先拆解问题")
    wt.stream_thinking("，再给答案")
    card = wt._thinking_widgets[0]
    check("思考卡片创建且默认折叠",
          card.expanded is False and not card.browser.isVisible()
          and card.title_label.text() == "💭 思考中…", card.title_label.text())
    card.expanded = True
    card._update_browser()
    card.browser.setVisible(True)
    check("思考卡片展开可见内容", card.browser.toPlainText() == "先拆解问题，再给答案",
          card.browser.toPlainText())
    wt.stream_append("答案正文")
    wt.seal_stream()
    check("思考结束后标题复位且不计入正文",
          card.title_label.text() == "💭 思考过程" and wt.get_all_text() == "答案正文",
          repr(wt.get_all_text()))
    wt.grab().save(os.path.join(hist_dir, "render_thinking.png"))

    # 含思考块的历史消息渲染（重新加载路径）；追加而非覆盖，
    # 保留 tool_use 内容供保存/加载往返断言
    conv["messages"] += [
        {"role": "user", "content": "想一下"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "内部推理过程……", "signature": "sig1"},
            {"type": "text", "text": "想好了。"},
        ]},
    ]
    win.load_conversation_messages()
    win.grab().save(os.path.join(hist_dir, "render_thinking_history.png"))
    check("含思考块历史渲染无崩溃", True)

    # 停止生成按钮：双态切换 + 空闲时停止无副作用
    win._set_requesting_state(True)
    check("停止按钮: 请求中变为停止态",
          win.send_btn.text() == "⏹ 停止" and win._request_active, win.send_btn.text())
    win._stop_generation()
    check("停止按钮: 停止后恢复发送态",
          win.send_btn.text() == "发送" and not win._request_active, win.send_btn.text())

    # 生成中再次发送被守卫拒绝（Enter 直呼 send_message 的回归：
    # 此前会产生无人回收的野生 worker 线程）
    win._request_active = True
    win.input_edit.setPlainText("并发测试")
    win.send_message()
    check("并发守卫: 请求进行中 send_message 不创建新 worker",
          win.api_worker is None and win._request_active)
    win._request_active = False
    win.input_edit.clear()

    # 工具确认弹窗：非模态、可 ESC/停止取消、防重复决定
    from PySide6.QtWidgets import QMessageBox as _QMessageBox
    from aichat.agent import AgentWorker, ToolApprover
    appr = ToolApprover()
    win.on_confirm_requested("bash", '{"cmd": "echo hi"}', appr)
    box = win._confirm_box
    check("确认弹窗: 非模态弹出且线程仍等待",
          box is not None and not box.isModal() and box.isVisible()
          and not appr._event.is_set())
    box.button(_QMessageBox.StandardButton.No).click()
    for _ in range(30):
        app.processEvents()
        if win._confirm_box is None:
            break
        time.sleep(0.02)
    check("确认弹窗: 点击否 → 拒绝并回收",
          appr.approved is False and appr._event.is_set() and win._confirm_box is None)
    appr2 = ToolApprover()
    win.on_confirm_requested("bash", "{}", appr2)
    win._confirm_box.button(_QMessageBox.StandardButton.Yes).click()
    for _ in range(30):
        app.processEvents()
        if win._confirm_box is None:
            break
        time.sleep(0.02)
    check("确认弹窗: 点击是 → 批准且 decided 不被 finished 覆盖",
          appr2.approved is True and appr2._event.is_set())

    # 版本号单一来源（__init__.__version__ → APP_VERSION）
    from aichat import __version__
    from aichat.window import APP_VERSION
    check("版本号: 单一来源拼接", APP_VERSION == f"V{__version__}",
          f"{APP_VERSION} vs {__version__}")

    # 中断标记：停止生成后气泡追加提示段（seal 会把流式段换成正式渲染段，
    # 故浏览器数量 +2：正文一段 + 标记一段）
    wm = win.add_message_widget("assistant", "")
    wm.stream_append("写到一半")
    n_before = len(wm._all_text_browsers)
    wm.mark_interrupted()
    check("中断标记: 气泡末尾追加提示段",
          len(wm._all_text_browsers) == n_before + 2
          and "已中断" in wm._all_text_browsers[-1].toPlainText(),
          str([b.toPlainText() for b in wm._all_text_browsers]))

    # 桌宠隐藏时暂停 30fps 动画定时器，显示时恢复；初始不可见不空转
    pw = win.cat_widget
    check("桌宠: 初始不可见时定时器不空转", not pw._timer.isActive())
    pw.show()
    pw.hide()
    check("桌宠: 隐藏时暂停动画定时器", not pw._timer.isActive())
    pw.show()
    check("桌宠: 显示时恢复动画定时器", pw._timer.isActive())
    pw.hide()

    # 错误气泡移除判定：有工具卡片/思考卡的气泡不算空
    we = win.add_message_widget("assistant", "")
    check("is_empty: 空气泡为空", we.is_empty())
    we.add_tool_call("te", "bash", "{}")
    check("is_empty: 有工具卡片不为空", not we.is_empty())
    we2 = win.add_message_widget("assistant", "")
    we2.stream_thinking("思考")
    check("is_empty: 有思考卡不为空", not we2.is_empty())

    # 工具确认批准器：stop() 主动唤醒等待中的线程
    from aichat.agent import AgentWorker, ToolApprover
    worker_ap = AgentWorker(messages=[], fmt="anthropic", api_key="k",
                            base_url="http://127.0.0.1:1", model="m")
    pending = ToolApprover()
    worker_ap._pending_approver = pending
    worker_ap.stop()
    check("stop(): 唤醒卡在确认等待的批准器（按拒绝处理）",
          pending._event.is_set() and pending.approved is False)

    # gpt-5 属于多模态模型，不应误报视觉警告
    win.model = "gpt-5"
    win.supports_vision = False
    check("视觉检测: gpt-5 不再误报", win._check_model_supports_vision())

    # 玄猫各姿态渲染 + AI 状态联动
    win.cat_widget._t = 1.2
    for pose in ("sit", "walk", "run", "drowsy", "sleep", "stretch",
                 "think", "work", "happy", "sad"):
        win.cat_widget._enter_pose(pose, 5)
        win.cat_widget.repaint()
    check("玄猫十种姿态渲染无崩溃", True)
    pw = win.cat_widget
    pw._run_flags.clear()   # 进程监控可能在本机检测到浏览器而触发奔跑，清掉保证确定性
    for state in ("thinking", "working", "happy", "sad", "idle"):
        pw.set_ai_state(state)
        pw.repaint()
    check("玄猫 AI 状态联动（thinking/working/happy/sad/idle）",
          pw._ai_state == "idle" and pw._pose == "stretch", f"{pw._ai_state}/{pw._pose}")
    pw.set_ai_state("working")
    pw.set_run_flag("process", True)   # AI 忙碌时进程奔跑让位
    check("AI 忙碌时进程触发让位", pw._pose == "work", pw._pose)
    pw.set_ai_state("idle")
    check("AI 空闲后恢复进程奔跑", pw._pose == "run", pw._pose)
    pw.set_run_flag("process", False)
    check("进程结束回到平静", pw._pose == "stretch", pw._pose)

    # 设置对话框：思考强度档位往返
    from aichat.window import SettingsDialog
    dlg = SettingsDialog()
    check("设置对话框: 思考强度下拉含 4 档", dlg.thinking_combo.count() == 4)
    dlg.thinking_combo.setCurrentIndex(3)
    check("设置对话框: get_settings 携带 thinking_effort",
          dlg.get_settings()["thinking_effort"] == "high")
    dlg.thinking_combo.setCurrentIndex(0)
    check("设置对话框: 关闭档位为 off", dlg.get_settings()["thinking_effort"] == "off")
    check("设置对话框: 上下文档位下拉含 3 档", dlg.context_combo.count() == 3)
    dlg.context_combo.setCurrentIndex(0)
    check("设置对话框: get_settings 携带 context_preset",
          dlg.get_settings()["context_preset"] == "128k")
    dlg.context_combo.setCurrentIndex(2)
    check("设置对话框: 1M 档位", dlg.get_settings()["context_preset"] == "1m")

    # 保存/加载往返
    win.save_conversations(delay=False)
    win2 = ChatWindow()
    check("历史保存/加载往返", len(win2.conversations) >= 1
          and any("tool_use" in str(m) for c in win2.conversations.values()
                  for m in c["messages"]))
    check("思考块含签名往返保留", any(
        isinstance(m.get("content"), list)
        and any(b.get("type") == "thinking" and b.get("signature") == "sig1"
                for b in m["content"])
        for c in win2.conversations.values() for m in c["messages"]))
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
        test_stop_generation()
        test_interrupted_half_round()
        test_cancel_writeback_race()
        test_empty_blocks_guard()
        test_sse_flush()
        test_interruptible_backoff()
        test_output_cap_cache()
        test_parse_output_cap_patterns()
        test_prompt_too_long_halving()
        test_api_clients(base)
        test_api_block_start_input()
        test_agent_worker(base, workdir)
        test_thinking_support(base)
        test_thinking_degrade()
        test_error_history_writeback()
        test_retry_backoff()
        test_output_limit_clamp()
        test_context_window()
        test_encoding_tools(workdir)
        test_office_read(workdir)
        test_responses_done_fallback()
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
