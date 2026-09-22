#!/usr/bin/env python3
"""Create the local keystore password and encrypted OKX API config once.

``make start`` runs this before the bot. When ``conf/.password_verification``,
``conf/connectors/okx_perpetual.yml`` and ``HBOT_PASSWORD`` in ``.compose.env``
already exist, it does nothing. The password stays on this machine so later
starts do not ask again.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("OKX_TRADER_ROOT", Path(__file__).resolve().parents[1])).resolve()
COMPOSE_ENV = ROOT / ".compose.env"
PASSWORD_FILE = ROOT / "conf" / ".password_verification"
CONNECTOR_FILE = ROOT / "conf" / "connectors" / "okx_perpetual.yml"
KEYS_FILE = ROOT / "conf" / ".setup_keys.json"
WRITE_SCRIPT = ROOT / "scripts" / "write_okx_keys.py"
LEGACY_PROXY = "http://host.docker.internal:7897"
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_env(text: str) -> dict[str, str]:
    env = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if ENV_KEY_RE.fullmatch(key):
            env[key] = value
    return env


def render_env(env: dict[str, str]) -> str:
    order = ["COMPOSE_PROFILES", "HBOT_PASSWORD", "OKX_HTTP_PROXY", "GATEWAY_PASSPHRASE"]
    lines = []
    seen = set()
    for key in order + [key for key in env if key not in order]:
        if key in env and key not in seen:
            lines.append(f"{key}={env[key]}")
            seen.add(key)
    return "\n".join(lines) + "\n"


def shell_exports(env: dict[str, str]) -> str:
    lines = []
    for key, value in env.items():
        if not re.fullmatch(r"[A-Z0-9_]+", key):
            continue
        quoted = "'" + value.replace("'", "'\\''") + "'"
        lines.append(f"export {key}={quoted}")
    return "\n".join(lines)


def setup_status(env: dict[str, str], password_exists: bool, connector_exists: bool) -> dict[str, bool]:
    return {
        "password": bool(env.get("HBOT_PASSWORD")),
        "keystore": password_exists,
        "connector": connector_exists,
    }


def needs_setup(status: dict[str, bool]) -> bool:
    return not all(status.values())


def load_compose_env(path: Path = COMPOSE_ENV) -> dict[str, str]:
    if not path.is_file():
        return {}
    return parse_env(path.read_text(encoding="utf-8"))


def save_compose_env(env: dict[str, str], path: Path = COMPOSE_ENV) -> None:
    path.write_text(render_env(env), encoding="utf-8")
    os.chmod(path, 0o600)


def _prompt_secret(label: str) -> str:
    value = getpass.getpass(f"{label}: ").strip()
    if not value:
        raise SystemExit(f"{label} 不能为空")
    if any(char in value for char in "\n\r\x00"):
        raise SystemExit(f"{label} 不能包含换行")
    return value


def _prompt_password(confirm: bool) -> str:
    password = _prompt_secret("密钥库密码")
    if confirm and password != getpass.getpass("再输入一次密码: ").strip():
        raise SystemExit("两次输入的密码不一致")
    return password


def _prompt_proxy() -> str:
    print("容器访问 OKX 的 HTTP 代理，留空表示直连。")
    print(f"中国大陆常用本地代理，例如 {LEGACY_PROXY}")
    return input("代理: ").strip()


def collect_missing(status: dict[str, bool], replace_keys: bool) -> dict[str, str]:
    if not sys.stdin.isatty():
        raise SystemExit("首次配置需要在交互式终端中运行启动程序")
    print("首次配置只需做一次。密码和 OKX API 保存在本机，不会写入 Git。")
    print("之后启动会自动读取，不需要再次输入。")
    collected = {}
    if not status["password"] or not status["keystore"] or replace_keys:
        collected["password"] = _prompt_password(confirm=not status["keystore"])
    if not status["connector"] or replace_keys:
        collected["okx_perpetual_api_key"] = _prompt_secret("OKX API Key")
        collected["okx_perpetual_secret_key"] = _prompt_secret("OKX Secret Key")
        collected["okx_perpetual_passphrase"] = _prompt_secret("OKX Passphrase")
    if "OKX_HTTP_PROXY" not in load_compose_env() or replace_keys:
        collected["proxy"] = _prompt_proxy()
    return collected


def write_keys_file(payload: dict[str, str], path: Path = KEYS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _compose_up() -> None:
    subprocess.run(
        ["docker", "compose", "--env-file", str(COMPOSE_ENV), "up", "-d", "hummingbot"],
        cwd=ROOT, check=True,
    )


def _docker_python(password: str, script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", "-i", "-e", f"HBOT_PASSWORD={password}", "hummingbot", "python", "-"],
        input=script, text=True, cwd=ROOT,
    )


def verify_password(password: str) -> None:
    _compose_up()
    script = (
        "import os, sys\n"
        "from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger, validate_password\n"
        "ok = validate_password(ETHKeyFileSecretManger(os.environ['HBOT_PASSWORD']))\n"
        "sys.exit(0 if ok else 1)\n"
    )
    if _docker_python(password, script).returncode != 0:
        raise SystemExit("密码不正确，未保存。")


def install_keys(password: str, keys: dict[str, str]) -> None:
    write_keys_file({
        "okx_perpetual_api_key": keys["okx_perpetual_api_key"],
        "okx_perpetual_secret_key": keys["okx_perpetual_secret_key"],
        "okx_perpetual_passphrase": keys["okx_perpetual_passphrase"],
    })
    try:
        _compose_up()
        completed = _docker_python(password, WRITE_SCRIPT.read_text(encoding="utf-8"))
        if completed.returncode != 0:
            raise SystemExit("写入 OKX API 失败。请确认 Docker 已启动，并且密码与已有密钥库一致。")
    finally:
        KEYS_FILE.unlink(missing_ok=True)


def ensure(replace_keys: bool = False) -> dict[str, str]:
    env = load_compose_env()
    env.setdefault("COMPOSE_PROFILES", "")
    env_changed = False
    if not env.get("GATEWAY_PASSPHRASE"):
        env["GATEWAY_PASSPHRASE"] = secrets.token_urlsafe(24)
        env_changed = True
    keystore_exists = PASSWORD_FILE.is_file()
    connector_exists = CONNECTOR_FILE.is_file()
    status = setup_status(env, keystore_exists, connector_exists)
    if connector_exists and not keystore_exists and not replace_keys:
        raise SystemExit(
            "发现已加密的 OKX API，但密钥库校验文件缺失。"
            "请运行 python3 scripts/ensure_setup.py --replace-keys"
        )
    if replace_keys:
        status = {**status, "connector": False}
    if needs_setup(status) or replace_keys:
        collected = collect_missing(status, replace_keys)
        password = collected.get("password") or env.get("HBOT_PASSWORD", "")
        if not password:
            raise SystemExit("缺少密钥库密码")
        if keystore_exists and "password" in collected:
            verify_password(password)
        if "password" in collected:
            env["HBOT_PASSWORD"] = password
        if "proxy" in collected:
            env["OKX_HTTP_PROXY"] = collected["proxy"]
        save_compose_env(env)
        if "okx_perpetual_api_key" in collected:
            install_keys(password, collected)
        print("本机配置已保存。后续启动不会再次询问密码和 OKX API。")
    else:
        if "OKX_HTTP_PROXY" not in env:
            # Existing installs used the compose file's fixed local proxy.
            env["OKX_HTTP_PROXY"] = LEGACY_PROXY
            env_changed = True
        if env_changed:
            save_compose_env(env)
    return load_compose_env()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shell-exports", action="store_true",
                        help="Print shell export statements for .compose.env and exit.")
    parser.add_argument("--replace-keys", action="store_true",
                        help="Prompt again and replace the saved OKX API keys.")
    args = parser.parse_args()
    if args.shell_exports:
        env = load_compose_env()
        if needs_setup(setup_status(env, PASSWORD_FILE.is_file(), CONNECTOR_FILE.is_file())):
            raise SystemExit("本机配置尚未完成，请先运行启动程序")
        print(shell_exports(env))
        return
    ensure(replace_keys=args.replace_keys)


if __name__ == "__main__":
    main()
