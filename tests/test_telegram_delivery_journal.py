"""Crash/network fault injection at the publisher's actual send boundary."""
import copy
import os
import sys
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
import telegram_destinations as destinations
import telegram_multi_publisher as publisher


def server(identifier="a"):
    return {"id": identifier, "protocol": "vless", "country": "DE", "tls": True, "latency": 10,
            "status": "online", "valid": True, "should_remove": False,
            "raw": f"vless://uuid-{identifier}@example.com:443?security=tls"}


def destination(**kwargs):
    return destinations.normalize_destination({"chat_id": -1001, "enabled": True, "bot_status": "administrator", "daily_limit": 1, **kwargs})


def attempt(srv=None):
    srv = srv or server()
    return {"server_id": srv["id"], "fingerprint": publisher.base.telegram_fingerprint(srv), "at": 1_800_000_000}


class DeliveryJournalTests(unittest.TestCase):
    def run_publisher(self, response=None, state=None, checkpoint=None):
        state = state or {"version": 1, "destinations": {}, "bot_next_send_after": 0}
        clock = {"now": 1_800_000_000.0}
        records = []

        def persist(value, reason):
            if checkpoint:
                checkpoint(value, reason)
            records.append((reason, copy.deepcopy(value)))

        with ExitStack() as stack:
            for name, value in {"RUN_BUDGET_SECONDS": 20, "RUN_STOP_RESERVE_SECONDS": 0,
                                "load_state": Mock(return_value=state), "load_latest_servers": Mock(return_value=[server()]),
                                "ensure_global_enabled": Mock(), "load_destinations_remote": Mock(return_value={"destinations": [destination()]}),
                                "checkpoint_state": persist, "build_payload": Mock(return_value={}), "_write_output": Mock()}.items():
                stack.enter_context(patch.object(publisher, name, value))
            stack.enter_context(patch.object(publisher.time, "time", side_effect=lambda: clock["now"]))
            stack.enter_context(patch.object(publisher.time, "monotonic", side_effect=lambda: clock["now"]))
            stack.enter_context(patch.object(publisher.time, "sleep", side_effect=lambda secs: clock.update(now=clock["now"]+secs)))
            stack.enter_context(patch.object(publisher.base, "live_validate", side_effect=lambda value: value))
            stack.enter_context(patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}))
            request = stack.enter_context(patch.object(publisher.base, "_telegram_request_once", side_effect=response if isinstance(response, Exception) else None,
                                                       return_value=response or {"ok": True, "result": {"message_id": 1}}))
            result = publisher.main()
        return result, state, records, request

    def test_success_has_durable_intent_before_request_and_confirmed_counters_after(self):
        result, state, records, request = self.run_publisher()
        self.assertEqual(result, 0)
        request.assert_called_once()
        intents = [value for reason, value in records if reason == "send-attempt"]
        self.assertEqual(intents[0]["destinations"]["-1001"]["pending_delivery"]["server_id"], "a")
        target = state["destinations"]["-1001"]
        self.assertIsNone(target["pending_delivery"])
        self.assertEqual(target["total_sent"], 1)
        self.assertEqual(target["daily_sent"], 1)
        self.assertEqual(target["uncertain_deliveries"], {})

    def test_timeout_is_quarantined_and_never_automatically_retried(self):
        result, state, records, request = self.run_publisher(publisher.base.TransientTelegramError("timeout"))
        self.assertEqual(result, 0)
        request.assert_called_once()
        target = state["destinations"]["-1001"]
        self.assertIn("a", target["uncertain_deliveries"])
        self.assertNotIn("a", target["queue"])
        self.assertEqual(target["sent"], [])
        self.assertEqual(target["daily_sent"], 0)
        self.assertTrue(any(reason == "delivery-uncertain" for reason, _ in records))

    def test_429_is_definitive_not_uncertain_and_bot_cooldown_is_persisted(self):
        _, state, _, request = self.run_publisher(publisher.base.RateLimited(60))
        request.assert_called_once()
        target = state["destinations"]["-1001"]
        self.assertIsNone(target["pending_delivery"])
        self.assertEqual(target["uncertain_deliveries"], {})
        self.assertEqual(state["bot_next_send_after"], 1_800_000_060)
        self.assertEqual(target["sent"], [])

    def test_definitive_rejection_retains_retryable_queue_without_uncertainty(self):
        _, state, _, request = self.run_publisher(publisher.base.TelegramRejectedError("forbidden"))
        request.assert_called_once()
        target = state["destinations"]["-1001"]
        self.assertIsNone(target["pending_delivery"])
        self.assertEqual(target["uncertain_deliveries"], {})
        self.assertIn("a", target["queue"])

    def test_failed_intent_checkpoint_prevents_any_telegram_request(self):
        def checkpoint(state, reason):
            if reason == "send-attempt":
                raise RuntimeError("push failed")
        _, state, _, request = self.run_publisher(checkpoint=checkpoint)
        request.assert_not_called()
        self.assertEqual(state["destinations"]["-1001"]["sent"], [])

    def test_crash_after_request_before_success_checkpoint_is_held_on_restart(self):
        def checkpoint(state, reason):
            if reason in {"sent", "fatal"}:
                raise RuntimeError("process interrupted")
        result, _, records, request = self.run_publisher(checkpoint=checkpoint)
        self.assertEqual(result, 1)
        request.assert_called_once()
        durable = [value for reason, value in records if reason == "send-attempt"][-1]
        result, state, _, request = self.run_publisher(state=durable)
        self.assertEqual(result, 0)
        request.assert_not_called()
        self.assertIn("a", state["destinations"]["-1001"]["uncertain_deliveries"])

    def test_pending_attempt_filters_same_fingerprint_under_another_id(self):
        a, b = server("a"), server("b")
        b["raw"] = a["raw"]
        target = publisher._default_target_state()
        target["pending_delivery"] = attempt(a)
        publisher.prepare_target_state(target, [a, b], destination())
        self.assertEqual(target["queue"], [])
        self.assertIsNone(target["pending_delivery"])
        self.assertIn("a", target["uncertain_deliveries"])

    def test_explicit_retry_resolution_applies_once_even_if_it_becomes_uncertain_again(self):
        target = publisher._default_target_state()
        target["uncertain_deliveries"] = {"a": attempt()}
        dest = destination(delivery_resolutions={"a": {"decision": "retry", "nonce": "a"*16}})
        publisher.prepare_target_state(target, [server()], dest)
        self.assertEqual(target["queue"], ["a"])
        target["uncertain_deliveries"] = {"a": attempt()}
        publisher.prepare_target_state(target, [server()], dest)
        self.assertEqual(target["queue"], [])
        self.assertIn("a", target["uncertain_deliveries"])

    def test_explicit_sent_resolution_marks_history_and_preserves_spacing_and_quota(self):
        target = publisher._default_target_state()
        target["uncertain_deliveries"] = {"a": attempt()}
        dest = destination(retry_generation=1, delivery_resolutions={"a": {"decision": "sent", "nonce": "b"*16}})
        with patch.object(publisher.time, "time", return_value=1_800_000_010):
            publisher.prepare_target_state(target, [server()], dest)
        self.assertIn("a", target["sent"])
        self.assertEqual(target["daily_sent"], 1)
        self.assertGreaterEqual(target["next_send_after"], 1_800_000_100)
        self.assertEqual(target["uncertain_deliveries"], {})

    def test_corrupt_delivery_journal_never_silently_discards_attempt(self):
        for value in [{"pending_delivery": {}}, {"uncertain_deliveries": []},
                      {"uncertain_deliveries": {"b": attempt()}}, {"pending_delivery": {**attempt(), "at": float("inf")}}]:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                publisher._normalize_target_state(value)


if __name__ == "__main__":
    unittest.main()
