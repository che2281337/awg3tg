"""Работа с AWG-сервером, установленным приложением AmneziaVPN (docker-контейнер).

Бот делает то же, что и приложение Amnezia при «Поделиться VPN»:
дописывает [Peer] в /opt/amnezia/awg/awg0.conf, применяет его через
`awg syncconf` без разрыва соединений и добавляет клиента в clientsTable,
чтобы он был виден в разделе «Пользователи» приложения.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import shlex
from dataclasses import dataclass
from datetime import datetime

from . import conf as wgconf
from .keys import generate_keypair, public_from_private

log = logging.getLogger(__name__)

AWG_DIR = "/opt/amnezia/awg"
CLIENTS_TABLE = f"{AWG_DIR}/clientsTable"
SERVER_PUBLIC_KEY = f"{AWG_DIR}/wireguard_server_public_key.key"
SERVER_PSK = f"{AWG_DIR}/wireguard_psk.key"

# container -> (config path, interface, binary)
KNOWN_CONTAINERS: dict[str, tuple[str, str, str]] = {
    "amnezia-awg2": (f"{AWG_DIR}/awg0.conf", "awg0", "awg"),  # AmneziaWG 2.0 / 3.x
    "amnezia-awg": (f"{AWG_DIR}/wg0.conf", "wg0", "wg"),  # AmneziaWG legacy (1.x)
}


class AwgError(RuntimeError):
    pass


@dataclass
class NewPeer:
    private_key: str
    public_key: str
    ip: str


@dataclass
class ServerInfo:
    container: str
    port: int
    server_public_key: str
    preshared_key: str | None
    awg_params: dict[str, str]
    subnet_address: str
    subnet_cidr: str


@dataclass
class PeerStats:
    latest_handshake: int
    rx: int
    tx: int


def qt_date_now() -> str:
    """Формат QDateTime::toString() по умолчанию (Qt::TextDate), как у Amnezia."""
    now = datetime.now()
    return now.strftime("%a %b ") + str(now.day) + now.strftime(" %H:%M:%S %Y")


class AwgServer:
    def __init__(
        self,
        container: str | None = None,
        config_path: str | None = None,
        interface: str | None = None,
        binary: str | None = None,
        docker: str = "docker",
    ) -> None:
        self.container = container
        self._config_path = config_path
        self._interface = interface
        self._binary = binary
        self.docker = docker
        self._lock = asyncio.Lock()

    # ---------- низкоуровневое ----------

    async def _run(self, *args: str, stdin: bytes | None = None, check: bool = True) -> str:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(stdin)
        if check and proc.returncode != 0:
            raise AwgError(f"{' '.join(args[:4])}...: {err.decode(errors='replace').strip() or proc.returncode}")
        return out.decode(errors="replace")

    async def _exec(self, script: str, stdin: bytes | None = None, check: bool = True) -> str:
        assert self.container, "container not detected"
        flags = ["-i"] if stdin is not None else []
        return await self._run(
            self.docker, "exec", *flags, self.container, "bash", "-c", script, stdin=stdin, check=check
        )

    async def read_file(self, path: str) -> str:
        return await self._exec(f"cat {shlex.quote(path)}")

    async def write_file(self, path: str, content: str) -> None:
        tmp = path + ".tmp"
        await self._exec(
            f"cat > {shlex.quote(tmp)} && mv {shlex.quote(tmp)} {shlex.quote(path)}",
            stdin=content.encode(),
        )

    # ---------- настройка ----------

    async def detect(self) -> None:
        """Определяет контейнер AWG, если он не задан явно."""
        if not self.container:
            names = (await self._run(self.docker, "ps", "--format", "{{.Names}}")).split()
            for candidate in KNOWN_CONTAINERS:
                if candidate in names:
                    self.container = candidate
                    break
            else:
                raise AwgError(
                    "Не найден запущенный контейнер amnezia-awg2/amnezia-awg. "
                    "Установите AmneziaWG на сервер через приложение AmneziaVPN или задайте AWG_CONTAINER."
                )
        defaults = KNOWN_CONTAINERS.get(self.container, KNOWN_CONTAINERS["amnezia-awg2"])
        self._config_path = self._config_path or defaults[0]
        self._interface = self._interface or defaults[1]
        self._binary = self._binary or defaults[2]
        log.info(
            "AWG: container=%s config=%s iface=%s bin=%s",
            self.container,
            self._config_path,
            self._interface,
            self._binary,
        )

    @property
    def config_path(self) -> str:
        assert self._config_path
        return self._config_path

    async def load_config(self) -> wgconf.WgConfig:
        return wgconf.parse(await self.read_file(self.config_path))

    async def server_info(self, cfg: wgconf.WgConfig | None = None) -> ServerInfo:
        cfg = cfg or await self.load_config()
        iface = cfg.interface

        port = iface.get("ListenPort")
        if not port:
            raise AwgError("В конфиге сервера нет ListenPort")

        try:
            server_pub = (await self.read_file(SERVER_PUBLIC_KEY)).strip()
        except AwgError:
            server_pub = ""
        if not server_pub:
            priv = iface.get("PrivateKey")
            if not priv:
                raise AwgError("Не удалось получить публичный ключ сервера")
            server_pub = public_from_private(priv)

        try:
            psk = (await self.read_file(SERVER_PSK)).strip() or None
        except AwgError:
            psk = None

        network = self._network(cfg)
        return ServerInfo(
            container=self.container or "",
            port=int(port),
            server_public_key=server_pub,
            preshared_key=psk,
            awg_params=cfg.awg_params(),
            subnet_address=str(network.network_address),
            subnet_cidr=str(network.prefixlen),
        )

    @staticmethod
    def _interface_address(cfg: wgconf.WgConfig) -> ipaddress.IPv4Interface:
        address = cfg.interface.get("Address")
        if not address:
            raise AwgError("В конфиге сервера нет Address")
        for part in address.split(","):
            try:
                iface = ipaddress.ip_interface(part.strip())
            except ValueError:
                continue
            if isinstance(iface, ipaddress.IPv4Interface):
                return iface
        raise AwgError(f"Не найден IPv4-адрес в Address = {address}")

    @classmethod
    def _network(cls, cfg: wgconf.WgConfig) -> ipaddress.IPv4Network:
        return cls._interface_address(cfg).network

    @classmethod
    def ip_in_use(cls, cfg: wgconf.WgConfig, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return True
        iface = cls._interface_address(cfg)
        if addr == iface.ip or addr not in iface.network:
            return True
        return addr in cls._used_ips(cfg, iface)

    @staticmethod
    def _used_ips(cfg: wgconf.WgConfig, iface: ipaddress.IPv4Interface) -> set:
        used = set()
        for peer in cfg.peers:
            for allowed in peer.get_all("AllowedIPs"):
                for part in allowed.split(","):
                    try:
                        net = ipaddress.ip_network(part.strip(), strict=False)
                    except ValueError:
                        continue
                    if isinstance(net, ipaddress.IPv4Network) and net.subnet_of(iface.network):
                        used.update(net)
        return used

    @classmethod
    def allocate_ip(cls, cfg: wgconf.WgConfig, reserved: set[str] | frozenset[str] = frozenset()) -> str:
        """Первый свободный адрес; `reserved` — адреса отключённых ключей из БД."""
        iface = cls._interface_address(cfg)
        used = {iface.ip} | cls._used_ips(cfg, iface)
        for r in reserved:
            try:
                used.add(ipaddress.ip_address(r))
            except ValueError:
                pass
        for host in iface.network.hosts():
            if host not in used:
                return str(host)
        raise AwgError(f"В подсети {iface.network} закончились свободные адреса")

    async def apply(self) -> None:
        b, i = self._binary, self._interface
        await self._exec(f"{b} syncconf {i} <({b}-quick strip {shlex.quote(self.config_path)})")

    # ---------- clientsTable (список пользователей в приложении Amnezia) ----------

    async def _load_clients_table(self) -> list:
        try:
            raw = await self.read_file(CLIENTS_TABLE)
        except AwgError:
            return []
        try:
            data = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError:
            log.warning("clientsTable повреждён, будет перезаписан")
            return []
        if isinstance(data, dict):  # старый формат {clientId: {clientName}}
            data = [
                {"clientId": cid, "userData": {"clientName": (v or {}).get("clientName", "")}}
                for cid, v in data.items()
            ]
        return data if isinstance(data, list) else []

    async def _save_clients_table(self, table: list) -> None:
        await self.write_file(CLIENTS_TABLE, json.dumps(table, ensure_ascii=False, indent=4) + "\n")

    # ---------- публичное API ----------

    async def add_peer(
        self,
        client_name: str,
        *,
        keypair: tuple[str, str] | None = None,
        ip: str | None = None,
        reserved: set[str] | frozenset[str] = frozenset(),
    ) -> tuple[NewPeer, ServerInfo]:
        """Добавляет пира. С `keypair`/`ip` — восстанавливает ранее отключённый ключ:
        если его адрес за это время заняли, выделяется новый."""
        async with self._lock:
            cfg = await self.load_config()
            info = await self.server_info(cfg)
            private_key, public_key = keypair or generate_keypair()
            if cfg.find_peer(public_key) is not None:
                peer_ip = (cfg.find_peer(public_key).get("AllowedIPs") or "").split("/")[0]
                return NewPeer(private_key, public_key, peer_ip or ip or ""), info
            if not ip or self.ip_in_use(cfg, ip):
                ip = self.allocate_ip(cfg, reserved)
            cfg.add_peer(public_key, info.preshared_key, f"{ip}/32")
            await self.write_file(self.config_path, cfg.dump())
            await self.apply()

            try:
                table = await self._load_clients_table()
                table.append(
                    {
                        "clientId": public_key,
                        "userData": {"clientName": client_name, "creationDate": qt_date_now()},
                    }
                )
                await self._save_clients_table(table)
            except AwgError:
                log.exception("Не удалось обновить clientsTable (ключ при этом выдан)")

            return NewPeer(private_key, public_key, ip), info

    async def remove_peer(self, public_key: str) -> bool:
        async with self._lock:
            cfg = await self.load_config()
            removed = cfg.remove_peer(public_key)
            if removed:
                await self.write_file(self.config_path, cfg.dump())
                await self.apply()
            try:
                table = await self._load_clients_table()
                new_table = [c for c in table if c.get("clientId") != public_key]
                if len(new_table) != len(table):
                    await self._save_clients_table(new_table)
            except AwgError:
                log.exception("Не удалось обновить clientsTable")
            return removed

    async def stats(self) -> dict[str, PeerStats]:
        """`awg show <iface> dump`: последнее рукопожатие и трафик по каждому пиру."""
        out = await self._exec(f"{self._binary} show {self._interface} dump", check=False)
        result: dict[str, PeerStats] = {}
        for i, line in enumerate(out.splitlines()):
            cols = line.split("\t")
            if i == 0 or len(cols) < 8:
                continue  # первая строка — сам интерфейс
            try:
                result[cols[0]] = PeerStats(int(cols[4]), int(cols[5]), int(cols[6]))
            except ValueError:
                continue
        return result
