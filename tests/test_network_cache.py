import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import validator


class NetworkCacheTests(unittest.TestCase):
    def setUp(self):
        validator._geo_cache.clear()
        validator._geo_requests.clear()
        validator._geo_inflight.clear()

    def test_country_cache_is_per_ip_and_unknown_is_retried_after_short_ttl(self):
        unknown = io.BytesIO(json.dumps({"status": "fail"}).encode())
        known = io.BytesIO(json.dumps({"status": "success", "countryCode": "DE", "country": "Germany"}).encode())
        with patch.object(validator.time, "monotonic", return_value=100) as clock, \
             patch.object(validator.urllib.request, "urlopen", side_effect=[unknown, known]) as request:
            self.assertEqual(validator.lookup_country("8.8.8.8")["country"], "XX")
            self.assertEqual(validator.lookup_country("8.8.8.8")["country"], "XX")
            request.assert_called_once()
            clock.return_value = 401
            self.assertEqual(validator.lookup_country("8.8.8.8")["country"], "DE")
            self.assertEqual(request.call_count, 2)

    def test_geoip_can_be_disabled_and_request_budget_fails_to_unknown(self):
        with patch.dict(os.environ, {"BROUTE_GEOIP_ENABLED": "0"}), patch.object(validator.urllib.request, "urlopen") as request:
            self.assertEqual(validator.lookup_country("8.8.8.8")["country"], "XX")
            request.assert_not_called()
        with patch.object(validator.time, "monotonic", return_value=100), patch.object(validator.urllib.request, "urlopen") as request:
            validator._geo_requests.extend([99] * 30)
            self.assertEqual(validator.lookup_country("1.1.1.1")["country"], "XX")
            request.assert_not_called()

    def test_explicit_dns_timeout_does_not_attempt_a_socket(self):
        with patch.dict(os.environ, {"BROUTE_BOUNDED_DNS": "1"}), \
             patch("proxy_probe.resolve_public", side_effect=subprocess.TimeoutExpired("resolver", 2)), \
             patch.object(validator.socket, "create_connection") as connect:
            self.assertFalse(validator.resolve_and_check_tcp("example.com", 443)["online"])
            connect.assert_not_called()
