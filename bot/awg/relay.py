"""Входной сервер для «белых списков» (relay).

Клиент подключается к серверу в РФ (его IP открыт в режиме белых списков), а тот просто
перенаправляет пакеты на зарубежный VPN-сервер (iptables DNAT). Трафик остаётся
зашифрованным — на входном сервере VPN не нужен и ничего не расшифровывается.

Бот сам настраивает входной сервер по SSH: пишет скрипт /etc/awg3tg-relay.sh
(свои цепочки iptables AWG3TG_*, чужие правила не трогает) и systemd-службу, которая
применяет его после перезагрузки.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass

from .runner import Runner, RunnerError
from .server import AwgError

SCRIPT_PATH = "/etc/awg3tg-relay.sh"
UNIT_PATH = "/etc/systemd/system/awg3tg-relay.service"
SYSCTL_PATH = "/etc/sysctl.d/99-awg3tg-relay.conf"
CHAINS = (("nat", "PREROUTING", "AWG3TG_PRE"), ("nat", "POSTROUTING", "AWG3TG_POST"), ("filter", "FORWARD", "AWG3TG_FWD"))

# Свободные порты для перенаправлений берутся отсюда, если «родной» порт занят
PORT_RANGE_START = 20000
PORT_RANGE_END = 60000

UNIT = """[Unit]
Description=awg3tg relay: port forwarding to VPN servers
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/sh /etc/awg3tg-relay.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""


@dataclass(frozen=True)
class Forward:
    transport: str  # udp | tcp
    listen: int  # порт на входном сервере
    target_ip: str
    target_port: int


def build_script(forwards: list[Forward]) -> str:
    lines = [
        "#!/bin/sh",
        "# Создано ботом awg3tg: перенаправление на VPN-серверы (белые списки). Не редактируйте вручную —",
        "# бот перезапишет файл. Применяется службой awg3tg-relay после перезагрузки.",
        "set -e",
        "sysctl -qw net.ipv4.ip_forward=1",
    ]
    for table, parent, chain in CHAINS:
        lines += [
            f"iptables -t {table} -N {chain} 2>/dev/null || true",
            f"iptables -t {table} -F {chain}",
            f"iptables -t {table} -C {parent} -j {chain} 2>/dev/null || iptables -t {table} -I {parent} -j {chain}",
        ]
    for f in sorted(forwards, key=lambda f: (f.transport, f.listen)):
        dst = f"{f.target_ip}:{f.target_port}"
        lines += [
            f"iptables -t nat -A AWG3TG_PRE -p {f.transport} --dport {f.listen} -j DNAT --to-destination {dst}",
            f"iptables -t nat -A AWG3TG_POST -p {f.transport} -d {f.target_ip} --dport {f.target_port} -j MASQUERADE",
            f"iptables -A AWG3TG_FWD -p {f.transport} -d {f.target_ip} --dport {f.target_port} -j ACCEPT",
            f"iptables -A AWG3TG_FWD -p {f.transport} -s {f.target_ip} --sport {f.target_port} -j ACCEPT",
        ]
    return "\n".join(lines) + "\n"


def allocate_ports(
    wanted: list[tuple[tuple, str, int]], current: dict[tuple, int], reserved: set[tuple[str, int]]
) -> dict[tuple, int]:
    """Порты на входном сервере: wanted = [(ключ, transport, родной порт)].
    Уже выданный порт сохраняется (чтобы не менять ключи клиентов), иначе берётся родной порт
    сервера, а если он занят — первый свободный из диапазона."""
    used = set(reserved)
    result: dict[tuple, int] = {}
    pending = []
    for key, transport, native in wanted:
        port = current.get(key)
        if port and (transport, port) not in used:
            result[key] = port
            used.add((transport, port))
        else:
            pending.append((key, transport, native))
    for key, transport, native in pending:
        port = native
        if (transport, port) in used:
            port = next(p for p in range(PORT_RANGE_START, PORT_RANGE_END) if (transport, p) not in used)
        result[key] = port
        used.add((transport, port))
    return result


class RelayClient:
    """Настройка входного сервера по SSH (root или пользователь с sudo без пароля)."""

    def __init__(self, runner: Runner, root: str = "") -> None:
        self.runner = runner
        self.root = root  # префикс путей — для тестов

    def _p(self, path: str) -> str:
        return shlex.quote(self.root + path)

    async def _sh(self, script: str, stdin: bytes | None = None, check: bool = True) -> str:
        try:
            code, out, err = await self.runner.run(["sh", "-c", script], stdin)
        except RunnerError as e:
            raise AwgError(str(e)) from e
        if check and code != 0:
            raise AwgError((err or out).strip()[-300:] or f"код {code}")
        return out

    async def check(self) -> None:
        """Сервер доступен и на нём есть iptables."""
        out = await self._sh("command -v iptables || echo NO_IPTABLES", check=False)
        if "NO_IPTABLES" in out:
            raise AwgError("на входном сервере нет iptables — установите: apt install -y iptables")

    async def current_script(self) -> str:
        return await self._sh(f"cat {self._p(SCRIPT_PATH)} 2>/dev/null || true", check=False)

    async def active_rules(self) -> int:
        out = await self._sh("iptables -t nat -S AWG3TG_PRE 2>/dev/null || true", check=False)
        return sum(1 for line in out.splitlines() if "DNAT" in line)

    async def apply(self, forwards: list[Forward]) -> None:
        script, tmp = self._p(SCRIPT_PATH), self._p(SCRIPT_PATH + ".tmp")
        await self._sh(
            f"cat > {tmp} && mv {tmp} {script} && chmod 755 {script} "
            f"&& printf 'net.ipv4.ip_forward = 1\\n' > {self._p(SYSCTL_PATH)} "
            f"&& if command -v systemctl >/dev/null 2>&1 && [ -d {self._p('/etc/systemd/system')} ]; then "
            f"printf %s {shlex.quote(UNIT)} > {self._p(UNIT_PATH)}; systemctl daemon-reload >/dev/null 2>&1 || true; "
            "systemctl enable awg3tg-relay.service >/dev/null 2>&1 || true; fi "
            f"&& sh {script}",
            stdin=build_script(forwards).encode(),
        )

    async def sync(self, forwards: list[Forward]) -> bool:
        """Применяет правила, если на сервере они другие или пропали (например, после перезагрузки
        без службы). Возвращает True, если что-то менялось."""
        await self.check()
        if await self.current_script() == build_script(forwards) and await self.active_rules() == len(forwards):
            return False
        await self.apply(forwards)
        return True

    async def remove(self) -> None:
        """Убирает правила бота с сервера (при удалении входа из бота)."""
        cmds = [f"iptables -t {t} -D {p} -j {c} 2>/dev/null; iptables -t {t} -F {c} 2>/dev/null; "
                f"iptables -t {t} -X {c} 2>/dev/null" for t, p, c in CHAINS]
        await self._sh(
            "; ".join(cmds)
            + "; if command -v systemctl >/dev/null 2>&1; then systemctl disable awg3tg-relay.service "
            f">/dev/null 2>&1; fi; rm -f {self._p(SCRIPT_PATH)} {self._p(UNIT_PATH)} {self._p(SYSCTL_PATH)}; true",
            check=False,
        )

    async def close(self) -> None:
        await self.runner.close()


class MockRelay(RelayClient):
    """Демо-режим: «входной сервер» в локальной папке (файл DOWN — сервер недоступен)."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self.applied = 0

    def _path(self, path: str) -> str:
        return os.path.join(self.root, path.lstrip("/"))

    async def check(self) -> None:
        if os.path.exists(os.path.join(self.root, "DOWN")):
            raise AwgError("демо-вход выключен (файл DOWN)")
        os.makedirs(self.root, exist_ok=True)

    async def current_script(self) -> str:
        try:
            with open(self._path(SCRIPT_PATH), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    async def active_rules(self) -> int:
        return (await self.current_script()).count("-j DNAT")

    async def apply(self, forwards: list[Forward]) -> None:
        os.makedirs(os.path.dirname(self._path(SCRIPT_PATH)), exist_ok=True)
        with open(self._path(SCRIPT_PATH), "w", encoding="utf-8") as f:
            f.write(build_script(forwards))
        self.applied += 1

    async def remove(self) -> None:
        try:
            os.remove(self._path(SCRIPT_PATH))
        except FileNotFoundError:
            pass

    async def close(self) -> None:
        pass
