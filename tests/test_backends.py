from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from localforge.backends.ollama import OllamaBackend
from localforge.config import BackendConfig
from localforge.models import Message, Role


class BackendTests(unittest.TestCase):
    def test_ollama_requests_configured_context_window(self) -> None:
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"message": {"content": "done"}}
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.post.return_value = response

        with patch("localforge.backends.ollama.httpx.Client", return_value=client):
            backend = OllamaBackend(
                BackendConfig(
                    model="qwen2.5-coder:14b",
                    max_tokens=2048,
                    context_window_tokens=32768,
                )
            )
            result = backend.generate([Message(Role.USER, "hello")])

        self.assertEqual(result, "done")
        payload = client.post.call_args.kwargs["json"]
        self.assertEqual(payload["options"]["num_predict"], 2048)
        self.assertEqual(payload["options"]["num_ctx"], 32768)


if __name__ == "__main__":
    unittest.main()
