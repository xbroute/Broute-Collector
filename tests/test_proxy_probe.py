"""Real protocol tests use only loopback fixtures; no public proxies or Telegram."""
import base64
import datetime
import json
import shutil
import socket
import ssl
import ipaddress
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from proxy_probe import outbound_from_uri, probe, resolve_public, UnsupportedConfig

IDENTITY = "bf000d23-0752-40b4-affe-68f7707a9661"


class ProbePolicyTests(unittest.TestCase):
    def test_unsupported_configs_do_not_starve_later_candidates_and_evidence_carries(self):
        from proxy_probe import validate_with_proxy
        from validation_scheduler import select_for_validation, carry_forward_validation
        records = [{"id": str(i), "raw": f"vless://{IDENTITY}@8.8.8.8:443?type=xhttp", "sources": [{"source_url": "fixture"}]} for i in range(60)]
        selected, _, _ = select_for_validation(records, {}, 40)
        unsupported = {"online": False, "resolved": False, "ip": None, "latency_ms": None,
                       "validation_method": "proxy_https", "probe_status": "unsupported_config", "probe_at": int(time.time())}
        with patch("proxy_probe.probe", return_value=unsupported):
            previous = {row["id"]: validate_with_proxy(row) for row in selected}
        self.assertTrue(all(value["last_checked"] for value in previous.values()))
        following, _, _ = select_for_validation(records, previous, 40)
        unseen = {row["id"] for row in records} - {row["id"] for row in selected}
        self.assertTrue(unseen.issubset({row["id"] for row in following}))
        prior = dict(previous["0"], status="online", probe_status="passed", proxy_tls_verification="pki", probe_target_tls_verified=True)
        carried = carry_forward_validation(records[0], prior)
        for field in ("probe_status", "probe_at", "validation_method", "proxy_tls_verification", "probe_target_tls_verified"):
            self.assertEqual(carried[field], prior[field])

    def test_endpoint_dns_cannot_reach_private_or_multicast_addresses(self):
        for address in ("127.0.0.1", "169.254.169.254", "100.64.0.1", "224.0.0.1", "::1"):
            with self.assertRaises(ValueError):
                resolve_public(address, 443, 1)

    def test_transport_and_tls_fields_are_preserved_and_unsupported_modes_rejected(self):
        value = outbound_from_uri(f"vless://{IDENTITY}@example.com:443?security=reality&pbk=test&sid=1234&type=ws&host=cdn.example&path=%2Fsocket")
        self.assertEqual(value["tls"]["reality"]["public_key"], "test")
        self.assertEqual(value["tls"]["server_name"], "example.com")
        self.assertEqual(value["transport"]["headers"]["Host"], "cdn.example")
        self.assertEqual(value["transport"]["path"], "/socket")
        for raw in (f"vless://{IDENTITY}@example.com:443?type=xhttp", "ss://YWVzLTEyOC1nY206cGFzcw@example.com:443?plugin=unknown"):
            with self.assertRaises(UnsupportedConfig):
                outbound_from_uri(raw)

    def test_missing_engine_and_invalid_target_never_report_success(self):
        with patch("proxy_probe.shutil.which", return_value=None):
            self.assertEqual(probe(f"vless://{IDENTITY}@8.8.8.8:443")["probe_status"], "engine_unavailable")
        with self.assertRaises(ValueError):
            probe(f"vless://{IDENTITY}@8.8.8.8:443", url="http://127.0.0.1/private")


@unittest.skipUnless(shutil.which("sing-box"), "real protocol engines run inside the CI runtime container")
class RealProxyTests(unittest.TestCase):
    def test_application_request_through_six_real_protocols_and_wrong_password(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(204)
                self.end_headers()
            def log_message(self, *_):
                pass
        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        target = f"http://127.0.0.1:{http.server_port}/generate_204"
        binary = shutil.which("sing-box")
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
                now = datetime.datetime.now(datetime.timezone.utc)
                cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1)).not_valid_after(now + datetime.timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False).sign(key, hashes.SHA256())
                (root / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
                (root / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
                tls = {"enabled": True, "certificate_path": str(root / "cert.pem"), "key_path": str(root / "key.pem")}
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(str(root / "cert.pem"), str(root / "key.pem"))
                http.socket = context.wrap_socket(http.socket, server_side=True)
                target = f"https://127.0.0.1:{http.server_port}/generate_204"
                trusted = ssl.create_default_context(cafile=str(root / "cert.pem"))
                for protocol in ("shadowsocks", "vless", "vmess", "trojan", "hysteria2", "tuic"):
                    with self.subTest(protocol=protocol):
                        with socket.socket() as reserve:
                            reserve.bind(("127.0.0.1", 0))
                            port = reserve.getsockname()[1]
                        inbound = {"type": protocol, "listen": "127.0.0.1", "listen_port": port}
                        if protocol == "shadowsocks":
                            inbound.update(method="aes-128-gcm", password="test-password")
                            raw = f"ss://aes-128-gcm:test-password@127.0.0.1:{port}"
                        elif protocol == "vmess":
                            inbound["users"] = [{"uuid": IDENTITY}]
                            payload = {"add": "127.0.0.1", "port": port, "id": IDENTITY, "net": "tcp", "aid": 0}
                            raw = "vmess://" + base64.b64encode(json.dumps(payload).encode()).decode()
                        elif protocol == "vless":
                            inbound["users"] = [{"uuid": IDENTITY}]
                            raw = f"vless://{IDENTITY}@127.0.0.1:{port}"
                        else:
                            inbound["tls"] = tls
                            inbound["users"] = [{"password": "test-password", **({"uuid": IDENTITY} if protocol == "tuic" else {})}]
                            credentials = IDENTITY + ":test-password" if protocol == "tuic" else "test-password"
                            raw = f"{protocol}://{credentials}@127.0.0.1:{port}?insecure=1&sni=localhost"
                        fixture = {"log": {"disabled": True}, "inbounds": [inbound], "outbounds": [{"type": "direct", "tag": "direct"}], "route": {"final": "direct"}}
                        filename = root / "server.json"
                        filename.write_text(json.dumps(fixture))
                        checked = subprocess.run([binary, "check", "-c", str(filename)], capture_output=True)
                        self.assertEqual(checked.returncode, 0, checked.stderr.decode())
                        process = subprocess.Popen([binary, "run", "-c", str(filename)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        try:
                            time.sleep(.1)
                            with patch("proxy_probe.ssl.create_default_context", return_value=trusted):
                                result = probe(raw, url=target, _allow_test_network=True)
                            self.assertTrue(result["online"], result)
                            self.assertTrue(result["probe_target_tls_verified"])
                            if protocol == "shadowsocks":
                                with patch("proxy_probe.ssl.create_default_context", return_value=trusted):
                                    wrong = probe(raw.replace("test-password@", "wrong-password@"), url=target, timeout=2, _allow_test_network=True)
                                self.assertFalse(wrong["online"])
                                untrusted = probe(raw, url=target, _allow_test_network=True)
                                self.assertFalse(untrusted["online"], "untrusted destination certificate must fail")
                        finally:
                            process.terminate()
                            process.wait(timeout=5)
        finally:
            http.shutdown()
            http.server_close()
            thread.join(timeout=2)
