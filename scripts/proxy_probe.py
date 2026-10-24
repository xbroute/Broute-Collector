"""Bounded application-traffic validation through sing-box, without direct fallback."""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from common import _parse_generic_uri, _parse_shadowsocks, _parse_vmess, detect_protocol, safe_b64decode, shadowsocks_uri_parts


class UnsupportedConfig(ValueError):
    pass


_dns_cache = {}
_dns_lock = threading.Lock()


def outbound_from_uri(raw):
    protocol = detect_protocol(raw)
    if len(raw) > 16384 or protocol not in {"vless", "vmess", "trojan", "shadowsocks", "hysteria2", "tuic"}:
        raise UnsupportedConfig("unsupported protocol")
    parsed = _parse_vmess(raw) if protocol == "vmess" else _parse_shadowsocks(raw) if protocol == "shadowsocks" else _parse_generic_uri(raw, protocol)
    if not parsed.valid:
        raise UnsupportedConfig("invalid config")
    vmess = None
    if protocol == "vmess":
        vmess = json.loads(safe_b64decode(raw[8:].split("#", 1)[0]))
        query = {"type": str(vmess.get("net") or "tcp"), "path": str(vmess.get("path") or "/"),
                 "host": str(vmess.get("host") or ""), "sni": str(vmess.get("sni") or vmess.get("host") or parsed.address),
                 "security": str(vmess.get("tls") or "none"), "serviceName": str(vmess.get("path") or "")}
        uri = None
    elif protocol == "shadowsocks":
        method, password, uri = shadowsocks_uri_parts(raw)
        query = {key: values[-1] for key, values in parse_qs(uri.query).items()}
        if query.get("plugin"):
            raise UnsupportedConfig("Shadowsocks plugin unsupported")
    else:
        uri = urlsplit(raw)
        query = {key: values[-1] for key, values in parse_qs(uri.query).items()}
    result = {"type": protocol, "tag": "proxy", "server": parsed.address, "server_port": parsed.port, "connect_timeout": "5s"}
    if protocol in {"vless", "vmess", "tuic"}:
        result["uuid"] = str(uuid.UUID(parsed.uuid_or_password))
    if protocol == "vless":
        if query.get("encryption", "none") != "none":
            raise UnsupportedConfig("VLESS encryption extension unsupported")
        if query.get("flow"):
            result["flow"] = query["flow"]
    elif protocol == "vmess":
        result.update(security=str(vmess.get("scy") or "auto"), alter_id=int(vmess.get("aid") or 0))
    elif protocol == "trojan":
        result["password"] = unquote(uri.netloc.rsplit("@", 1)[0])
    elif protocol == "shadowsocks":
        result.update(method=method, password=password)
    elif protocol == "hysteria2":
        result["password"] = unquote(uri.netloc.rsplit("@", 1)[0]) if "@" in uri.netloc else ""
        if query.get("obfs"):
            if query["obfs"] != "salamander":
                raise UnsupportedConfig("unsupported Hysteria obfuscation")
            result["obfs"] = {"type": "salamander", "password": query.get("obfs-password", query.get("obfs_password", ""))}
        if query.get("mport"):
            raise UnsupportedConfig("port hopping unsupported")
    elif protocol == "tuic":
        result["password"] = unquote(uri.password or "")
        result["congestion_control"] = query.get("congestion_control", "cubic")
        result["udp_relay_mode"] = query.get("udp_relay_mode", "native")
    security = query.get("security", "tls" if parsed.tls else "none").lower()
    if parsed.tls or protocol in {"hysteria2", "tuic"}:
        tls = {"enabled": True, "server_name": query.get("sni") or query.get("peer") or parsed.address,
               "insecure": query.get("insecure", query.get("allowInsecure", "0")).lower() in {"1", "true"}}
        if query.get("alpn"):
            tls["alpn"] = query["alpn"].split(",")
        if security == "reality":
            tls["reality"] = {"enabled": True, "public_key": query.get("pbk", ""), "short_id": query.get("sid", "")}
            tls["utls"] = {"enabled": True, "fingerprint": query.get("fp", "chrome")}
        elif query.get("fp"):
            tls["utls"] = {"enabled": True, "fingerprint": query["fp"]}
        result["tls"] = tls
    if protocol in {"vless", "vmess", "trojan"}:
        transport = query.get("type", query.get("network", "tcp"))
        if transport in {"tcp", "raw"}:
            if query.get("headerType", "none") != "none":
                raise UnsupportedConfig("TCP camouflage unsupported")
        elif transport in {"ws", "httpupgrade"}:
            result["transport"] = {"type": transport, "path": query.get("path", "/")}
            if query.get("host"):
                result["transport"].update({"headers": {"Host": query["host"]}} if transport == "ws" else {"host": query["host"]})
        elif transport == "grpc":
            result["transport"] = {"type": "grpc", "service_name": query.get("serviceName", "")}
        elif transport in {"http", "h2"}:
            result["transport"] = {"type": "http", "host": [query.get("host") or parsed.address], "path": query.get("path", "/")}
        else:
            raise UnsupportedConfig("transport unsupported")
    return result


def resolve_public(host, port, timeout, *, allow_private=False):
    key = host, port, allow_private
    with _dns_lock:
        cached = _dns_cache.get(key)
        if cached and cached[0] > time.monotonic():
            return list(cached[1])
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        code = "import socket,json,sys; print(json.dumps(list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),type=socket.SOCK_STREAM)))))"
        result = subprocess.run([sys.executable, "-c", code, host, str(port)], capture_output=True, timeout=timeout, check=True)
        if len(result.stdout) > 65536:
            raise ValueError("oversized DNS response")
        addresses = json.loads(result.stdout)
    public = [address for address in addresses if allow_private or
              (ipaddress.ip_address(address).is_global and not ipaddress.ip_address(address).is_multicast)]
    if not public or len(public) != len(addresses):
        raise ValueError("endpoint has no permitted public address")
    with _dns_lock:
        if len(_dns_cache) >= 1024:
            _dns_cache.pop(next(iter(_dns_cache)))
        _dns_cache[key] = time.monotonic() + 60, tuple(public)
    return public


def _receive(sock, count, deadline):
    result = b""
    while len(result) < count:
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        block = sock.recv(count - len(result))
        if not block or time.monotonic() >= deadline:
            raise OSError("proxy handshake interrupted")
        result += block
    return result


def request_through_socks(port, username, password, url, deadline):
    target = urlsplit(url)
    target_port = target.port or (443 if target.scheme == "https" else 80)
    with socket.create_connection(("127.0.0.1", port), timeout=max(.01, deadline - time.monotonic())) as stream:
        stream.sendall(b"\x05\x01\x02")
        if _receive(stream, 2, deadline) != b"\x05\x02":
            raise OSError("SOCKS authentication not selected")
        user, secret = username.encode(), password.encode()
        stream.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(secret)]) + secret)
        if _receive(stream, 2, deadline) != b"\x01\x00":
            raise OSError("SOCKS authentication failed")
        host = target.hostname.encode("idna")
        stream.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack("!H", target_port))
        version, status, _, kind = _receive(stream, 4, deadline)
        if version != 5 or status != 0:
            raise OSError("proxy connection rejected")
        count = 4 if kind == 1 else 16 if kind == 4 else _receive(stream, 1, deadline)[0] if kind == 3 else 0
        if not count:
            raise OSError("invalid SOCKS reply")
        _receive(stream, count + 2, deadline)
        stream.settimeout(max(.01, deadline - time.monotonic()))
        request = f"GET {target.path or '/'}{'?' + target.query if target.query else ''} HTTP/1.1\r\nHost: {target.netloc}\r\nConnection: close\r\n\r\n".encode("ascii")
        def exchange(connection):
            connection.sendall(request)
            header = b""
            while b"\r\n" not in header and len(header) < 4096:
                header += _receive(connection, 1, deadline)
            return header.split(b" ", 2)[1] == b"204"
        if target.scheme == "https":
            with ssl.create_default_context().wrap_socket(stream, server_hostname=target.hostname) as secure:
                return exchange(secure)
        return exchange(stream)


def probe(raw, *, timeout=10, url=None, binary=None, _allow_test_network=False):
    start = time.monotonic()
    deadline = start + timeout
    result = {"online": False, "resolved": False, "ip": None, "latency_ms": None,
              "validation_method": "proxy_https", "probe_status": "failed", "probe_at": int(time.time()),
              "proxy_tls_verification": "unverified", "probe_target_tls_verified": False}
    url = url or os.environ.get("BROUTE_PROBE_URL", "https://www.gstatic.com/generate_204")
    target = urlsplit(url)
    if not target.hostname or target.username or target.password or any(c.isspace() or ord(c) < 32 for c in url) or (target.scheme != "https" and not _allow_test_network):
        raise ValueError("probe target must be an HTTPS URL without credentials")
    executable = binary or shutil.which("sing-box")
    if not executable:
        return dict(result, probe_status="engine_unavailable")
    try:
        outbound = outbound_from_uri(raw)
        addresses = resolve_public(outbound["server"], outbound["server_port"], min(2, timeout), allow_private=_allow_test_network)
        outbound["server"] = addresses[0]
        result.update(resolved=True, ip=addresses[0])
        tls = outbound.get("tls", {})
        verification = ("reality" if tls.get("reality") else "disabled" if tls.get("insecure") else "pki") if tls else "not_applicable"
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        username, password = secrets.token_hex(8), secrets.token_hex(16)
        config = {"log": {"disabled": True}, "inbounds": [{"type": "socks", "listen": "127.0.0.1", "listen_port": port,
                  "users": [{"username": username, "password": password}]}], "outbounds": [outbound], "route": {"final": "proxy"}}
        with tempfile.TemporaryDirectory(prefix="broute-probe-") as directory:
            filename = Path(directory) / "config.json"
            filename.write_text(json.dumps(config))
            filename.chmod(0o600)
            checked = subprocess.run([executable, "check", "-c", str(filename)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     timeout=max(.01, deadline - time.monotonic()))
            if checked.returncode:
                return dict(result, probe_status="unsupported_config")
            process = subprocess.Popen([executable, "run", "-c", str(filename)], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        result["online"] = request_through_socks(port, username, password, url, deadline)
                        break
                    except ConnectionRefusedError:
                        time.sleep(.02)
                if result["online"]:
                    result.update(probe_status="passed", latency_ms=int((time.monotonic() - start) * 1000),
                                  proxy_tls_verification=verification, probe_target_tls_verified=target.scheme == "https")
                return result
            finally:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
    except (UnsupportedConfig, ValueError, KeyError, TypeError, OverflowError, IndexError):
        return dict(result, probe_status="unsupported_config")
    except (OSError, subprocess.SubprocessError):
        return result


def validate_with_proxy(server, previous=None):
    from validator import validate_server
    result = probe(str(server.get("raw") or ""))
    if result["probe_status"] in {"unsupported_config", "engine_unavailable"}:
        from validation_scheduler import carry_forward_validation
        value = carry_forward_validation(server, previous)
        value.update(status="unknown", latency=None, should_remove=False,
                     last_checked=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    else:
        value = validate_server(server, previous, checker=lambda *_: result)
    for name in ("validation_method", "probe_status", "probe_at", "proxy_tls_verification", "probe_target_tls_verified"):
        if name in result:
            value[name] = result[name]
    return value
