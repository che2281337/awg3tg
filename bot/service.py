"""Бизнес-логика: серверы, устройства (ключи), подписки, оплаты, рефералы, трафик."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import urllib.request
from dataclasses import dataclass

import segno

from .awg.export import ClientParams, build_native_config, build_vpn_url
from .awg.mock import MockAwgServer
from .awg.runner import SshKeyStore, SshRunner, parse_ssh_target
from .awg.server import AwgError, AwgServer, ServerInfo
from .config import Settings
from .db import Database, Key, Payment, Plan, Server, User, now
from .utils import today

log = logging.getLogger(__name__)

ADMIN_DEVICE_LIMIT = 100

# Защита от переполнения: SQLite хранит 64-битные числа, а даты дальше 9999 года
# Python не форматирует. Сроки и лимиты ограничены разумными значениями.
MAX_DAYS = 36500  # за одну операцию — 100 лет
MAX_SUB_UNTIL = 4102444800  # 01.01.2100 — дальше подписка не продлевается
MAX_DEVICES = 100

DEMO_SERVERS = [("Германия", "🇩🇪"), ("Нидерланды", "🇳🇱"), ("Финляндия", "🇫🇮")]


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
    location: str = ""

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


@dataclass
class ServerCheck:
    server: Server
    ok: bool
    changed: bool  # статус изменился с прошлой проверки
    error: str = ""


def validate_conn(conn: str) -> str:
    conn = conn.strip()
    if conn in ("local", "") or conn.startswith("mock:"):
        return conn or "local"
    parse_ssh_target(conn)  # ValueError, если адрес кривой
    return conn


class ServerPool:
    """AwgServer для каждого сервера из БД: локально, по SSH или демо."""

    def __init__(self, settings: Settings, local: AwgServer | None = None) -> None:
        self.settings = settings
        self.ssh = SshKeyStore(os.path.join(os.path.dirname(settings.db_path) or ".", "ssh"))
        self._local = local
        self._cache: dict[int, AwgServer] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def make(self, row: Server) -> AwgServer:
        s = self.settings
        if row.conn == "local":
            if self._local is not None:
                return self._local
            return AwgServer(
                container=row.container or s.awg_container,
                config_path=s.awg_config_path,
                interface=s.awg_interface,
                binary=s.awg_bin,
                docker=s.docker_bin,
            )
        if row.conn.startswith("mock:"):
            return MockAwgServer(row.conn[5:])
        self.ssh.ensure_key()
        return AwgServer(container=row.container, runner=SshRunner(row.conn, self.ssh))

    async def get(self, row: Server) -> AwgServer:
        lock = self._locks.setdefault(row.id, asyncio.Lock())
        async with lock:
            srv = self._cache.get(row.id)
            if srv is None:
                srv = self.make(row)
                try:
                    await srv.detect()
                except Exception:
                    if srv is not self._local:
                        await srv.close()
                    raise
                self._cache[row.id] = srv
            return srv

    async def drop(self, server_id: int) -> None:
        srv = self._cache.pop(server_id, None)
        if srv is not None and srv is not self._local:
            await srv.close()

    async def close(self) -> None:
        for sid in list(self._cache):
            await self.drop(sid)


class VpnService:
    def __init__(self, settings: Settings, db: Database, local: AwgServer | None = None) -> None:
        self.settings = settings
        self.db = db
        self.pool = ServerPool(settings, local)
        self._lock = asyncio.Lock()

    # ---------- запуск ----------

    async def start(self) -> None:
        servers = await self.db.servers()
        if not servers:
            await self._bootstrap_servers()
            servers = await self.db.servers()
        if servers:
            moved = await self.db.assign_orphan_keys(servers[0].id)
            if moved:
                log.info("Ключи старой версии (%s) привязаны к серверу %s", moved, servers[0].title)
        for c in await self.check_servers():
            if c.ok:
                log.info("Сервер %s доступен", c.server.title)
            else:
                log.warning("Сервер %s недоступен: %s", c.server.title, c.error)
        if not servers:
            log.warning("Серверов нет — добавьте их в боте: 🛠 Админка → 🖥 Серверы → ➕ Добавить")

    async def _bootstrap_servers(self) -> None:
        """Первый запуск: демо-серверы или сервер, на котором запущен бот."""
        s = self.settings
        if s.awg_mock:
            base = os.path.join(os.path.dirname(s.db_path) or ".", "mock-servers")
            for i, (name, flag) in enumerate(DEMO_SERVERS, 1):
                await self.db.add_server(name, flag, f"10.0.0.{i}", f"mock:{os.path.join(base, str(i))}")
            log.warning("ДЕМО-РЕЖИМ: созданы эмулированные серверы %s", ", ".join(n for n, _ in DEMO_SERVERS))
            return
        probe = self.pool._local or AwgServer(
            container=s.awg_container,
            config_path=s.awg_config_path,
            interface=s.awg_interface,
            binary=s.awg_bin,
            docker=s.docker_bin,
        )
        try:
            await probe.detect()
            await probe.server_info()
        except AwgError as e:
            log.info("На этой машине AmneziaWG не найден (%s) — серверы добавляются из админки", e)
            return
        host = s.server_host or await asyncio.to_thread(detect_public_ip)
        await self.db.add_server(s.server_location, s.server_flag, host, "local", s.awg_container)
        log.info("Добавлен локальный сервер %s %s (%s)", s.server_flag, s.server_location, host)

    def is_admin(self, tg_id: int | None) -> bool:
        return tg_id is not None and tg_id in self.settings.admin_ids

    # ---------- серверы ----------

    async def _awg(self, server_id: int | None) -> tuple[Server, AwgServer]:
        row = await self.db.get_server(server_id) if server_id is not None else None
        if row is None:
            raise ServiceError("Сервер этого устройства удалён. Перенесите устройство на другую локацию.")
        try:
            return row, await self.pool.get(row)
        except AwgError as e:
            await self._mark(row, False, str(e))
            raise

    async def _mark(self, row: Server, ok: bool, error: str = "") -> bool:
        changed = bool(row.status_ok) != ok
        await self.db.update_server(row.id, status_ok=int(ok), last_error=error or None, last_check=now())
        return changed

    async def available_servers(self) -> list[Server]:
        """Локации, которые можно выбрать для нового устройства."""
        return [s for s in await self.db.servers(only_active=True) if s.status_ok]

    async def check_server(self, row: Server) -> ServerCheck:
        try:
            awg = await self.pool.get(row)
            await awg.server_info()
            await self._reconcile(row, awg)
        except Exception as e:  # сеть, SSH, docker — всё считаем «недоступен»
            await self.pool.drop(row.id)
            changed = await self._mark(row, False, str(e))
            return ServerCheck(row, False, changed, str(e))
        changed = await self._mark(row, True)
        return ServerCheck(row, True, changed)

    async def check_servers(self) -> list[ServerCheck]:
        rows = await self.db.servers()
        return list(await asyncio.gather(*(self.check_server(r) for r in rows)))

    async def _reconcile(self, row: Server, awg: AwgServer) -> None:
        """Снимает с сервера пиры, которые по базе должны быть выключены или перенесены
        (если в момент отключения сервер был недоступен). Чужие пиры не трогает."""
        cfg = await awg.load_config()
        for peer in cfg.peers:
            pub = peer.get("PublicKey")
            if not pub:
                continue
            key = await self.db.key_by_public(pub)
            if key is not None and (key.server_id != row.id or not key.enabled):
                log.info("Сервер %s: снимаю лишний пир ключа #%s", row.title, key.id)
                await awg.remove_peer(pub)

    async def add_server(self, name: str, flag: str, host: str, conn: str, container: str | None = None) -> Server:
        try:
            conn = validate_conn(conn)
        except ValueError as e:
            raise ServiceError(f"Неверное подключение: {e}")
        if not host and conn not in ("local",) and not conn.startswith("mock:"):
            host = parse_ssh_target(conn)[1]
        if not host:
            host = await asyncio.to_thread(detect_public_ip)
        probe_row = Server(0, name, flag, host, conn, container, 1, 0, now(), 1, None, 0)
        awg = self.pool.make(probe_row)
        try:
            await awg.detect()
            await awg.server_info()
        except AwgError as e:
            raise ServiceError(f"Не удалось подключиться к серверу: {e}")
        finally:
            if awg is not self.pool._local:
                await awg.close()
        row = await self.db.add_server(name, flag, host, conn, container)
        await self._mark(row, True)
        return row

    async def update_server(self, row: Server, **fields) -> None:
        await self.db.update_server(row.id, **fields)
        await self.pool.drop(row.id)

    async def delete_server(self, row: Server) -> None:
        if await self.db.server_keys(row.id):
            raise ServiceError("На сервере есть устройства. Сначала перенесите их на другие серверы.")
        await self.pool.drop(row.id)
        await self.db.delete_server(row.id)

    async def server_details(self, row: Server) -> tuple[ServerInfo, int, int]:
        """(параметры AWG, пиров на сервере, онлайн)."""
        _, awg = await self._awg(row.id)
        cfg = await awg.load_config()
        info = await awg.server_info(cfg)
        stats = await awg.stats()
        online = sum(1 for s in stats.values() if s.latest_handshake and now() - s.latest_handshake < 180)
        return info, len(cfg.peers), online

    # ---------- ключи ----------

    def _params(self, key: Key, row: Server, info: ServerInfo) -> ClientParams:
        return ClientParams(
            host=row.host,
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
            # Так сервер будет подписан в приложении AmneziaVPN: «MyVPN 🇩🇪 Германия»
            description=f"{self.settings.server_name} {row.title}",
            mtu=self.settings.client_mtu,
        )

    def _rendered(self, key: Key, row: Server, info: ServerInfo) -> RenderedKey:
        params = self._params(key, row, info)
        return RenderedKey(
            key, build_vpn_url(params), build_native_config(params), self.settings.server_name, row.title
        )

    async def render(self, key: Key) -> RenderedKey:
        # Параметры обфускации читаются с сервера каждый раз: если их поменяли
        # в приложении Amnezia, повторно отправленный ключ будет актуальным.
        row, awg = await self._awg(key.server_id)
        return self._rendered(key, row, await awg.server_info())

    def device_limit(self, user: User) -> int:
        if self.is_admin(user.tg_id):
            return ADMIN_DEVICE_LIMIT
        return user.device_limit or self.settings.default_devices

    def has_access(self, user: User) -> bool:
        return not user.banned and (user.active or self.is_admin(user.tg_id))

    @staticmethod
    def _peer_name(user: User | None, device: str) -> str:
        return f"{device} | {user.title}" if user else device

    async def create_device(self, user: User, name: str, server_id: int, *, force: bool = False) -> RenderedKey:
        """force=True — админ создаёт устройство в обход подписки и лимита."""
        async with self._lock:
            user = await self.db.get_user(user.tg_id) or user
            if not force:
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
            row = await self.db.get_server(server_id)
            if row is None or (not force and not (row.active and row.status_ok)):
                raise ServiceError("Эта локация сейчас недоступна, выберите другую.")
            row, awg = await self._awg(row.id)
            peer, info = await awg.add_peer(
                self._peer_name(user, name), reserved=await self.db.reserved_ips(row.id)
            )
            try:
                key = await self.db.add_key(user.tg_id, name, peer.public_key, peer.private_key, peer.ip, row.id)
            except Exception:
                await awg.remove_peer(peer.public_key)
                raise
        return self._rendered(key, row, info)

    async def delete_device(self, key: Key) -> None:
        if key.enabled and key.server_id is not None and await self.db.get_server(key.server_id):
            _, awg = await self._awg(key.server_id)
            await awg.remove_peer(key.public_key)
        await self.db.delete_key(key.id)

    async def rename_device(self, key: Key, name: str) -> None:
        await self.db.update_key(key.id, name=name[:40])

    async def disable_key(self, key: Key) -> None:
        if key.enabled and key.server_id is not None and await self.db.get_server(key.server_id):
            _, awg = await self._awg(key.server_id)
            await awg.remove_peer(key.public_key)
        await self.db.update_key(key.id, enabled=0, last_rx=0, last_tx=0)

    async def enable_key(self, key: Key, user: User | None = None) -> bool:
        """Возвращает True, если у ключа сменился IP (клиенту нужно получить ключ заново)."""
        row, awg = await self._awg(key.server_id)
        others = await self.db.other_ips(row.id, key.id)
        ip = key.ip if key.ip and key.ip not in others else None
        peer, _ = await awg.add_peer(
            self._peer_name(user, key.name), keypair=(key.private_key, key.public_key), ip=ip, reserved=others
        )
        await self.db.update_key(key.id, enabled=1, ip=peer.ip, last_rx=0, last_tx=0)
        return peer.ip != key.ip

    async def move_device(self, key: Key, server_id: int, *, force: bool = False) -> RenderedKey:
        """Перенос устройства на другую локацию. Ключ шифрования остаётся тем же,
        но меняются адрес сервера и его ключ — клиенту нужно импортировать новый конфиг."""
        if key.server_id == server_id:
            raise ServiceError("Устройство уже на этом сервере.")
        target = await self.db.get_server(server_id)
        if target is None or (not force and not (target.active and target.status_ok)):
            raise ServiceError("Эта локация сейчас недоступна, выберите другую.")
        user = await self.db.get_user(key.tg_id) if key.tg_id else None
        async with self._lock:
            # Сначала поднимаем ключ на новом сервере — если он недоступен, ничего не ломаем.
            row, awg = await self._awg(target.id)
            peer, info = await awg.add_peer(
                self._peer_name(user, key.name),
                keypair=(key.private_key, key.public_key),
                reserved=await self.db.reserved_ips(row.id),
            )
            old_server, was_enabled = key.server_id, key.enabled
            await self.db.update_key(key.id, server_id=row.id, ip=peer.ip, enabled=1, last_rx=0, last_tx=0)
            if was_enabled and old_server is not None and await self.db.get_server(old_server):
                try:
                    _, old = await self._awg(old_server)
                    await old.remove_peer(key.public_key)
                except (AwgError, ServiceError) as e:
                    # Старый сервер недоступен: пир снимется при следующей успешной проверке.
                    log.warning("Не удалось снять ключ #%s со старого сервера: %s", key.id, e)
        moved = await self.db.get_key(key.id)
        assert moved
        if user is not None and not self.has_access(user):
            await self.disable_key(moved)
            moved = await self.db.get_key(key.id)
            assert moved
        return self._rendered(moved, row, info)

    async def sync_user(self, tg_id: int) -> list[Key]:
        """Приводит ключи на серверах в соответствие с подпиской и лимитом устройств.
        Ошибка одного сервера не мешает остальным. Возвращает ключи, у которых сменился IP."""
        user = await self.db.get_user(tg_id)
        if user is None:
            return []
        keys = await self.db.user_keys(tg_id)
        changed: list[Key] = []
        access = self.has_access(user)
        limit = self.device_limit(user) if access else 0
        for i, k in enumerate(keys):  # самые старые устройства в приоритете
            try:
                if i < limit and not k.enabled:
                    if await self.enable_key(k, user):
                        changed.append(k)
                elif i >= limit and k.enabled:
                    await self.disable_key(k)
            except (AwgError, ServiceError) as e:
                log.warning("Ключ #%s: сервер недоступен (%s), повторю позже", k.id, e)
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

    async def is_newbie(self, user: User) -> bool:
        """Новичок — ни разу не оплачивал подписку."""
        return not self.is_admin(user.tg_id) and not await self.db.has_paid(user.tg_id)

    async def price_for(self, user: User, plan: Plan) -> int:
        """Цена тарифа для пользователя: новичкам скидка на первую оплату
        (только для тарифов не длиннее FIRST_DISCOUNT_MAX_DAYS)."""
        pct = self.settings.first_discount_percent
        if pct <= 0 or plan.days > self.settings.first_discount_max_days or not await self.is_newbie(user):
            return plan.price
        return max(0, plan.price * (100 - min(pct, 100)) // 100)

    async def first_offer(self, user: User) -> tuple[Plan, int] | None:
        """Самый короткий тариф со скидкой новичка — для приветствия."""
        for plan in await self.db.plans():
            price = await self.price_for(user, plan)
            if price < plan.price:
                return plan, price
        return None

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
            await self.delete_device(k)  # если сервер недоступен — ошибка, аккаунт не удаляется
        await self.db.delete_user(tg_id)

    # ---------- фоновые задачи ----------

    async def collect_traffic(self) -> None:
        """Счётчики awg сбрасываются при перезапуске контейнера и снятии пира,
        поэтому копим разницу между замерами в traffic_daily."""
        day = today()
        for row in await self.db.servers():
            try:
                _, awg = await self._awg(row.id)
                stats = await awg.stats()
            except (AwgError, ServiceError) as e:
                log.warning("Трафик: сервер %s недоступен: %s", row.title, e)
                continue
            for key in await self.db.enabled_keys(row.id):
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

    async def close(self) -> None:
        await self.pool.close()


def detect_public_ip() -> str:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                ip = resp.read().decode().strip()
                if ip:
                    return ip
        except Exception:
            continue
    raise ServiceError("Не удалось определить публичный IP — укажите адрес сервера явно (SERVER_HOST)")
