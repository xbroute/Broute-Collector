import copy
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import telegram_bot_control as control
import telegram_bot_destination_extension as extension
import telegram_destinations as destinations
import telegram_managed_sources as sources
import telegram_multi_publisher as multi
import telegram_publisher as publisher


def server():
    return {"id": "one", "raw": "vless://id@8.8.8.8:443?security=tls", "valid": True,
            "status": "online", "address": "8.8.8.8", "port": 443, "protocol": "vless",
            "country_name": "Test", "country": "XX"}


def target(chat_id=-1001):
    return destinations.normalize_destination({"chat_id": chat_id, "enabled": True,
                                               "bot_status": "administrator"})


class TelegramCommandSafetyTests(unittest.TestCase):
    def test_invalid_destination_command_does_not_block_later_off(self):
        updates = [{"update_id": 21, "message": {"from": {"id": 111}, "chat": {"id": 111},
                                                   "text": "/target_interval 1 0"}},
                   {"update_id": 22, "message": {"from": {"id": 111}, "chat": {"id": 111},
                                                   "text": "/publisher_off"}}]
        store = {"manager_user_ids": [111], "destinations": [target()]}
        restore = extension.install(control)
        try:
            with (
                patch.object(extension, "_load_store", return_value=store),
                patch.object(control, "BOT_TOKEN", "test-token"),
                patch.object(control, "GH_TOKEN", "test-github-token"),
                patch.object(control, "load_bot_state", return_value={"last_update_id": 0}),
                patch.object(control, "save_bot_state") as save,
                patch.object(control, "register_commands"),
                patch.object(control, "telegram_api", return_value=updates),
                patch.object(control, "safe_send_text"),
                patch.object(control, "set_enabled", return_value=True) as set_enabled,
                patch.object(control, "status_text", return_value="OFF"),
            ):
                self.assertEqual(control.main(), 0)
                set_enabled.assert_called_once_with(False)
                self.assertEqual(save.call_args.args[0]["last_update_id"], 22)
        finally:
            restore()

    def test_malformed_callback_does_not_poison_control_poll(self):
        updates = [{"update_id": 31, "callback_query": {"data": "publisher:on", "from": {"id": 111},
                                                       "message": {"chat": ["bad"]}}},
                   {"update_id": 32, "message": {"from": {"id": 111}, "chat": {"id": 111},
                                                   "text": "/publisher_off"}}]
        with (
            patch.object(control, "BOT_TOKEN", "test-token"), patch.object(control, "GH_TOKEN", "test-gh"),
            patch.object(control, "load_bot_state", return_value={"last_update_id": 0}),
            patch.object(control, "save_bot_state") as save, patch.object(control, "register_commands"),
            patch.object(control, "telegram_api", return_value=updates), patch.object(control, "process_action") as process,
        ):
            self.assertEqual(control.main(), 0)
            self.assertEqual(process.call_args.args[0], "off")
            self.assertEqual(save.call_args.args[0]["last_update_id"], 32)

    def test_http_and_network_errors_do_not_expose_bot_token_or_body(self):
        token = "123456:private-bot-token"
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        for error in (HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b"private-subscription-url")),
                      URLError(url)):
            with self.subTest(error=type(error).__name__), patch.object(control, "urlopen", side_effect=error):
                with self.assertRaises(RuntimeError) as raised:
                    control._json_request(url)
                self.assertNotIn(token, str(raised.exception))
                self.assertNotIn("private-subscription", str(raised.exception))
                self.assertIn("api.telegram.org", str(raised.exception))

    def test_single_interval_below_minimum_is_rejected(self):
        for value in ("0", "5", "14", "0m"):
            with self.subTest(value=value), self.assertRaises(destinations.DestinationValidationError):
                destinations.parse_interval_spec(value)


class TelegramPublisherSafetyTests(unittest.TestCase):
    def test_template_values_are_never_interpreted_as_placeholders(self):
        value = server()
        value["country_name"] = "{config}"
        dest = target()
        dest["template"] = "{{country}} {country}\n{config}"
        rendered = multi.render_target_message(dest, value)
        self.assertTrue(str(rendered).startswith("{country} {config}"))
        self.assertEqual(str(rendered).count("<pre>"), 1)
        self.assertEqual(rendered.copy_text, publisher.brand_raw_config(value["raw"], "vless"))

    def test_connection_paths_have_distinct_telegram_fingerprints(self):
        first, second = server(), server()
        first["raw"] = "vless://id@example.com:443/one?security=tls"
        second["raw"] = first["raw"].replace("/one?", "/two?")
        self.assertNotEqual(publisher.telegram_fingerprint(first), publisher.telegram_fingerprint(second))
        second["raw"] = first["raw"].replace("security=", "Security=")
        self.assertNotEqual(publisher.telegram_fingerprint(first), publisher.telegram_fingerprint(second))

    def test_failed_remote_destination_refresh_does_not_use_stale_enabled_checkout(self):
        with patch.object(multi.os.path, "isdir", return_value=True), patch.object(multi, "_git", return_value=subprocess.CompletedProcess([], 1)), patch.object(multi, "_read_json") as read:
            with self.assertRaises(RuntimeError):
                multi.load_destinations_remote()
            read.assert_not_called()

    def test_corrupt_or_unknown_version_state_never_resets_sent_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "state.json"
            for contents in (
                "{bad", "[]", '{"version": 99}', '{"destinations": []}',
                '{"destinations": {"-1001": null}}',
                '{"destinations": {"-1001": {"sent": "lost-history"}}}',
                '{"destinations": {"-1001": {"next_send_after": "invalid"}}}',
                '{"bot_next_send_after": Infinity}',
            ):
                with self.subTest(contents=contents), patch.object(multi, "STATE_PATH", str(path)):
                    path.write_text(contents)
                    with self.assertRaises(RuntimeError):
                        multi.load_state()
                    self.assertEqual(path.read_text(), contents)

    def test_ambiguous_send_is_not_automatically_retried(self):
        guard = Mock()
        with patch.object(publisher, "_telegram_request_once", side_effect=publisher.TransientTelegramError("retry")) as request, patch.object(multi.time, "sleep"):
            with self.assertRaises(multi.DeliveryUncertain):
                multi.send_payload("test-token", {}, before_request=guard)
        self.assertEqual(request.call_count, 1)
        guard.assert_called_once()

    def test_send_guard_prevents_any_request_when_master_is_off(self):
        guard = Mock(side_effect=multi.PublishingDisabled("OFF"))
        with patch.object(publisher, "_telegram_request_once") as request:
            with self.assertRaises(multi.PublishingDisabled):
                multi.send_payload("test-token", {}, before_request=guard)
        request.assert_not_called()

    def test_unchanged_local_checkpoint_still_pushes_an_unpushed_commit(self):
        calls = []

        def git(args, cwd, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "1" if args[0] == "rev-list" else "", "")

        with patch.object(multi, "_write_json_atomic"), patch.object(multi.os.path, "isdir", return_value=True), patch.object(multi, "_git", side_effect=git):
            multi.checkpoint_state({"destinations": {}}, "retry")
        self.assertTrue(any(args[0] == "push" for args in calls))
        self.assertFalse(any(args[0] == "commit" for args in calls))

    def _run_mocked_main(self, *, refresh=None, send=None, ensure=None):
        clock = {"now": 100.0}
        records = []
        enabled = {"destinations": [target(), target(-1002)]}
        state = {"version": 1, "bot_next_send_after": 0, "destinations": {}}
        with ExitStack() as stack:
            for name, value in {"RUN_BUDGET_SECONDS": 100, "RUN_STOP_RESERVE_SECONDS": 0,
                                "load_state": Mock(return_value=state), "load_latest_servers": Mock(return_value=[server()]),
                                "ensure_global_enabled": ensure or Mock(), "load_destinations_remote": refresh or Mock(return_value=enabled),
                                "build_payload": Mock(return_value={}), "_write_output": Mock(),
                                "checkpoint_state": lambda state, reason: records.append(copy.deepcopy(state))}.items():
                stack.enter_context(patch.object(multi, name, value))
            stack.enter_context(patch.object(multi.time, "time", side_effect=lambda: clock["now"]))
            stack.enter_context(patch.object(multi.time, "monotonic", side_effect=lambda: clock["now"]))
            stack.enter_context(patch.object(multi.time, "sleep", side_effect=lambda seconds: clock.update(now=clock["now"] + seconds)))
            stack.enter_context(patch.object(publisher, "live_validate", side_effect=lambda value: value))
            stack.enter_context(patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}))
            sender = stack.enter_context(patch.object(multi, "send_payload", side_effect=send or AssertionError("unexpected send")))
            result = multi.main()
        return result, records, sender, clock

    def test_destination_off_during_live_check_prevents_send(self):
        refresh = Mock(side_effect=[{"destinations": [target()]}, {"destinations": []}, {"destinations": []}, {"destinations": []}])
        result, _, sender, _ = self._run_mocked_main(refresh=refresh)
        self.assertEqual(result, 0)
        sender.assert_not_called()

    def test_master_off_at_send_boundary_prevents_send(self):
        result, _, sender, _ = self._run_mocked_main(ensure=Mock(side_effect=[None, None, multi.PublishingDisabled("OFF")]))
        self.assertEqual(result, 0)
        sender.assert_not_called()

    def test_rate_limit_pauses_all_destinations_and_is_persisted(self):
        attempts = []

        def send(*args, **kwargs):
            attempts.append(None)
            if len(attempts) == 1:
                raise publisher.RateLimited(60)
            raise multi.PublishingDisabled("test completed")

        result, records, sender, clock = self._run_mocked_main(send=send)
        self.assertEqual(result, 0)
        self.assertEqual(sender.call_count, 2)
        self.assertGreaterEqual(clock["now"], 160)
        self.assertTrue(any(value["bot_next_send_after"] == 160 for value in records))


class ManagedKeyRotationTests(unittest.TestCase):
    def test_dedicated_key_can_read_existing_bot_token_encrypted_sources(self):
        original = [{"id": "one", "url": "https://example.com/private", "enabled": True}]
        envelope = sources.encrypt_sources(original, "old-token")
        with patch.dict(os.environ, {"TELEGRAM_SOURCE_ENCRYPTION_KEY": "new-key", "TELEGRAM_BOT_TOKEN": "old-token"}):
            self.assertEqual(sources.decrypt_sources(envelope), original)
            rotated = sources.encrypt_sources(original)
        self.assertEqual(sources.decrypt_sources(rotated, "new-key"), original)

    def test_nonempty_invalid_envelopes_are_not_an_empty_source_list(self):
        for payload in (None, [], [1], {"version": "bad"}, {"version": 1}, {"version": 1, "ciphertext": "broken"}):
            with self.subTest(payload=payload), self.assertRaises(sources.SourceCryptoError):
                sources.decrypt_sources(payload, "test-key")

    def test_corrupt_destination_envelope_is_not_an_empty_manager_store(self):
        for payload in (None, [], [1], {"version": "bad"}, {"version": 1}, {"version": 1, "ciphertext": "broken"}):
            with self.subTest(payload=payload), self.assertRaises(destinations.DestinationStateError):
                destinations.decrypt_store(payload, "test-key")


if __name__ == "__main__":
    unittest.main()
