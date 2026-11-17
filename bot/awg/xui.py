"""VLESS и AmneziaWG через панель 3x-ui (3.x): клиенты добавляются и удаляются через API панели.

Бот заходит на сервер по SSH (как и к Amnezia) и обращается к API панели с самого сервера —
`curl` на 127.0.0.1, поэтому панель не обязана быть доступна из интернета.

API-токен бот выпускает себе сам командой `x-ui setting -getApiToken -tokenName awg3tg` и
хранит только на сервере, в конфиге curl `/etc/awg3tg/3x-ui.curl` (права 600). Токен не
передаётся в командной строке и не попадает в базу бота.

Необязательный файл `/etc/awg3tg/3x-ui.json` на сервере:
    {"inbound": 3}                     — id или remark VLESS-подключения (inbound) для бота;
    {"awg_inbound": 4}                 — то же для AmneziaWG;
    {"url": "https://host:port/path/"} — адрес панели, если автоопределение не подходит
                                          (например, задан Listening Domain).
Без него бот берёт включённое VLESS-подключение (предпочитая порт 443) и первое включённое
AmneziaWG-подключение.

Интерфейсы совпадают с XrayServer и AwgServer (Amnezia), поэтому остальной код бота
работает с обоими вариантами одинаково.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import re
import secrets
import shlex
import string
import urllib.parse
from dataclasses import dataclass, field

from .keys import generate_keypair
from .runner import Runner, RunnerError
from .server import AwgError, NewPeer, PeerStats, ServerInfo
from .xray import XrayInfo, XrayStats

log = logging.getLogger(__name__)

XUI_BINARY = "/usr/local/x-ui/x-ui"
XUI_DOCKER_CONTAINER = "3x-ui"
XUI_DOCKER_BINARY = "/app/x-ui"
STATE_DIR = "/etc/awg3tg"
CURL_CONFIG = f"{STATE_DIR}/3x-ui.curl"
OVERRIDES = f"{STATE_DIR}/3x-ui.json"
AWG_PSK_FILE = f"{STATE_DIR}/3x-ui-awg.psk"
TOKEN_NAME = "awg3tg"
VISION = "xtls-rprx-vision"
# Имя контейнера в ключе vpn:// — по нему приложение AmneziaVPN понимает, что это AmneziaWG 2/3
AMNEZIA_AWG_CONTAINER = "amnezia-awg2"
# Клиенты AWG от бота: email = awg-<20 hex от публичного ключа> (уникален в панели, не меняется)
AWG_EMAIL_RE = re.compile(r"^awg-[0-9a-f]{20}$")
# Как в 3x-ui (amneziawg.EffectiveMTU): без явного MTU клиенту — 1420 минус S4, но не меньше 1280
AWG_DEFAULT_MTU, AWG_MAX_MTU = 1420, 1420

# Поля XHTTP, которые нужны клиенту (как их передаёт в ссылке сама панель — buildXhttpExtra).
# Чисто серверные (scMaxBufferedPosts, scStreamUpServerSecs, noSSEHeader) клиенту не нужны.
_XHTTP_STRING_FIELDS = (
    "xPaddingBytes",
    "uplinkHTTPMethod",
    "sessionIDPlacement",
    "sessionIDKey",
    "sessionIDTable",
    "sessionIDLength",
    "seqPlacement",
    "seqKey",
    "uplinkDataPlacement",
    "uplinkDataKey",
    "scMaxEachPostBytes",
    "scMinPostsIntervalMs",
)
_XHTTP_CORE_DEFAULTS = {"scMaxEachPostBytes": "1000000", "scMinPostsIntervalMs": "30"}
_XHTTP_OBFS_FIELDS = ("xPaddingKey", "xPaddingHeader", "xPaddingPlacement", "xPaddingMethod")


def _json_field(value) -> dict:
    """settings / streamSettings в API панели — объект или JSON-строка."""
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError) as e:
        raise AwgError(f"3x-ui: повреждённые настройки подключения: {e}")
    return data if isinstance(data, dict) else {}


# ======================================================================
# VLESS
# ======================================================================


def normalize_inbound(raw: dict) -> dict:
    """Подключение из API панели → {inbound, settings, stream} с разобранным JSON."""
    return {
        "inbound": raw,
        "settings": _json_field(raw.get("settings")),
        "stream": _json_field(raw.get("streamSettings")),
    }


def clients_of(cfg: dict) -> list[dict]:
    return [c for c in (cfg["settings"].get("clients") or []) if isinstance(c, dict)]


def xhttp_extra(xs: dict) -> dict:
    extra: dict = {}
    for key in _XHTTP_STRING_FIELDS:
        value = xs.get(key)
        if isinstance(value, str) and value and value != _XHTTP_CORE_DEFAULTS.get(key):
            extra[key] = value
    for legacy, renamed in (("sessionPlacement", "sessionIDPlacement"), ("sessionKey", "sessionIDKey")):
        if renamed not in extra and isinstance(xs.get(legacy), str) and xs[legacy]:
            extra[renamed] = xs[legacy]
    if xs.get("xPaddingObfsMode") is True:
        extra["xPaddingObfsMode"] = True
        for key in _XHTTP_OBFS_FIELDS:
            if isinstance(xs.get(key), str) and xs[key]:
                extra[key] = xs[key]
    if xs.get("uplinkChunkSize"):
        extra["uplinkChunkSize"] = xs["uplinkChunkSize"]
    if xs.get("noGRPCHeader") is True:
        extra["noGRPCHeader"] = True
    for key in ("xmux", "downloadSettings"):
        if isinstance(xs.get(key), dict) and xs[key]:
            extra[key] = xs[key]
    headers = {k: v for k, v in (xs.get("headers") or {}).items() if k.lower() != "host"}
    if headers:
        extra["headers"] = headers
    return extra


def parse_xui_info(cfg: dict) -> XrayInfo:
    inbound, settings, stream = cfg["inbound"], cfg["settings"], cfg["stream"]
    decryption = settings.get("decryption") or "none"
    if decryption != "none":
        raise AwgError("3x-ui: VLESS Encryption (decryption ≠ none) бот не поддерживает — выключите его в подключении")
    network = stream.get("network") or "tcp"
    if network == "raw":
        network = "tcp"
    if network not in ("tcp", "xhttp"):
        raise AwgError(f"3x-ui: транспорт {network} бот не поддерживает — используйте TCP (RAW) или XHTTP")
    security = stream.get("security") or "none"
    flow = ""
    if network == "tcp" and security in ("reality", "tls") and not inbound.get("disableFlow"):
        clients = clients_of(cfg)
        # Как у существующих клиентов; для пустого подключения — Vision (по инструкции для TCP/RAW)
        flow = next((c.get("flow", "") for c in clients if c.get("flow")), "" if clients else VISION)
    info = XrayInfo(port=int(inbound.get("port") or 443), network=network, security=security, flow=flow)
    if security == "reality":
        rs = stream.get("realitySettings") or {}
        inner = rs.get("settings") or {}
        names = [n for n in rs.get("serverNames") or [] if n]
        info.sni = names[0] if names else ""
        info.fingerprint = inner.get("fingerprint") or "chrome"
        info.public_key = inner.get("publicKey") or rs.get("publicKey") or ""
        if not info.public_key:
            raise AwgError("3x-ui: у подключения REALITY нет publicKey — нажмите Get New Cert в панели")
        ids = rs.get("shortIds") or []
        info.short_id = ids[0] if ids else ""
    elif security == "tls":
        ts = stream.get("tlsSettings") or {}
        inner = ts.get("settings") or {}
        info.sni = ts.get("serverName", "")
        info.fingerprint = inner.get("fingerprint") or ts.get("fingerprint") or "chrome"
        info.alpn = ",".join(ts.get("alpn") or [])
    if network == "xhttp":
        xs = stream.get("xhttpSettings") or {}
        info.path = xs.get("path", "")
        info.host = xs.get("host", "")
        info.mode = xs.get("mode", "")
        info.xhttp_extra = xhttp_extra(xs)
    return info


# ======================================================================
# AmneziaWG
# ======================================================================


def awg_email(public_key: str) -> str:
    return "awg-" + hashlib.sha256(public_key.encode()).hexdigest()[:20]


def awg_effective_mtu(server: dict) -> int:
    mtu = int(server.get("mtu") or 0)
    if mtu > 0:
        return min(mtu, AWG_MAX_MTU)
    return max(AWG_DEFAULT_MTU - max(int(server.get("s4") or 0), 0), 1280)


def awg_params_from_server(server: dict) -> dict[str, str]:
    """Параметры обфускации сервера 3x-ui → поля [Interface] клиента (как их пишет сама панель)."""
    params: dict[str, str] = {}
    for key in ("Jc", "Jmin", "Jmax", "S1", "S2"):
        params[key] = str(int(server.get(key.lower()) or 0))
    for key in ("S3", "S4"):
        if int(server.get(key.lower()) or 0) > 0:
            params[key] = str(int(server[key.lower()]))
    for i in range(1, 5):
        params[f"H{i}"] = str(server.get(f"h{i}") or i)
    for i in range(1, 6):
        if server.get(f"i{i}"):
            params[f"I{i}"] = str(server[f"i{i}"])
    for key in (
        "HeaderProtectionKey",
        "ContentPaddingAddition",
        "RekeyAfterTime",
        "RekeyTimeout",
        "RejectAfterTime",
        "KeepaliveTimeout",
        "MaxHandshakeAttempts",
    ):
        value = server.get(key[0].lower() + key[1:])
        if value:
            params[key] = str(value)
    for key in ("RandomTrailers", "DisableCookies"):
        if server.get(key[0].lower() + key[1:]):
            params[key] = "on"
    return params


@dataclass
class XuiAwgConfig:
    """AmneziaWG-подключение панели в том виде, который ждёт остальной код бота."""

    inbound: dict
    server: dict
    clients: list[dict] = field(default_factory=list)

    @property
    def peers(self) -> list[dict]:
        """Как WgConfig.peers у Amnezia: у каждого пира есть PublicKey и AllowedIPs."""
        return [
            {"PublicKey": c.get("publicKey", ""), "AllowedIPs": ", ".join(c.get("allowedIPs") or [])}
            for c in self.clients
        ]

    def find(self, public_key: str) -> dict | None:
        return next((c for c in self.clients if c.get("publicKey") == public_key), None)

    @property
    def network(self) -> ipaddress.IPv4Network:
        try:
            return ipaddress.ip_network(
                f"{self.server.get('subnetIp') or '10.8.1.0'}/{int(self.server.get('subnetCidr') or 24)}",
                strict=False,
            )
        except ValueError as e:
            raise AwgError(f"3x-ui: неверная подсеть AmneziaWG: {e}")

    def used_ips(self) -> set:
        net = self.network
        used = {next(net.hosts())}  # первый адрес подсети — у самого сервера
        for c in self.clients:
            for allowed in c.get("allowedIPs") or []:
                try:
                    sub = ipaddress.ip_network(allowed, strict=False)
                except ValueError:
                    continue
                if isinstance(sub, ipaddress.IPv4Network) and sub.subnet_of(net):
                    used.update(sub)
        return used

    def ip_free(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return addr in self.network and addr not in self.used_ips()

    def allocate_ip(self, reserved: set[str] | frozenset[str] = frozenset()) -> str:
        used = self.used_ips()
        for r in reserved:
            try:
                used.add(ipaddress.ip_address(r))
            except ValueError:
                pass
        for host in self.network.hosts():
            if host not in used:
                return str(host)
        raise AwgError(f"3x-ui: в подсети {self.network} закончились свободные адреса")


# ======================================================================
# Подключение к панели
# ======================================================================


def parse_show(output: str) -> dict:
    """Вывод `x-ui setting -show -getListen` → port, base_path, ssl, listen."""
    result: dict = {"ssl": False, "listen": ""}
    for line in output.splitlines():
        line = line.strip()
        if m := re.fullmatch(r"port:\s*(\d+)", line):
            result["port"] = int(m.group(1))
        elif m := re.fullmatch(r"webBasePath:\s*(\S*)", line):
            result["base_path"] = m.group(1)
        elif m := re.fullmatch(r"listenIP:\s*(\S*)", line):
            result["listen"] = m.group(1)
        elif line == "Panel is secure with SSL":
            result["ssl"] = True
    if "port" not in result:
        raise AwgError(f"3x-ui: не удалось узнать порт панели: {output.strip()[-300:]}")
    return result


def panel_url(show: dict) -> str:
    listen = show.get("listen") or ""
    host = "127.0.0.1" if listen in ("", "0.0.0.0", "::", "[::]") else listen
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    base = "/" + (show.get("base_path") or "").strip("/")
    base = base.rstrip("/") + "/"
    return f"{'https' if show.get('ssl') else 'http'}://{host}:{show['port']}{base}"


def new_sub_id() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


class XuiPanel:
    """Общее для VLESS и AWG: поиск панели на сервере, токен, вызовы API, выбор подключения."""

    backend = "3x-ui"
    container: str | None = None  # для совместимости с клиентами Amnezia
    protocol = ""  # протокол подключения в панели
    override_key = ""  # ключ в /etc/awg3tg/3x-ui.json
    kind = ""  # для сообщений

    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self.cli: list[str] = []
        self.url = ""
        self.inbound_id: int | None = None
        self._inbound_hint: int | str | None = None
        self._lock = asyncio.Lock()

    # ---------- низкоуровневое ----------

    async def _sh(self, script: str, stdin: bytes | None = None, check: bool = True) -> str:
        try:
            code, out, err = await self.runner.run(["bash", "-c", script], stdin)
        except RunnerError as e:
            raise AwgError(str(e)) from e
        if check and code != 0:
            raise AwgError(f"3x-ui: {(err or out).strip()[-300:] or f'код {code}'}")
        return out

    async def _mint_token(self) -> None:
        """Выпускает (или перевыпускает) токен бота и кладёт его в конфиг curl на сервере."""
        cli = shlex.join(self.cli)
        script = (
            "set -e; umask 077; "
            f"mkdir -p {STATE_DIR}; "
            f"out=$({cli} setting -getApiToken -tokenName {TOKEN_NAME} -tokenScope admin 2>&1) "
            '|| { printf "%s\\n" "$out" | grep -v "^apiToken:" >&2; exit 1; }; '
            "t=$(printf '%s\\n' \"$out\" | sed -n 's/^apiToken: *//p' | tail -n1); "
            '[ -n "$t" ] || { echo "панель не выдала токен" >&2; exit 1; }; '
            f"printf 'header = \"Authorization: Bearer %s\"\\n' \"$t\" > {CURL_CONFIG}.tmp; "
            f"mv {CURL_CONFIG}.tmp {CURL_CONFIG}"
        )
        await self._sh(script)
        log.info("3x-ui [%s]: выпущен API-токен бота", self.runner.description)

    async def _request(self, method: str, path: str, body=None) -> tuple[int, str]:
        args = ["curl", "-sS", "-k", "--max-time", "25", "-K", CURL_CONFIG, "-X", method]
        args += ["-H", "Accept: application/json", "-w", "\\n%{http_code}"]
        stdin = None
        if body is not None:
            args += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
            stdin = json.dumps(body, ensure_ascii=False).encode()
        args.append(self.url + "panel/api/" + path)
        out = await self._sh(shlex.join(args), stdin=stdin)
        text, _, code = out.rstrip("\n").rpartition("\n")
        return int(code) if code.isdigit() else 0, text

    async def _api(self, method: str, path: str, body=None):
        code, text = await self._request(method, path, body)
        if code == 401:  # токен отозвали в панели — выпускаем новый и повторяем один раз
            await self._mint_token()
            code, text = await self._request(method, path, body)
        if code == 404:
            raise AwgError("3x-ui: API не найдено — проверьте адрес панели (порт и путь)")
        if code != 200:
            raise AwgError(f"3x-ui: HTTP {code or 'ошибка'} на {path}: {text.strip()[:200]}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise AwgError(f"3x-ui: непонятный ответ на {path}: {text.strip()[:200]}")
        if not data.get("success"):
            raise AwgError(f"3x-ui: {data.get('msg') or 'ошибка'}")
        return data.get("obj")

    # ---------- подключение к панели ----------

    async def detect(self) -> None:
        found = (
            await self._sh(
                f"if [ -x {XUI_BINARY} ]; then echo native; "
                "elif command -v docker >/dev/null 2>&1 && "
                f"docker ps --format '{{{{.Names}}}}' | grep -qx {XUI_DOCKER_CONTAINER}; then echo docker; "
                "else echo none; fi",
                check=False,
            )
        ).strip()
        if found == "native":
            self.cli = [XUI_BINARY]
        elif found == "docker":
            self.cli = ["docker", "exec", XUI_DOCKER_CONTAINER, XUI_DOCKER_BINARY]
        else:
            raise AwgError("3x-ui не найден")
        overrides = await self._overrides()
        self._inbound_hint = overrides.get(self.override_key)
        if overrides.get("url"):
            self.url = str(overrides["url"]).rstrip("/") + "/"
        else:
            self.url = panel_url(parse_show(await self._sh(shlex.join([*self.cli, "setting", "-show", "-getListen"]))))
        if (await self._sh(f"test -s {CURL_CONFIG} && echo yes", check=False)).strip() != "yes":
            await self._mint_token()
        self.inbound_id = await self._pick_inbound()
        log.info(
            "3x-ui [%s]: панель %s, %s-подключение id=%s", self.runner.description, self.url, self.kind, self.inbound_id
        )

    async def _overrides(self) -> dict:
        raw = await self._sh(f"cat {OVERRIDES} 2>/dev/null", check=False)
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise AwgError(f"3x-ui: {OVERRIDES} повреждён: {e}")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _sort_key(inbound: dict):
        return int(inbound["id"])

    async def _pick_inbound(self) -> int:
        inbounds = [
            i for i in (await self._api("GET", "inbounds/list") or [])
            if i.get("protocol") == self.protocol and not i.get("nodeId")
        ]
        hint = self._inbound_hint
        if hint not in (None, ""):
            for i in inbounds:
                if str(i.get("id")) == str(hint) or i.get("remark") == hint:
                    return int(i["id"])
            raise AwgError(f"3x-ui: подключение «{hint}» из {OVERRIDES} не найдено")
        enabled = [i for i in inbounds if i.get("enable")]
        if not enabled:
            raise AwgError(f"3x-ui: нет включённого {self.kind}-подключения — создайте его в панели (Inbounds)")
        enabled.sort(key=self._sort_key)
        return int(enabled[0]["id"])

    async def _load_inbound(self) -> dict:
        assert self.inbound_id is not None, "3x-ui not detected"
        try:
            raw = await self._api("GET", f"inbounds/get/{self.inbound_id}")
        except AwgError:
            # подключение могли удалить или пересоздать в панели — выбираем заново
            self.inbound_id = await self._pick_inbound()
            raw = await self._api("GET", f"inbounds/get/{self.inbound_id}")
        if not isinstance(raw, dict) or raw.get("protocol") != self.protocol:
            raise AwgError(f"3x-ui: подключение бота больше не {self.kind}")
        return raw

    async def _add_client(self, client: dict) -> None:
        """Добавляет клиента в подключение бота; если такой email уже есть в панели — подключает его."""
        try:
            await self._api("POST", "clients/add", {"client": client, "inboundIds": [self.inbound_id]})
        except AwgError as add_error:
            email = _quote(client["email"])
            try:
                await self._api("GET", f"clients/get/{email}")
            except AwgError:
                raise add_error
            await self._api("POST", f"clients/{email}/attach", {"inboundIds": [self.inbound_id]})

    async def _delete_client(self, email: str) -> None:
        await self._api("POST", f"clients/del/{_quote(email)}?keepTraffic=0")

    @staticmethod
    def _base_client(email: str, comment: str) -> dict:
        return {
            "email": email,
            "enable": True,
            "limitIp": 0,
            "totalGB": 0,
            "expiryTime": 0,  # сроки ведёт бот: по окончании подписки он сам снимает ключ
            "tgId": 0,
            "subId": new_sub_id(),
            "comment": comment[:200],
            "reset": 0,
        }

    async def close(self) -> None:
        await self.runner.close()


class XuiServer(XuiPanel):
    """VLESS-подключение панели 3x-ui (интерфейс XrayServer)."""

    protocol = "vless"
    override_key = "inbound"
    kind = "VLESS"

    @staticmethod
    def _sort_key(inbound: dict):
        return (int(inbound.get("port") or 0) != 443, int(inbound["id"]))

    async def load_config(self) -> dict:
        return normalize_inbound(await self._load_inbound())

    async def info(self, cfg: dict | None = None) -> XrayInfo:
        return parse_xui_info(cfg or await self.load_config())

    @staticmethod
    def client_ids(cfg: dict) -> list[str]:
        return [c.get("id", "") for c in clients_of(cfg)]

    def _vless_client(self, client_name: str, client_id: str, flow: str) -> dict:
        # email = UUID: уникален в панели и служит ключом статистики
        return {**self._base_client(client_id, client_name), "id": client_id, "flow": flow}

    async def add_client(self, client_name: str, client_id: str | None = None) -> tuple[str, XrayInfo]:
        from .xray import new_client_id

        async with self._lock:
            cfg = await self.load_config()
            info = parse_xui_info(cfg)
            client_id = client_id or new_client_id()
            if client_id not in self.client_ids(cfg):
                await self._add_client(self._vless_client(client_name, client_id, info.flow))
            return client_id, info

    async def add_clients(self, clients: list[tuple[str, str]]) -> int:
        async with self._lock:
            cfg = await self.load_config()
            info = parse_xui_info(cfg)
            have = set(self.client_ids(cfg))
            added = 0
            for name, client_id in clients:
                if client_id in have:
                    continue
                await self._add_client(self._vless_client(name, client_id, info.flow))
                have.add(client_id)
                added += 1
            return added

    async def remove_clients(self, client_ids: set[str]) -> int:
        async with self._lock:
            cfg = await self.load_config()
            emails = [c.get("email") or "" for c in clients_of(cfg) if c.get("id") in client_ids]
            removed = 0
            for email in emails:
                if not email:
                    continue
                await self._delete_client(email)
                removed += 1
            return removed

    async def remove_client(self, client_id: str) -> bool:
        return await self.remove_clients({client_id}) > 0

    async def stats(self) -> dict[str, XrayStats]:
        """Трафик клиентов из панели (счётчики панели не сбрасываются при перезапуске Xray)."""
        cfg = await self.load_config()
        by_email = {c.get("email"): c.get("id") for c in clients_of(cfg) if c.get("email")}
        result: dict[str, XrayStats] = {}
        for st in cfg["inbound"].get("clientStats") or []:
            client_id = by_email.get(st.get("email")) or st.get("uuid")
            if client_id:
                result[client_id] = XrayStats(int(st.get("up") or 0), int(st.get("down") or 0))
        return result


class XuiAwgServer(XuiPanel):
    """AmneziaWG-подключение панели 3x-ui (интерфейс AwgServer).

    Ключи клиентов генерирует бот (как и для Amnezia), адрес — первый свободный в подсети
    подключения. PresharedKey у всех клиентов бота один — он лежит на сервере в
    /etc/awg3tg/3x-ui-awg.psk (как общий PSK у Amnezia)."""

    protocol = "amneziawg"
    override_key = "awg_inbound"
    kind = "AmneziaWG"

    def __init__(self, runner: Runner) -> None:
        super().__init__(runner)
        self._psk: str | None = None

    async def load_config(self) -> XuiAwgConfig:
        raw = await self._load_inbound()
        settings = _json_field(raw.get("settings"))
        server = settings.get("server")
        if not isinstance(server, dict) or not server.get("publicKey"):
            raise AwgError("3x-ui: у AmneziaWG-подключения нет ключей сервера")
        clients = [c for c in settings.get("clients") or [] if isinstance(c, dict)]
        return XuiAwgConfig(raw, server, clients)

    async def _preshared_key(self, cfg: XuiAwgConfig) -> str:
        if self._psk:
            return self._psk
        psk = (await self._sh(f"cat {AWG_PSK_FILE} 2>/dev/null", check=False)).strip()
        if not psk:
            # Файл пропал, а клиенты бота остались — берём их PSK, иначе выданные ключи сломаются
            psk = next(
                (c["preSharedKey"] for c in cfg.clients if AWG_EMAIL_RE.match(c.get("email", "")) and c.get("preSharedKey")),
                "",
            ) or base64.b64encode(secrets.token_bytes(32)).decode()
            await self._sh(
                f"umask 077; mkdir -p {STATE_DIR}; cat > {AWG_PSK_FILE}.tmp && mv {AWG_PSK_FILE}.tmp {AWG_PSK_FILE}",
                stdin=(psk + "\n").encode(),
            )
        self._psk = psk
        return psk

    async def server_info(self, cfg: XuiAwgConfig | None = None) -> ServerInfo:
        cfg = cfg or await self.load_config()
        net = cfg.network
        return ServerInfo(
            container=AMNEZIA_AWG_CONTAINER,
            port=int(cfg.inbound.get("port") or 0),
            server_public_key=cfg.server["publicKey"],
            preshared_key=await self._preshared_key(cfg),
            awg_params=awg_params_from_server(cfg.server),
            subnet_address=str(net.network_address),
            subnet_cidr=str(net.prefixlen),
            mtu=str(awg_effective_mtu(cfg.server)),
        )

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
            existing = cfg.find(public_key)
            if existing is not None:
                peer_ip = ((existing.get("allowedIPs") or [""])[0]).split("/")[0]
                return NewPeer(private_key, public_key, peer_ip or ip or ""), info
            if not ip or not cfg.ip_free(ip):
                ip = cfg.allocate_ip(reserved)
            client = {
                **self._base_client(awg_email(public_key), client_name),
                "privateKey": private_key,
                "publicKey": public_key,
                "allowedIPs": [f"{ip}/32"],
                "preSharedKey": info.preshared_key,
            }
            await self._add_client(client)
            return NewPeer(private_key, public_key, ip), info

    async def remove_peer(self, public_key: str) -> bool:
        async with self._lock:
            cfg = await self.load_config()
            client = cfg.find(public_key)
            if client is None or not client.get("email"):
                return False
            await self._delete_client(client["email"])
            return True

    async def stats(self) -> dict[str, PeerStats]:
        """Трафик и «последняя активность» из статистики панели (lastOnline, мс)."""
        cfg = await self.load_config()
        by_email = {c.get("email"): c.get("publicKey") for c in cfg.clients if c.get("email")}
        result: dict[str, PeerStats] = {}
        for st in cfg.inbound.get("clientStats") or []:
            public_key = by_email.get(st.get("email"))
            if public_key:
                result[public_key] = PeerStats(
                    int(st.get("lastOnline") or 0) // 1000, int(st.get("up") or 0), int(st.get("down") or 0)
                )
        return result


# ======================================================================
# Автовыбор: Amnezia или 3x-ui
# ======================================================================


class _Auto:
    """Протокол на сервере: контейнер Amnezia или панель 3x-ui — что найдётся в detect().
    Если есть оба, используется Amnezia (как в прежних версиях бота)."""

    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self.impl = None

    def _candidates(self) -> list:
        raise NotImplementedError

    async def detect(self) -> None:
        errors = []
        for impl in self._candidates():
            try:
                await impl.detect()
            except AwgError as e:
                errors.append(str(e))
                continue
            self.impl = impl
            return
        raise AwgError("; ".join(errors))

    @property
    def backend(self) -> str:
        return getattr(self.impl, "backend", "Amnezia") if self.impl is not None else ""

    def __getattr__(self, name: str):
        impl = self.__dict__.get("impl")
        if impl is None:
            raise AwgError("протокол на сервере ещё не найден")
        return getattr(impl, name)

    async def close(self) -> None:
        await self.runner.close()


class AutoVless(_Auto):
    """VLESS: Xray от приложения Amnezia (amnezia-xray) или панель 3x-ui."""

    def _candidates(self) -> list:
        from .xray import XrayServer

        return [XrayServer(runner=self.runner), XuiServer(self.runner)]


class AutoAwg(_Auto):
    """AmneziaWG: контейнер amnezia-awg2/amnezia-awg или панель 3x-ui."""

    def __init__(self, runner: Runner, container: str | None = None) -> None:
        super().__init__(runner)
        self._container = container

    def _candidates(self) -> list:
        from .server import AwgServer

        return [AwgServer(container=self._container, runner=self.runner), XuiAwgServer(self.runner)]
