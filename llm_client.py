"""LLM client supporting OpenAI-compatible and Anthropic message formats with
tool calling. Conversation history is kept in each provider's native format.
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
        self.system_prompt = ""
        self.history = []  # native-format message list
        if self.provider not in ("openai", "anthropic"):
            raise LLMError(f"不支持的 provider: {self.provider}")
        if not self.api_base_url:
            raise LLMError("api_base_url 未配置")
        if not self.api_key or self.api_key == "YOUR_API_KEY_HERE":
            raise LLMError("api_key 未配置 (请在 llm_config.json 中填入你的密钥)")

    def reset(self, system_prompt):
        self.system_prompt = system_prompt
        if self.provider == "anthropic":
            self.history = []
        else:
            self.history = [{"role": "system", "content": system_prompt}]

    # ---- main entry: do one model call with current history ----
    def turn(self, user_text):
        """Append a user message, call the model, return assistant content + tool calls.

        Returns dict:
          thinking: str  (reasoning text to display)
          tool_calls: list of {"id","name","args":dict}
          has_action: bool
        The assistant message is appended to history by the caller? No:
        we append the assistant message inside, tool results added later by
        add_tool_result.
        """
        if self.provider == "openai":
            return self._turn_openai(user_text)
        return self._turn_anthropic(user_text)

    def turn_stream(self, user_text):
        """Streaming version of turn(). Generator yielding incremental
        thinking text then a final result.

        Yields tuples:
          ("chunk", text_fragment)  -- incremental thinking text
          ("final", result_dict)    -- result_dict == turn()'s return
        """
        if self.provider == "openai":
            yield from self._turn_openai_stream(user_text)
        else:
            yield from self._turn_anthropic_stream(user_text)

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
        return body

    def _turn_openai(self, user_text):
        self.history.append({"role": "user", "content": user_text})
        body = self._build_openai_body(self.history)
        data = self._post(f"{self.api_base_url}/chat/completions",
                          headers={"Authorization": f"Bearer {self.api_key}"},
                          body=body)
        msg = data["choices"][0]["message"]
        thinking_parts = []
        if msg.get("reasoning_content"):
            thinking_parts.append(msg["reasoning_content"])
        if msg.get("content"):
            thinking_parts.append(msg["content"])
        thinking = "\n".join(thinking_parts).strip()

        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "{}") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({"id": tc.get("id"), "name": fn.get("name"), "args": args})

        # Append an assistant message that remains valid for strict
        # OpenAI-compatible servers such as DeepSeek. Reasoning models can
        # return only reasoning_content and no tool call; replaying that as an
        # empty assistant message makes the next request fail with:
        # "content or tool_calls must be set".
        assistant_msg = self._build_openai_assistant_message(
            content=msg.get("content"),
            reasoning=msg.get("reasoning_content"),
            tool_calls=msg.get("tool_calls"),
        )
        self.history.append(assistant_msg)
        return {"thinking": thinking, "tool_calls": tool_calls}

    def _turn_openai_stream(self, user_text):
        self.history.append({"role": "user", "content": user_text})
        body = self._build_openai_body(self.history, stream=True)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        resp = self._post_stream(f"{self.api_base_url}/chat/completions",
                                 headers=headers, body=body)
        content_parts = []
        reasoning_parts = []
        tool_calls_acc = {}  # index -> dict
        for evt in resp:
            if evt.get("object") == "chat.completion.chunk":
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
        raw_tcs = []
        for idx in sorted(tool_calls_acc.keys()):
            slot = tool_calls_acc[idx]
            try:
                args = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            built_tcs.append({"id": slot["id"], "name": slot["name"], "args": args})
            raw_tcs.append({
                "id": slot["id"], "type": "function",
                "function": {"name": slot["name"], "arguments": slot["arguments"]},
            })
        assistant_msg = self._build_openai_assistant_message(
            content=content,
            reasoning=reasoning,
            tool_calls=raw_tcs,
        )
        self.history.append(assistant_msg)
        yield ("final", {"thinking": thinking, "tool_calls": built_tcs})

    @staticmethod
    def _build_openai_assistant_message(content=None, reasoning=None, tool_calls=None):
        assistant_msg = {"role": "assistant"}
        if content:
            assistant_msg["content"] = content
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        if "content" not in assistant_msg and "tool_calls" not in assistant_msg:
            fallback = (reasoning or "").strip()
            assistant_msg["content"] = fallback or "(no assistant content)"
        return assistant_msg

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

    # ---------- Anthropic format ----------
    def _turn_anthropic(self, user_text):
        self.history.append({"role": "user", "content": user_text})
        body = {
            "model": self.model,
            "system": self.system_prompt,
            "messages": self.history,
            "tools": build_anthropic_tools(),
            "tool_choice": {"type": "any" if self.tool_choice == "required" else "auto"},
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        data = self._post(
            f"{self.api_base_url}/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            body=body,
        )
        content_blocks = data.get("content", [])
        thinking_parts = []
        tool_calls = []
        for block in content_blocks:
            if block.get("type") == "text":
                thinking_parts.append(block.get("text", ""))
            elif block.get("type") == "thinking":
                thinking_parts.append(block.get("thinking", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "args": block.get("input", {}) or {},
                })
        thinking = "\n".join(p for p in thinking_parts if p).strip()
        # append assistant message (raw content list keeps tool_use ids intact)
        self.history.append({"role": "assistant", "content": content_blocks})
        return {"thinking": thinking, "tool_calls": tool_calls}

    def _turn_anthropic_stream(self, user_text):
        self.history.append({"role": "user", "content": user_text})
        body = {
            "model": self.model,
            "system": self.system_prompt,
            "messages": self.history,
            "tools": build_anthropic_tools(),
            "tool_choice": {"type": "any" if self.tool_choice == "required" else "auto"},
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        resp = self._post_stream(
            f"{self.api_base_url}/messages",
            headers={"x-api-key": self.api_key,
                     "anthropic-version": "2023-06-01"},
            body=body,
        )
        content_blocks = []
        thinking_parts = []
        tool_calls = []
        for evt in resp:
            etype = evt.get("type", "")
            if etype == "content_block_start":
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
        self.history.append({"role": "assistant", "content": content_blocks})
        yield ("final", {"thinking": thinking, "tool_calls": built_tcs})

    # ---------- record a tool result ----------
    def add_tool_result(self, tool_call_id, name, result_text):
        if self.provider == "openai":
            self.history.append({
                "role": "tool",
                "tool_call_id": tool_call_id or "",
                "name": name,
                "content": result_text,
            })
        else:
            self.history.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call_id or "",
                        "content": result_text,
                    }
                ],
            })

    def trim_history(self, keep_turns):
        """Keep the first system/opening message(s) and the last keep_turns turn-pairs."""
        if keep_turns is None or keep_turns <= 0:
            return
        if self.provider == "openai":
            system = [self.history[0]] if self.history and self.history[0]["role"] == "system" else []
            rest = self.history[len(system):]
        else:
            system = []
            rest = self.history[:]
        # keep the last keep_turns*2 messages (each turn = user+assistant(+tool result))
        window = rest[-max(keep_turns * 2, 6):]
        if self.provider == "openai":
            while window and window[0].get("role") == "tool":
                window = window[1:]
        self.history = system + window

    # ---------- low level ----------
    def _post(self, url, headers, body):
        headers = dict(headers)
        headers.setdefault("content-type", "application/json")
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=self.timeout)
        except requests.RequestException as e:
            raise LLMError(f"网络请求失败: {e}") from e
        if resp.status_code >= 400:
            raise LLMError(f"API 返回错误 {resp.status_code}: {resp.text[:500]}")
        try:
            return resp.json()
        except ValueError:
            raise LLMError(f"API 返回非 JSON 响应: {resp.text[:500]}")

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
