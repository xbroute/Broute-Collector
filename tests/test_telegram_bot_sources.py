import base64
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import telegram_bot_control as control
import telegram_bot_source_extension as extension
import telegram_managed_sources as managed


class ManagedSourceCoreTests(unittest.TestCase):
    def test_encryption_round_trip_and_wrong_key_rejected(self):
        sources = [
            {
                "id": "abc123",
                "url": "https://example.com/private/sub?token=secret",
                "enabled": True,
            }
        ]
        payload = managed.encrypt_sources(sources, secret="secret-a")
        serialized = str(payload)
        self.assertNotIn("token=secret", serialized)
        self.assertEqual(managed.decrypt_sources(payload, secret="secret-a"), sources)
        with self.assertRaises(managed.SourceCryptoError):
            managed.decrypt_sources(payload, secret="secret-b")

    def test_private_dns_target_is_rejected(self):
        with mock.patch(
            "telegram_managed_sources.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 443))],
        ):
            with self.assertRaises(managed.SourceValidationError):
                managed.validate_public_url("https://example.com/sub")

    def test_global_dns_target_is_accepted(self):
        with mock.patch(
            "telegram_managed_sources.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("1.1.1.1", 443))],
        ):
            self.assertEqual(
                managed.validate_public_url("HTTPS://Example.COM/sub#label"),
                "https://example.com/sub",
            )

    def test_fetch_connection_is_pinned_to_the_validated_ip(self):
        response = mock.Mock()
        response.status = 200
        response.getheaders.return_value = [("Content-Type", "text/plain")]
        response.read.return_value = b"vless://test"

        connection = mock.Mock()
        connection.getresponse.return_value = response

        with (
            mock.patch(
                "telegram_managed_sources.socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("1.1.1.1", 443))],
            ),
            mock.patch(
                "telegram_managed_sources._PinnedHTTPSConnection",
                return_value=connection,
            ) as pinned,
        ):
            status, _, body = managed._fetch_one_hop("https://Example.com/sub?q=1")

        self.assertEqual(status, 200)
        self.assertEqual(body, b"vless://test")
        pinned.assert_called_once_with("example.com", "1.1.1.1", 443, 15)
        connection.request.assert_called_once()
        request_args = connection.request.call_args
        self.assertEqual(request_args.args[:2], ("GET", "/sub?q=1"))
        self.assertEqual(request_args.kwargs["headers"]["Host"], "example.com")

    def test_redirect_destination_is_revalidated_and_pinned(self):
        with mock.patch(
            "telegram_managed_sources._fetch_one_hop",
            side_effect=[
                (302, {"location": "https://second.example/sub"}, b""),
                (200, {}, b"vless://test"),
            ],
        ) as one_hop:
            text = managed.fetch_subscription("https://first.example/sub")

        self.assertEqual(text, "vless://test")
        self.assertEqual(one_hop.call_count, 2)
        self.assertEqual(one_hop.call_args_list[0].args[0], "https://first.example/sub")
        self.assertEqual(one_hop.call_args_list[1].args[0], "https://second.example/sub")

    def test_valid_config_count_supports_plain_and_base64_subscriptions(self):
        config = (
            "vless://00000000-0000-0000-0000-000000000000@example.com:443"
            "?encryption=none&security=tls&type=ws#test"
        )
        self.assertGreaterEqual(managed.valid_config_count(config), 1)
        encoded = base64.b64encode(config.encode()).decode()
        self.assertGreaterEqual(managed.valid_config_count(encoded), 1)


class BotSourceCommandParsingTests(unittest.TestCase):
    def setUp(self):
        self.restore = extension.install(control)

    def tearDown(self):
        self.restore()

    def test_private_bare_url_becomes_clean_source_add_action(self):
        url = "https://example.com/sub?token=very-secret"
        update = {
            "update_id": 1,
            "message": {
                "from": {"id": 123},
                "chat": {"id": 123, "type": "private"},
                "text": url,
            },
        }
        action, user_id, chat_id, thread_id, callback_id = control.update_to_action(update)
        self.assertEqual(action, "source_add")
        self.assertNotIn("secret", action)
        self.assertEqual((user_id, chat_id), (123, 123))
        self.assertIsNone(thread_id)
        self.assertIsNone(callback_id)

    def test_group_bare_url_is_not_auto_added(self):
        update = {
            "update_id": 2,
            "message": {
                "from": {"id": 123},
                "chat": {"id": -1001, "type": "supergroup"},
                "text": "https://example.com/sub",
            },
        }
        action, *_ = control.update_to_action(update)
        self.assertIsNone(action)

    def test_source_list_button_is_recognized_without_url_leak(self):
        update = {
            "update_id": 3,
            "callback_query": {
                "id": "cb",
                "from": {"id": 123},
                "data": "source:list",
                "message": {"chat": {"id": 123, "type": "private"}},
            },
        }
        action, user_id, chat_id, _, callback_id = control.update_to_action(update)
        self.assertEqual(action, "source_list")
        self.assertEqual((user_id, chat_id, callback_id), (123, 123, "cb"))


if __name__ == "__main__":
    unittest.main()
