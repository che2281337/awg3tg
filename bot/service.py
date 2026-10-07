"""Бизнес-логика: серверы, устройства (ключи AWG / VLESS), подписки, оплаты, рефералы, трафик."""

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
from .awg.mock import MockAwgServer, MockXrayServer
from .awg.runner import LocalRunner, Runner, SshKeyStore, SshRunner, parse_ssh_target
from .awg.server import AwgError, AwgServer, ServerInfo
from .awg.xray import XrayServer, build_vless_url, new_client_id
from .config import Settings
from .db import PROTOCOLS, Database, Key, Payment, Plan, Server, User, now
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
    vpn_url: str  # vpn://… для AWG или vless://… для VLESS
    conf: str  # .conf для AWG; для VLESS пусто
    server_name: str = "awg"
    location: str = ""

    @property
    def is_vless(self) -> bool:
        return self.key.protocol == "vless"

    @property
    def filename(self) -> str:
        # Имя туннеля в AmneziaWG берётся из имени файла: до 15 символов [A-Za-z0-9_=+.-]
        base = re.sub(r"[^A-Za-z0-9_=+.-]+", "", self.server_name)[:10] or "awg"
        return f"{base}_{self.key.id}.conf"

    def qr_png(self) -> bytes | None:
        """AWG — QR с .conf, VLESS — QR со ссылкой vless://."""
        try:
            buf = io.BytesIO()
            segno.make(self.conf or self.vpn_url, error="l", micro=False).save(buf, kind="png", scale=5, border=2)
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
    protocols: tuple[str, ...] = ()


def validate_conn(conn: str) -> str:
    conn = conn.strip()
    if conn in ("local", "") or conn.startswith("mock:"):
        return conn or "local"
    parse_ssh_target(conn)  # ValueError, если адрес кривой
    return conn


class ServerPool:
    """Клиенты AWG и Xray для каждого сервера из БД (общее подключение: локально, SSH или демо)."""

    def __init__(self, settings: Settings, local: AwgServer | None = None) -> None:
        self.settings = settings
        self.ssh = SshKeyStore(os.path.join(os.path.dirname(settings.db_path) or ".", "ssh"))
        self._local = local
        self._runners: dict[int, Runner] = {}
        self._clients: dict[tuple[int, str], AwgServer | XrayServer] = {}
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}

    def _runner(self, row: Server) -> Runner:
        runner = self._runners.get(row.id) if row.id else None
        if runner is None:
            if row.conn == "local":
                runner = self._local.runner if self._local is not None else LocalRunner(self.settings.docker_bin)
            else:
                self.ssh.ensure_key()
                runner = SshRunner(row.conn, self.ssh)
            if row.id:
                self._runners[row.id] = runner
        return runner

    def make(self, row: Server, protocol: str) -> AwgServer | XrayServer:
        s = self.settings
        if row.conn.startswith("mock:"):
            return MockAwgServer(row.conn[5:]) if protocol == "awg" else MockXrayServer(row.conn[5:])
        if protocol == "awg" and row.conn == "local" and self._local is not None:
            return self._local
        runner = self._runner(row)
        if protocol == "awg":
            if row.conn == "local":
                return AwgServer(
                    container=row.container or s.awg_container,
                    config_path=s.awg_config_path,
                    interface=s.awg_interface,
                    binary=s.awg_bin,
                    runner=runner,
                )
            return AwgServer(container=row.container, runner=runner)
        return XrayServer(runner=runner)

    async def get(self, row: Server, protocol: str = "awg"):
        k = (row.id, protocol)
        lock = self._locks.setdefault(k, asyncio.Lock())
        async with lock:
            client = self._clients.get(k)
            if client is None:
                client = self.make(row, protocol)
                await client.detect()
                self._clients[k] = client
            return client

    async def drop(self, server_id: int) -> None:
        for k in [k for k in self._clients if k[0] == server_id]:
            self._clients.pop(k, None)
        runner = self._runners.pop(server_id, None)
        if runner is not None and (self._local is None or runner is not self._local.runner):
            await runner.close()

    async def close(self) -> None:
        for sid in {k[0] for k in self._clients} | set(self._runners):
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
                log.info("Сервер %s доступен, протоколы: %s", c.server.title, ", ".join(c.protocols))
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
        probe = Server(0, s.server_location, s.server_flag, "", "local", s.awg_container, 1, 0, now(), 1, None, 0, "")
        protocols, error = await self._probe(probe)
        if not protocols:
            log.info("На этой машине Amnezia не найдена (%s) — серверы добавляются из админки", error)
            return
        host = s.server_host or await asyncio.to_thread(detect_public_ip)
        await self.db.add_server(s.server_location, s.server_flag, host, "local", s.awg_container)
        log.info("Добавлен локальный сервер %s %s (%s): %s", s.server_flag, s.server_location, host, protocols)

    async def _probe(self, row: Server) -> tuple[list[str], str]:
        """Какие протоколы Amnezia есть на сервере. Сервер ещё не сохранён — без кеша."""
        found, errors = [], []
        for proto in PROTOCOLS:
            client = self.pool.make(row, proto)
            try:
                await client.detect()
                if proto == "awg":
                    await client.server_info()
                else:
                    await client.info()
                found.append(proto)
            except AwgError as e:
                errors.append(f"{PROTOCOLS[proto]}: {e}")
            finally:
                if client is not self.pool._local:
                    await client.close()  # временное подключение для проверки
        return found, "; ".join(errors)

    def is_admin(self, tg_id: int | None) -> bool:
        return tg_id is not None and tg_id in self.settings.admin_ids

    # ---------- серверы ----------

    async def _client(self, server_id: int | None, protocol: str):
        row = await self.db.get_server(server_id) if server_id is not None else None
        if row is None:
            raise ServiceError("Сервер этого устройства удалён. Перенесите устройство на другую локацию.")
        try:
            return row, await self.pool.get(row, protocol)
        except AwgError as e:
            if protocol in row.protocol_list:
                await self._mark(row, False, str(e))
            raise

    async def _mark(self, row: Server, ok: bool, error: str = "", protocols: list[str] | None = None) -> bool:
        changed = bool(row.status_ok) != ok
        fields: dict = {"status_ok": int(ok), "last_error": error or None, "last_check": now()}
        if protocols is not None:
            fields["protocols"] = ",".join(protocols)
        await self.db.update_server(row.id, **fields)
        return changed

    async def available_servers(self, protocol: str | None = None) -> list[Server]:
        """Локации, которые можно выбрать (для нового устройства — с нужным протоколом)."""
        rows = [s for s in await self.db.servers(only_active=True) if s.status_ok and s.protocol_list]
        if protocol:
            rows = [s for s in rows if protocol in s.protocol_list]
        return rows

    async def check_server(self, row: Server) -> ServerCheck:
        found, errors = [], []
        for proto in PROTOCOLS:
            try:
                client = await self.pool.get(row, proto)
                if proto == "awg":
                    await client.server_info()
                else:
                    await client.info()
                await self._reconcile(row, proto, client)
                found.append(proto)
            except Exception as e:  # сеть, SSH, docker — протокол недоступен
                errors.append(f"{PROTOCOLS[proto]}: {e}")
        if not found:
            await self.pool.drop(row.id)
        ok = bool(found)
        # Протокол, который был и пропал, — тоже проблема: сообщаем админу.
        lost = [p for p in row.protocol_list if p not in found]
        error = "; ".join(errors) if (not ok or lost) else ""
        changed = await self._mark(row, ok, error, found)
        return ServerCheck(row, ok, changed, error, tuple(found))

    async def check_servers(self) -> list[ServerCheck]:
        rows = await self.db.servers()
        return list(await asyncio.gather(*(self.check_server(r) for r in rows)))

    async def _reconcile(self, row: Server, protocol: str, client) -> None:
        """Снимает с сервера ключи, которые по базе должны быть выключены или перенесены
        (если в момент отключения сервер был недоступен). Чужие ключи не трогает."""
        cfg = await client.load_config()
        ids = [p.get("PublicKey") for p in cfg.peers] if protocol == "awg" else client.client_ids(cfg)
        extra = set()
        for cid in ids:
            if not cid:
                continue
            key = await self.db.key_by_public(cid)
            if key is not None and (key.server_id != row.id or not key.enabled or key.protocol != protocol):
                extra.add(cid)
        if not extra:
            return
        log.info("Сервер %s: снимаю лишние ключи %s (%s)", row.title, PROTOCOLS[protocol], len(extra))
        if protocol == "awg":
            for cid in extra:
                await client.remove_peer(cid)
        else:
            await client.remove_clients(extra)

    async def add_server(self, name: str, flag: str, host: str, conn: str, container: str | None = None) -> Server:
        try:
            conn = validate_conn(conn)
        except ValueError as e:
            raise ServiceError(f"Неверное подключение: {e}")
        if not host and conn not in ("local",) and not conn.startswith("mock:"):
            host = parse_ssh_target(conn)[1]
        if not host:
            host = await asyncio.to_thread(detect_public_ip)
        probe = Server(0, name, flag, host, conn, container, 1, 0, now(), 1, None, 0, "")
        protocols, error = await self._probe(probe)
        if not protocols:
            raise ServiceError(f"Не удалось подключиться к серверу или на нём нет AmneziaWG / XRay: {error}")
        row = await self.db.add_server(name, flag, host, conn, container)
        await self._mark(row, True, "", protocols)
        row = await self.db.get_server(row.id)
        assert row
        return row

    async def update_server(self, row: Server, **fields) -> None:
        await self.db.update_server(row.id, **fields)
        await self.pool.drop(row.id)

    async def delete_server(self, row: Server) -> None:
        if await self.db.server_keys(row.id):
            raise ServiceError("На сервере есть устройства. Сначала перенесите их на другие серверы.")
        await self.pool.drop(row.id)
        await self.db.delete_server(row.id)

    async def server_details(self, row: Server) -> dict[str, str]:
        """Краткое описание каждого протокола на сервере для админки."""
        details: dict[str, str] = {}
        for proto in row.protocol_list:
            try:
                _, client = await self._client(row.id, proto)
                if proto == "awg":
                    cfg = await client.load_config()
                    info = await client.server_info(cfg)
                    stats = await client.stats()
                    online = sum(1 for s in stats.values() if s.latest_handshake and now() - s.latest_handshake < 180)
                    from .awg.conf import protocol_version

                    ver = protocol_version(info.awg_params) or "1.0"
                    details[proto] = f"v{ver}, порт {info.port}, пиров {len(cfg.peers)}, онлайн {online}"
                else:
                    cfg = await client.load_config()
                    xi = await client.info(cfg)
                    details[proto] = (
                        f"{xi.network}/{xi.security}, порт {xi.port}, SNI {xi.sni or '—'}, "
                        f"клиентов {len(client.client_ids(cfg))}"
                    )
            except (AwgError, ServiceError) as e:
                details[proto] = f"ошибка: {str(e)[:200]}"
        return details

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

    def _rendered_awg(self, key: Key, row: Server, info: ServerInfo) -> RenderedKey:
        params = self._params(key, row, info)
        return RenderedKey(
            key, build_vpn_url(params), build_native_config(params), self.settings.server_name, row.title
        )

    def _rendered_vless(self, key: Key, row: Server, info) -> RenderedKey:
        label = f"{self.settings.server_name} {row.title} {key.name}"
        url = build_vless_url(info, row.host, key.public_key, label)
        return RenderedKey(key, url, "", self.settings.server_name, row.title)

    async def render(self, key: Key) -> RenderedKey:
        # Параметры читаются с сервера каждый раз: если их поменяли в приложении
        # Amnezia, повторно отправленный ключ будет актуальным.
        row, client = await self._client(key.server_id, key.protocol)
        if key.protocol == "vless":
            return self._rendered_vless(key, row, await client.info())
        return self._rendered_awg(key, row, await client.server_info())

    def base_limit(self, user: User) -> int:
        """Устройств по тарифу (без доп. слотов)."""
        return user.device_limit or self.settings.default_devices

    async def device_limit(self, user: User) -> int:
        """Итоговый лимит: по тарифу + действующие доп. слоты."""
        if self.is_admin(user.tg_id):
            return ADMIN_DEVICE_LIMIT
        return min(self.base_limit(user) + await self.db.active_slot_count(user.tg_id), MAX_DEVICES)

    def has_access(self, user: User) -> bool:
        return not user.banned and (user.active or self.is_admin(user.tg_id))

    @staticmethod
    def _peer_name(user: User | None, device: str) -> str:
        return f"{device} | {user.title}" if user else device

    async def create_device(
        self, user: User, name: str, server_id: int, protocol: str = "awg", *, force: bool = False
    ) -> RenderedKey:
        """force=True — админ создаёт устройство в обход подписки и лимита."""
        if protocol not in PROTOCOLS:
            raise ServiceError("Неизвестный протокол.")
        async with self._lock:
            user = await self.db.get_user(user.tg_id) or user
            if not force:
                if user.banned:
                    raise ServiceError("Аккаунт заблокирован.")
                if not self.has_access(user):
                    raise ServiceError("Нет активной подписки. Оформите её в разделе «💳 Тарифы».")
                keys = await self.db.user_keys(user.tg_id)
                limit = await self.device_limit(user)
                if len(keys) >= limit:
                    raise ServiceError(
                        f"Достигнут лимит устройств ({len(keys)}/{limit}). "
                        "Удалите ненужное устройство или выберите тариф с большим числом устройств."
                    )
            row = await self.db.get_server(server_id)
            if row is None or (not force and not (row.active and row.status_ok)):
                raise ServiceError("Эта локация сейчас недоступна, выберите другую.")
            if protocol not in row.protocol_list:
                raise ServiceError(f"{PROTOCOLS[protocol]} на этом сервере сейчас недоступен.")
            row, client = await self._client(row.id, protocol)
            peer_name = self._peer_name(user, name)
            if protocol == "vless":
                client_id, info = await client.add_client(peer_name, new_client_id())
                try:
                    key = await self.db.add_key(user.tg_id, name, client_id, "", "", row.id, "vless")
                except Exception:
                    await client.remove_client(client_id)
                    raise
                return self._rendered_vless(key, row, info)
            peer, info = await client.add_peer(peer_name, reserved=await self.db.reserved_ips(row.id))
            try:
                key = await self.db.add_key(user.tg_id, name, peer.public_key, peer.private_key, peer.ip, row.id)
            except Exception:
                await client.remove_peer(peer.public_key)
                raise
        return self._rendered_awg(key, row, info)

    async def _remove_from_server(self, key: Key, server_id: int | None = None) -> None:
        sid = key.server_id if server_id is None else server_id
        if sid is None or await self.db.get_server(sid) is None:
            return
        _, client = await self._client(sid, key.protocol)
        if key.protocol == "vless":
            await client.remove_client(key.public_key)
        else:
            await client.remove_peer(key.public_key)

    async def delete_device(self, key: Key) -> None:
        if key.enabled:
            await self._remove_from_server(key)
        await self.db.delete_key(key.id)

    async def rename_device(self, key: Key, name: str) -> None:
        await self.db.update_key(key.id, name=name[:40])

    async def disable_key(self, key: Key) -> None:
        if key.enabled:
            await self._remove_from_server(key)
        await self.db.update_key(key.id, enabled=0, last_rx=0, last_tx=0)

    async def enable_key(self, key: Key, user: User | None = None) -> bool:
        """Возвращает True, если у ключа сменился IP (клиенту нужно получить ключ заново)."""
        row, client = await self._client(key.server_id, key.protocol)
        if key.protocol == "vless":
            await client.add_client(self._peer_name(user, key.name), key.public_key)
            await self.db.update_key(key.id, enabled=1, last_rx=0, last_tx=0)
            return False
        others = await self.db.other_ips(row.id, key.id)
        ip = key.ip if key.ip and key.ip not in others else None
        peer, _ = await client.add_peer(
            self._peer_name(user, key.name), keypair=(key.private_key, key.public_key), ip=ip, reserved=others
        )
        await self.db.update_key(key.id, enabled=1, ip=peer.ip, last_rx=0, last_tx=0)
        return peer.ip != key.ip

    async def move_device(self, key: Key, server_id: int, *, force: bool = False) -> RenderedKey:
        """Перенос устройства на другую локацию (тем же протоколом). Ключ шифрования / UUID
        остаётся прежним, но меняется адрес сервера — клиенту нужно импортировать новый ключ."""
        if key.server_id == server_id:
            raise ServiceError("Устройство уже на этом сервере.")
        target = await self.db.get_server(server_id)
        if target is None or (not force and not (target.active and target.status_ok)):
            raise ServiceError("Эта локация сейчас недоступна, выберите другую.")
        if key.protocol not in target.protocol_list:
            raise ServiceError(f"На этом сервере нет {key.protocol_name}.")
        user = await self.db.get_user(key.tg_id) if key.tg_id else None
        async with self._lock:
            # Сначала поднимаем ключ на новом сервере — если он недоступен, ничего не ломаем.
            row, client = await self._client(target.id, key.protocol)
            name = self._peer_name(user, key.name)
            if key.protocol == "vless":
                _, info = await client.add_client(name, key.public_key)
                new_ip = ""
            else:
                peer, info = await client.add_peer(
                    name, keypair=(key.private_key, key.public_key), reserved=await self.db.reserved_ips(row.id)
                )
                new_ip = peer.ip
            old_server, was_enabled = key.server_id, key.enabled
            await self.db.update_key(key.id, server_id=row.id, ip=new_ip, enabled=1, last_rx=0, last_tx=0)
            if was_enabled:
                try:
                    await self._remove_from_server(key, old_server)
                except (AwgError, ServiceError) as e:
                    # Старый сервер недоступен: ключ снимется при следующей успешной проверке.
                    log.warning("Не удалось снять ключ #%s со старого сервера: %s", key.id, e)
        moved = await self.db.get_key(key.id)
        assert moved
        if user is not None and not self.has_access(user):
            await self.disable_key(moved)
            moved = await self.db.get_key(key.id)
            assert moved
        if key.protocol == "vless":
            return self._rendered_vless(moved, row, info)
        return self._rendered_awg(moved, row, info)

    async def sync_user(self, tg_id: int) -> list[Key]:
        """Приводит ключи на серверах в соответствие с подпиской и лимитом устройств.
        Ошибка одного сервера не мешает остальным. Возвращает ключи, у которых сменился IP."""
        user = await self.db.get_user(tg_id)
        if user is None:
            return []
        keys = await self.db.user_keys(tg_id)
        changed: list[Key] = []
        access = self.has_access(user)
        limit = await self.device_limit(user) if access else 0
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

    async def set_user_server(self, tg_id: int, server_id: int | None) -> None:
        await self.db.update_user(tg_id, server_id=server_id)

    async def user_server(self, user: User) -> Server | None:
        """Сервер, выбранный при покупке, если он ещё доступен."""
        if not user.server_id:
            return None
        row = await self.db.get_server(user.server_id)
        return row if row and row.active and row.status_ok and row.protocol_list else None

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

    # ---------- доп. слоты устройств ----------

    async def add_slot(self, tg_id: int, days: int, source: str = "admin"):
        if not 0 < days <= MAX_DAYS:
            raise ServiceError(f"Срок слота должен быть от 1 до {MAX_DAYS} дней.")
        slot = await self.db.add_slot(tg_id, clamp_ts(now() + days * 86400), source)
        await self.sync_user(tg_id)
        return slot

    async def extend_slot(self, slot_id: int, days: int):
        slot = await self.db.get_slot(slot_id)
        if slot is None:
            raise ServiceError("Слот не найден.")
        if not -MAX_DAYS <= days <= MAX_DAYS:
            raise ServiceError(f"Срок должен быть от -{MAX_DAYS} до {MAX_DAYS} дней.")
        base = max(now(), slot.until) if days > 0 else slot.until
        await self.db.update_slot(slot.id, until=clamp_ts(base + days * 86400), notified=0)
        await self.sync_user(slot.tg_id)
        return await self.db.get_slot(slot.id)

    async def remove_slot(self, slot_id: int) -> None:
        slot = await self.db.get_slot(slot_id)
        if slot is None:
            return
        await self.db.delete_slot(slot.id)
        await self.sync_user(slot.tg_id)

    async def expire_slots(self) -> list:
        """Закончившиеся слоты: отключаем лишние устройства. Возвращает слоты для уведомления."""
        expired = await self.db.slots_expired_unnotified()
        for slot in expired:
            await self.db.update_slot(slot.id, notified=2)
        for tg_id in {s.tg_id for s in expired}:
            await self.sync_user(tg_id)
        return expired

    # ---------- оплата ----------

    async def confirm_payment(self, payment_id: int, admin_id: int) -> PaymentResult:
        if not await self.db.decide_payment(payment_id, "paid", admin_id):
            raise ServiceError("Платёж уже обработан.")
        payment = await self.db.get_payment(payment_id)
        assert payment
        if payment.kind == "slot":
            slot = await self.db.get_slot(payment.slot_id) if payment.slot_id else None
            if slot is not None and slot.tg_id == payment.tg_id:
                await self.extend_slot(slot.id, payment.days)  # продление того же слота
            else:
                await self.add_slot(payment.tg_id, payment.days, "paid")
            user = await self.db.get_user(payment.tg_id)
            assert user
            return PaymentResult(payment, user)
        if payment.server_id and await self.db.get_server(payment.server_id):
            await self.set_user_server(payment.tg_id, payment.server_id)
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
        """Счётчики awg/xray обнуляются при перезапуске, поэтому копим разницу между замерами."""
        day = today()
        for row in await self.db.servers():
            for proto in row.protocol_list:
                try:
                    _, client = await self._client(row.id, proto)
                    stats = await client.stats()
                except (AwgError, ServiceError) as e:
                    log.warning("Трафик: %s на %s недоступен: %s", PROTOCOLS[proto], row.title, e)
                    continue
                for key in await self.db.enabled_keys(row.id, proto):
                    st = stats.get(key.public_key)
                    if st is None:
                        continue
                    drx = st.rx - key.last_rx if st.rx >= key.last_rx else st.rx
                    dtx = st.tx - key.last_tx if st.tx >= key.last_tx else st.tx
                    if proto == "awg":
                        hs = st.latest_handshake
                    else:  # у Xray нет рукопожатий — «был онлайн», если шёл трафик
                        hs = now() if (drx or dtx) else 0
                    await self.db.add_traffic(key, day, drx, dtx, st.rx, st.tx, hs)
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
