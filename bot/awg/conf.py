"""Парсер и сериализатор конфигов WireGuard/AmneziaWG.

Конфиг хранится как список секций, чтобы при записи обратно сохранить
порядок ключей и комментарии сервера (в том числе закомментированные
строки `# I1 = ...`, которые клиент Amnezia пишет в awg0.conf).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Параметры обфускации [Interface], которые должны совпадать у клиента и сервера
# (или которые клиент Amnezia копирует в клиентский конфиг). Порядок — как в
# template.conf официального клиента.
AWG_PARAMS: tuple[str, ...] = (
    "Jc",
    "Jmin",
    "Jmax",
    "S1",
    "S2",
    "S3",
    "S4",
    "H1",
    "H2",
    "H3",
    "H4",
    "I1",
    "I2",
    "I3",
    "I4",
    "I5",
    # AmneziaWG 3.x
    "HeaderProtectionKey",
    "ContentPaddingAddition",
    "RekeyAfterTime",
    "RekeyTimeout",
    "RejectAfterTime",
    "KeepaliveTimeout",
    "MaxHandshakeAttempts",
    "RandomTrailers",
    "DisableCookies",
)

AWG3_MARKERS: tuple[str, ...] = (
    "HeaderProtectionKey",
    "ContentPaddingAddition",
    "RekeyAfterTime",
    "RekeyTimeout",
    "RejectAfterTime",
    "KeepaliveTimeout",
    "MaxHandshakeAttempts",
)

_KV_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$")
_COMMENTED_I_RE = re.compile(r"^\s*#\s*(I[1-5])\s*=\s*(.*?)\s*$")


@dataclass
class Section:
    name: str
    # Сырые строки секции (без заголовка), чтобы сохранить форматирование.
    lines: list[str] = field(default_factory=list)

    def get(self, key: str) -> str | None:
        """Значение ключа (регистр ключа не важен, как в wg-quick)."""
        for line in self.lines:
            m = _KV_RE.match(line)
            if m and m.group(1).lower() == key.lower():
                return m.group(2)
        return None

    def get_all(self, key: str) -> list[str]:
        result = []
        for line in self.lines:
            m = _KV_RE.match(line)
            if m and m.group(1).lower() == key.lower():
                result.append(m.group(2))
        return result

    def commented_i_params(self) -> dict[str, str]:
        """`# I1 = <...>` — так Amnezia хранит CPS-пакеты в серверном конфиге."""
        result = {}
        for line in self.lines:
            m = _COMMENTED_I_RE.match(line)
            if m and m.group(2):
                result[m.group(1)] = m.group(2)
        return result


@dataclass
class WgConfig:
    # Строки до первой секции (обычно пусто).
    preamble: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)

    @property
    def interface(self) -> Section:
        for s in self.sections:
            if s.name.lower() == "interface":
                return s
        raise ValueError("В конфиге нет секции [Interface]")

    @property
    def peers(self) -> list[Section]:
        return [s for s in self.sections if s.name.lower() == "peer"]

    def find_peer(self, public_key: str) -> Section | None:
        for p in self.peers:
            if p.get("PublicKey") == public_key:
                return p
        return None

    def add_peer(self, public_key: str, preshared_key: str | None, allowed_ip: str) -> None:
        lines = [f"PublicKey = {public_key}"]
        if preshared_key:
            lines.append(f"PresharedKey = {preshared_key}")
        lines.append(f"AllowedIPs = {allowed_ip}")
        lines.append("")
        self.sections.append(Section("Peer", lines))

    def remove_peer(self, public_key: str) -> bool:
        peer = self.find_peer(public_key)
        if peer is None:
            return False
        self.sections.remove(peer)
        return True

    def awg_params(self) -> dict[str, str]:
        """Параметры обфускации из [Interface] сервера, включая `# I1..I5`."""
        iface = self.interface
        params: dict[str, str] = {}
        commented = iface.commented_i_params()
        for key in AWG_PARAMS:
            value = iface.get(key)
            if value is None and key in commented:
                value = commented[key]
            if value is not None and value != "":
                params[key] = value
        return params

    def dump(self) -> str:
        out: list[str] = list(self.preamble)
        for s in self.sections:
            out.append(f"[{s.name}]")
            out.extend(s.lines)
        text = "\n".join(out)
        if not text.endswith("\n"):
            text += "\n"
        return text


def parse(text: str) -> WgConfig:
    cfg = WgConfig()
    current: Section | None = None
    for raw in text.replace("\r", "").split("\n"):
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = Section(stripped[1:-1].strip())
            cfg.sections.append(current)
            continue
        if current is None:
            cfg.preamble.append(raw)
        else:
            current.lines.append(raw)
    # Убираем хвостовые пустые строки последней секции — dump() добавит перевод строки.
    if cfg.sections:
        lines = cfg.sections[-1].lines
        while lines and not lines[-1].strip():
            lines.pop()
        lines.append("")
    return cfg


def is_awg3(params: dict[str, str]) -> bool:
    """Та же эвристика, что в клиенте Amnezia (awgProtocolConfig.cpp)."""
    if any(params.get(k, "").strip() for k in AWG3_MARKERS):
        return True
    for k in ("RandomTrailers", "DisableCookies"):
        v = params.get(k, "").strip()
        if v and v.lower() != "off":
            return True
    return False


def protocol_version(params: dict[str, str]) -> str:
    if is_awg3(params):
        return "3.1"
    if params.get("S3") or params.get("S4") or any("-" in params.get(h, "") for h in ("H1", "H2", "H3", "H4")):
        return "2"
    if any(params.get(f"I{i}") for i in range(1, 6)):
        return "1.5"
    return ""
