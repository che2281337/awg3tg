from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


def _ids(value: str) -> set[int]:
    return {int(x) for x in value.replace(" ", "").split(",") if x}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


@dataclass
class Settings:
    bot_token: str
    admin_ids: set[int] = field(default_factory=set)
    # Публичный IP/домен сервера, который попадёт в Endpoint ключа.
    server_host: str = ""
    server_name: str = "AmneziaWG"
    dns1: str = "1.1.1.1"
    dns2: str = "1.0.0.1"
    client_mtu: str | None = None
    awg_container: str | None = None
    awg_config_path: str | None = None
    awg_interface: str | None = None
    awg_bin: str | None = None
    docker_bin: str = "docker"
    db_path: str = "data/bot.db"
    timezone: str = "Europe/Moscow"

    # Подписка
    currency: str = "₽"
    payment_details: str = "Реквизиты для оплаты не заданы — напишите администратору."
    support: str = ""
    trial_days: int = 3
    trial_devices: int = 1
    ref_bonus_days: int = 7
    # Сколько устройств можно добавить без тарифа (админ может выставить вручную).
    default_devices: int = 1

    # Фоновые задачи
    stats_interval: int = 300

    # Демо-режим без VPN-сервера (см. bot/awg/mock.py)
    awg_mock: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")
        details = os.getenv("PAYMENT_DETAILS", "").strip().replace("\\n", "\n")
        return cls(
            bot_token=token,
            admin_ids=_ids(os.getenv("ADMIN_IDS", "")),
            server_host=os.getenv("SERVER_HOST", "").strip(),
            server_name=os.getenv("SERVER_NAME", "AmneziaWG").strip() or "AmneziaWG",
            dns1=os.getenv("DNS1", "1.1.1.1").strip(),
            dns2=os.getenv("DNS2", "1.0.0.1").strip(),
            client_mtu=os.getenv("CLIENT_MTU", "").strip() or None,
            awg_container=os.getenv("AWG_CONTAINER", "").strip() or None,
            awg_config_path=os.getenv("AWG_CONFIG_PATH", "").strip() or None,
            awg_interface=os.getenv("AWG_INTERFACE", "").strip() or None,
            awg_bin=os.getenv("AWG_BIN", "").strip() or None,
            docker_bin=os.getenv("DOCKER_BIN", "docker").strip() or "docker",
            db_path=os.getenv("DB_PATH", "data/bot.db").strip(),
            timezone=os.getenv("TZ_NAME", "Europe/Moscow").strip() or "Europe/Moscow",
            currency=os.getenv("CURRENCY", "₽").strip() or "₽",
            payment_details=details or cls.payment_details,
            support=os.getenv("SUPPORT", "").strip(),
            trial_days=_int("TRIAL_DAYS", 3),
            trial_devices=_int("TRIAL_DEVICES", 1),
            ref_bonus_days=_int("REF_BONUS_DAYS", 7),
            default_devices=_int("DEFAULT_DEVICES", 1),
            stats_interval=max(60, _int("STATS_INTERVAL", 300)),
            awg_mock=os.getenv("AWG_MOCK", "").strip().lower() in ("1", "true", "yes", "on"),
        )
