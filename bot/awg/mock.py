"""Демо-режим (AWG_MOCK=1): эмуляция контейнера amnezia-awg2 в локальной папке.

Нужен, чтобы попробовать бота на своём компьютере без VPN-сервера и Docker:
выдача ключей, подписки, оплаты, админка и статистика работают как настоящие,
но ключи никуда не подключаются (Endpoint указывает на SERVER_HOST).
Трафик и подключения имитируются случайными числами.
"""

from __future__ import annotations

import logging
import os
import random
import time

from .keys import generate_keypair
from .server import AWG_DIR, KNOWN_CONTAINERS, AwgError, AwgServer, PeerStats

log = logging.getLogger(__name__)


def _sample_server_conf() -> str:
    priv, _ = generate_keypair()
    _, hpk = generate_keypair()
    r = random.Random()
    h = sorted(r.sample(range(100_000_000, 2_000_000_000), 8))
    return f"""[Interface]
PrivateKey = {priv}
Address = 10.8.1.0/24
ListenPort = 55424
Jc = 4
Jmin = 12
Jmax = 50
S1 = {r.randint(20, 80)}
S2 = {r.randint(90, 150)}
S3 = {r.randint(12, 40)}
S4 = {r.randint(12, 24)}
H1 = {h[0]}-{h[1]}
H2 = {h[2]}-{h[3]}
H3 = {h[4]}-{h[5]}
H4 = {h[6]}-{h[7]}
HeaderProtectionKey = {hpk}
ContentPaddingAddition = 10-100
RekeyAfterTime = 100-120
RekeyTimeout = 3-7
RejectAfterTime = 150-180
KeepaliveTimeout = 5-15
MaxHandshakeAttempts = 15-20
RandomTrailers = on
DisableCookies = on
# I1 = <r 2><b 0x858000010001000000000669636c6f756403636f6d0000010001c00c000100010000105a00044d583737>
"""


class MockAwgServer(AwgServer):
    def __init__(self, root: str) -> None:
        super().__init__(container="amnezia-awg2")
        self.root = os.path.abspath(root)
        self._counters: dict[str, PeerStats] = {}

    def _local(self, path: str) -> str:
        return os.path.join(self.root, path.lstrip("/"))

    async def detect(self) -> None:
        conf_path, iface, binary = KNOWN_CONTAINERS["amnezia-awg2"]
        self._config_path, self._interface, self._binary = conf_path, iface, binary
        conf = self._local(conf_path)
        if not os.path.exists(conf):
            os.makedirs(os.path.dirname(conf), exist_ok=True)
            _, psk = generate_keypair()
            with open(conf, "w") as f:
                f.write(_sample_server_conf())
            with open(self._local(f"{AWG_DIR}/wireguard_psk.key"), "w") as f:
                f.write(psk + "\n")
        log.warning("ДЕМО-РЕЖИМ: VPN-сервер эмулируется в %s, ключи не будут подключаться", self.root)

    async def read_file(self, path: str) -> str:
        try:
            with open(self._local(path), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            raise AwgError(f"{path}: нет такого файла")

    async def write_file(self, path: str, content: str) -> None:
        local = self._local(path)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local + ".tmp", "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(local + ".tmp", local)

    async def apply(self) -> None:
        pass

    async def stats(self) -> dict[str, PeerStats]:
        """Имитация: примерно половина устройств «онлайн» и качает трафик."""
        cfg = await self.load_config()
        now = int(time.time())
        result = {}
        for peer in cfg.peers:
            pub = peer.get("PublicKey")
            if not pub:
                continue
            st = self._counters.setdefault(pub, PeerStats(0, 0, 0))
            if random.random() < 0.5:
                st.latest_handshake = now - random.randint(0, 120)
                st.rx += random.randint(1, 30) * 1024 * 1024
                st.tx += random.randint(10, 300) * 1024 * 1024
            result[pub] = PeerStats(st.latest_handshake, st.rx, st.tx)
        return result
