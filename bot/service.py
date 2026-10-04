"""Связка БД + AWG-сервера: выдача, повторная отправка и отзыв ключей."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import urllib.request
from dataclasses import dataclass

import segno

from .awg.export import ClientParams, build_native_config, build_vpn_url
from .awg.server import AwgServer, ServerInfo
from .config import Settings
from .db import Database, Key

log = logging.getLogger(__name__)


@dataclass
class RenderedKey:
    key: Key
    vpn_url: str
    conf: str

    @property
    def filename(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.key.name).strip("_") or "awg"
        return f"{safe[:40]}.conf"

    def qr_png(self) -> bytes | None:
        """QR с .conf — его понимают и AmneziaVPN, и AmneziaWG."""
        try:
            buf = io.BytesIO()
            segno.make(self.conf, error="l", micro=False).save(buf, kind="png", scale=5, border=2)
            return buf.getvalue()
        except Exception:  # конфиг может не влезть в QR
            log.warning("Не удалось построить QR для ключа %s", self.key.id)
            return None


class LimitReached(Exception):
    pass


class KeyService:
    def __init__(self, settings: Settings, db: Database, server: AwgServer) -> None:
        self.settings = settings
        self.db = db
        self.server = server
        self.host = settings.server_host
        self._issue_lock = asyncio.Lock()

    async def start(self) -> None:
        await self.server.detect()
        if not self.host:
            self.host = detect_public_ip()
            log.info("SERVER_HOST не задан, определён автоматически: %s", self.host)
        info = await self.server.server_info()
        log.info(
            "AWG сервер готов: порт %s, параметры: %s",
            info.port,
            ", ".join(sorted(info.awg_params)) or "нет (обычный WireGuard?)",
        )

    def user_limit(self, key_limit: int | None) -> int:
        return self.settings.max_keys_per_user if key_limit is None else key_limit

    def _params(self, key: Key, info: ServerInfo) -> ClientParams:
        return ClientParams(
            host=self.host,
            port=info.port,
            client_ip=key.ip,
            client_private_key=key.private_key,
            client_public_key=key.public_key,
            server_public_key=info.server_public_key,
            preshared_key=info.preshared_key,
            awg_params=info.awg_params,
            dns1=self.settings.dns1,
            dns2=self.settings.dns2,
            container=info.container,
            subnet_address=info.subnet_address,
            subnet_cidr=info.subnet_cidr,
            description=self.settings.server_name,
            mtu=self.settings.client_mtu,
        )

    async def render(self, key: Key) -> RenderedKey:
        # Параметры обфускации читаются с сервера каждый раз: если их поменяли
        # в приложении Amnezia, повторно отправленный ключ будет актуальным.
        info = await self.server.server_info()
        params = self._params(key, info)
        return RenderedKey(key, build_vpn_url(params), build_native_config(params))

    async def issue(self, tg_id: int | None, name: str, *, key_limit: int | None = None) -> RenderedKey:
        async with self._issue_lock:
            if tg_id is not None and key_limit is not None:
                if len(await self.db.user_keys(tg_id)) >= key_limit:
                    raise LimitReached
            peer, info = await self.server.add_peer(name)
            try:
                key = await self.db.add_key(tg_id, name, peer.public_key, peer.private_key, peer.ip)
            except Exception:
                await self.server.remove_peer(peer.public_key)
                raise
        params = self._params(key, info)
        return RenderedKey(key, build_vpn_url(params), build_native_config(params))

    async def revoke(self, key: Key) -> None:
        await self.server.remove_peer(key.public_key)
        await self.db.delete_key(key.id)


def detect_public_ip() -> str:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                ip = resp.read().decode().strip()
                if ip:
                    return ip
        except Exception:
            continue
    raise SystemExit("Не удалось определить публичный IP. Укажите SERVER_HOST в .env")


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"
