"""Encrypt OKX API credentials inside the Hummingbot container.

The host writes ``conf/.setup_keys.json`` and the keystore password via
``HBOT_PASSWORD``. This process deletes the plaintext key file before exiting.
"""

import json
import os
import sys
from pathlib import Path

KEYS_PATH = Path("/home/hummingbot/conf/.setup_keys.json")


def main() -> None:
    password = os.environ.get("HBOT_PASSWORD") or ""
    if not password:
        sys.stderr.write("HBOT_PASSWORD is not set\n")
        sys.exit(1)
    if not KEYS_PATH.is_file():
        sys.stderr.write("setup key file is missing\n")
        sys.exit(1)
    try:
        payload = json.loads(KEYS_PATH.read_text(encoding="utf-8"))
        from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger, store_password_verification
        from hummingbot.client.config.config_helpers import ClientConfigAdapter
        from hummingbot.client.config.security import Security
        from hummingbot.client.settings import AllConnectorSettings

        secrets = ETHKeyFileSecretManger(password)
        if Security.new_password_required():
            store_password_verification(secrets)
        if not Security.login(secrets):
            sys.stderr.write("invalid password\n")
            sys.exit(1)
        config_keys = AllConnectorSettings.get_connector_config_keys("okx_perpetual")
        if config_keys is None:
            sys.stderr.write("okx_perpetual connector is unavailable\n")
            sys.exit(1)
        cfg = ClientConfigAdapter(config_keys)
        for field in ("okx_perpetual_api_key", "okx_perpetual_secret_key", "okx_perpetual_passphrase"):
            if not payload.get(field):
                sys.stderr.write(f"missing {field}\n")
                sys.exit(1)
            setattr(cfg, field, payload[field])
        Security.update_secure_config(cfg)
    finally:
        KEYS_PATH.unlink(missing_ok=True)
    print("okx_perpetual keys saved")


if __name__ == "__main__":
    main()
