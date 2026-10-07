"""Настоящее SSH-подключение: локальный SSH-сервер на asyncssh выполняет команды
бота через поддельный docker (tests/fakebin), как удалённый VPN-сервер."""

import asyncio
import os

import asyncssh
import pytest

from bot.awg.runner import SshKeyStore, SshRunner
from bot.awg.server import AwgError, AwgServer
from bot.config import Settings
from bot.db import Database
from bot.service import VpnService

from .conftest import FAKEBIN


async def _handle(process: asyncssh.SSHServerProcess) -> None:
    env = dict(os.environ, PATH=f"{FAKEBIN}:{os.environ['PATH']}")
    proc = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        process.command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    data = await process.stdin.read()
    out, err = await proc.communicate(data.encode() if data else None)
    process.stdout.write(out.decode())
    process.stderr.write(err.decode())
    process.exit(proc.returncode or 0)


@pytest.fixture
async def ssh_server(tmp_path, fake_container):
    keys = SshKeyStore(str(tmp_path / "botssh"))
    pub = keys.ensure_key()
    auth = tmp_path / "authorized_keys"
    auth.write_text(pub + "\n")
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_host_keys=[host_key],
        authorized_client_keys=str(auth),
        process_factory=_handle,
        encoding="utf-8",
    )
    port = server.sockets[0].getsockname()[1]
    yield keys, port, host_key
    server.close()
    await server.wait_closed()


async def test_ssh_runner_end_to_end(ssh_server, tmp_path, fake_container):
    keys, port, _ = ssh_server
    awg = AwgServer(runner=SshRunner(f"root@127.0.0.1:{port}", keys))
    await awg.detect()
    assert awg.container == "amnezia-awg2"
    info = await awg.server_info()
    assert info.port == 55424 and "HeaderProtectionKey" in info.awg_params

    peer, _ = await awg.add_peer("Телефон | @u")  # запись файла идёт через stdin по SSH
    conf = (fake_container / "opt/amnezia/awg/awg0.conf").read_text()
    assert peer.public_key in conf
    assert (await awg.stats())[peer.public_key].rx == 1024
    assert await awg.remove_peer(peer.public_key)
    assert keys.known_key("127.0.0.1", port) is not None  # ключ сервера запомнен
    await awg.close()


async def test_ssh_host_key_change_is_rejected(ssh_server):
    keys, port, _ = ssh_server
    keys.remember("127.0.0.1", port, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOtherServerKey")
    awg = AwgServer(runner=SshRunner(f"root@127.0.0.1:{port}", keys))
    with pytest.raises(AwgError, match="изменился"):
        await awg.detect()
    keys.forget("127.0.0.1", port)
    await awg.detect()  # после сброса — снова подключается
    await awg.close()


async def test_ssh_wrong_key_is_denied(ssh_server, tmp_path):
    _, port, _ = ssh_server
    stranger = SshKeyStore(str(tmp_path / "stranger"))
    stranger.ensure_key()
    awg = AwgServer(runner=SshRunner(f"root@127.0.0.1:{port}", stranger))
    with pytest.raises(AwgError, match="authorized_keys"):
        await awg.detect()


async def test_service_adds_remote_server(ssh_server, tmp_path):
    keys, port, _ = ssh_server
    settings = Settings(bot_token="x", admin_ids={1}, db_path=str(tmp_path / "data" / "bot.db"))
    # бот берёт SSH-ключ из data/ssh — подкладываем тот, что разрешён на сервере
    os.makedirs(tmp_path / "data", exist_ok=True)
    os.rename(keys.dir, tmp_path / "data" / "ssh")
    db = Database(settings.db_path)
    await db.connect()
    service = VpnService(settings, db)
    row = await service.add_server("Нидерланды", "🇳🇱", "5.6.7.8", f"root@127.0.0.1:{port}")
    assert row.status_ok and row.conn == f"root@127.0.0.1:{port}"
    user, _ = await db.touch_user(10, "u", "U")
    await service.extend(10, 30)
    rk = await service.create_device(user, "Телефон", row.id)
    assert rk.location == "🇳🇱 Нидерланды" and "Endpoint = 5.6.7.8:55424" in rk.conf
    await service.delete_device(rk.key)
    await service.close()
    await db.close()
