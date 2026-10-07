"""Бизнес-логика: устройства (ключи), подписки, оплаты, рефералы, трафик."""

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
from .db import Database, Key, Payment, User, now
from .utils import today

log = logging.getLogger(__name__)

ADMIN_DEVICE_LIMIT = 100

# Защита от переполнения: SQLite хранит 64-битные числа, а даты дальше 9999 года
# Python не форматирует. Сроки и лимиты ограничены разумными значениями.
MAX_DAYS = 36500  # за одну операцию — 100 лет
MAX_SUB_UNTIL = 4102444800  # 01.01.2100 — дальше подписка не продлевается
MAX_DEVICES = 100


def clamp_ts(ts: int) -> int:
    return max(0, min(int(ts), MAX_SUB_UNTIL))


class ServiceError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


@dataclass
class RenderedKey:
    key: Key
    vpn_url: str
    conf: str
    server_name: str = "awg"

    @property
    def filename(self) -> str:
        # Имя туннеля в AmneziaWG берётся из имени файла: до 15 символов [A-Za-z0-9_=+.-]
        base = re.sub(r"[^A-Za-z0-9_=+.-]+", "", self.server_name)[:10] or "awg"
        return f"{base}_{self.key.id}.conf"

    def qr_png(self) -> bytes | None:
        """QR с .conf — его понимают и AmneziaVPN, и AmneziaWG."""
        try:
            buf = io.BytesIO()
            segno.make(self.conf, error="l", micro=False).save(buf, kind="png", scale=5, border=2)
            return buf.getvalue()
        except Exception:  # конфиг может не влезть в QR
            log.warning("Не удалось построить QR для ключа %s", self.key.id)
            return None


@dataclass
class PaymentResult:
    payment: Payment
    user: User
    referrer: User | None = None


class VpnService:
    def __init__(self, settings: Settings, db: Database, server: AwgServer) -> None:
        self.settings = settings
        self.db = db
        self.server = server
        self.host = settings.server_host
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        await self.server.detect()
        if not self.host:
            self.host = await asyncio.to_thread(detect_public_ip)
            log.info("SERVER_HOST не задан, определён автоматически: %s", self.host)
        info = await self.server.server_info()
        log.info(
            "AWG сервер готов: порт %s, параметры: %s",
            info.port,
            ", ".join(sorted(info.awg_params)) or "нет (обычный WireGuard?)",
        )

    def is_admin(self, tg_id: int | None) -> bool:
        return tg_id is not None and tg_id in self.settings.admin_ids

    # ---------- ключи ----------

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

    def _rendered(self, key: Key, info: ServerInfo) -> RenderedKey:
        params = self._params(key, info)
        return RenderedKey(key, build_vpn_url(params), build_native_config(params), self.settings.server_name)

    async def render(self, key: Key) -> RenderedKey:
        # Параметры обфускации читаются с сервера каждый раз: если их поменяли
        # в приложении Amnezia, повторно отправленный ключ будет актуальным.
        return self._rendered(key, await self.server.server_info())

    def device_limit(self, user: User) -> int:
        if self.is_admin(user.tg_id):
            return ADMIN_DEVICE_LIMIT
        return user.device_limit or self.settings.default_devices

    def has_access(self, user: User) -> bool:
        return not user.banned and (user.active or self.is_admin(user.tg_id))

    @staticmethod
    def _peer_name(user: User, device: str) -> str:
        return f"{device} | {user.title}"

    async def create_device(self, user: User, name: str) -> RenderedKey:
        async with self._lock:
            user = await self.db.get_user(user.tg_id) or user
            if user.banned:
                raise ServiceError("Аккаунт заблокирован.")
            if not self.has_access(user):
                raise ServiceError("Нет активной подписки. Оформите её в разделе «💳 Тарифы».")
            keys = await self.db.user_keys(user.tg_id)
            limit = self.device_limit(user)
            if len(keys) >= limit:
                raise ServiceError(
                    f"Достигнут лимит устройств ({len(keys)}/{limit}). "
                    "Удалите ненужное устройство или выберите тариф с большим числом устройств."
                )
            peer, info = await self.server.add_peer(
                self._peer_name(user, name), reserved=await self.db.reserved_ips()
            )
            try:
                key = await self.db.add_key(user.tg_id, name, peer.public_key, peer.private_key, peer.ip)
            except Exception:
                await self.server.remove_peer(peer.public_key)
                raise
        return self._rendered(key, info)

    async def delete_device(self, key: Key) -> None:
        await self.server.remove_peer(key.public_key)
        await self.db.delete_key(key.id)

    async def rename_device(self, key: Key, name: str) -> None:
        await self.db.update_key(key.id, name=name[:40])

    async def disable_key(self, key: Key) -> None:
        if key.enabled:
            await self.server.remove_peer(key.public_key)
        await self.db.update_key(key.id, enabled=0, last_rx=0, last_tx=0)

    async def enable_key(self, key: Key, user: User | None = None) -> bool:
        """Возвращает True, если у ключа сменился IP (клиенту нужно получить ключ заново)."""
        name = self._peer_name(user, key.name) if user else key.name
        reserved = await self.db.reserved_ips() - {key.ip}
        peer, _ = await self.server.add_peer(
            name, keypair=(key.private_key, key.public_key), ip=key.ip, reserved=reserved
        )
        await self.db.update_key(key.id, enabled=1, ip=peer.ip, last_rx=0, last_tx=0)
        return peer.ip != key.ip

    async def sync_user(self, tg_id: int) -> list[Key]:
        """Приводит ключи на сервере в соответствие с подпиской и лимитом устройств.
        Возвращает ключи, у которых сменился IP."""
        user = await self.db.get_user(tg_id)
        if user is None:
            return []
        keys = await self.db.user_keys(tg_id)
        changed: list[Key] = []
        if not self.has_access(user):
            for k in keys:
                if k.enabled:
                    await self.disable_key(k)
            return changed
        limit = self.device_limit(user)
        for i, k in enumerate(keys):  # самые старые устройства в приоритете
            if i < limit and not k.enabled:
                if await self.enable_key(k, user):
                    changed.append(k)
            elif i >= limit and k.enabled:
                await self.disable_key(k)
        return changed

    # ---------- подписка ----------

    async def extend(self, tg_id: int, days: int, devices: int | None = None) -> User:
        user = await self.db.get_user(tg_id)
        if user is None:
            raise ServiceError("Пользователь не найден.")
        if not -MAX_DAYS <= days <= MAX_DAYS:
            raise ServiceError(f"Срок должен быть от -{MAX_DAYS} до {MAX_DAYS} дней.")
        base = max(now(), user.sub_until or 0)
        fields: dict = {"sub_until": clamp_ts(base + days * 86400), "notified": 0}
        if devices is not None:
            fields["device_limit"] = max(0, min(devices, MAX_DEVICES))
        elif not user.device_limit:
            fields["device_limit"] = self.settings.default_devices
        await self.db.update_user(tg_id, **fields)
        await self.sync_user(tg_id)
        user = await self.db.get_user(tg_id)
        assert user
        return user

    async def set_sub_until(self, tg_id: int, until: int | None) -> None:
        await self.db.update_user(tg_id, sub_until=None if until is None else clamp_ts(until), notified=0)
        await self.sync_user(tg_id)

    async def set_device_limit(self, tg_id: int, limit: int) -> None:
        await self.db.update_user(tg_id, device_limit=max(0, min(limit, MAX_DEVICES)))
        await self.sync_user(tg_id)

    def trial_available(self, user: User) -> bool:
        return (
            self.settings.trial_days > 0
            and not user.trial_used
            and not user.active
            and not self.is_admin(user.tg_id)
        )

    async def start_trial(self, user: User) -> User:
        user = await self.db.get_user(user.tg_id) or user
        if not self.trial_available(user):
            raise ServiceError("Пробный период уже использован.")
        await self.db.update_user(user.tg_id, trial_used=1)
        return await self.extend(
            user.tg_id, self.settings.trial_days, devices=max(user.device_limit, self.settings.trial_devices)
        )

    # ---------- оплата ----------

    async def confirm_payment(self, payment_id: int, admin_id: int) -> PaymentResult:
        if not await self.db.decide_payment(payment_id, "paid", admin_id):
            raise ServiceError("Платёж уже обработан.")
        payment = await self.db.get_payment(payment_id)
        assert payment
        user = await self.extend(payment.tg_id, payment.days, devices=payment.devices)

        referrer = None
        if user.referrer_id and not user.ref_rewarded and self.settings.ref_bonus_days > 0:
            await self.db.update_user(user.tg_id, ref_rewarded=1)
            if await self.db.get_user(user.referrer_id):
                referrer = await self.extend(user.referrer_id, self.settings.ref_bonus_days)
        return PaymentResult(payment, user, referrer)

    async def reject_payment(self, payment_id: int, admin_id: int) -> Payment:
        if not await self.db.decide_payment(payment_id, "rejected", admin_id):
            raise ServiceError("Платёж уже обработан.")
        payment = await self.db.get_payment(payment_id)
        assert payment
        return payment

    # ---------- управление аккаунтами ----------

    async def ban(self, tg_id: int) -> None:
        await self.db.update_user(tg_id, banned=1)
        await self.sync_user(tg_id)

    async def unban(self, tg_id: int) -> None:
        await self.db.update_user(tg_id, banned=0)
        await self.sync_user(tg_id)

    async def delete_user(self, tg_id: int) -> None:
        for k in await self.db.user_keys(tg_id):
            if k.enabled:
                await self.server.remove_peer(k.public_key)
        await self.db.delete_user(tg_id)

    # ---------- фоновые задачи ----------

    async def collect_traffic(self) -> None:
        """Счётчики awg сбрасываются при перезапуске контейнера и снятии пира,
        поэтому копим разницу между замерами в traffic_daily."""
        stats = await self.server.stats()
        day = today()
        for key in await self.db.enabled_keys():
            st = stats.get(key.public_key)
            if st is None:
                continue
            drx = st.rx - key.last_rx if st.rx >= key.last_rx else st.rx
            dtx = st.tx - key.last_tx if st.tx >= key.last_tx else st.tx
            await self.db.add_traffic(key, day, drx, dtx, st.rx, st.tx, st.latest_handshake)
        await self.db.commit()

    async def expire_subscriptions(self) -> list[User]:
        expired = []
        for user in await self.db.users_to_expire():
            if self.is_admin(user.tg_id) and not user.banned:
                continue
            await self.sync_user(user.tg_id)
            expired.append(user)
        return expired


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
