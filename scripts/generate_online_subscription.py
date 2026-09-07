from __future__ import annotations

import base64

from common import load_json

SERVERS_PATH = "data/servers.json"
ONLINE_SUB_PATH = "data/online-sub.txt"
ONLINE_SUB_B64_PATH = "data/online-sub-base64.txt"


def eligible_online(server: dict) -> bool:
    return (
        server.get("status") == "online"
        and server.get("valid") is True
        and not server.get("should_remove", False)
        and bool(server.get("raw"))
    )


def online_lines(servers: list[dict]) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for server in servers:
        if not isinstance(server, dict) or not eligible_online(server):
            continue
        raw = str(server.get("raw") or "").strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        lines.append(raw)
    return lines


def write_online_subscription() -> int:
    servers = load_json(SERVERS_PATH, [])
    if not isinstance(servers, list):
        raise RuntimeError("data/servers.json is not a list")

    lines = online_lines(servers)
    plain = "\n".join(lines)
    if plain:
        plain += "\n"

    with open(ONLINE_SUB_PATH, "w", encoding="utf-8") as f:
        f.write(plain)

    encoded = base64.b64encode("\n".join(lines).encode("utf-8")).decode("ascii")
    with open(ONLINE_SUB_B64_PATH, "w", encoding="utf-8") as f:
        f.write(encoded)

    print(f"[online-sub] wrote {len(lines)} currently-online configs")
    return len(lines)


if __name__ == "__main__":
    write_online_subscription()
