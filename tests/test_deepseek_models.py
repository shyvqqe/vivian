"""Exercise model routing and the DeepSeek API schema without network calls."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from core import ai_models, config, deepseek, groq, hub_models


class DeepSeekModelTests(unittest.TestCase):
    def test_deepseek_only_supports_automatic_text_and_images(self):
        with patch.multiple(config, GEMINI_API_KEY="", OPENROUTER_API_KEY="", DEEPSEEK_API_KEY="testing"):
            with patch.object(groq, "groq_client", None):
                self.assertEqual(ai_models.get_default_model(), deepseek.FLASH_MODEL)
                self.assertEqual(ai_models.get_vision_model(0, 1), deepseek.FLASH_MODEL)

    def test_pro_is_a_personal_choice_and_flash_handles_its_images(self):
        with patch.object(config, "DEEPSEEK_API_KEY", "testing"):
            self.addCleanup(ai_models._SELECTIONS.pop, (9876, 7654), None)
            self.addCleanup(ai_models._LOCAL_CHANGES.pop, (9876, 7654), None)
            ai_models.set_selection(9876, 7654, deepseek.PRO_MODEL)
            self.assertEqual(ai_models.get_text_model(9876, 7654), deepseek.PRO_MODEL)
            self.assertEqual(ai_models.get_vision_model(9876, 7654), deepseek.FLASH_MODEL)
            self.assertIsNone(ai_models.get_selection(9876, 7653))
            self.assertEqual(ai_models.fallback_models(9876, 7654, deepseek.PRO_MODEL), ())

    def test_text_and_png_are_sent_with_a_short_non_thinking_budget(self):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "Опиши изображение"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ]}]

        def mock_request(request):
            self.assertEqual(str(request.url), "https://api.deepseek.com/chat/completions")
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.headers["authorization"], "Bearer testing")
            self.assertEqual(json.loads(request.content), {
                "model": deepseek.FLASH_MODEL, "messages": messages, "max_tokens": 500,
                "stream": False, "thinking": {"type": "disabled"},
            })
            return httpx.Response(
                200, content=b'\n\n{"choices":[{"message":{"content":"PNG"},"finish_reason":"stop"}]}',
            )

        with httpx.Client(transport=httpx.MockTransport(mock_request)) as client:
            with patch.object(config, "DEEPSEEK_API_KEY", "testing"), patch.object(deepseek, "_CLIENT", client):
                response = ai_models.create_completion(model=deepseek.FLASH_MODEL, messages=messages,
                                                       max_completion_tokens=500, _deadline=time.monotonic() + 5)
        self.assertEqual(response.choices[0].message.content, "PNG")

    def test_retryable_error_does_not_expose_api_response(self):
        with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(429, text="raw provider error with private content")
        )) as client:
            with patch.object(config, "DEEPSEEK_API_KEY", "testing"), patch.object(deepseek, "_CLIENT", client):
                with self.assertRaises(ai_models.ModelUnavailable) as error:
                    ai_models.create_completion(model=deepseek.FLASH_MODEL,
                                                messages=[{"role": "user", "content": "Hello"}])
        self.assertTrue(error.exception.retryable)
        self.assertIn("лимит", error.exception.public_message)
        self.assertNotIn("private content", str(error.exception))

    def test_expired_budget_skips_request(self):
        with patch.object(config, "DEEPSEEK_API_KEY", "testing"), patch.object(deepseek, "_get_client") as client:
            with self.assertRaises(ai_models.ModelUnavailable) as error:
                ai_models.create_completion(model=deepseek.FLASH_MODEL, messages=[], _deadline=0)
        client.assert_not_called()
        self.assertTrue(error.exception.retryable)

    def test_keep_alive_lines_do_not_extend_generation_budget(self):
        class WaitingResponse(httpx.SyncByteStream):
            def __iter__(self):
                yield b"\n"
                yield b"\n"
                raise AssertionError("Read more body data after the deadline")

        with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=WaitingResponse())
        )) as client:
            ticks = iter([0, 1, 4])
            clock = SimpleNamespace(monotonic=lambda: next(ticks))
            with (
                patch.object(config, "DEEPSEEK_API_KEY", "testing"),
                patch.object(deepseek, "_CLIENT", client),
                patch.object(deepseek, "time", clock),
                self.assertRaises(ai_models.ModelUnavailable) as error,
            ):
                ai_models.create_completion(model=deepseek.FLASH_MODEL, messages=[], _deadline=3)
        self.assertTrue(error.exception.retryable)
        self.assertIn("долго", error.exception.public_message)

    def test_feature_provider_requests_json_for_moderation(self):
        def mock_request(request):
            data = json.loads(request.content)
            self.assertEqual(data["model"], deepseek.FLASH_MODEL)
            self.assertEqual(data["response_format"], {"type": "json_object"})
            return httpx.Response(200, json={
                "choices": [{"message": {"content": '{"category":"none"}'}, "finish_reason": "stop"}],
            })

        with httpx.Client(transport=httpx.MockTransport(mock_request)) as client:
            with patch.object(config, "DEEPSEEK_API_KEY", "testing"), patch.object(deepseek, "_CLIENT", client):
                value = hub_models._request([{"role": "user", "content": "Return JSON."}],
                                            "deepseek", 512, 0, True)
        self.assertEqual(json.loads(value), {"category": "none"})


if __name__ == "__main__":
    unittest.main()
