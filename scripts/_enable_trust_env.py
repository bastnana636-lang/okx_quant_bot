"""Make Hummingbot's aiohttp session honor HTTP(S)_PROXY.

Needed when the host uses Shadowrocket/Clash TUN fake-ip, which Colima
cannot follow. Safe to run repeatedly.
"""
from pathlib import Path

TARGET = Path("/home/hummingbot/hummingbot/core/web_assistant/connections/connections_factory.py")
OLD = "self._shared_client = aiohttp.ClientSession()"
NEW = "self._shared_client = aiohttp.ClientSession(trust_env=True)"


def main() -> None:
    text = TARGET.read_text()
    if "ClientSession(trust_env=True)" in text:
        return
    if OLD not in text:
        raise SystemExit(f"unexpected connections_factory.py contents: {TARGET}")
    TARGET.write_text(text.replace(OLD, NEW, 1))


if __name__ == "__main__":
    main()
