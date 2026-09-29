"""API 客户端层：纯标准库实现，支持两种格式。

- Anthropic Messages 格式（POST {base_url}/v1/messages）
- OpenAI Responses 格式（POST {base_url}/responses）

内部统一使用 Anthropic 风格的消息结构（content blocks）：
    {"role": "user"|"assistant", "content": str | [block, ...]}
块类型：
    {"type": "text", "text": str}
    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": str}}
    {"type": "tool_use", "id": str, "name": str, "input": dict}
    {"type": "tool_result", "tool_use_id": str, "content": str}

流式请求通过 on_text 回调吐出文本增量，返回值携带完整的响应内容块，
工具调用总是在流结束后以完整形态返回（内部组装），上层无需关心 SSE 细节。
"""

import json
import urllib.error
import urllib.request

ANTHROPIC = "anthropic"
RESPONSES = "responses"
API_FORMATS = (ANTHROPIC, RESPONSES)

FORMAT_LABELS = {
    ANTHROPIC: "Anthropic (Messages)",
    RESPONSES: "OpenAI (Responses)",
}

# Anthropic 官方要求 max_tokens 必填
MAX_TOKENS = 8192
# 单次请求的读写超时（流式响应按每个 SSE 事件计算）
STREAM_TIMEOUT = 300


class ApiError(Exception):
    """API 请求失败（网络 / HTTP / 协议错误）"""


def make_client(fmt: str, api_key: str, base_url: str, model: str):
    """按格式创建对应客户端"""
    if fmt == ANTHROPIC:
        return AnthropicClient(api_key, base_url, model)
    if fmt == RESPONSES:
        return ResponsesClient(api_key, base_url, model)
    raise ApiError(f"未知的 API 格式: {fmt}")


def join_url(base: str, suffix: str) -> str:
    """拼接端点；用户填的 base 已带后缀时不重复追加"""
    base = (base or "").rstrip("/")
    if base.endswith(suffix):
        return base
    return base + suffix


def _http_error_message(e: urllib.error.HTTPError) -> str:
    """把 HTTPError 转成带响应体信息的可读消息"""
    detail = ""
    try:
        body = e.read().decode("utf-8", errors="replace")
        data = json.loads(body)
        # Anthropic 与 OpenAI 的错误结构都是 {"error": {"message": ...}}
        detail = (data.get("error") or {}).get("message") or body[:500]
    except Exception:
        detail = ""
    return f"HTTP {e.code}: {detail or e.reason}"


def _iter_sse(resp):
    """解析 SSE 流，产出 (event, data_json_str)"""
    event_name = None
    data_lines = []
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name, data_lines = None, []
        elif line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                return
            data_lines.append(payload)


def tools_to_responses(tools):
    """Anthropic 形态的工具定义 → Responses 形态"""
    return [
        {
            "type": "function",
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        }
        for t in (tools or [])
    ]


def _text_output_role(role: str) -> str:
    return "output_text" if role == "assistant" else "input_text"


def message_to_responses_items(msg: dict) -> list:
    """内部消息 → Responses API 的 input items（一条消息可能展开为多条 item）"""
    content = msg.get("content")
    role = msg.get("role", "user")

    if content is None:
        return []
    if isinstance(content, str):
        return [{"role": role, "content": content}] if content.strip() else []

    items = []
    text_parts = []

    def flush_text():
        if text_parts:
            parts = [
                {"type": _text_output_role(role), "text": t}
                for t in text_parts
                if t.strip()
            ]
            if parts:
                items.append(
                    {"role": role, "content": parts[0] if len(parts) == 1 else parts}
                )
            text_parts.clear()

    for block in content:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "image":
            src = block.get("source", {})
            data = src.get("data", "")
            media = src.get("media_type", "image/png")
            flush_text()
            items.append(
                {
                    "role": role,
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": f"data:{media};base64,{data}",
                        }
                    ],
                }
            )
        elif btype == "tool_use":
            flush_text()
            items.append(
                {
                    "type": "function_call",
                    "call_id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "arguments": json.dumps(
                        block.get("input") or {}, ensure_ascii=False
                    ),
                }
            )
        elif btype == "tool_result":
            flush_text()
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": block.get("tool_use_id", ""),
                    "output": str(block.get("content", "")),
                }
            )
    flush_text()
    return items


def responses_output_to_blocks(items: list) -> list:
    """Responses API 的 output items → 内部内容块"""
    blocks = []
    for item in items or []:
        itype = item.get("type")
        if itype == "message":
            for part in item.get("content") or []:
                if part.get("type") in ("output_text", "text"):
                    text = part.get("text", "")
                    if text.strip():
                        blocks.append({"type": "text", "text": text})
        elif itype == "function_call":
            try:
                args = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": item.get("arguments", "")}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": item.get("call_id") or item.get("id", ""),
                    "name": item.get("name", ""),
                    "input": args,
                }
            )
    return blocks


def normalize_history_message(msg: dict) -> dict:
    """把旧版 OpenAI Chat Completions 格式的历史消息转成内部格式"""
    role = msg.get("role", "user")
    content = msg.get("content")

    if isinstance(content, str) or content is None:
        return {"role": role, "content": content or ""}

    blocks = []
    for item in content:
        itype = item.get("type")
        if itype == "text":
            blocks.append({"type": "text", "text": item.get("text", "")})
        elif itype == "image_url":
            # 旧格式: {"image_url": {"url": "data:image/png;base64,..."}}
            url = (item.get("image_url") or {}).get("url", "")
            if url.startswith("data:") and ";base64," in url:
                head, data = url.split(";base64,", 1)
                media = head[len("data:"):] or "image/png"
                blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media, "data": data},
                    }
                )
        elif itype in ("image", "tool_use", "tool_result"):
            blocks.append(item)
    return {"role": role, "content": blocks}


class _BaseClient:
    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "").strip()
        self.model = (model or "").strip()

    def stream(self, messages, system, tools=None, on_text=None, is_cancelled=None):
        """发起一次流式请求。

        on_text: 文本增量回调 on_text(str)
        is_cancelled: 可选的取消检查回调，返回 True 时尽快中止
        返回: {"blocks": [内部内容块], "stop_reason": str}
        """
        raise NotImplementedError

    def _open_stream(self, url, payload, headers):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            return urllib.request.urlopen(req, timeout=STREAM_TIMEOUT)
        except urllib.error.HTTPError as e:
            raise ApiError(_http_error_message(e)) from e
        except urllib.error.URLError as e:
            raise ApiError(f"连接失败: {e.reason}") from e
        except OSError as e:
            raise ApiError(f"网络错误: {e}") from e

    @staticmethod
    def _emit_text(on_text, delta, is_cancelled):
        if on_text and delta and not (is_cancelled and is_cancelled()):
            on_text(delta)


class AnthropicClient(_BaseClient):
    format_name = ANTHROPIC

    def endpoint(self) -> str:
        return join_url(self.base_url, "/v1/messages")

    def stream(self, messages, system, tools=None, on_text=None, is_cancelled=None):
        payload = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": system or "",
            "messages": [dict(m) for m in messages],
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        # 官方 Anthropic 用 x-api-key；OpenRouter 等网关用 Bearer Token
        if self.api_key.startswith("sk-or-"):
            auth = {"Authorization": f"Bearer {self.api_key}"}
        else:
            auth = {"x-api-key": self.api_key}
        resp = self._open_stream(
            self.endpoint(),
            payload,
            {**auth, "anthropic-version": "2023-06-01"},
        )

        blocks_by_index = {}
        json_parts = {}  # index -> tool_use 参数累积的 JSON 片段
        stop_reason = "end_turn"
        try:
            with resp:
                for event, data in _iter_sse(resp):
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    etype = obj.get("type") or event or ""
                    if is_cancelled and is_cancelled():
                        break
                    if etype == "content_block_start":
                        idx = obj.get("index", len(blocks_by_index))
                        block = dict(obj.get("content_block") or {})
                        if block.get("type") == "tool_use":
                            json_parts[idx] = ""
                        blocks_by_index[idx] = block
                    elif etype == "content_block_delta":
                        idx = obj.get("index", -1)
                        delta = obj.get("delta") or {}
                        dtype = delta.get("type")
                        if idx not in blocks_by_index:
                            continue
                        if dtype == "text_delta":
                            text = delta.get("text", "")
                            blocks_by_index[idx]["text"] = (
                                blocks_by_index[idx].get("text", "") + text
                            )
                            self._emit_text(on_text, text, is_cancelled)
                        elif dtype == "input_json_delta":
                            json_parts[idx] = json_parts.get(idx, "") + delta.get(
                                "partial_json", ""
                            )
                    elif etype == "message_delta":
                        stop_reason = obj.get("delta", {}).get(
                            "stop_reason", stop_reason
                        )
                    elif etype == "error":
                        err = obj.get("error") or {}
                        raise ApiError(
                            f"{err.get('type', 'error')}: {err.get('message', '')}"
                        )
        except ApiError:
            raise
        except OSError as e:
            raise ApiError(f"网络错误: {e}") from e

        blocks = []
        for idx in sorted(blocks_by_index):
            block = blocks_by_index[idx]
            if block.get("type") == "tool_use":
                raw = json_parts.get(idx, "")
                if raw.strip():
                    try:
                        block["input"] = json.loads(raw)
                    except json.JSONDecodeError:
                        block["input"] = {"_raw": raw}
                elif "input" not in block:
                    # 部分网关在 content_block_start 中直接给出完整 input 且不增量发送，
                    # 此时保留已收到的参数，仅在缺失时补空表
                    block["input"] = {}
            if block.get("type") == "text" and not (block.get("text") or "").strip():
                continue
            blocks.append(block)
        return {"blocks": blocks, "stop_reason": stop_reason}


class ResponsesClient(_BaseClient):
    format_name = RESPONSES

    def endpoint(self) -> str:
        return join_url(self.base_url, "/responses")

    def stream(self, messages, system, tools=None, on_text=None, is_cancelled=None):
        items = []
        for msg in messages:
            items.extend(message_to_responses_items(msg))
        payload = {
            "model": self.model,
            "input": items,
            "stream": True,
        }
        if system:
            payload["instructions"] = system
        if tools:
            payload["tools"] = tools_to_responses(tools)
        resp = self._open_stream(
            self.endpoint(),
            payload,
            {"Authorization": f"Bearer {self.api_key}"},
        )

        final_output = None
        error_message = None
        try:
            with resp:
                for event, data in _iter_sse(resp):
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    etype = obj.get("type") or event or ""
                    if is_cancelled and is_cancelled():
                        break
                    if etype == "response.output_text.delta":
                        self._emit_text(on_text, obj.get("delta", ""), is_cancelled)
                    elif etype == "response.completed":
                        final_output = (obj.get("response") or {}).get("output")
                    elif etype in ("response.failed", "error"):
                        err = obj.get("error") or {}
                        error_message = err.get("message") or str(obj)[:500]
                    elif etype == "response.incomplete" and final_output is None:
                        # 截断的响应也带完整 output
                        final_output = (obj.get("response") or {}).get("output")
        except ApiError:
            raise
        except OSError as e:
            raise ApiError(f"网络错误: {e}") from e

        if final_output is None:
            if error_message:
                raise ApiError(error_message)
            raise ApiError("响应流提前结束（未收到 response.completed）")

        blocks = responses_output_to_blocks(final_output)
        return {"blocks": blocks, "stop_reason": "end_turn"}
