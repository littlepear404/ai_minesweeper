"""LLM client supporting OpenAI-compatible and Anthropic message formats with
tool calling. Stateless only: every call is built fresh from
(system_prompt, board_snapshot); no conversation history is kept client-side.
"""
import json
from urllib.parse import urlparse

import requests


# Common tool definitions (provider-agnostic). Converted per provider below.
COMMON_TOOLS = [
    {
        "name": "reveal",
        "description": (
            "翻开一个格子。若该格是雷则游戏失败(输)；若是数字格则显示周围8格中的雷数；"
            "若是空白(0)格会自动展开相邻连续的安全区域。坐标 row/col 均从0开始。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "row": {"type": "integer", "description": "行索引，从0开始"},
                "col": {"type": "integer", "description": "列索引，从0开始"},
            },
            "required": ["row", "col"],
        },
    },
    {
        "name": "toggle_flag",
        "description": (
            "切换一个未翻开格子的插旗状态(已插旗则取消，未插旗则插旗)。"
            "用于标记你认为是雷的格子，避免误翻。coordinate row/col 均从0开始。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "row": {"type": "integer", "description": "行索引，从0开始"},
                "col": {"type": "integer", "description": "列索引，从0开始"},
            },
            "required": ["row", "col"],
        },
    },
    {
        "name": "chord",
        "description": (
            "对一个已翻开的数字格执行双击(chord)。当该数字格周围已插旗数 等于 "
            "该数字时，会一次性翻开其周围所有未翻开且未插旗的格子。若你标记的"
            "雷有误，双击会踩雷导致游戏失败(与人类双击同样规则)。这能一步推进"
            "多个安全格，强烈推荐在确定雷已标对时使用。coordinate row/col 均从0开始。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "row": {"type": "integer", "description": "已翻开数字格的行索引，从0开始"},
                "col": {"type": "integer", "description": "已翻开数字格的列索引，从0开始"},
            },
            "required": ["row", "col"],
        },
    },
]


def build_openai_tools():
    return [
        {"type": "function", "function": {
            "name": t["name"], "description": t["description"],
            "parameters": t["parameters"],
        }}
        for t in COMMON_TOOLS
    ]


def build_anthropic_tools():
    return [
        {"name": t["name"], "description": t["description"],
         "input_schema": t["parameters"]}
        for t in COMMON_TOOLS
    ]


class LLMError(Exception):
    pass


def _norm_openai_usage(raw):
    """Normalize an OpenAI-style usage chunk to {input_tokens, output_tokens}.

    Returns None when no usable numbers are present.
    """
    if not isinstance(raw, dict):
        return None
    inp = raw.get("prompt_tokens")
    out = raw.get("completion_tokens")
    if inp is None and out is None:
        return None
    return {"input_tokens": inp, "output_tokens": out}


class LLMClient:
    def __init__(self, config):
        self.provider = config.get("provider", "openai").lower()
        base = config.get("api_base_url", "").rstrip("/")
        # Normalize: accept either the base (".../v1") or a full endpoint
        # (".../v1/chat/completions"/".../v1/messages"); strip the known
        # endpoint suffix so we can re-append it later without duplication.
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        elif base.endswith("/messages"):
            base = base[: -len("/messages")]
        self.api_base_url = base
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "")
        self.temperature = config.get("temperature", 0.4)
        self.max_tokens = config.get("max_tokens", 1024)
        self.timeout = config.get("request_timeout", 120)
        self.tool_choice = config.get("tool_choice", "required")
        self.omit_tool_choice = self._should_omit_tool_choice(config)
        self.tool_temperature = config.get("tool_temperature")
        if self.provider not in ("openai", "anthropic"):
            raise LLMError(f"不支持的 provider: {self.provider}")
        if not self.api_base_url:
            raise LLMError("api_base_url 未配置")
        if not self.api_key or self.api_key == "YOUR_API_KEY_HERE":
            raise LLMError("api_key 未配置 (请在 llm_config.json 中填入你的密钥)")

    def call_stateless_stream(self, system_text, board_text):
        """Streaming call with NO history involvement.

        Builds fresh messages=[system+user] for every call. Yields
        ("chunk", text_fragment) tuples for incremental thinking text and a
        single ("final", {"thinking", "tool_calls", "usage"}) tuple at the
        end. ``usage`` is {"input_tokens", "output_tokens"} or None when the
        server did not report it.
        """
        if self.provider == "openai":
            yield from self._stateless_openai_stream(system_text, board_text)
        else:
            yield from self._stateless_anthropic_stream(system_text, board_text)

    # ---------- OpenAI format ----------
    def _build_openai_body(self, messages, stream=False):
        body = {
            "model": self.model,
            "messages": messages,
            "tools": build_openai_tools(),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.tool_choice is not None and not self.omit_tool_choice:
            body["tool_choice"] = self.tool_choice
        if stream:
            body["stream"] = True
            # Ask for a final usage chunk so token costs can be tracked.
            body["stream_options"] = {"include_usage": True}
        if self.tool_temperature is not None:
            body["tool_temperature"] = self.tool_temperature
        return body

    # ---------- shared streaming parsers ----------
    # Both providers share one parser each. Each generator yields incremental
    # thinking text as ("chunk", fragment) and finishes with a single
    # ("final", {"thinking", "tool_calls", "usage"}) tuple.
    def _parse_openai_stream(self, resp):
        content_parts = []
        reasoning_parts = []
        tool_calls_acc = {}  # index -> dict
        usage = None
        for evt in resp:
            # With stream_options.include_usage the server sends a final
            # chunk with empty choices and a usage payload.
            if isinstance(evt, dict) and evt.get("usage"):
                usage = evt["usage"]
                continue
            if not self._is_openai_chunk(evt):
                continue
            choices = evt.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta", {}) or {}
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
                yield ("chunk", delta["reasoning_content"])
            if delta.get("content"):
                content_parts.append(delta["content"])
                yield ("chunk", delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function", {}) or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        thinking_parts = []
        if reasoning:
            thinking_parts.append(reasoning)
        if content:
            thinking_parts.append(content)
        thinking = "\n".join(thinking_parts).strip()
        built_tcs = []
        for idx in sorted(tool_calls_acc.keys()):
            slot = tool_calls_acc[idx]
            try:
                args = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            built_tcs.append({"id": slot["id"], "name": slot["name"], "args": args})
        yield ("final", {"thinking": thinking, "tool_calls": built_tcs,
                         "usage": _norm_openai_usage(usage)})

    def _parse_anthropic_stream(self, resp):
        content_blocks = []
        thinking_parts = []
        usage_in = None
        usage_out = None
        for evt in resp:
            etype = evt.get("type", "")
            if etype == "message_start":
                u = (evt.get("message") or {}).get("usage") or {}
                usage_in = u.get("input_tokens", usage_in)
                usage_out = u.get("output_tokens", usage_out)
            elif etype == "message_delta":
                u = evt.get("usage") or {}
                # output_tokens here is cumulative; keep the latest value.
                usage_out = u.get("output_tokens", usage_out)
            elif etype == "content_block_start":
                block = evt.get("content_block", {}) or {}
                idx = evt.get("index", len(content_blocks))
                while len(content_blocks) <= idx:
                    content_blocks.append(dict(block))
            elif etype == "content_block_delta":
                idx = evt.get("index", 0)
                delta = evt.get("delta", {}) or {}
                block = content_blocks[idx] if idx < len(content_blocks) else {}
                dt = delta.get("type", "")
                if dt == "text_delta":
                    txt = delta.get("text", "")
                    block["text"] = block.get("text", "") + txt
                    thinking_parts.append(txt)
                    yield ("chunk", txt)
                elif dt == "thinking_delta":
                    txt = delta.get("thinking", "")
                    block["thinking"] = block.get("thinking", "") + txt
                    thinking_parts.append(txt)
                    yield ("chunk", txt)
                elif dt == "input_json_delta":
                    block["input"] = (block.get("input", "") or "") + delta.get("partial_json", "")
                elif dt == "tool_use":
                    # tools normally arrive via content_block_start/stop
                    pass
            elif etype == "message_stop":
                break
        built_tcs = []
        for block in content_blocks:
            if block.get("type") == "tool_use":
                raw = block.get("input", "")
                try:
                    args = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    args = {}
                block["input"] = args
                built_tcs.append({
                    "id": block.get("id"), "name": block.get("name"), "args": args,
                })
        thinking = "\n".join(p for p in thinking_parts if p).strip()
        usage = None
        if usage_in is not None or usage_out is not None:
            usage = {"input_tokens": usage_in, "output_tokens": usage_out}
        yield ("final", {"thinking": thinking, "tool_calls": built_tcs,
                         "usage": usage})

    def _should_omit_tool_choice(self, config):
        """Compatibility switch for OpenAI-compatible servers.

        DeepSeek official thinking/reasoning mode rejects the OpenAI
        `tool_choice` parameter while still accepting `tools`. Keep the default
        behavior for other compatible APIs, and allow config to override either
        direction when a proxy or model differs from the official behavior.
        """
        override = config.get("omit_tool_choice")
        if override is not None:
            return bool(override)
        if self.provider != "openai":
            return False
        host = (urlparse(self.api_base_url).hostname or "").lower()
        model = (self.model or "").lower()
        is_deepseek_official = host == "api.deepseek.com" or host.endswith(".api.deepseek.com")
        is_deepseek_reasoning = "deepseek-reasoner" in model or "deepseek-v4" in model
        return is_deepseek_official and is_deepseek_reasoning

    @staticmethod
    def _is_openai_chunk(evt):
        """Detect an OpenAI-style streaming chunk by structure, not by the
        literal `object` field.

        Some OpenAI-compatible servers omit or rename `object`
        (e.g. do not send "chat.completion.chunk"). Gating purely on that
        string silently drops every chunk, so we instead accept any event
        that carries the expected `choices`/`delta` payload (and never an
        explicit unrelated type such as SSE comments or DONE markers).
        """
        if not isinstance(evt, dict):
            return False
        if evt.get("object") == "chat.completion.chunk":
            return True
        choices = evt.get("choices")
        if not isinstance(choices, list) or not choices:
            return False
        delta = choices[0].get("delta")
        return isinstance(delta, dict)

    # ---------- stateless provider calls ----------
    def _stateless_openai_stream(self, system_text, board_text):
        messages = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": board_text},
        ]
        body = self._build_openai_body(messages, stream=True)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        resp = self._post_stream(f"{self.api_base_url}/chat/completions",
                                 headers=headers, body=body)
        yield from self._parse_openai_stream(resp)

    def _stateless_anthropic_stream(self, system_text, board_text):
        body = {
            "model": self.model,
            "system": system_text,
            "messages": [{"role": "user", "content": board_text}],
            "tools": build_anthropic_tools(),
            "tool_choice": {"type": "any" if self.tool_choice == "required" else "auto"},
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        resp = self._post_stream(
            f"{self.api_base_url}/messages",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            body=body,
        )
        yield from self._parse_anthropic_stream(resp)

    # ---------- low level ----------
    def _post_stream(self, url, headers, body):
        """POST with stream=True; generator yielding decoded JSON event dicts.
        Each line is `data: {json}` -> parsed dict.  Empty/comments ignored.
        """
        headers = dict(headers)
        headers.setdefault("content-type", "application/json")
        try:
            resp = requests.post(url, headers=headers, json=body,
                                 timeout=self.timeout, stream=True)
        except requests.RequestException as e:
            raise LLMError(f"网络请求失败: {e}") from e
        if resp.status_code >= 400:
            data = resp.text
            raise LLMError(f"API 返回错误 {resp.status_code}: {data[:500]}")
        # SSE streams usually omit charset; requests defaults to ISO-8859-1,
        # which mojibakes CJK. Force UTF-8 (SSE is always UTF-8 per spec).
        try:
            resp.encoding = "utf-8"
        except Exception:
            pass
        try:
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if line.startswith("data:"):
                    line = line[len("data:"):].strip()
                if not line or line.startswith(":") or line == "[DONE]":
                    if line == "[DONE]":
                        return
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
        except requests.RequestException as e:
            raise LLMError(f"流式读取失败: {e}") from e
