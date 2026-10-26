import sys
import unittest
import socket
import os
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from server_source_fetch import fetch_public_text
from collector import SourceFetchError


class PinnedSourceTests(unittest.TestCase):
    def test_successful_empty_source_is_not_a_fetch_failure(self):
        with patch("server_source_fetch._fetch_one_hop", return_value=(200, {}, b"")):
            self.assertEqual(fetch_public_text("https://example.com/sub", 3, 100, 0, "test"), "")

    def test_redirect_to_private_endpoint_is_rejected_before_second_request(self):
        import telegram_managed_sources as sources
        response = Mock(status=302)
        response.getheaders.return_value = [("Location", "http://169.254.169.254/metadata")]
        answers = [[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))] for address in ("8.8.8.8", "169.254.169.254")]
        with patch.dict(os.environ, {"BROUTE_BOUNDED_DNS": "0"}), \
             patch.object(sources.socket, "getaddrinfo", side_effect=answers), \
             patch.object(sources, "_PinnedHTTPSConnection") as connection:
            connection.return_value.getresponse.return_value = response
            with self.assertRaises(SourceFetchError):
                fetch_public_text("https://example.com/sub", 3, 100, 0, "test")
            connection.assert_called_once()

    def test_each_public_redirect_uses_pinned_fetch_and_body_limit(self):
        with patch("server_source_fetch._fetch_one_hop", side_effect=[(302, {"location": "/next"}, b""), (200, {}, b"text")]) as request:
            self.assertEqual(fetch_public_text("https://example.com/sub", 3, 100, 0, "test"), "text")
            self.assertEqual(request.call_count, 2)
            self.assertEqual(request.call_args.args[0], "https://example.com/next")
        with patch("server_source_fetch._fetch_one_hop", return_value=(200, {}, b"too large")):
            with self.assertRaises(SourceFetchError):
                fetch_public_text("https://example.com/sub", 3, 2, 0, "test")
