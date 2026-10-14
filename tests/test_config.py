"""Environment-driven provider settings."""

import unittest
from unittest.mock import patch

from joy.config import CHAT, DEFAULT_MODEL, DEFAULT_TIMEOUT, RESPONSES, Settings


class ConfigTest(unittest.TestCase):
    def test_defaults_to_openai_responses(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.api, RESPONSES)
        self.assertEqual(settings.model, DEFAULT_MODEL)
        self.assertEqual(settings.api_key, "sk-test")
        self.assertIsNone(settings.base_url)
        self.assertEqual(settings.client_kwargs()["api_key"], "sk-test")

    def test_joy_vars_override_openai_vars(self):
        environment = {
            "OPENAI_API_KEY": "sk-openai",
            "JOY_API_KEY": "sk-9router",
            "JOY_API": "chat",
            "JOY_MODEL": "cc/claude-opus-4-7",
            "JOY_BASE_URL": "http://localhost:20128/v1",
        }
        with patch.dict("os.environ", environment, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.api, CHAT)
        self.assertEqual(settings.model, "cc/claude-opus-4-7")
        self.assertEqual(settings.api_key, "sk-9router")
        self.assertEqual(settings.base_url, "http://localhost:20128/v1")
        self.assertFalse(settings.is_responses)
        self.assertEqual(settings.client_kwargs()["base_url"], "http://localhost:20128/v1")

    def test_api_aliases(self):
        for value in ("chat", "chat_completions", "CHAT-COMPLETIONS"):
            with patch.dict("os.environ", {"JOY_API": value}, clear=True):
                self.assertEqual(Settings.from_env().api, CHAT)

    def test_invalid_api_kind(self):
        with patch.dict("os.environ", {"JOY_API": "soap"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_timeout_is_configurable(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(Settings.from_env().client_kwargs()["timeout"], DEFAULT_TIMEOUT)
        with patch.dict("os.environ", {"JOY_TIMEOUT": "45"}, clear=True):
            self.assertEqual(Settings.from_env().client_kwargs()["timeout"], 45.0)

    def test_invalid_timeout(self):
        for value in ("soon", "0", "-5"):
            with patch.dict("os.environ", {"JOY_TIMEOUT": value}, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_env()

    def test_missing_key_uses_placeholder_for_client(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.api_key, "")
        self.assertEqual(settings.client_kwargs()["api_key"], "not-needed")
