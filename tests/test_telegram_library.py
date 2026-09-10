"""Optional real-library integration tests with entirely in-memory HTTP transport."""

from datetime import datetime, timedelta, timezone
import json
import logging
import unittest
from unittest.mock import patch

from household_agent.telegram import TelegramSender, deliver_one, make_message

try:
    import telegram
except ModuleNotFoundError as error:
    if error.name != "telegram":
        raise
    raise unittest.SkipTest("python-telegram-bot is not installed") from error

from telegram.error import NetworkError
from telegram.request import BaseRequest


class FakeRequest(BaseRequest):
    """Keep Bot's request serialization and response parsing, replacing only I/O."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.initialize_count = 0
        self.shutdown_count = 0
        self.active = False
        self.fail_get_me_once = False

    async def initialize(self):
        self.initialize_count += 1
        self.active = True

    async def shutdown(self):
        self.shutdown_count += 1
        self.active = False

    async def do_request(self, url, method, request_data=None, **timeouts):
        if not self.active:
            raise AssertionError("Request transport is not initialized")
        endpoint = url.rsplit("/", 1)[-1]
        payload = json.loads(request_data.json_payload) if request_data else {}
        self.calls.append({
            "endpoint": endpoint, "method": method,
            "payload": payload, "timeouts": timeouts,
        })
        user = {
            "id": 123, "is_bot": True,
            "first_name": "Test", "username": "household_test_bot",
        }
        if endpoint == "getMe":
            if self.fail_get_me_once:
                self.fail_get_me_once = False
                raise NetworkError("Stubbed initialization failure")
            result = user
        elif endpoint == "sendMessage":
            result = {
                "message_id": len(self.calls), "date": 1789016400,
                "from": user,
                "chat": {"id": int(payload["chat_id"]), "type": "supergroup"},
                "text": payload["text"],
            }
        else:
            raise AssertionError(f"Unexpected Telegram endpoint: {endpoint}")
        return 200, json.dumps({"ok": True, "result": result}).encode("utf-8")


class TelegramLibraryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name in ("httpx", "httpcore"):
            logger = logging.getLogger(name)
            self.addCleanup(logger.setLevel, logger.level)
        self.request = FakeRequest()
        self.updates_request = FakeRequest()
        # Supply both transports so Bot never creates a real HTTP client.
        self.bot = telegram.Bot(
            token="123:TEST", request=self.request,
            get_updates_request=self.updates_request,
        )
        factory_patch = patch("telegram.Bot", return_value=self.bot)
        self.factory = factory_patch.start()
        self.addCleanup(factory_patch.stop)

    async def test_lazy_context_real_get_me_plaintext_serialization_and_shutdown(self):
        adapter = TelegramSender("123:TEST", "-1234")
        self.factory.assert_not_called()
        texts = ["<name> & _task_ \u00e4\U0001f600\nSecond line", "Next message"]

        async with adapter as sender:
            self.assertIs(sender, adapter)
            self.factory.assert_called_once_with(token="123:TEST")
            self.assertEqual(self.request.calls, [])
            self.assertEqual(self.request.initialize_count, 0)
            self.assertEqual(self.updates_request.initialize_count, 0)
            for text in texts:
                await sender.send(text)
            self.assertEqual(self.bot.id, 123)
            self.assertEqual(self.bot.username, "household_test_bot")
            self.assertEqual(
                [call["endpoint"] for call in self.request.calls],
                ["getMe", "sendMessage", "sendMessage"],
            )
            self.assertEqual(self.request.calls[0]["payload"], {})
            for call, text in zip(self.request.calls[1:], texts):
                self.assertEqual(call["method"], "POST")
                self.assertEqual(call["payload"], {"chat_id": "-1234", "text": text})
                # PTB removes None parameters rather than sending JSON null.
                self.assertNotIn("parse_mode", call["payload"])
                self.assertEqual(call["timeouts"], {
                    "connect_timeout": 10, "read_timeout": 30,
                    "write_timeout": 30, "pool_timeout": 10,
                })
            for request in (self.request, self.updates_request):
                self.assertEqual(request.initialize_count, 1)
                self.assertEqual(request.shutdown_count, 0)

        self.assertEqual(self.request.calls[0]["method"], "POST")
        self.assertEqual(self.updates_request.calls, [])
        for request in (self.request, self.updates_request):
            self.assertEqual(request.shutdown_count, 1)
            self.assertFalse(request.active)
        with self.assertRaises(RuntimeError):
            await adapter.send("Closed")

    async def test_get_me_network_failure_then_deliver_one_retry_succeeds(self):
        self.request.fail_get_me_once = True
        now = datetime(2026, 9, 10, 6, tzinfo=timezone.utc)
        due = now + timedelta(seconds=30)
        state = {"outbox": [make_message("daily", "daily", "2026-09-10", "Hallo")]}
        saved = []

        def save(candidate):
            self.assertIsNot(candidate, state)
            saved.append(json.loads(json.dumps(candidate)))

        async with TelegramSender("123:TEST", "-1234") as sender:
            with self.assertLogs("household_agent.telegram", "WARNING") as logs:
                self.assertFalse(await deliver_one(
                    state, sender.send, save, now, monotonic=lambda: 100.0,
                ))
            self.assertNotIn("123:TEST", " ".join(logs.output))
            self.assertEqual([call["endpoint"] for call in self.request.calls], ["getMe"])
            self.assertEqual(state["outbox"][0]["attempts"], 1)
            self.assertEqual(state["outbox"][0]["next_attempt_at"], due.isoformat())
            self.assertFalse(state["outbox"][0]["parts"][0]["sent"])
            self.assertEqual(len(saved), 2)
            self.assertEqual(saved[-1], state)

            self.assertFalse(await deliver_one(
                state, sender.send, save, due - timedelta(seconds=1),
                monotonic=lambda: 100.0,
            ))
            self.assertEqual(len(self.request.calls), 1)
            self.assertEqual(len(saved), 2)

            self.assertTrue(await deliver_one(
                state, sender.send, save, due, monotonic=lambda: 100.0,
            ))
            self.assertEqual(
                [call["endpoint"] for call in self.request.calls],
                ["getMe", "getMe", "sendMessage"],
            )
            self.assertEqual(self.request.calls[-1]["payload"], {
                "chat_id": "-1234", "text": "Hallo",
            })
            self.assertEqual(self.bot.id, 123)
            self.assertEqual(state["outbox"][0]["attempts"], 2)
            self.assertIsNone(state["outbox"][0]["next_attempt_at"])
            self.assertTrue(state["outbox"][0]["parts"][0]["sent"])
            self.assertEqual(saved[-1], state)
            self.assertEqual([
                (entry["outbox"][0]["attempts"],
                 entry["outbox"][0]["parts"][0]["sent"],
                 entry["outbox"][0]["next_attempt_at"])
                for entry in saved
            ], [(1, False, None), (1, False, due.isoformat()),
                (2, False, None), (2, True, None)])
            self.assertFalse(await deliver_one(
                state, sender.send, save, due, monotonic=lambda: 100.0,
            ))
            self.assertEqual(len(self.request.calls), 3)
            self.assertEqual(len(saved), 4)

        self.factory.assert_called_once_with(token="123:TEST")
        self.assertEqual(self.updates_request.calls, [])
        for request in (self.request, self.updates_request):
            self.assertEqual(request.initialize_count, 2)
            self.assertEqual(request.shutdown_count, 1)
            self.assertFalse(request.active)


if __name__ == "__main__":
    unittest.main()
