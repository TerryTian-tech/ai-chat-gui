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
    {"type": "thinking", "thinking": str, "signature": str}   # 思考块（含签名，原样保留）

流式请求通过 on_text / on_thinking 回调分别吐出正文与思考增量，
返回值携带完整的响应内容块，工具调用总是在流结束后以完整形态返回
（内部组装），上层无需关心 SSE 细节。
"""

import json
import re
import time
import urllib.error
import urllib.request

ANTHROPIC = "anthropic"
RESPONSES = "responses"
API_FORMATS = (ANTHROPIC, RESPONSES)

FORMAT_LABELS = {
    ANTHROPIC: "Anthropic (Messages)",
    RESPONSES: "OpenAI (Responses)",
}

# Anthropic 官方要求 max_tokens 必填；384K 对齐 1M 上下文档模型的最大输出。
# 输出上限较小的模型会因此报 400，由 _open_with_degrade 按服务端上报的
# 上限自动收紧重试，对用户无感
MAX_TOKENS = 384 * 1024
# 单次请求的读写超时（流式响应按每个 SSE 事件计算）
STREAM_TIMEOUT = 300

# 思考强度档位（两种格式通用；off 表示不发送思考参数）
THINKING_OFF = "off"
THINKING_LOW = "low"
THINKING_MEDIUM = "medium"
THINKING_HIGH = "high"
THINKING_LEVELS = (THINKING_OFF, THINKING_LOW, THINKING_MEDIUM, THINKING_HIGH)

# Anthropic 各档位对应的思考预算（budget_tokens，官方下限 1024）
ANTHROPIC_BUDGETS = {
    THINKING_LOW: 2048,
    THINKING_MEDIUM: 8192,
    THINKING_HIGH: 16384,
}
# 思考 token 计入 max_tokens，官方要求 max_tokens > budget_tokens，
# 预留 4096 给可见正文
THINKING_HEADROOM = 4096

# 连接阶段可重试的 HTTP 状态码与重试次数（指数退避 1s/2s）
_RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
_MAX_OPEN_ATTEMPTS = 3

# 请求携带的历史字符预算（粗略估算，超出的旧轮次不随请求发送）。
# 按 1M token 上下文的模型校准：中文最坏情况约 1 字符 ≈ 1 token，
# 80 万字符 ≈ 80 万 token，加上输出与估算误差仍留有约 20% 余量；
# 英文场景字符数远高于 token 数，实际余量更大。
# 只裁剪请求，不动本地历史存档；按完整轮次边界裁剪，保证 tool_use/tool_result 配对完整
CONTEXT_CHAR_LIMIT = 800_000

# 上下文档位（设置界面可选）→ 请求携带的历史字符预算。
# token→字符按中文最坏情况 1:1 换算并预留输出与误差余量
CONTEXT_PRESETS = {
    "128k": 100_000,
    "200k": 160_000,
    "1m": 800_000,
}
# prompt 超长减半重试的窗口下限（低于此值不再减半，直接报错）
_MIN_CONTEXT_CHARS = 25_000

# 服务端输出上限缓存：(base_url, model) → 解析出的 max_tokens 上限。
# 避免每次请求都先用 384K 撞一次 400（agent 循环 20 轮就是 20 次浪费）
_OUTPUT_CAPS = {}


def normalize_context_preset(preset) -> str:
    """把外部传入的上下文档位归一化到合法档位（未知值回退为 1m）"""
    return preset if preset in CONTEXT_PRESETS else "1m"


def _retry_delay(http_error, default_delay: float) -> float:
    """重试等待秒数：优先用服务端的 Retry-After，夹在 [1, 30] 区间"""
    try:
        wait = float(http_error.headers.get("Retry-After") or default_delay)
    except (TypeError, ValueError):
        wait = default_delay
    return min(max(wait, 1.0), 30.0)


# 服务端输出上限的报错措辞（按优先级排列；避免裸 "maximum" —— 会把
# "maximum context length is 128000" 的上下文长度误当输出上限）
_OUTPUT_CAP_PATTERNS = (
    r"max_tokens[^>]{0,60}>\s*(\d+)",   # Anthropic: max_tokens: 393216 > 64000
    r"(?:at most|less than or equal to|no more than|up to|maximum allowed|maximum of)"
    r"[^\d]{0,40}?(\d+)",
)


def _parse_output_cap(error, sent_limit):
    """从 400 错误消息中解析服务端允许的最大输出 token 数。

    仅当消息明确指向输出上限超限、且解析值落在 [1024, 我方发送值) 区间时
    返回该上限，否则返回 None（不是超限错误或解析不可信，不触发收紧重试）。"""
    message = str(error)
    lowered = message.lower()
    if sent_limit is None or not ("max_tokens" in lowered or "output token" in lowered):
        return None
    for pattern in _OUTPUT_CAP_PATTERNS:
        match = re.search(pattern, message, re.IGNORECASE)
        if not match:
            continue
        cap = int(match.group(1))
        if 1024 <= cap < sent_limit:
            return cap
        break  # 命中但数值不可信（过小/不小于发送值），不再尝试后续措辞
    return None


def _prompt_too_long(error) -> bool:
    """判断错误是否为 prompt/上下文超长（此时可减半上下文窗口重试）"""
    message = str(error).lower()
    return any(kw in message for kw in ("prompt too long", "prompt is too long",
                                        "context length", "maximum context",
                                        "input length and", "reduce the length"))


def estimate_message_chars(msg: dict) -> int:
    """粗略估算一条消息占用的上下文字符数（图片按固定开销计，base64 不计入）"""
    content = msg.get("content")
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    total = 0
    for block in content:
        btype = block.get("type")
        if btype == "text":
            total += len(block.get("text", ""))
        elif btype == "thinking":
            total += len(block.get("thinking", ""))
        elif btype == "image":
            total += 2000  # 图片按约 2k token 的固定开销估算
        elif btype == "tool_use":
            total += len(json.dumps(block.get("input") or {}, ensure_ascii=False))
        elif btype == "tool_result":
            total += len(str(block.get("content", "")))
    return total


def _is_tool_result_message(msg: dict) -> bool:
    """user 消息是否仅由 tool_result 组成（agent 工具回传，不是新轮次的开始）"""
    content = msg.get("content")
    return (msg.get("role") == "user" and isinstance(content, list) and bool(content)
            and all(b.get("type") == "tool_result" for b in content))


def window_messages(messages: list, char_limit=None) -> list:
    """请求级历史窗口：总估算超预算时丢弃最旧的完整轮次，只在安全边界切分。

    char_limit 为空时使用模块默认预算 CONTEXT_CHAR_LIMIT（1M 档）。

    规则：
    - 首条任务 user 消息永远保留（任务锚点）；
    - 多任务轮：从旧到新丢弃完整任务轮（user 任务消息及其全部后续消息），
      但最新一轮始终保留；
    - 单任务长 agent 历史：从旧到新丢弃完整工具交换对
      （assistant(tool_use) + 紧随的 tool_result user）；
    - tool_use/tool_result 配对永不被拆散，裁剪结果始终是合法的请求历史。
    只影响请求，不改动本地历史存档。
    """
    total = sum(estimate_message_chars(m) for m in messages)
    if total <= (char_limit or CONTEXT_CHAR_LIMIT):
        return messages

    n = len(messages)
    starts = [i for i, m in enumerate(messages)
              if m.get("role") == "user" and not _is_tool_result_message(m)]

    if len(starts) >= 2:
        # 多任务轮：第一轮完整保留 + 从新到旧收集轮次直到预算用尽
        first_end = starts[1]
        kept = list(range(first_end))
        kept_chars = sum(estimate_message_chars(m) for m in messages[:first_end])
        recent = []
        for pos in range(len(starts) - 1, 0, -1):
            s = starts[pos]
            e = starts[pos + 1] if pos + 1 < len(starts) else n
            chars = sum(estimate_message_chars(m) for m in messages[s:e])
            if kept_chars + chars > (char_limit or CONTEXT_CHAR_LIMIT) and recent:
                break
            recent.append((s, e))
            kept_chars += chars
        if not recent:
            return messages
        for s, e in reversed(recent):
            kept.extend(range(s, e))
        return [messages[i] for i in kept]

    # 单任务长 agent 历史：从旧到新丢弃完整工具交换对，
    # 至少保留任务锚点（首条 user）与最后的回答
    anchor_kept = bool(starts) and starts[0] == 0
    i = 1 if anchor_kept else 0
    while i < n and total > (char_limit or CONTEXT_CHAR_LIMIT):
        if (messages[i].get("role") == "assistant"
                and isinstance(messages[i].get("content"), list)
                and any(b.get("type") == "tool_use" for b in messages[i]["content"])
                and i + 1 < n and _is_tool_result_message(messages[i + 1])
                and i + 2 <= n - 1):  # 交换对之后还要留有回答
            total -= sum(estimate_message_chars(m) for m in messages[i:i + 2])
            i += 2
        else:
            break
    if i == (1 if anchor_kept else 0):
        return messages  # 无可安全丢弃的交换对
    kept = ([messages[0]] if anchor_kept else []) + messages[i:]
    return kept


def normalize_thinking_effort(effort) -> str:
    """把外部传入的思考强度归一化到合法档位（未知值回退为 off）"""
    return effort if effort in THINKING_LEVELS else THINKING_OFF


class ApiError(Exception):
    """API 请求失败（网络 / HTTP / 协议错误）"""


def make_client(fmt: str, api_key: str, base_url: str, model: str,
                thinking_effort: str = THINKING_OFF, context_chars=None):
    """按格式创建对应客户端"""
    if fmt == ANTHROPIC:
        return AnthropicClient(api_key, base_url, model, thinking_effort, context_chars)
    if fmt == RESPONSES:
        return ResponsesClient(api_key, base_url, model, thinking_effort, context_chars)
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
    # 流结束（或 [DONE] 前直接断开）时冲刷残留缓冲：
    # 部分网关的末尾事件不带空行结尾，不冲刷会丢事件
    if data_lines:
        yield event_name, "\n".join(data_lines)


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
        elif btype == "thinking":
            # Responses 的推理状态由服务端管理，历史中的思考块不回传
            continue
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
        if itype == "reasoning":
            parts = []
            for part in item.get("summary") or []:
                if part.get("type") in ("summary_text", "text") and part.get("text"):
                    parts.append(part["text"])
            for part in item.get("content") or []:
                # 少数网关会暴露原始思考文本（reasoning_text）
                if part.get("type") in ("reasoning_text", "text") and part.get("text"):
                    parts.append(part["text"])
            if parts:
                blocks.append({"type": "thinking", "thinking": "\n\n".join(parts)})
        elif itype == "message":
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
        elif itype in ("image", "tool_use", "tool_result",
                       "thinking", "redacted_thinking"):
            blocks.append(item)
    return {"role": role, "content": blocks}


class _BaseClient:
    def __init__(self, api_key: str, base_url: str, model: str,
                 thinking_effort: str = THINKING_OFF, context_chars=None):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "").strip()
        self.model = (model or "").strip()
        self.thinking_effort = normalize_thinking_effort(thinking_effort)
        # 请求级上下文预算（字符）；None 时用模块默认档（1M）
        self.context_chars = context_chars

    def stream(self, messages, system, tools=None, on_text=None, is_cancelled=None,
               on_thinking=None):
        """发起一次流式请求。

        on_text: 正文文本增量回调 on_text(str)
        on_thinking: 思考过程增量回调 on_thinking(str)（独立于正文，避免污染）
        is_cancelled: 可选的取消检查回调，返回 True 时尽快中止
        返回: {"blocks": [内部内容块], "stop_reason": str}
        """
        raise NotImplementedError

    def _interruptible_sleep(self, seconds: float, is_cancelled):
        """可被取消打断的退避等待（100ms 粒度轮询取消标志，替代不可中断的 sleep）"""
        if is_cancelled is None:
            time.sleep(seconds)
            return
        deadline = time.time() + seconds
        while not is_cancelled():
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    def _open_stream(self, url, payload, headers, is_cancelled=None):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        # 连接阶段（尚未收到任何流增量）对 429/5xx/网络闪断做指数退避重试，
        # 此时重试不会产生重复输出；流中途失败无法安全续传，仍直接抛出
        delay = 1.0
        for attempt in range(_MAX_OPEN_ATTEMPTS):
            if is_cancelled is not None and is_cancelled():
                raise ApiError("已取消")
            try:
                return urllib.request.urlopen(req, timeout=STREAM_TIMEOUT)
            except urllib.error.HTTPError as e:
                if e.code in _RETRYABLE_HTTP_CODES and attempt < _MAX_OPEN_ATTEMPTS - 1:
                    wait = _retry_delay(e, delay)
                    print(f"请求失败（HTTP {e.code}），{wait:.0f}s 后重试: {url}")
                    self._interruptible_sleep(wait, is_cancelled)
                    delay *= 2
                    continue
                raise ApiError(_http_error_message(e)) from e
            except urllib.error.URLError as e:
                if attempt < _MAX_OPEN_ATTEMPTS - 1:
                    self._interruptible_sleep(delay, is_cancelled)
                    delay *= 2
                    continue
                raise ApiError(f"连接失败: {e.reason}") from e
            except OSError as e:
                if attempt < _MAX_OPEN_ATTEMPTS - 1:
                    self._interruptible_sleep(delay, is_cancelled)
                    delay *= 2
                    continue
                raise ApiError(f"网络错误: {e}") from e

    # 服务端拒绝思考参数的错误特征。注意不含 "max_tokens"：
    # 输出上限类错误由 _parse_output_cap 专门处理（含数值解析与缓存），
    # 思考类错误必须是明确的思考措辞，避免把 max_tokens 报错误当思考问题
    _THINKING_ERROR_KEYWORDS = ("thinking", "reasoning", "budget", "effort")

    def _thinking_rejected(self, error: ApiError) -> bool:
        """判断错误是否因思考参数被拒（此时可去掉参数降级重试一次）"""
        if self.thinking_effort == THINKING_OFF:
            return False
        message = str(error).lower()
        return any(kw in message for kw in self._THINKING_ERROR_KEYWORDS)

    def _open_with_degrade(self, url, build_payload, headers, is_cancelled=None):
        """建立流连接；服务端拒绝思考参数 / 输出超限 / prompt 超长时自动修正后重试。

        修正可串联（最多 4 次尝试）：
        - max_tokens 超限 → 按服务端上报的上限收紧（按 (base_url, model) 缓存，
          后续请求直接带上，不再白挨 400）；
        - 思考参数被拒 → 去掉思考参数；
        - prompt 超长 → 上下文窗口减半。
        修正次数耗尽时原样抛出最后一次的真实错误。"""
        cache_key = (self.base_url, self.model)
        with_thinking = True
        output_limit = _OUTPUT_CAPS.get(cache_key)
        context_chars = None
        last_error = None
        for _ in range(4):
            payload = build_payload(with_thinking, output_limit, context_chars)
            try:
                return self._open_stream(url, payload, headers, is_cancelled)
            except ApiError as e:
                last_error = e
                cap = _parse_output_cap(e, payload.get("max_tokens"))
                if cap is not None:
                    if cap == output_limit:
                        raise  # 已按该上限重试过仍报超限
                    print(f"max_tokens 超出模型输出上限，按 {cap} 收紧重试: {e}")
                    output_limit = cap
                    _OUTPUT_CAPS[cache_key] = cap
                    continue
                if self._thinking_rejected(e):
                    if not with_thinking:
                        raise  # 已去掉思考参数仍被拒
                    print(f"思考参数被服务端拒绝，已自动降级重试: {e}")
                    with_thinking = False
                    continue
                if _prompt_too_long(e):
                    base_chars = context_chars or self.context_chars or CONTEXT_CHAR_LIMIT
                    halved = base_chars // 2
                    # 大预算减到下限为止；小预算（特殊端点/测试）不受下限约束
                    if base_chars > _MIN_CONTEXT_CHARS and halved < _MIN_CONTEXT_CHARS:
                        raise  # 已到窗口下限
                    if halved >= base_chars:
                        raise
                    print(f"prompt 超长，上下文窗口减半至 {halved} 字符重试: {e}")
                    context_chars = halved
                    continue
                raise
        raise last_error  # 原样透传最后一次的真实错误，不用笼统消息覆盖

    @staticmethod
    def _emit_text(on_text, delta, is_cancelled):
        if on_text and delta and not (is_cancelled and is_cancelled()):
            on_text(delta)


class AnthropicClient(_BaseClient):
    format_name = ANTHROPIC

    def endpoint(self) -> str:
        return join_url(self.base_url, "/v1/messages")

    @staticmethod
    def _strip_thinking(msg: dict) -> dict:
        """关闭思考时从历史消息中剥离思考块（Anthropic 会拒绝未开启思考却带思考块的请求）"""
        content = msg.get("content")
        if isinstance(content, list) and any(
                b.get("type") in ("thinking", "redacted_thinking") for b in content):
            kept = [b for b in content
                    if b.get("type") not in ("thinking", "redacted_thinking")]
            if not kept:
                # 思考阶段即被 max_tokens 截断的回合剥完会剩空 content，
                # 官方端点拒绝空数组，用占位文本块保住消息结构
                kept = [{"type": "text", "text": "[thinking omitted]"}]
            msg = dict(msg)
            msg["content"] = kept
        return msg

    def _build_payload(self, messages, system, tools, with_thinking,
                       output_limit=None, context_chars=None):
        max_tokens = MAX_TOKENS
        thinking_on = with_thinking and self.thinking_effort != THINKING_OFF
        if thinking_on:
            # 思考 token 计入 max_tokens，需同步抬高上限
            max_tokens = max(max_tokens,
                             ANTHROPIC_BUDGETS[self.thinking_effort] + THINKING_HEADROOM)
        if output_limit is not None and output_limit < max_tokens:
            max_tokens = output_limit
            # 上限装不下思考预算（max_tokens 必须大于 budget_tokens）时连思考一起去掉
            if thinking_on and output_limit < (
                    ANTHROPIC_BUDGETS[self.thinking_effort] + THINKING_HEADROOM):
                thinking_on = False
        # 上下文窗口裁剪对两个分支都生效（此前 else 分支用全量覆盖导致默认档失效）
        budget = context_chars if context_chars is not None else self.context_chars
        windowed = [dict(m) for m in window_messages(messages, budget)]
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system or "",
            "messages": windowed,
            "stream": True,
        }
        if thinking_on:
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": ANTHROPIC_BUDGETS[self.thinking_effort],
            }
        else:
            payload["messages"] = [self._strip_thinking(m) for m in windowed]
        if tools:
            payload["tools"] = tools
        return payload

    def stream(self, messages, system, tools=None, on_text=None, is_cancelled=None,
               on_thinking=None):
        # 官方 Anthropic 用 x-api-key；OpenRouter 等网关用 Bearer Token
        if self.api_key.startswith("sk-or-"):
            auth = {"Authorization": f"Bearer {self.api_key}"}
        else:
            auth = {"x-api-key": self.api_key}
        resp = self._open_with_degrade(
            self.endpoint(),
            lambda with_thinking, output_limit=None, context_chars=None:
                self._build_payload(messages, system, tools, with_thinking,
                                    output_limit, context_chars),
            {**auth, "anthropic-version": "2023-06-01"},
            is_cancelled,
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
                        elif dtype == "thinking_delta":
                            thinking = delta.get("thinking", "")
                            blocks_by_index[idx]["thinking"] = (
                                blocks_by_index[idx].get("thinking", "") + thinking
                            )
                            self._emit_text(on_thinking, thinking, is_cancelled)
                        elif dtype == "signature_delta":
                            # 签名必须完整保留：agent 回传历史时官方端点会校验
                            blocks_by_index[idx]["signature"] = (
                                blocks_by_index[idx].get("signature", "")
                                + delta.get("signature", "")
                            )
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
            if block.get("type") == "thinking" and not (block.get("thinking") or "").strip():
                continue
            blocks.append(block)
        return {"blocks": blocks, "stop_reason": stop_reason}


class ResponsesClient(_BaseClient):
    format_name = RESPONSES

    def endpoint(self) -> str:
        return join_url(self.base_url, "/responses")

    def _build_payload(self, messages, system, tools, with_thinking,
                       output_limit=None, context_chars=None):
        # output_limit 仅用于 Anthropic 侧的输出上限收紧；Responses 不发送
        # 输出上限参数（由服务端取模型默认值），此参数仅为签名兼容而保留
        budget = context_chars if context_chars is not None else self.context_chars
        items = []
        for msg in window_messages(messages, budget):
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
        if with_thinking and self.thinking_effort != THINKING_OFF:
            payload["reasoning"] = {"effort": self.thinking_effort}
        return payload

    def stream(self, messages, system, tools=None, on_text=None, is_cancelled=None,
               on_thinking=None):
        resp = self._open_with_degrade(
            self.endpoint(),
            lambda with_thinking, output_limit=None, context_chars=None:
                self._build_payload(messages, system, tools, with_thinking,
                                    output_limit, context_chars),
            {"Authorization": f"Bearer {self.api_key}"},
            is_cancelled,
        )

        final_output = None
        error_message = None
        done_items = []  # 逐项到达的 output_item.done，作为无 response.completed 网关的兜底
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
                    elif etype in ("response.reasoning_summary_text.delta",
                                   "response.reasoning_text.delta"):
                        self._emit_text(on_thinking, obj.get("delta", ""), is_cancelled)
                    elif etype == "response.output_item.done":
                        item = obj.get("item")
                        if item is not None:
                            done_items.append(item)
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
            if not done_items:
                raise ApiError("响应流提前结束（未收到 response.completed）")
            # 部分兼容网关只逐项发 output_item.done 就结束流
            final_output = done_items

        blocks = responses_output_to_blocks(final_output)
        return {"blocks": blocks, "stop_reason": "end_turn"}
