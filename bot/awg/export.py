"""Сборка клиентского конфига и ключа `vpn://` в формате приложения AmneziaVPN.

Формат повторяет ExportController::generateConnectionConfig из
amnezia-client: JSON сервера -> qCompress -> base64url без `=`.
qCompress = 4 байта длины (big-endian) + zlib-поток.
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from dataclasses import dataclass

from .conf import AWG_PARAMS, is_awg3, protocol_version

# Текущая версия формата конфига в клиенте Amnezia (serverConfigUtils.h).
CONFIG_FORMAT_VERSION = 1

DEFAULT_KEEPALIVE = "25"
# Клиент Amnezia для AWG 3.x выдаёт keepalive диапазоном (protocolConstants.h).
DEFAULT_KEEPALIVE_AWG3 = "25-35"
DEFAULT_MTU = "1376"


@dataclass
class ClientParams:
    host: str
    port: int
    client_ip: str
    client_private_key: str
    client_public_key: str
    server_public_key: str
    preshared_key: str | None
    awg_params: dict[str, str]
    dns1: str
    dns2: str
    container: str = "amnezia-awg2"
    subnet_address: str = ""
    subnet_cidr: str = ""
    description: str = ""
    mtu: str | None = None
    keepalive: str | None = None
    allowed_ips: tuple[str, ...] = ("0.0.0.0/0", "::/0")

    @property
    def persistent_keepalive(self) -> str:
        if self.keepalive:
            return self.keepalive
        return DEFAULT_KEEPALIVE_AWG3 if is_awg3(self.awg_params) else DEFAULT_KEEPALIVE

    @property
    def endpoint(self) -> str:
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{host}:{self.port}"


def build_native_config(p: ClientParams) -> str:
    """Конфиг для AmneziaWG / AmneziaVPN (.conf, импорт файлом или QR)."""
    dns = ", ".join(d for d in (p.dns1, p.dns2) if d)
    lines = ["[Interface]", f"Address = {p.client_ip}/32"]
    if dns:
        lines.append(f"DNS = {dns}")
    lines.append(f"PrivateKey = {p.client_private_key}")
    if p.mtu:
        lines.append(f"MTU = {p.mtu}")
    for key in AWG_PARAMS:
        value = p.awg_params.get(key)
        if value:
            lines.append(f"{key} = {value}")
    lines += [
        "",
        "[Peer]",
        f"PublicKey = {p.server_public_key}",
    ]
    if p.preshared_key:
        lines.append(f"PresharedKey = {p.preshared_key}")
    lines += [
        f"AllowedIPs = {', '.join(p.allowed_ips)}",
        f"Endpoint = {p.endpoint}",
        f"PersistentKeepalive = {p.persistent_keepalive}",
        "",
    ]
    return "\n".join(lines)


def build_amnezia_json(p: ClientParams) -> dict:
    native = build_native_config(p)

    last_config: dict = {
        "config": native,
        "hostName": p.host,
        "port": p.port,
        "client_ip": p.client_ip,
        "client_priv_key": p.client_private_key,
        "client_pub_key": p.client_public_key,
        "server_pub_key": p.server_public_key,
        "clientId": p.client_public_key,
        "allowed_ips": list(p.allowed_ips),
        "persistent_keep_alive": p.persistent_keepalive,
        "mtu": p.mtu or DEFAULT_MTU,
    }
    if p.preshared_key:
        last_config["psk_key"] = p.preshared_key
    for key in AWG_PARAMS:
        if p.awg_params.get(key):
            last_config[key] = p.awg_params[key]

    awg: dict = {
        "port": str(p.port),
        "transport_proto": "udp",
    }
    version = protocol_version(p.awg_params)
    if version:
        awg["protocol_version"] = version
    if p.subnet_address:
        awg["subnet_address"] = p.subnet_address
    if p.subnet_cidr:
        awg["subnet_cidr"] = p.subnet_cidr
    for key in AWG_PARAMS:
        if key.startswith("I"):
            # Клиент Amnezia всегда пишет I1..I5 в серверную часть, даже пустые.
            awg[key] = p.awg_params.get(key, "")
        elif p.awg_params.get(key):
            awg[key] = p.awg_params[key]
    awg["last_config"] = json.dumps(last_config, ensure_ascii=False, separators=(",", ":"))

    config: dict = {
        "containers": [{"container": p.container, "awg": awg}],
        "defaultContainer": p.container,
        "description": p.description or p.host,
        "hostName": p.host,
        "format_version": CONFIG_FORMAT_VERSION,
    }
    if p.dns1:
        config["dns1"] = p.dns1
    if p.dns2:
        config["dns2"] = p.dns2
    return config


def q_compress(data: bytes, level: int = 8) -> bytes:
    return struct.pack(">I", len(data)) + zlib.compress(data, level)


def q_uncompress(data: bytes) -> bytes:
    (size,) = struct.unpack(">I", data[:4])
    out = zlib.decompress(data[4:])
    if len(out) != size:
        raise ValueError("Неверная длина распакованных данных")
    return out


def encode_vpn_url(config: dict) -> str:
    raw = json.dumps(config, ensure_ascii=False, indent=4).encode()
    return "vpn://" + base64.urlsafe_b64encode(q_compress(raw)).decode().rstrip("=")


def decode_vpn_url(url: str) -> dict:
    data = url.strip().removeprefix("vpn://")
    data += "=" * (-len(data) % 4)
    return json.loads(q_uncompress(base64.urlsafe_b64decode(data)))


def build_vpn_url(p: ClientParams) -> str:
    return encode_vpn_url(build_amnezia_json(p))
