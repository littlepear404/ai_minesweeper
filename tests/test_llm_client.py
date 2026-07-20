"""Unit tests for llm_client init, chunk detection, and stream parsing.

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

    def test_openai_usage_chunk_captured(self):
        # stream_options.include_usage -> final chunk carries usage, no delta.
        events = [
            {"choices": [{"delta": {"content": "hi"}}]},
            {"choices": [], "usage": {"prompt_tokens": 123,
                                      "completion_tokens": 45,
                                      "total_tokens": 168}},
        ]
        chunks, final = self._fake_stream(events)
        self.assertEqual(final["usage"],
                         {"input_tokens": 123, "output_tokens": 45})

    def test_no_usage_yields_none(self):
        events = [{"choices": [{"delta": {"content": "hi"}}]}]
        chunks, final = self._fake_stream(events)
        self.assertIsNone(final["usage"])


class TestAnthropicStreamParse(unittest.TestCase):
    def _fake_stream(self, events):
        c = make_client(provider="anthropic")
        with mock.patch.object(c, "_post_stream", return_value=iter(events)):
            final = None
            for kind, val in c.call_stateless_stream("sys", "board"):
                if kind == "final":
                    final = val
        return final

    def test_usage_from_message_events(self):
        events = [
            {"type": "message_start",
             "message": {"usage": {"input_tokens": 200, "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "go"}},
            {"type": "message_delta",
             "usage": {"output_tokens": 30}},
            {"type": "message_stop"},
        ]
        final = self._fake_stream(events)
        self.assertEqual(final["usage"],
                         {"input_tokens": 200, "output_tokens": 30})


class TestRequestBodies(unittest.TestCase):
    def _capture_body(self, client):
        sent = {}

        def fake_post(url, headers, body):
            sent["body"] = body
            return iter([])

        with mock.patch.object(client, "_post_stream", side_effect=fake_post):
            for _ in client.call_stateless_stream("sys", "board"):
                pass
        return sent["body"]

    def test_openai_body_stream_options_and_reasoning_effort(self):
        c = make_client(reasoning_effort="low")
        body = self._capture_body(c)
        self.assertTrue(body["stream"])
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertEqual(body["reasoning_effort"], "low")

    def test_openai_body_omits_reasoning_effort_by_default(self):
        body = self._capture_body(make_client())
        self.assertNotIn("reasoning_effort", body)

    def test_anthropic_body_marks_prompt_cache(self):
        body = self._capture_body(make_client(provider="anthropic"))
        self.assertEqual(body["system"][0]["cache_control"],
                         {"type": "ephemeral"})
        self.assertEqual(body["tools"][-1]["cache_control"],
                         {"type": "ephemeral"})


if __name__ == "__main__":
    unittest.main()
