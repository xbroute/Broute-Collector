import base64
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from common import parse_config_line
from deduplicator import canonical_raw_connection_key
from parser import parse_all, extract_lines


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class ParserReliabilityTests(unittest.TestCase):
    def test_non_object_vmess_does_not_abort_other_configs(self):
        for payload in ([], None, 17, "bad"):
            with self.subTest(payload=payload):
                source = {"content": f"vmess://{b64(json.dumps(payload))}\nvless://id@8.8.8.8:443"}
                self.assertEqual(len(parse_all([source])), 1)

    def test_vmess_fragment_is_only_display_metadata(self):
        raw = "vmess://" + b64(json.dumps({"add": "8.8.8.8", "port": 443, "id": "test"}))
        self.assertTrue(parse_config_line(raw + "#a-label").valid)

    def test_non_finite_vmess_port_does_not_abort_collection(self):
        for port in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(port=port):
                raw = "vmess://" + b64(json.dumps({"add": "8.8.8.8", "port": port, "id": "test"}))
                source = {"content": raw + "\nvless://id@8.8.8.8:443"}
                self.assertEqual(len(parse_all([source])), 1)

    def test_shadowsocks_ipv6_and_escaped_password(self):
        raw = "ss://2022-blake3-aes-128-gcm:pass%3Aword%2B%2F%3D@[2606:4700:4700::1111]:443/?plugin=x#label"
        config = parse_config_line(raw)
        self.assertTrue(config.valid)
        self.assertEqual(config.address, "2606:4700:4700::1111")
        self.assertEqual(config.uuid_or_password, "pass:word+/=")

    def test_encoded_shadowsocks_ipv6(self):
        for raw in (
            f"ss://{b64('aes-256-gcm:pw')}@[2606:4700:4700::1111]:443",
            f"ss://{b64('aes-256-gcm:pw@[2606:4700:4700::1111]:443')}",
        ):
            with self.subTest(raw=raw):
                self.assertTrue(parse_config_line(raw).valid)

    def test_required_credentials_are_not_optional(self):
        for scheme in ("vless", "trojan", "tuic"):
            with self.subTest(scheme=scheme):
                self.assertFalse(parse_config_line(f"{scheme}://8.8.8.8:443").valid)

    def test_hysteria2_uri_defaults_to_tls_and_quic(self):
        config = parse_config_line("hy2://password@8.8.8.8?sni=example.com")
        self.assertTrue(config.valid)
        self.assertEqual(config.port, 443)
        self.assertTrue(config.tls)
        self.assertEqual(config.transport, "quic")

    def test_explicit_zero_hysteria2_port_is_not_the_default(self):
        self.assertFalse(parse_config_line("hy2://password@8.8.8.8:0").valid)

    def test_case_sensitive_query_names_keep_distinct_connections(self):
        one = "vless://id@8.8.8.8:443?path=/one"
        two = one.replace("path=", "Path=")
        self.assertNotEqual(canonical_raw_connection_key(one, "vless"),
                            canonical_raw_connection_key(two, "vless"))

    def test_wrapped_subscription_base64_padding(self):
        raw = "vless://id@8.8.8.8:443?security=tls#test"
        encoded = b64(raw)
        wrapped = "\r\n".join(encoded[i:i + 13] for i in range(0, len(encoded), 13))
        self.assertEqual(extract_lines(wrapped), [raw])

    def test_shared_address_space_is_rejected(self):
        self.assertFalse(parse_config_line("vless://id@100.64.0.1:443").valid)


class ShadowsocksIdentityTests(unittest.TestCase):
    def key(self, raw):
        return canonical_raw_connection_key(raw, "shadowsocks")

    def test_legacy_base64_case_is_connection_data(self):
        # Decoding cGFz... vs cGFZ... produces different passwords. Hostname
        # lowercasing must never be applied to a legacy credential payload.
        one = f"ss://{b64('aes-256-gcm:password@8.8.8.8:443')}"
        two = one.replace("cGFz", "cGFZ")
        self.assertNotEqual(one, two)
        self.assertNotEqual(self.key(one), self.key(two))

    def test_equivalent_shadowsocks_encodings_share_identity(self):
        forms = [
            "ss://aes-256-gcm:pw@EXAMPLE.com:443#one",
            f"ss://{b64('aes-256-gcm:pw')}@example.com:443#two",
            f"ss://{b64('aes-256-gcm:pw@example.com:443')}#three",
        ]
        self.assertEqual(len({self.key(raw) for raw in forms}), 1)

    def test_shadowsocks_plugin_changes_are_distinct(self):
        raw = f"ss://{b64('aes-256-gcm:pw')}@example.com:443/?plugin="
        self.assertNotEqual(self.key(raw + "one"), self.key(raw + "two"))
        self.assertEqual(self.key("malformed"), self.key("malformed#label"))


if __name__ == "__main__":
    unittest.main()
