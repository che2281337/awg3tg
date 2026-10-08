"""VLESS (Xray) от приложения AmneziaVPN: контейнер amnezia-xray.

Как и приложение Amnezia, бот добавляет клиентов в inbounds[0].settings.clients
файла /opt/amnezia/xray/server.json и перезапускает контейнер (Xray не умеет
перечитывать конфиг на лету — VLESS-клиенты этого сервера переподключатся за пару секунд).

Для статистики трафика бот включает в server.json встроенный Stats API Xray
(api + stats + policy). Приложение Amnezia при изменении настроек XRay может
перезаписать server.json — бот вернёт API при следующем изменении клиентов.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import uuid as uuidlib
from dataclasses import dataclass

from .server import AwgError, ContainerClient

log = logging.getLogger(__name__)

XRAY_CONTAINER = "amnezia-xray"
XRAY_DIR = "/opt/amnezia/xray"
SERVER_CONFIG = f"{XRAY_DIR}/server.json"
PUBLIC_KEY = f"{XRAY_DIR}/xray_public.key"
SHORT_ID = f"{XRAY_DIR}/xray_short_id.key"
API_PORT = 10085
API_TAG = "api"


@dataclass
class XrayInfo:
    port: int
    network: str  # tcp | xhttp | kcp
    security: str  # reality | tls | none
    flow: str
    sni: str = ""
    fingerprint: str = "chrome"
    public_key: str = ""
    short_id: str = ""
    alpn: str = ""
    path: str = ""
    host: str = ""
    mode: str = ""


@dataclass
class XrayStats:
    rx: int  # от клиента (uplink)
    tx: int  # клиенту (downlink)


def new_client_id() -> str:
    return str(uuidlib.uuid4())


def _inbound(cfg: dict) -> dict:
    for inbound in cfg.get("inbounds", []):
        if inbound.get("protocol") == "vless":
            return inbound
    raise AwgError("В server.json нет VLESS-inbound — настройте XRay в приложении AmneziaVPN")


def parse_info(cfg: dict, public_key: str = "", short_id: str = "") -> XrayInfo:
    inbound = _inbound(cfg)
    stream = inbound.get("streamSettings") or {}
    network = stream.get("network") or "tcp"
    if network == "raw":
        network = "tcp"
    security = stream.get("security") or "none"
    clients = (inbound.get("settings") or {}).get("clients") or []
    flow = next((c.get("flow", "") for c in clients if c.get("flow")), "")
    if network != "tcp" or security not in ("reality", "tls"):
        flow = ""  # vision работает только поверх raw/tcp + tls/reality (как в клиенте Amnezia)
    info = XrayInfo(port=int(inbound.get("port") or 443), network=network, security=security, flow=flow)
    if security == "reality":
        rs = stream.get("realitySettings") or {}
        names = rs.get("serverNames") or []
        info.sni = names[0] if names else (rs.get("dest") or "").split(":")[0]
        info.fingerprint = rs.get("fingerprint") or "chrome"
        info.public_key = public_key or rs.get("publicKey", "")
        ids = rs.get("shortIds") or []
        info.short_id = short_id or (ids[0] if ids else "")
    elif security == "tls":
        ts = stream.get("tlsSettings") or {}
        info.sni = ts.get("serverName", "")
        info.fingerprint = ts.get("fingerprint") or "chrome"
        info.alpn = ",".join(ts.get("alpn") or [])
    if network == "xhttp":
        xs = stream.get("xhttpSettings") or {}
        info.path = xs.get("path", "")
        info.host = xs.get("host", "")
        info.mode = xs.get("mode", "")
    return info


def build_vless_url(info: XrayInfo, host: str, client_id: str, name: str) -> str:
    """Ссылка vless:// — её понимают AmneziaVPN, v2rayNG, Hiddify, Streisand, FoXray и др."""
    params: dict[str, str] = {"type": info.network, "encryption": "none", "security": info.security}
    if info.flow:
        params["flow"] = info.flow
    if info.security in ("reality", "tls"):
        if info.sni:
            params["sni"] = info.sni
        params["fp"] = info.fingerprint or "chrome"
    if info.security == "reality":
        params["pbk"] = info.public_key
        if info.short_id:
            params["sid"] = info.short_id
    if info.alpn:
        params["alpn"] = info.alpn
    if info.network == "xhttp":
        if info.path:
            params["path"] = info.path
        if info.host:
            params["host"] = info.host
        if info.mode:
            params["mode"] = info.mode
    addr = f"[{host}]" if ":" in host and not host.startswith("[") else host
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"vless://{client_id}@{addr}:{info.port}?{query}#{urllib.parse.quote(name)}"


def ensure_stats_api(cfg: dict) -> bool:
    """Добавляет в конфиг сервера Stats API. Возвращает True, если что-то изменилось."""
    changed = False
    if "stats" not in cfg:
        cfg["stats"] = {}
        changed = True
    api = cfg.get("api") or {}
    if api.get("tag") != API_TAG or "StatsService" not in (api.get("services") or []):
        cfg["api"] = {"tag": API_TAG, "services": ["StatsService"]}
        changed = True
    policy = cfg.setdefault("policy", {})
    level0 = policy.setdefault("levels", {}).setdefault("0", {})
    for flag in ("statsUserUplink", "statsUserDownlink"):
        if not level0.get(flag):
            level0[flag] = True
            changed = True
    inbounds = cfg.setdefault("inbounds", [])
    if not any(i.get("tag") == API_TAG for i in inbounds):
        inbounds.append(
            {
                "tag": API_TAG,
                "listen": "127.0.0.1",
                "port": API_PORT,
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"},
            }
        )
        changed = True
    routing = cfg.setdefault("routing", {})
    rules = routing.setdefault("rules", [])
    if not any(API_TAG in (r.get("inboundTag") or []) for r in rules):
        rules.insert(0, {"type": "field", "inboundTag": [API_TAG], "outboundTag": API_TAG})
        changed = True
    return changed


class XrayServer(ContainerClient):
    clients_table_path = f"{XRAY_DIR}/clientsTable"

    async def detect(self) -> None:
        if not self.container:
            if XRAY_CONTAINER not in await self.running_containers():
                raise AwgError("Не найден контейнер amnezia-xray — установите XRay через приложение AmneziaVPN")
            self.container = XRAY_CONTAINER
        log.info("Xray [%s]: container=%s", self.runner.description, self.container)

    async def load_config(self) -> dict:
        raw = await self.read_file(SERVER_CONFIG)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise AwgError(f"server.json повреждён: {e}")

    async def _read_opt(self, path: str) -> str:
        try:
            return (await self.read_file(path)).strip()
        except AwgError:
            return ""

    async def info(self, cfg: dict | None = None) -> XrayInfo:
        cfg = cfg or await self.load_config()
        return parse_info(cfg, await self._read_opt(PUBLIC_KEY), await self._read_opt(SHORT_ID))

    async def restart(self) -> None:
        assert self.container
        await self._run("docker", "restart", self.container)

    async def _config_ok(self, path: str) -> str:
        """Проверка конфига самим Xray. Пустая строка — всё хорошо, иначе текст ошибки."""
        out = await self._exec(f"xray run -test -config {path} 2>&1; echo \"rc=$?\"", check=False)
        return "" if out.rstrip().endswith("rc=0") else out.strip()[-500:]

    async def _save(self, cfg: dict) -> None:
        """Пишет server.json только если Xray его принимает — сломанный конфиг уронил бы VLESS."""
        tmp = SERVER_CONFIG + ".new"
        with_api = json.loads(json.dumps(cfg))
        ensure_stats_api(with_api)
        for candidate in (with_api, cfg):  # без статистики — если эта версия Xray её не принимает
            await self.write_file(tmp, json.dumps(candidate, ensure_ascii=False, indent=4) + "\n")
            error = await self._config_ok(tmp)
            if not error:
                break
            log.warning("Xray отклонил конфиг: %s", error)
        else:
            await self._exec(f"rm -f {tmp}", check=False)
            raise AwgError(f"Xray не принял новый конфиг: {error}")
        await self._exec(f"mv {tmp} {SERVER_CONFIG}")
        await self.restart()

    @staticmethod
    def client_ids(cfg: dict) -> list[str]:
        return [c.get("id", "") for c in (_inbound(cfg).get("settings") or {}).get("clients") or []]

    async def add_client(self, client_name: str, client_id: str | None = None) -> tuple[str, XrayInfo]:
        async with self._lock:
            cfg = await self.load_config()
            info = await self.info(cfg)
            client_id = client_id or new_client_id()
            inbound = _inbound(cfg)
            clients = inbound.setdefault("settings", {}).setdefault("clients", [])
            if not any(c.get("id") == client_id for c in clients):
                entry = {"id": client_id, "email": client_id}  # email — ключ статистики трафика
                if info.flow:
                    entry["flow"] = info.flow
                clients.append(entry)
                await self._save(cfg)
                await self._table_add(client_id, client_name)
            return client_id, info

    async def remove_clients(self, client_ids: set[str]) -> int:
        """Удаляет сразу нескольких клиентов — один перезапуск контейнера."""
        async with self._lock:
            cfg = await self.load_config()
            settings = _inbound(cfg).setdefault("settings", {})
            before = settings.get("clients") or []
            after = [c for c in before if c.get("id") not in client_ids]
            removed = len(before) - len(after)
            if removed:
                settings["clients"] = after
                await self._save(cfg)
            for cid in client_ids:
                await self._table_remove(cid)
            return removed

    async def remove_client(self, client_id: str) -> bool:
        return await self.remove_clients({client_id}) > 0

    async def stats(self) -> dict[str, XrayStats]:
        """Трафик по клиентам из Stats API (счётчики обнуляются при перезапуске контейнера)."""
        out = await self._exec(f"xray api statsquery --server=127.0.0.1:{API_PORT} -pattern 'user>>>'", check=False)
        try:
            data = json.loads(out) if out.strip() else {}
        except json.JSONDecodeError:
            return {}
        result: dict[str, XrayStats] = {}
        for item in data.get("stat") or []:
            parts = str(item.get("name", "")).split(">>>")
            if len(parts) != 4 or parts[0] != "user" or parts[2] != "traffic":
                continue
            st = result.setdefault(parts[1], XrayStats(0, 0))
            value = int(item.get("value") or 0)
            if parts[3] == "uplink":
                st.rx = value
            elif parts[3] == "downlink":
                st.tx = value
        return result
