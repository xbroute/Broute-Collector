import pathlib
import socket
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import validator


def info(ip, family=socket.AF_INET):
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))


class ValidatorReliabilityTests(unittest.TestCase):
    def test_ipv6_endpoint_is_checked(self):
        ip = "2606:4700:4700::1111"
        with (
            patch.object(socket, "getaddrinfo", return_value=[info(ip, socket.AF_INET6)]),
            patch.object(socket, "gethostbyname", side_effect=socket.gaierror),
            patch.object(socket, "create_connection", return_value=MagicMock()) as connect,
        ):
            result = validator.resolve_and_check_tcp(ip, 443)
        self.assertTrue(result["online"])
        self.assertEqual(connect.call_args.args[0], (ip, 443))

    def test_unreachable_first_address_does_not_hide_working_second_address(self):
        with (
            patch.object(socket, "getaddrinfo", return_value=[info("8.8.8.8"), info("1.1.1.1")]),
            patch.object(socket, "gethostbyname", return_value="8.8.8.8"),
            patch.object(socket, "create_connection", side_effect=[OSError("down"), MagicMock()]),
        ):
            result = validator.resolve_and_check_tcp("example.com", 443)
        self.assertTrue(result["online"])
        self.assertEqual(result["ip"], "1.1.1.1")

    def test_dns_cannot_turn_public_config_into_private_socket(self):
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "::1"):
            with (
                self.subTest(ip=ip),
                patch.object(socket, "getaddrinfo", return_value=[info(ip)]),
                patch.object(socket, "gethostbyname", return_value=ip),
                patch.object(socket, "create_connection", return_value=MagicMock()) as connect,
            ):
                self.assertFalse(validator.resolve_and_check_tcp("example.com", 443)["online"])
                connect.assert_not_called()

    def test_failure_keeps_last_success_timestamp(self):
        previous = {"country": "US", "country_name": "United States", "last_seen": "2026-09-29T01:00:00Z", "success_count": 2}
        check = {"resolved": False, "online": False, "latency_ms": None, "ip": None}
        with patch.object(validator, "resolve_and_check_tcp", return_value=check):
            result = validator.validate_server({"address": "example.com", "port": 443}, previous)
        self.assertEqual(result["last_seen"], previous["last_seen"])
        self.assertEqual(result["success_count"], 2)

    def test_country_response_with_wrong_shape_is_nonfatal(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"[]"
        with patch.object(validator.urllib.request, "urlopen", return_value=response):
            self.assertEqual(validator.lookup_country("8.8.8.8")["country"], "XX")


if __name__ == "__main__":
    unittest.main()
