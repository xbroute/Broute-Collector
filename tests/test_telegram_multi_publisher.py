import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import telegram_destinations as destinations
import telegram_multi_publisher as multi


def sample_server(server_id="srv-1", raw=None):
    return {
        "id": server_id,
        "status": "online",
        "valid": True,
        "should_remove": False,
        "raw": raw or f"vless://uuid-{server_id}@example.com:443?security=tls&type=ws#old",
        "protocol": "vless",
        "address": "example.com",
        "port": 443,
        "transport": "ws",
        "security": "tls",
        "tls": True,
        "latency": 44,
        "country": "US",
        "country_name": "United States",
    }


def destination(template=None):
    return {
        "key": "abc",
        "chat_id": -100123,
        "title": "Example Channel",
        "chat_type": "channel",
        "bot_status": "administrator",
        "enabled": True,
        "min_delay_seconds": 30,
        "max_delay_seconds": 90,
        "template": template or destinations.DEFAULT_TEMPLATE,
        "message_thread_id": None,
    }


class TelegramMultiPublisherTests(unittest.TestCase):
    def test_custom_template_keeps_exact_copy_text(self):
        server = sample_server()
        dest = destination("برای {destination}\n{country}\n\n{config}\n\n{brand}")
        message = multi.render_target_message(dest, server)

        self.assertIn("Example Channel", str(message))
        self.assertIn("<pre>", str(message))
        self.assertEqual(
            message.copy_text,
            multi.base.brand_raw_config(server["raw"], "vless"),
        )

    def test_template_html_is_escaped(self):
        server = sample_server()
        dest = destination("<b>{country}</b>\n\n{config}")
        message = multi.render_target_message(dest, server)
        self.assertIn("&lt;b&gt;United States&lt;/b&gt;", str(message))
        self.assertNotIn("<b>United States</b>", str(message))

    def test_two_destinations_keep_independent_cycle_queues(self):
        servers = [sample_server("a"), sample_server("b")]

        first = multi._default_target_state()
        second = multi._default_target_state()

        online_first = multi.prepare_target_state(first, servers)
        online_second = multi.prepare_target_state(second, servers)
        self.assertEqual(set(first["queue"]), set(online_first.keys()))
        self.assertEqual(set(second["queue"]), set(online_second.keys()))

        sent_id = first["queue"][0]
        fp = multi.base.telegram_fingerprint(online_first[sent_id])
        first["cycle_sent"] = [sent_id]
        first["cycle_sent_fingerprints"] = [fp]
        first["sent"] = [sent_id]
        first["sent_fingerprints"] = [fp]
        first["queue"] = first["queue"][1:]

        multi.prepare_target_state(first, servers)
        multi.prepare_target_state(second, servers)

        self.assertNotIn(sent_id, first["queue"])
        self.assertIn(sent_id, second["queue"])

    def test_empty_completed_cycle_rolls_forward(self):
        servers = [sample_server("a"), sample_server("b")]
        target = multi._default_target_state()
        online = multi.prepare_target_state(target, servers)
        ids = list(online)
        fps = [multi.base.telegram_fingerprint(online[x]) for x in ids]

        target["cycle"] = 7
        target["cycle_sent"] = ids
        target["cycle_sent_fingerprints"] = fps
        target["sent"] = ids
        target["sent_fingerprints"] = fps
        target["queue"] = []

        multi.prepare_target_state(target, servers)
        self.assertEqual(target["cycle"], 8)
        self.assertEqual(set(target["queue"]), set(ids))
        self.assertEqual(target["cycle_sent"], [])
        self.assertEqual(target["cycle_sent_fingerprints"], [])
        self.assertEqual(set(target["sent"]), set(ids))

    def test_payload_targets_channel_without_topic_by_default(self):
        server = sample_server()
        dest = destination()
        message = multi.render_target_message(dest, server)
        payload = multi.build_payload(dest, message)
        self.assertEqual(payload["chat_id"], -100123)
        self.assertNotIn("message_thread_id", payload)
        self.assertEqual(payload["parse_mode"], "HTML")

    def test_payload_uses_topic_when_configured(self):
        server = sample_server()
        dest = destination()
        dest["message_thread_id"] = 1944
        payload = multi.build_payload(dest, multi.render_target_message(dest, server))
        self.assertEqual(payload["message_thread_id"], 1944)


if __name__ == "__main__":
    unittest.main()
