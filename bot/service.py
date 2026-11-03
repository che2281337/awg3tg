"""Бизнес-логика: серверы, устройства (ключи AWG / VLESS), подписки, оплаты, рефералы, трафик."""

from __future__ import annotations

import asyncio
import contextlib
import io
import ipaddress
import logging
import os
import re
import socket
import urllib.request
from dataclasses import dataclass, replace

import segno

from .awg.export import ClientParams, build_native_config, build_vpn_url
from .awg.mock import MockAwgServer, MockXrayServer
from .awg.relay import Forward, MockRelay, RelayClient, allocate_ports
from .awg.runner import LocalRunner, Runner, SshKeyStore, SshRunner, parse_ssh_target
from .awg.server import AwgError, AwgServer, ServerInfo
from .awg.xray import XrayServer, build_vless_url, new_client_id
from .config import Settings
from .db import PROTOCOLS, Database, Key, Payment, Plan, Relay, RelayPort, Server, User, now
from .utils import today
from .yoomoney import Operation, YooMoney, YooMoneyError, quickpay_url

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
    whitelist: bool = False  # ключ через входной сервер в РФ (для белых списков)

    @property
    def is_vless(self) -> bool:
        return self.key.protocol == "vless"

    @property
    def filename(self) -> str:
        # Имя туннеля в AmneziaWG берётся из имени файла: до 15 символов [A-Za-z0-9_=+.-]
        base = re.sub(r"[^A-Za-z0-9_=+.-]+", "", self.server_name)[:10] or "awg"
        if self.whitelist:
            base = base[:8] + "WL"
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
class AutoPayment:
    """Результат проверки счёта ЮMoney: оплачен (result) или ушёл админу на проверку (review)."""

    payment: Payment
    result: PaymentResult | None = None
    review: bool = False


# Сколько живёт неоплаченный счёт ЮMoney (потом создаётся новый)
INVOICE_TTL = 24 * 3600


@dataclass
class RelayCheck:
    relay: Relay
    ok: bool
    changed: bool  # статус изменился с прошлой проверки
    error: str = ""
    applied: bool = False  # правила на входе были (пере)записаны


WHITELIST_SUFFIX = "· белые списки"


@dataclass
class ServerCheck:
    server: Server
    ok: bool
    changed: bool  # статус изменился с прошлой проверки
    error: str = ""
    protocols: tuple[str, ...] = ()
    restored: int = 0  # сколько ключей бот вернул на сервер (их стёрли, например переустановкой)


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
        self._pay_lock = asyncio.Lock()
        self.yoomoney: YooMoney | None = YooMoney(settings.yoomoney_token) if settings.yoomoney_token else None
        self.yoomoney_wallet = settings.yoomoney_wallet
        self._relay_clients: dict[int, RelayClient] = {}
        self._relay_lock = asyncio.Lock()
        # Ключи, с которыми прямо сейчас идёт операция (удаление, отключение, перенос…):
        # проверка сервера не должна «восстанавливать» их посреди операции.
        self._busy: dict[int, int] = {}  # key_id -> сколько операций идёт

    # ---------- запуск ----------

    async def start(self) -> None:
        await self._init_yoomoney()
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
        rows = [s for s in await self.db.servers(only_active=True) if s.status_ok and s.visible_protocols]
        if protocol:
            rows = [s for s in rows if protocol in s.visible_protocols]
        return rows

    async def set_protocol_hidden(self, row: Server, protocol: str, hidden: bool) -> None:
        """Скрыть протокол сервера для новых устройств (существующие продолжают работать)."""
        current = set(row.hidden_list)
        current = current | {protocol} if hidden else current - {protocol}
        await self.db.update_server(row.id, hidden=",".join(p for p in PROTOCOLS if p in current))

    @contextlib.asynccontextmanager
    async def _busy_key(self, key_id: int):
        self._busy[key_id] = self._busy.get(key_id, 0) + 1
        try:
            yield
        finally:
            if self._busy[key_id] <= 1:
                del self._busy[key_id]
            else:
                self._busy[key_id] -= 1

    async def check_server(self, row: Server) -> ServerCheck:
        found, errors, restored = [], [], 0
        for proto in PROTOCOLS:
            try:
                client = await self.pool.get(row, proto)
                if proto == "awg":
                    await client.server_info()
                else:
                    await client.info()
                restored += await self._reconcile(row, proto, client)
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
        return ServerCheck(row, ok, changed, error, tuple(found), restored)

    async def check_servers(self) -> list[ServerCheck]:
        rows = await self.db.servers()
        return list(await asyncio.gather(*(self.check_server(r) for r in rows)))

    async def _reconcile(self, row: Server, protocol: str, client) -> int:
        """Приводит сервер в соответствие с базой:
        * снимает ключи, которые по базе выключены или перенесены (если в момент отключения
          сервер был недоступен). Чужие ключи (не из бота) не трогает;
        * возвращает ключи, которые по базе должны работать, но на сервере пропали — например,
          протокол переустановили в приложении Amnezia. Возвращает, сколько ключей вернул."""
        cfg = await client.load_config()
        ids = [p.get("PublicKey") for p in cfg.peers] if protocol == "awg" else client.client_ids(cfg)
        present = {cid for cid in ids if cid}
        extra = set()
        for cid in present:
            key = await self.db.key_by_public(cid)
            if key is not None and key.id not in self._busy and (
                key.server_id != row.id or not key.enabled or key.protocol != protocol
            ):
                extra.add(cid)
        if extra:
            log.info("Сервер %s: снимаю лишние ключи %s (%s)", row.title, PROTOCOLS[protocol], len(extra))
            if protocol == "awg":
                for cid in extra:
                    await client.remove_peer(cid)
            else:
                await client.remove_clients(extra)
        missing = [
            k
            for k in await self.db.enabled_keys(row.id, protocol)
            if k.public_key not in present and k.id not in self._busy
        ]
        if not missing:
            return 0
        log.warning("Сервер %s: на нём нет %s ключей %s — возвращаю", row.title, len(missing), PROTOCOLS[protocol])
        names = {}
        for k in missing:
            user = await self.db.get_user(k.tg_id) if k.tg_id else None
            names[k.id] = self._peer_name(user, k.name)
        if protocol == "vless":
            return await client.add_clients([(names[k.id], k.public_key) for k in missing])
        restored = 0
        for k in missing:
            fresh = await self.db.get_key(k.id)  # пока шли по списку, ключ могли удалить или выключить
            if fresh is None or not fresh.enabled or fresh.server_id != row.id or fresh.id in self._busy:
                continue
            others = await self.db.other_ips(row.id, k.id)
            ip = k.ip if k.ip and k.ip not in others else None
            peer, _ = await client.add_peer(names[k.id], keypair=(k.private_key, k.public_key), ip=ip, reserved=others)
            if peer.ip != k.ip:
                await self.db.update_key(k.id, ip=peer.ip)
            restored += 1
        return restored

    async def check_duplicate(self, host: str, conn: str, exclude_id: int = 0) -> None:
        """Одна машина — одна запись: две записи на один сервер стали бы снимать друг у друга клиентов."""
        for s in await self.db.servers():
            if s.id == exclude_id:
                continue
            if conn == s.conn or (host and host == s.host):
                raise ServiceError(f"Этот сервер уже добавлен: {s.title} ({s.host}, {s.conn}).")

    async def add_server(self, name: str, flag: str, host: str, conn: str, container: str | None = None) -> Server:
        try:
            conn = validate_conn(conn)
        except ValueError as e:
            raise ServiceError(f"Неверное подключение: {e}")
        if not host and conn not in ("local",) and not conn.startswith("mock:"):
            host = parse_ssh_target(conn)[1]
        if not host:
            host = await asyncio.to_thread(detect_public_ip)
        await self.check_duplicate(host, conn)
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

    # ---------- входы для белых списков ----------

    def _relay_client(self, row: Relay) -> RelayClient:
        client = self._relay_clients.get(row.id)
        if client is None:
            if row.conn.startswith("mock:"):
                client = MockRelay(row.conn[5:])
            else:
                self.pool.ssh.ensure_key()
                client = RelayClient(SshRunner(row.conn, self.pool.ssh))
            self._relay_clients[row.id] = client
        return client

    async def _drop_relay(self, relay_id: int) -> None:
        client = self._relay_clients.pop(relay_id, None)
        if client is not None:
            await client.close()

    @staticmethod
    async def _resolve(host: str) -> str:
        """IP для DNAT: iptables не умеет перенаправлять на домен."""
        try:
            return str(ipaddress.ip_address(host.strip("[]")))
        except ValueError:
            pass
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None, family=socket.AF_INET)
        except OSError as e:
            raise ServiceError(f"не удалось узнать IP {host}: {e}") from e
        return infos[0][4][0]

    async def _target_port(self, row: Server, protocol: str) -> int:
        client = await self.pool.get(row, protocol)
        info = await (client.server_info() if protocol == "awg" else client.info())
        return int(info.port)

    async def desired_relay_ports(self, relay: Relay) -> list[RelayPort]:
        """Какие порты входа на какие серверы вести. Порт, уже выданный клиентам, не меняется;
        для недоступного сервера сохраняется прежнее направление."""
        current = {(p.server_id, p.protocol): p for p in await self.db.relay_ports(relay.id)}
        targets: dict[tuple[int, str], tuple[str, int]] = {}
        wanted = []
        for row in await self.db.servers():
            protocols = set(row.protocol_list) | {proto for sid, proto in current if sid == row.id}
            for proto in sorted(protocols):
                key = (row.id, proto)
                if row.status_ok and proto in row.protocol_list:
                    try:
                        targets[key] = (await self._resolve(row.host), await self._target_port(row, proto))
                    except (AwgError, ServiceError) as e:
                        log.warning("Вход %s: сервер %s (%s) пропущен: %s", relay.name, row.title, proto, e)
                if key not in targets and key in current:
                    targets[key] = (current[key].target_ip, current[key].target_port)
                if key in targets:
                    wanted.append((key, "udp" if proto == "awg" else "tcp", targets[key][1]))
        reserved: set[tuple[str, int]] = set()
        if not relay.conn.startswith("mock:"):
            reserved.add(("tcp", parse_ssh_target(relay.conn)[2]))  # не занять порт SSH самого входа
        ports = allocate_ports(wanted, {k: p.listen_port for k, p in current.items()}, reserved)
        return [RelayPort(relay.id, sid, proto, ports[(sid, proto)], *targets[(sid, proto)]) for sid, proto in ports]

    async def sync_relay(self, relay: Relay) -> RelayCheck:
        """Проверяет вход и при необходимости перенастраивает перенаправления."""
        async with self._relay_lock:
            relay = await self.db.get_relay(relay.id) or relay
            applied, error = False, ""
            try:
                ports = await self.desired_relay_ports(relay)
                forwards = [Forward(p.transport, p.listen_port, p.target_ip, p.target_port) for p in ports]
                applied = await self._relay_client(relay).sync(forwards)
                await self.db.set_relay_ports(relay.id, ports)
            except (AwgError, ServiceError) as e:
                error = str(e)
                await self._drop_relay(relay.id)
            ok = not error
            changed = bool(relay.status_ok) != ok
            await self.db.update_relay(relay.id, status_ok=int(ok), last_error=error or None, last_check=now())
            if applied:
                log.info("Вход %s: перенаправления обновлены", relay.name)
            fresh = await self.db.get_relay(relay.id)
            return RelayCheck(fresh or relay, ok, changed, error, applied)

    async def check_relays(self) -> list[RelayCheck]:
        return [await self.sync_relay(r) for r in await self.db.relays()]

    async def add_relay(self, name: str, host: str, conn: str) -> Relay:
        try:
            conn = validate_conn(conn)
        except ValueError as e:
            raise ServiceError(f"Неверное подключение: {e}")
        if conn == "local":
            raise ServiceError("Входной сервер должен быть отдельным сервером в РФ, а не сервером с ботом.")
        if not host and not conn.startswith("mock:"):
            host = parse_ssh_target(conn)[1]
        relay = await self.db.add_relay(name, host, conn)
        check = await self.sync_relay(relay)
        if not check.ok:
            await self.db.delete_relay(relay.id)
            raise ServiceError(f"Не удалось настроить входной сервер: {check.error}")
        return check.relay

    async def update_relay(self, relay: Relay, **fields) -> RelayCheck:
        await self.db.update_relay(relay.id, **fields)
        await self._drop_relay(relay.id)
        return await self.sync_relay(relay)

    async def delete_relay(self, relay: Relay) -> None:
        try:
            await self._relay_client(relay).remove()
        except (AwgError, ServiceError, OSError) as e:  # вход уже недоступен — просто забываем его
            log.warning("Вход %s: не удалось убрать правила: %s", relay.name, e)
        await self._drop_relay(relay.id)
        await self.db.delete_relay(relay.id)
        if relay.is_main:
            rest = await self.db.relays()
            if rest:
                await self.db.set_main_relay(rest[0].id)

    async def whitelist_route(self, key: Key) -> tuple[Relay, RelayPort] | None:
        """Через какой вход и порт выдать ключ «для белых списков» (None — сейчас недоступно)."""
        relay = await self.db.main_relay()
        if relay is None or not relay.status_ok:
            return None
        for p in await self.db.relay_ports(relay.id):
            if p.server_id == key.server_id and p.protocol == key.protocol:
                return relay, p
        return None

    # ---------- ключи ----------

    def _params(self, key: Key, row: Server, info: ServerInfo, via: tuple[Relay, RelayPort] | None = None) -> ClientParams:
        suffix = f" {WHITELIST_SUFFIX}" if via else ""
        return ClientParams(
            host=via[0].host if via else row.host,
            port=via[1].listen_port if via else info.port,
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
            description=f"{self.settings.server_name} {row.title}{suffix}",
            mtu=self.settings.client_mtu,
        )

    def _rendered_awg(
        self, key: Key, row: Server, info: ServerInfo, via: tuple[Relay, RelayPort] | None = None
    ) -> RenderedKey:
        params = self._params(key, row, info, via)
        return RenderedKey(
            key,
            build_vpn_url(params),
            build_native_config(params),
            self.settings.server_name,
            row.title,
            whitelist=via is not None,
        )

    def _rendered_vless(self, key: Key, row: Server, info, via: tuple[Relay, RelayPort] | None = None) -> RenderedKey:
        label = f"{self.settings.server_name} {row.title} {key.name}"
        host = row.host
        if via:
            label += f" {WHITELIST_SUFFIX}"
            host, info = via[0].host, replace(info, port=via[1].listen_port)
        url = build_vless_url(info, host, key.public_key, label)
        return RenderedKey(key, url, "", self.settings.server_name, row.title, whitelist=via is not None)

    async def render(self, key: Key, whitelist: bool = False) -> RenderedKey:
        """whitelist=True — тот же ключ, но через основной входной сервер (для белых списков)."""
        # Параметры читаются с сервера каждый раз: если их поменяли в приложении
        # Amnezia, повторно отправленный ключ будет актуальным.
        via = None
        if whitelist:
            via = await self.whitelist_route(key)
            if via is None:
                raise ServiceError("Ключ для белых списков сейчас недоступен — попробуйте позже.")
        row, client = await self._client(key.server_id, key.protocol)
        if key.protocol == "vless":
            return self._rendered_vless(key, row, await client.info(), via)
        return self._rendered_awg(key, row, await client.server_info(), via)

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
            if protocol not in (row.protocol_list if force else row.visible_protocols):
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
        async with self._busy_key(key.id):
            if key.enabled:
                await self._remove_from_server(key)
            await self.db.delete_key(key.id)

    async def rename_device(self, key: Key, name: str) -> None:
        await self.db.update_key(key.id, name=name[:40])

    async def disable_key(self, key: Key) -> None:
        async with self._busy_key(key.id):
            if key.enabled:
                await self._remove_from_server(key)
            await self.db.update_key(key.id, enabled=0, last_rx=0, last_tx=0)

    async def enable_key(self, key: Key, user: User | None = None) -> bool:
        """Возвращает True, если у ключа сменился IP (клиенту нужно получить ключ заново)."""
        async with self._busy_key(key.id):
            return await self._enable_key(key, user)

    async def _enable_key(self, key: Key, user: User | None) -> bool:
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
        if key.protocol not in (target.protocol_list if force else target.visible_protocols):
            raise ServiceError(f"На этом сервере нет {key.protocol_name}.")
        user = await self.db.get_user(key.tg_id) if key.tg_id else None
        async with self._lock, self._busy_key(key.id):
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

    async def change_protocol(
        self, key: Key, protocol: str, server_id: int | None = None, *, force: bool = False
    ) -> RenderedKey:
        """Перевод устройства на другой протокол (и, если нужно, на другой сервер).
        Устройство, его слот и история трафика сохраняются, но ключ новый: у AWG и VLESS
        разные учётные данные, поэтому старый ключ в приложении перестаёт работать."""
        if protocol not in PROTOCOLS:
            raise ServiceError("Неизвестный протокол.")
        if protocol == key.protocol:
            raise ServiceError(f"Устройство уже на {key.protocol_name}.")
        target = await self.db.get_server(server_id or key.server_id) if (server_id or key.server_id) else None
        if target is None or (not force and not (target.active and target.status_ok)):
            raise ServiceError("Эта локация сейчас недоступна, выберите другую.")
        if protocol not in (target.protocol_list if force else target.visible_protocols):
            raise ServiceError(f"{PROTOCOLS[protocol]} на этом сервере сейчас недоступен.")
        user = await self.db.get_user(key.tg_id) if key.tg_id else None
        async with self._lock, self._busy_key(key.id):
            row, client = await self._client(target.id, protocol)
            name = self._peer_name(user, key.name)
            if protocol == "vless":
                client_id, info = await client.add_client(name, new_client_id())
                creds = {"public_key": client_id, "private_key": "", "ip": ""}
            else:
                peer, info = await client.add_peer(name, reserved=await self.db.reserved_ips(row.id))
                creds = {"public_key": peer.public_key, "private_key": peer.private_key, "ip": peer.ip}
            if key.enabled:
                # Старые учётные данные исчезнут из базы — снять их потом будет некому,
                # поэтому если старый сервер не отвечает, отменяем смену целиком.
                try:
                    await self._remove_from_server(key)
                except (AwgError, ServiceError) as e:
                    await self._drop_new_creds(client, protocol, creds["public_key"])
                    raise ServiceError("Сервер устройства сейчас не отвечает, попробуйте чуть позже.") from e
            await self.db.update_key(
                key.id, **creds, protocol=protocol, server_id=row.id, enabled=1,
                last_rx=0, last_tx=0, last_handshake=0,
            )
            changed = await self.db.get_key(key.id)
            assert changed
            # Отключённое (нет подписки / сверх лимита) устройство остаётся отключённым.
            if not key.enabled or (user is not None and not self.has_access(user)):
                await self.disable_key(changed)
                changed = await self.db.get_key(key.id)
                assert changed
        if protocol == "vless":
            return self._rendered_vless(changed, row, info)
        return self._rendered_awg(changed, row, info)

    @staticmethod
    async def _drop_new_creds(client, protocol: str, cid: str) -> None:
        try:
            if protocol == "vless":
                await client.remove_client(cid)
            else:
                await client.remove_peer(cid)
        except AwgError as e:
            log.warning("Не удалось откатить новый ключ %s: %s", cid, e)

    async def protocol_options(self, key: Key) -> list[Server]:
        """Серверы, на которые можно перевести устройство другим протоколом (текущий — первым)."""
        other = "vless" if key.protocol == "awg" else "awg"
        rows = await self.available_servers(other)
        return sorted(rows, key=lambda s: s.id != key.server_id)

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

    # ---------- подписка ----------

    async def extend(self, tg_id: int, days: int, devices: int | None = None) -> User:
        user = await self.db.get_user(tg_id)
        if user is None:
            raise ServiceError("Пользователь не найден.")
        if not -MAX_DAYS <= days <= MAX_DAYS:
            raise ServiceError(f"Срок должен быть от -{MAX_DAYS} до {MAX_DAYS} дней.")
        base = max(now(), user.sub_until or 0)
        fields: dict = {"sub_until": clamp_ts(base + days * 86400), "notified": 0, "reminded": None}
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
        await self.db.update_user(
            tg_id, sub_until=None if until is None else clamp_ts(until), notified=0, reminded=None
        )
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

    async def confirm_payment(
        self, payment_id: int, admin_id: int, from_status: tuple[str, ...] = ("pending",)
    ) -> PaymentResult:
        """admin_id = 0 — подтверждено автоматически (ЮMoney)."""
        if not await self.db.decide_payment(payment_id, "paid", admin_id, from_status):
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

    # ---------- автооплата ЮMoney ----------

    async def _init_yoomoney(self) -> None:
        if self.yoomoney is None or self.yoomoney_wallet:
            return
        try:
            self.yoomoney_wallet = await self.yoomoney.account()
            log.info("ЮMoney: кошелёк %s", self.yoomoney_wallet)
        except YooMoneyError as e:
            log.error("ЮMoney: не удалось узнать номер кошелька (%s) — задайте YOOMONEY_WALLET в .env", e)

    @property
    def autopay(self) -> bool:
        return self.yoomoney is not None and bool(self.yoomoney_wallet)

    async def plan_invoice(self, user: User, plan: Plan) -> Payment:
        price = await self.price_for(user, plan)
        title = plan.title + (" (скидка новичка)" if price < plan.price else "")
        return await self.db.invoice(
            user.tg_id,
            kind="plan",
            plan_id=plan.id,
            title=title,
            days=plan.days,
            devices=plan.devices,
            amount=price,
            fresh_after=now() - INVOICE_TTL // 2,
        )

    async def slot_invoice(self, user: User, slot_id: int | None) -> Payment:
        s = self.settings
        title = "Продление доп. слота устройства" if slot_id else "Доп. слот устройства"
        return await self.db.invoice(
            user.tg_id,
            kind="slot",
            slot_id=slot_id,
            title=title,
            days=s.slot_days,
            devices=1,
            amount=s.slot_price,
            fresh_after=now() - INVOICE_TTL // 2,
        )

    def pay_url(self, payment: Payment, success_url: str = "") -> str:
        assert payment.label
        return quickpay_url(
            self.yoomoney_wallet,
            payment.amount,
            payment.label,
            f"{self.settings.server_name}: {payment.title}"[:150],
            self.settings.yoomoney_payment_type,
            success_url,
        )

    def _enough(self, payment: Payment, op: Operation) -> bool:
        need = payment.amount * (100 - self.settings.yoomoney_fee_percent) / 100
        return op.amount + 0.005 >= need

    async def check_invoices(self, payment: Payment | None = None) -> list[AutoPayment]:
        """Ищет оплаты счетов в истории кошелька. payment — проверить только этот счёт (кнопка клиента)."""
        if self.yoomoney is None:
            return []
        async with self._pay_lock:
            if payment is None:
                await self.db.expire_invoices(now() - INVOICE_TTL)
                if not await self.db.open_invoices(now() - INVOICE_TTL):
                    return []
                ops = await self.yoomoney.operations()
            else:
                if not payment.label:
                    return []
                ops = await self.yoomoney.operations(label=payment.label, records=10)
            done = []
            for op in ops:
                if op.direction != "in" or op.status != "success" or not op.label:
                    continue
                # Просроченный счёт тоже засчитываем: клиент мог оплатить по старой ссылке.
                p = await self.db.payment_by_label(op.label)
                if p is None or p.status not in ("invoice", "expired"):
                    continue
                if not await self.db.claim_operation(p.id, op.operation_id, op.amount):
                    continue
                if not self._enough(p, op):
                    await self.db.set_payment_review(
                        p.id,
                        f"ЮMoney: зачислено {op.amount:.2f} {self.settings.currency} вместо {p.amount} "
                        f"(операция {op.operation_id}). Проверьте и подтвердите вручную.",
                    )
                    reviewed = await self.db.get_payment(p.id)
                    assert reviewed
                    done.append(AutoPayment(reviewed, review=True))
                    log.warning("ЮMoney: счёт №%s оплачен не полностью (%s из %s)", p.id, op.amount, p.amount)
                    continue
                try:
                    result = await self.confirm_payment(p.id, 0, from_status=("invoice", "expired"))
                except ServiceError as e:  # например, аккаунт удалили, пока он платил
                    log.error("ЮMoney: счёт №%s оплачен, но не активирован: %s", p.id, e)
                    continue
                log.info("ЮMoney: счёт №%s оплачен (%s), операция %s", p.id, op.amount, op.operation_id)
                done.append(AutoPayment(result.payment, result=result))
            return done

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
        for relay_id in list(self._relay_clients):
            await self._drop_relay(relay_id)
        if self.yoomoney:
            await self.yoomoney.close()


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
