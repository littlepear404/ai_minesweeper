"""Unit tests for llm_client formatting, chunk detection, and history trimming.

Network calls are mocked locally (no real HTTP), focusing on parsing logic.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_client import LLMClient, LLMError  # noqa: E402


def make_client(**over):
    cfg = {
        "provider": "openai",
        "api_base_url": "https://api.example.com/v1",
        "api_key": "sk-test",
        "model": "test-model",
    }
    cfg.update(over)
    return LLMClient(cfg)


class TestInit(unittest.TestCase):
    def test_missing_key_raises(self):
        with self.assertRaises(LLMError):
            LLMClient({"provider": "openai", "api_base_url": "x",
                       "api_key": "YOUR_API_KEY_HERE", "model": "m"})

    def test_bad_provider_raises(self):
        with self.assertRaises(LLMError):
            LLMClient({"provider": "foo", "api_base_url": "x",
                       "api_key": "k", "model": "m"})

    def test_endpoint_normalization(self):
        c = make_client(api_base_url="https://x.com/v1/chat/completions")
        self.assertEqual(c.api_base_url, "https://x.com/v1")
        c2 = make_client(api_base_url="https://x.com/v1/messages")
        self.assertEqual(c2.api_base_url, "https://x.com/v1")


class TestAssistantMessageBuilder(unittest.TestCase):
    def test_content_only(self):
        m = LLMClient._build_openai_assistant_message(content="hi")
        self.assertEqual(m["content"], "hi")
        self.assertNotIn("tool_calls", m)

    def test_tool_calls_only(self):
        tc = [{"id": "1", "type": "function",
               "function": {"name": "reveal", "arguments": "{}"}}]
        m = LLMClient._build_openai_assistant_message(tool_calls=tc)
        self.assertEqual(m["tool_calls"], tc)
        self.assertNotIn("content", m)

    def test_empty_falls_back_to_reasoning(self):
        m = LLMClient._build_openai_assistant_message(reasoning="  ")
        self.assertEqual(m["content"], "(no assistant content)")

    def test_empty_with_reasoning_uses_it(self):
        m = LLMClient._build_openai_assistant_message(reasoning="think")
        self.assertEqual(m["content"], "think")


class TestIsOpenAIChunk(unittest.TestCase):
    def test_classic_object_field(self):
        evt = {"object": "chat.completion.chunk", "choices": [{"delta": {}}]}
        self.assertTrue(LLMClient._is_openai_chunk(evt))

    def test_structural_match_no_object(self):
        # Some compat servers omit `object` but send choices/delta.
        evt = {"choices": [{"delta": {"content": "hi"}}]}
        self.assertTrue(LLMClient._is_openai_chunk(evt))

    def test_no_choices_rejected(self):
        self.assertFalse(LLMClient._is_openai_chunk({"object": "foo"}))

    def test_non_dict_rejected(self):
        self.assertFalse(LLMClient._is_openai_chunk("data"))


class TestTrimHistory(unittest.TestCase):
    def _openai_history(self):
        # system, then pairs of user/assistant(+tool)
        h = [{"role": "system", "content": "s"}]
        for i in range(10):
            h.append({"role": "user", "content": f"u{i}"})
            h.append({"role": "assistant", "content": f"a{i}",
                      "tool_calls": [{"id": str(i), "type": "function",
                                      "function": {"name": "reveal",
                                                   "arguments": "{}"}}]})
            h.append({"role": "tool", "tool_call_id": str(i), "content": "ok"})
        return h

    def test_keeps_window(self):
        c = make_client()
        c.history = self._openai_history()
        c.trim_history(3)
        # system + last keep_turns*2=6 messages (2 turns)
        self.assertEqual(len(c.history), 1 + 3 * 2)
        self.assertEqual(c.history[0]["role"], "system")

    def test_drops_leading_tool(self):
        c = make_client()
        h = self._openai_history()
        # prepend a dangling tool with no matching assistant
        h.insert(1, {"role": "tool", "tool_call_id": "x", "content": "orphan"})
        c.history = h
        c.trim_history(10)
        self.assertNotEqual(c.history[1]["role"], "tool")

    def test_drops_leading_assistant_with_orphan_tool_calls(self):
        c = make_client()
        h = self._openai_history()
        # Replace the first retained assistant so its tool_calls have no following tool.
        orphan_assistant = {"role": "assistant", "content": "a",
                            "tool_calls": [{"id": "zz", "type": "function",
                                            "function": {"name": "reveal",
                                                         "arguments": "{}"}}]}
        h = [{"role": "system", "content": "s"}, orphan_assistant,
              {"role": "user", "content": "u"}]
        c.history = h
        c.trim_history(1)
        # The orphan assistant (with tool_calls but no tool result) must be dropped.
        self.assertNotIn("tool_calls",
                         [m for m in c.history if m.get("role") == "assistant"])


class TestStatelessStreamParse(unittest.TestCase):
    """Verify the stream parser extracts tool_calls from SSE-ish event dicts
    produced by a fake _post_stream (no network)."""

    def _fake_stream(self, events):
        c = make_client()
        with mock.patch.object(c, "_post_stream", return_value=iter(events)):
            chunks, final = [], None
            for kind, val in c.call_stateless_stream("sys", "board"):
                if kind == "chunk":
                    chunks.append(val)
                elif kind == "final":
                    final = val
        return chunks, final

    def test_parses_tool_calls(self):
        events = [
            {"object": "chat.completion.chunk",
             "choices": [{"delta": {"content": "let me"}}]},
            {"object": "chat.completion.chunk",
             "choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1",
                          "function": {"name": "reveal", "arguments": "{\"row\": 0"}}]}}]},
            {"object": "chat.completion.chunk",
             "choices": [{"delta": {"tool_calls": [{"index": 0,
                          "function": {"arguments": ", \"col\": 1}"}}]}}]},
        ]
        chunks, final = self._fake_stream(events)
        self.assertIn("let me", "".join(chunks))
        self.assertEqual(len(final["tool_calls"]), 1)
        tc = final["tool_calls"][0]
        self.assertEqual(tc["name"], "reveal")
        self.assertEqual(tc["args"], {"row": 0, "col": 1})

    def test_structural_chunk_no_object(self):
        # A compat server that omits `object` but sends choices/delta.
        events = [
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1",
                       "function": {"name": "toggle_flag",
                                    "arguments": "{\"row\":2,\"col\":3}"}}]}}]},
        ]
        chunks, final = self._fake_stream(events)
        self.assertEqual(len(final["tool_calls"]), 1)
        self.assertEqual(final["tool_calls"][0]["args"], {"row": 2, "col": 3})


if __name__ == "__main__":
    unittest.main()
