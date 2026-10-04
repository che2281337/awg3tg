from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

ACCESS_MODES = ("open", "approval", "closed")


def _ids(value: str) -> set[int]:
    return {int(x) for x in value.replace(" ", "").split(",") if x}


@dataclass
class Settings:
    bot_token: str
    admin_ids: set[int] = field(default_factory=set)
    # Публичный IP/домен сервера, который попадёт в Endpoint ключа.
    server_host: str = ""
    server_name: str = "AmneziaWG"
    access_mode: str = "approval"
    max_keys_per_user: int = 1
    dns1: str = "1.1.1.1"
    dns2: str = "1.0.0.1"
    client_mtu: str | None = None
    awg_container: str | None = None
    awg_config_path: str | None = None
    awg_interface: str | None = None
    awg_bin: str | None = None
    docker_bin: str = "docker"
    db_path: str = "data/bot.db"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")
        mode = os.getenv("ACCESS_MODE", "approval").strip().lower()
        if mode not in ACCESS_MODES:
            raise SystemExit(f"ACCESS_MODE должен быть одним из: {', '.join(ACCESS_MODES)}")
        return cls(
            bot_token=token,
            admin_ids=_ids(os.getenv("ADMIN_IDS", "")),
            server_host=os.getenv("SERVER_HOST", "").strip(),
            server_name=os.getenv("SERVER_NAME", "AmneziaWG").strip() or "AmneziaWG",
            access_mode=mode,
            max_keys_per_user=int(os.getenv("MAX_KEYS_PER_USER", "1")),
            dns1=os.getenv("DNS1", "1.1.1.1").strip(),
            dns2=os.getenv("DNS2", "1.0.0.1").strip(),
            client_mtu=os.getenv("CLIENT_MTU", "").strip() or None,
            awg_container=os.getenv("AWG_CONTAINER", "").strip() or None,
            awg_config_path=os.getenv("AWG_CONFIG_PATH", "").strip() or None,
            awg_interface=os.getenv("AWG_INTERFACE", "").strip() or None,
            awg_bin=os.getenv("AWG_BIN", "").strip() or None,
            docker_bin=os.getenv("DOCKER_BIN", "docker").strip() or "docker",
            db_path=os.getenv("DB_PATH", "data/bot.db").strip(),
        )
