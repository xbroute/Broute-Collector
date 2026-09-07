import base64
import json
import os
import sys
import unittest
from urllib.parse import unquote, urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from generator import _apply_display_name


class SubscriptionBrandingTests(unittest.TestCase):
    def test_vless_fragment_includes_country_and_brand(self):
        record = {
            "country": "DE",
            "country_name": "Germany",
            "protocol": "vless",
            "raw": "vless://user@example.com:443?security=tls&type=ws#OldName",
        }

        result = _apply_display_name(record)

        self.assertEqual(result["name"], "🇩🇪 Germany | @xbroute")
        self.assertEqual(
            unquote(urlsplit(result["raw"]).fragment),
            "🇩🇪 Germany | @xbroute",
        )
        self.assertNotIn("OldName", result["raw"])

    def test_vmess_ps_includes_country_and_brand(self):
        payload = {
            "v": "2",
            "ps": "OldName",
            "add": "example.com",
            "port": "443",
            "id": "11111111-1111-1111-1111-111111111111",
            "aid": "0",
            "net": "ws",
            "type": "none",
            "host": "example.com",
            "path": "/",
            "tls": "tls",
        }
        encoded = base64.b64encode(
            json.dumps(payload).encode("utf-8")
        ).decode("utf-8")
        record = {
            "country": "US",
            "country_name": "United States",
            "protocol": "vmess",
            "raw": f"vmess://{encoded}",
        }

        result = _apply_display_name(record)
        decoded = base64.b64decode(
            result["raw"][len("vmess://"):]
        ).decode("utf-8")
        data = json.loads(decoded)

        self.assertEqual(result["name"], "🇺🇸 United States | @xbroute")
        self.assertEqual(data["ps"], "🇺🇸 United States | @xbroute")

    def test_unknown_country_keeps_brand_only(self):
        record = {
            "country": "XX",
            "country_name": "Unknown",
            "protocol": "trojan",
            "raw": "trojan://password@example.com:443?security=tls#OldName",
        }

        result = _apply_display_name(record)

        self.assertEqual(result["name"], "@xbroute")
        self.assertEqual(unquote(urlsplit(result["raw"]).fragment), "@xbroute")


if __name__ == "__main__":
    unittest.main()
