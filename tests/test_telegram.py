"""Telegram behavior without installed adapters, credentials, or network access."""

import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import importlib
import json
import logging
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from household_agent.telegram import (
    TelegramSender, daily_text, deliver_one, make_message, monthly_text, split_text,
)


class TextTests(unittest.TestCase):
    def test_daily_distinguishes_fixed_off_day_and_idle_active_resident(self):
        residents = [
            {"id": "a", "name": "Alice", "role": "adult", "off_days": []},
            {"id": "b", "name": "Bob", "role": "adult", "off_days": []},
            {"id": "c", "name": "Charlie", "role": "child", "off_days": ["Wednesday"]},
        ]
        original = deepcopy(residents)
        text = daily_text(date(2026, 9, 9), residents, [
            {"resident_id": "a", "task_name": "Sp\u00fclmaschine ausr\u00e4umen"},
            {"resident_id": "a", "task_name": "M\u00fcll <&> _rausbringen_"},
        ], [])
        self.assertIn("09.09.2026", text)
        self.assertIn("Alice: Sp\u00fclmaschine ausr\u00e4umen; M\u00fcll <&> _rausbringen_", text)
        self.assertIn("Bob hat heute keine Aufgabe erhalten.", text)
        self.assertIn("Charlie hat heute einen festen freien Tag.", text)
        self.assertEqual(residents, original)

    def test_empty_day_still_mentions_people_and_unassignable_tasks(self):
        text = daily_text(date(2026, 9, 9), [
            {"id": "a", "name": "Alice", "role": "adult", "off_days": []},
        ], [], ["Backofen reinigen"])
        self.assertIn("keine Aufgaben eingeplant", text)
        self.assertIn("Alice hat heute keine Aufgabe erhalten", text)
        self.assertIn("Hinweis: Backofen reinigen", text)
        self.assertIn("kein geeigneter Bewohner", text)
        self.assertNotIn("keine offenen Aufgaben", text)

    def test_monthly_includes_zero_and_historical_names_without_completion_claims(self):
        statistics = {
            "removed": {"name": "Alter Name", "count": 14},
            "zero": {"name": "Null", "count": 0},
            "one": {"name": "Eins", "count": 1},
        }
        original = deepcopy(statistics)
        text = monthly_text("2026-03", statistics)
        self.assertIn("Monatsstatistik f\u00fcr M\u00e4rz 2026", text)
        self.assertIn("Alter Name: 14 Aufgaben zugewiesen", text)
        self.assertIn("Null: 0 Aufgaben zugewiesen", text)
        self.assertIn("Eins: 1 Aufgabe zugewiesen", text)
        self.assertNotIn("erledigt", text)
        self.assertEqual(statistics, original)
        self.assertIn("Keine Aufgaben zugewiesen", monthly_text("2026-12", {}))

    def test_splitting_is_lossless_and_within_utf16_limit(self):
        for text in ("a" * 4096, "a" * 4097, "\U0001f600" * 5000,
                     "\u00e4\U0001f600e\u0301<&>\n" * 2000,
                     "a" * 4095 + "\U0001f600" + "b" * 4096):
            with self.subTest(length=len(text)):
                parts = split_text(text)
                self.assertEqual("".join(parts), text)
                self.assertTrue(all(0 < len(p.encode("utf-16-le")) // 2 <= 4096
                                    for p in parts))
        self.assertEqual(split_text("abc\ndefgh", 6), ["abc\n", "defgh"])

    def test_splitting_drops_only_whitespace_only_parts(self):
        self.assertEqual(split_text("X" + " " * 8200 + "Y"),
                         ["X" + " " * 4095, " " * 9 + "Y"])
        for text in (" " * 8200 + "Y", "X" + " " * 8200,
                     "X\n" + "\n\t\u2003" * 3000 + "Y"):
            with self.subTest(text=text[:20]):
                parts = split_text(text)
                self.assertTrue(all(part.strip() for part in parts))
                self.assertTrue(all(len(part.encode("utf-16-le")) // 2 <= 4096
                                    for part in parts))
                self.assertEqual("".join("".join(parts).split()), "".join(text.split()))

    def test_daily_message_with_long_padded_names_has_no_blank_parts(self):
        for padding in (" " * 8200, "\n\t\u2003" * 3000):
            with self.subTest(padding=repr(padding[:6])):
                name = "X" + padding + "Y"
                text = daily_text(date(2026, 9, 10), [
                    {"id": "a", "name": name, "role": "adult", "off_days": []},
                ], [{"resident_id": "a", "task_name": name}], [])
                message = make_message("daily", "daily", "2026-09-10", text)
                parts = [part["text"] for part in message["parts"]]
                self.assertTrue(all(part.strip() for part in parts))
                self.assertTrue(all(len(part.encode("utf-16-le")) // 2 <= 4096
                                    for part in parts))
                self.assertEqual("".join("".join(parts).split()), "".join(text.split()))

    def test_invalid_text_and_limits(self):
        for text in ("", " \n", "a\ud800"):
            with self.subTest(text=repr(text)), self.assertRaises(ValueError):
                split_text(text)
        for limit in (0, 1, 4097):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                split_text("abc", limit)

    def test_message_shape_and_json_roundtrip(self):
        message = make_message("daily:2026-09-09", "daily", "2026-09-09", "Hallo")
        self.assertEqual(message, {
            "id": "daily:2026-09-09", "kind": "daily", "reference": "2026-09-09",
            "parts": [{"text": "Hallo", "sent": False}],
            "attempts": 0, "next_attempt_at": None,
        })
        self.assertEqual(json.loads(json.dumps(message)), message)

    def test_module_import_does_not_require_telegram_package(self):
        with patch.dict(sys.modules, {"telegram": None}):
            importlib.reload(sys.modules["household_agent.telegram"])


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 10, 6, tzinfo=timezone.utc)
        self.state = {
            "reported_months": [],
            "outbox": [make_message("m1", "daily", "2026-09-10", "Hallo")],
            "assignment_history": [{"task_id": "keep"}],
        }
        self.saved = []
        self.send = AsyncMock()

    def save(self, candidate):
        self.assertIsNot(candidate, self.state)
        self.assertIsNot(candidate["outbox"], self.state["outbox"])
        self.saved.append(deepcopy(candidate))

    async def deliver(self, now=None):
        return await deliver_one(
            self.state, self.send, self.save, now or self.now, monotonic=lambda: 0,
        )

    async def test_ack_persisted_and_completed_entry_skipped(self):
        self.assertTrue(await self.deliver())
        self.assertFalse(self.saved[0]["outbox"][0]["parts"][0]["sent"])
        self.assertEqual(self.saved[0]["outbox"][0]["attempts"], 1)
        self.assertTrue(self.state["outbox"][0]["parts"][0]["sent"])
        self.assertFalse(await self.deliver())
        self.send.assert_awaited_once_with("Hallo")
        self.assertEqual(self.state["assignment_history"], [{"task_id": "keep"}])

    async def test_empty_outbox_does_nothing(self):
        self.state["outbox"] = []
        self.assertFalse(await self.deliver())
        self.assertEqual(self.saved, [])
        self.send.assert_not_awaited()

    async def test_fifo_only_one_message_per_call(self):
        self.state["outbox"].append(make_message("m2", "daily", "2026-09-10", "Zweite"))
        self.assertTrue(await self.deliver())
        self.send.assert_awaited_once_with("Hallo")
        self.assertTrue(await self.deliver())
        self.assertEqual([c.args[0] for c in self.send.await_args_list], ["Hallo", "Zweite"])

    async def test_backoff_blocks_later_messages_without_mutation(self):
        self.state["outbox"][0]["next_attempt_at"] = (self.now + timedelta(seconds=1)).isoformat()
        self.state["outbox"].append(make_message("m2", "daily", "2026-09-10", "Zweite"))
        original = deepcopy(self.state)
        self.assertFalse(await self.deliver())
        self.assertEqual(self.state, original)
        self.assertEqual(self.saved, [])
        self.send.assert_not_awaited()

    async def test_timeout_backoff_then_success(self):
        self.send.side_effect = [TimeoutError("secret URL"), None]
        with self.assertLogs("household_agent.telegram", "WARNING") as logs:
            self.assertFalse(await self.deliver())
        self.assertNotIn("secret URL", " ".join(logs.output))
        due = self.now + timedelta(seconds=30)
        self.assertEqual(self.state["outbox"][0]["next_attempt_at"], due.isoformat())
        self.assertFalse(await self.deliver(self.now + timedelta(seconds=29)))
        self.assertTrue(await self.deliver(due))
        self.assertEqual(self.state["outbox"][0]["attempts"], 2)
        self.assertIsNone(self.state["outbox"][0]["next_attempt_at"])

    async def test_real_hanging_sender_is_bounded(self):
        async def hang(text):
            await asyncio.Event().wait()

        with patch("household_agent.telegram.SEND_TIMEOUT", 0.01):
            with self.assertLogs("household_agent.telegram", "WARNING"):
                self.assertFalse(await deliver_one(self.state, hang, self.save, self.now))
        self.assertGreaterEqual(
            datetime.fromisoformat(self.state["outbox"][0]["next_attempt_at"]),
            self.now + timedelta(seconds=30.01),
        )

    async def test_exponential_backoff_is_capped(self):
        self.send.side_effect = OSError("network failure")
        for attempts, seconds in ((1, 60), (6, 1920), (7, 3600), (100000, 3600)):
            self.state["outbox"][0]["attempts"] = attempts
            self.state["outbox"][0]["next_attempt_at"] = None
            with self.assertLogs("household_agent.telegram", "WARNING"):
                self.assertFalse(await self.deliver())
            self.assertEqual(self.state["outbox"][0]["next_attempt_at"],
                             (self.now + timedelta(seconds=seconds)).isoformat())

    async def test_rate_limits_support_seconds_and_timedelta(self):
        for retry_after, seconds in ((95, 95), (timedelta(seconds=120), 120), (0, 30), (7200, 7200)):
            self.state["outbox"][0]["attempts"] = 0
            self.state["outbox"][0]["next_attempt_at"] = None
            error = Exception("https://api.telegram.org/botSECRET/sendMessage")
            error.retry_after = retry_after
            self.send.side_effect = error
            with self.assertLogs("household_agent.telegram", "WARNING") as logs:
                self.assertFalse(await self.deliver())
            self.assertNotIn("SECRET", " ".join(logs.output))
            self.assertEqual(self.state["outbox"][0]["next_attempt_at"],
                             (self.now + timedelta(seconds=seconds)).isoformat())

    async def test_rate_limit_deadline_includes_all_multipart_send_time(self):
        self.state["outbox"] = [make_message("monthly", "monthly", "2026-08", "x" * 12288)]
        clock = Mock(return_value=100.0)
        error = Exception("rate limited")
        error.retry_after = 45

        async def slow_send(text):
            clock.return_value += 20
            if clock.return_value == 160:
                raise error

        self.send.side_effect = slow_send
        with self.assertLogs("household_agent.telegram", "WARNING"):
            self.assertTrue(await deliver_one(
                self.state, self.send, self.save, self.now, monotonic=clock,
            ))
        self.assertEqual(self.send.await_count, 3)
        self.assertEqual([p["sent"] for p in self.state["outbox"][0]["parts"]],
                         [True, True, False])
        self.assertEqual(self.state["outbox"][0]["next_attempt_at"],
                         (self.now + timedelta(seconds=105)).isoformat())
        self.assertFalse(await self.deliver(self.now + timedelta(seconds=104)))
        self.assertEqual(self.send.await_count, 3)
        self.assertTrue(await self.deliver(self.now + timedelta(seconds=105)))
        self.assertEqual(self.send.await_count, 4)

    async def test_negative_clock_elapsed_does_not_shorten_backoff(self):
        self.send.side_effect = TimeoutError()
        with self.assertLogs("household_agent.telegram", "WARNING"):
            self.assertFalse(await deliver_one(
                self.state, self.send, self.save, self.now,
                monotonic=Mock(side_effect=[100.0, 90.0]),
            ))
        self.assertEqual(self.state["outbox"][0]["next_attempt_at"],
                         (self.now + timedelta(seconds=30)).isoformat())

    async def test_permanent_errors_wait_longer_and_do_not_log_exception(self):
        for name in ("Forbidden", "InvalidToken", "BadRequest", "Unauthorized"):
            self.state["outbox"][0]["next_attempt_at"] = None
            self.send.side_effect = type(name, (Exception,), {})("TOKEN https://secret")
            with self.assertLogs("household_agent.telegram", "WARNING") as logs:
                self.assertFalse(await self.deliver())
            self.assertIn("dauerhafter Fehler", " ".join(logs.output))
            self.assertNotIn("TOKEN", " ".join(logs.output))
            self.assertEqual(self.state["outbox"][0]["next_attempt_at"],
                             (self.now + timedelta(hours=1)).isoformat())

    async def test_multipart_retry_skips_acked_part_and_reports_month_only_at_end(self):
        self.state["outbox"] = [make_message("monthly", "monthly", "2026-08", "x" * 4096 + "Ende")]
        self.send.side_effect = [None, ConnectionError("lost"), None]
        with self.assertLogs("household_agent.telegram", "WARNING"):
            self.assertTrue(await self.deliver())
        self.assertEqual(self.state["reported_months"], [])
        self.assertEqual([p["sent"] for p in self.state["outbox"][0]["parts"]], [True, False])
        self.assertEqual(self.saved[1]["reported_months"], [])
        # Simulate a restart from the last durable snapshot.
        self.state = deepcopy(self.saved[-1])
        self.assertTrue(await self.deliver(self.now + timedelta(seconds=30)))
        self.assertEqual(self.state["reported_months"], ["2026-08"])
        self.assertEqual([c.args[0] for c in self.send.await_args_list], ["x" * 4096, "Ende", "Ende"])
        self.assertFalse(await self.deliver())
        self.assertEqual(self.state["reported_months"], ["2026-08"])

    async def test_monthly_report_not_duplicated_in_reported_months(self):
        self.state["reported_months"] = ["2026-08"]
        self.state["outbox"] = [make_message("monthly", "monthly", "2026-08", "Bericht")]
        self.assertTrue(await self.deliver())
        self.assertEqual(self.state["reported_months"], ["2026-08"])

    async def test_delayed_header_persisted_before_send_and_frozen_after_failure(self):
        self.state["outbox"] = [make_message("old", "daily", "2026-09-09", "Aufgaben 09.09.2026\n" + "x" * 4096)]
        self.send.side_effect = [TimeoutError(), None, None]
        with self.assertLogs("household_agent.telegram", "WARNING"):
            self.assertFalse(await self.deliver())
        frozen = deepcopy(self.state["outbox"][0]["parts"])
        full = "".join(p["text"] for p in frozen)
        self.assertTrue(full.startswith("Nachtr\u00e4gliche Mitteilung f\u00fcr den 09.09.2026:"))
        self.assertEqual(self.saved[0]["outbox"][0]["parts"], frozen)
        self.assertTrue(await self.deliver(self.now + timedelta(days=1)))
        self.assertEqual([p["text"] for p in self.state["outbox"][0]["parts"]],
                         [p["text"] for p in frozen])
        self.assertEqual(self.state["outbox"][0]["reference"], "2026-09-09")

    async def test_first_attempt_today_stays_unchanged_when_retried_tomorrow(self):
        self.send.side_effect = [TimeoutError(), None]
        with self.assertLogs("household_agent.telegram", "WARNING"):
            self.assertFalse(await self.deliver())
        self.assertTrue(await self.deliver(self.now + timedelta(days=1)))
        self.assertEqual([c.args[0] for c in self.send.await_args_list], ["Hallo", "Hallo"])

    async def test_local_day_marks_first_attempt_delayed_before_utc_midnight(self):
        now = datetime(2026, 9, 10, 23, tzinfo=timezone.utc)
        self.assertTrue(await deliver_one(
            self.state, self.send, self.save, now, local_day=date(2026, 9, 11),
        ))
        self.send.assert_awaited_once_with(
            "Nachtr\u00e4gliche Mitteilung f\u00fcr den 10.09.2026:\n\nHallo"
        )
        self.assertEqual(self.state["outbox"][0]["reference"], "2026-09-10")
        self.assertEqual(self.saved[0]["outbox"][0]["parts"][0]["text"],
                         self.send.await_args.args[0])

    async def test_local_day_avoids_false_delay_after_utc_midnight(self):
        now = datetime(2026, 9, 11, 1, tzinfo=timezone.utc)
        self.assertTrue(await deliver_one(
            self.state, self.send, self.save, now, local_day=date(2026, 9, 10),
        ))
        self.send.assert_awaited_once_with("Hallo")

    async def test_local_day_never_changes_previously_attempted_text(self):
        self.send.side_effect = [TimeoutError(), None]
        with self.assertLogs("household_agent.telegram", "WARNING"):
            self.assertFalse(await deliver_one(
                self.state, self.send, self.save, self.now, local_day=date(2026, 9, 10),
            ))
        self.assertTrue(await deliver_one(
            self.state, self.send, self.save, self.now + timedelta(days=3),
            local_day=date(2026, 9, 13),
        ))
        self.assertEqual([c.args[0] for c in self.send.await_args_list], ["Hallo", "Hallo"])

    async def test_presend_save_failure_leaves_original_untouched_and_never_sends(self):
        self.state["outbox"][0]["reference"] = "2026-09-09"
        original = deepcopy(self.state)
        failure = OSError("disk full")
        save = Mock(side_effect=failure)
        with self.assertRaises(OSError) as caught:
            await deliver_one(self.state, self.send, save, self.now)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.state, original)
        self.send.assert_not_awaited()
        save.assert_called_once()

    async def test_ack_save_failure_propagates_without_marking_sent_or_scheduling_retry(self):
        self.state["outbox"] = [make_message("monthly", "monthly", "2026-08", "Bericht")]
        failure = OSError("disk full")

        def save(candidate):
            if self.saved:
                raise failure
            self.save(candidate)

        with self.assertRaises(OSError) as caught:
            await deliver_one(self.state, self.send, save, self.now)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.state, self.saved[0])
        self.assertFalse(self.state["outbox"][0]["parts"][0]["sent"])
        self.assertIsNone(self.state["outbox"][0]["next_attempt_at"])
        self.assertEqual(self.state["reported_months"], [])
        # Telegram ACK without durable local ACK necessarily permits duplicates.
        self.assertTrue(await self.deliver())
        self.assertEqual(self.send.await_count, 2)

    async def test_retry_save_failure_propagates_and_keeps_last_committed_state(self):
        failure = OSError("disk full")
        self.send.side_effect = TimeoutError("network")

        def save(candidate):
            if self.saved:
                raise failure
            self.save(candidate)

        with self.assertLogs("household_agent.telegram", "WARNING"):
            with self.assertRaises(OSError) as caught:
                await deliver_one(self.state, self.send, save, self.now)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.state, self.saved[0])

    async def test_save_crash_after_durable_write_does_not_mutate_memory(self):
        original = deepcopy(self.state)

        def save_then_crash(candidate):
            self.save(candidate)
            raise OSError("directory sync failed after replace")

        with self.assertRaises(OSError):
            await deliver_one(self.state, self.send, save_then_crash, self.now)
        self.assertEqual(self.state, original)
        self.assertEqual(self.saved[0]["outbox"][0]["attempts"], 1)
        self.send.assert_not_awaited()
        self.state = deepcopy(self.saved[0])
        self.assertTrue(await self.deliver())

    async def test_crash_after_durable_ack_does_not_resend_after_restart(self):
        self.state["outbox"] = [make_message("monthly", "monthly", "2026-08", "Bericht")]

        def save_then_crash_on_ack(candidate):
            self.save(candidate)
            if candidate["outbox"][0]["parts"][0]["sent"]:
                raise OSError("directory sync failed after ACK replace")

        with self.assertRaises(OSError):
            await deliver_one(self.state, self.send, save_then_crash_on_ack, self.now)
        self.assertFalse(self.state["outbox"][0]["parts"][0]["sent"])
        self.assertEqual(self.state["reported_months"], [])
        self.state = deepcopy(self.saved[-1])
        self.assertEqual(self.state["reported_months"], ["2026-08"])
        self.assertFalse(await self.deliver())
        self.send.assert_awaited_once_with("Bericht")

    async def test_unexpected_send_exception_is_retried_without_exposing_repr(self):
        class TransportError(Exception):
            def __str__(self):
                raise AssertionError("Exception text must not be inspected")

            def __repr__(self):
                raise AssertionError("Exception repr must not be inspected")

        self.send.side_effect = TransportError()
        with self.assertLogs("household_agent.telegram", "WARNING"):
            self.assertFalse(await self.deliver())
        self.assertEqual(self.state["outbox"][0]["next_attempt_at"],
                         (self.now + timedelta(seconds=30)).isoformat())

    async def test_cancellation_propagates_without_retry(self):
        self.send.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.deliver()
        self.assertIsNone(self.state["outbox"][0]["next_attempt_at"])
        self.assertFalse(self.state["outbox"][0]["parts"][0]["sent"])

    async def test_now_must_be_aware_utc(self):
        for now in (datetime(2026, 9, 10), self.now.astimezone(timezone(timedelta(hours=2)))):
            with self.assertRaises(ValueError):
                await self.deliver(now)
        self.send.assert_not_awaited()
        self.assertEqual(self.saved, [])


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name in ("httpx", "httpcore"):
            logger = logging.getLogger(name)
            self.addCleanup(logger.setLevel, logger.level)
        self.bot = AsyncMock()
        self.factory = Mock(return_value=self.bot)
        telegram = patch.dict(sys.modules, {"telegram": SimpleNamespace(Bot=self.factory)})
        telegram.start()
        self.addCleanup(telegram.stop)

    async def test_lazy_bot_context_plaintext_and_explicit_timeouts(self):
        bot = AsyncMock()
        bot.__aexit__.return_value = False
        factory = Mock(return_value=bot)
        adapter = TelegramSender("TOKEN", "-1234")
        factory.assert_not_called()
        levels = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
        try:
            with patch.dict(sys.modules, {"telegram": SimpleNamespace(Bot=factory)}):
                async with adapter as sender:
                    self.assertIs(sender, adapter)
                    bot.initialize.assert_not_awaited()
                    bot.__aenter__.assert_not_awaited()
                    await sender.send("<name> & _task_ \U0001f600")
                factory.assert_called_once_with(token="TOKEN")
                bot.initialize.assert_awaited_once_with()
                bot.shutdown.assert_awaited_once_with()
                bot.__aexit__.assert_not_awaited()
                bot.send_message.assert_awaited_once_with(
                    chat_id="-1234", text="<name> & _task_ \U0001f600", parse_mode=None,
                    connect_timeout=10, read_timeout=30, write_timeout=30, pool_timeout=10,
                )
        finally:
            for name, level in levels.items():
                logging.getLogger(name).setLevel(level)
        with self.assertRaises(RuntimeError):
            await adapter.send("closed")

    async def test_enter_and_exit_without_send_never_initialize(self):
        async with TelegramSender("TOKEN", "-1234"):
            self.factory.assert_called_once_with(token="TOKEN")
            self.bot.initialize.assert_not_awaited()
            self.bot.get_me.assert_not_awaited()
        self.bot.__aenter__.assert_not_awaited()
        self.bot.initialize.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()
        self.bot.shutdown.assert_awaited_once_with()

    async def test_initialize_failure_retries_through_delivery_backoff(self):
        now = datetime(2026, 9, 10, 6, tzinfo=timezone.utc)
        state = {"outbox": [make_message("daily", "daily", "2026-09-10", "Hallo")]}
        save = Mock()
        self.bot.initialize.side_effect = [OSError("https://secret/TOKEN"), None]
        async with TelegramSender("TOKEN", "-1234") as sender:
            with self.assertLogs("household_agent.telegram", "WARNING") as logs:
                self.assertFalse(await deliver_one(
                    state, sender.send, save, now, monotonic=lambda: 0,
                ))
            self.assertNotIn("TOKEN", " ".join(logs.output))
            self.bot.send_message.assert_not_awaited()
            due = now + timedelta(seconds=30)
            self.assertEqual(state["outbox"][0]["next_attempt_at"], due.isoformat())
            self.assertFalse(await deliver_one(state, sender.send, save, now))
            self.bot.initialize.assert_awaited_once_with()
            self.assertTrue(await deliver_one(state, sender.send, save, due))
            self.assertEqual(self.bot.initialize.await_count, 2)
            self.assertTrue(state["outbox"][0]["parts"][0]["sent"])
            await sender.send("Noch eine")
            self.assertEqual(self.bot.initialize.await_count, 2)
        self.bot.shutdown.assert_awaited_once_with()

    async def test_cancelled_initialization_closes_context_and_propagates(self):
        started = asyncio.Event()

        async def initialize():
            started.set()
            await asyncio.Event().wait()

        self.bot.initialize.side_effect = initialize
        adapter = TelegramSender("TOKEN", "-1234")

        async def run():
            async with adapter:
                await adapter.send("Hallo")

        task = asyncio.create_task(run())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.bot.shutdown.assert_awaited_once_with()
        self.bot.send_message.assert_not_awaited()
        with self.assertRaises(RuntimeError):
            await adapter.send("closed")

    async def test_cancellation_during_shutdown_waits_for_cleanup(self):
        started = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()

        async def shutdown():
            started.set()
            await release.wait()
            finished.set()

        self.bot.shutdown.side_effect = shutdown

        async def run():
            async with TelegramSender("TOKEN", "-1234"):
                pass

        task = asyncio.create_task(run())
        await started.wait()
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())
        self.bot.shutdown.assert_awaited_once_with()

    async def test_shutdown_failure_is_sanitized_and_does_not_hide_body_error(self):
        self.bot.shutdown.side_effect = OSError("https://secret/TOKEN")
        failure = ValueError("body failure")
        adapter = TelegramSender("TOKEN", "-1234")
        with self.assertLogs("household_agent.telegram", "WARNING") as logs:
            with self.assertRaises(ValueError) as caught:
                async with adapter:
                    raise failure
        self.assertIs(caught.exception, failure)
        self.assertNotIn("TOKEN", " ".join(logs.output))
        self.bot.shutdown.assert_awaited_once_with()
        with self.assertRaises(RuntimeError):
            await adapter.send("closed")


if __name__ == "__main__":
    unittest.main()
