"""Как бот выполняет команды docker на VPN-сервере: локально или по SSH.

* LocalRunner — бот запущен на том же сервере (нужен доступ к docker.sock).
* SshRunner   — удалённый сервер: бот заходит по SSH-ключу и выполняет там
  `docker exec ...`. На удалённом сервере ничего, кроме AmneziaWG от
  приложения Amnezia, ставить не нужно.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex

log = logging.getLogger(__name__)

COMMAND_TIMEOUT = 60


class RunnerError(RuntimeError):
    pass


class Runner:
    description = ""

    async def run(self, args: list[str], stdin: bytes | None = None) -> tuple[int, str, str]:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class LocalRunner(Runner):
    def __init__(self, docker: str = "docker") -> None:
        self.docker = docker
        self.description = "local"

    async def run(self, args: list[str], stdin: bytes | None = None) -> tuple[int, str, str]:
        if args and args[0] == "docker":
            args = [self.docker, *args[1:]]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise RunnerError(f"не найдена программа {args[0]} — установлен ли docker?") from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            raise RunnerError("команда выполнялась слишком долго")
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def parse_ssh_target(target: str) -> tuple[str, str, int]:
    """'root@1.2.3.4:22' / 'admin@host' / '1.2.3.4' -> (user, host, port)."""
    user = "root"
    rest = target.strip()
    if "@" in rest:
        user, rest = rest.split("@", 1)
    port = 22
    if rest.startswith("["):  # [IPv6]:port
        host, _, tail = rest[1:].partition("]")
        if tail.startswith(":") and tail[1:].isdigit():
            port = int(tail[1:])
    elif rest.count(":") == 1:
        host, p = rest.split(":")
        if not p.isdigit():
            raise ValueError("порт должен быть числом")
        port = int(p)
    else:
        host = rest
    if not user or not host or not 0 < port < 65536:
        raise ValueError("неверный адрес SSH")
    return user, host, port


class SshKeyStore:
    """Ключ бота для входа на серверы и запомненные ключи серверов (TOFU)."""

    def __init__(self, directory: str) -> None:
        self.dir = directory
        self.key_path = os.path.join(directory, "id_ed25519")
        self.hosts_path = os.path.join(directory, "known_hosts.json")

    def ensure_key(self) -> str:
        """Создаёт ключ при первом запуске, возвращает публичную часть."""
        import asyncssh

        os.makedirs(self.dir, exist_ok=True)
        if not os.path.exists(self.key_path):
            key = asyncssh.generate_private_key("ssh-ed25519", comment="awg3tg-bot")
            key.write_private_key(self.key_path)
            os.chmod(self.key_path, 0o600)
            log.info("Создан SSH-ключ бота: %s", self.key_path)
        key = asyncssh.read_private_key(self.key_path)
        return key.export_public_key().decode().strip()

    def _hosts(self) -> dict[str, str]:
        try:
            with open(self.hosts_path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def known_key(self, host: str, port: int) -> str | None:
        return self._hosts().get(f"{host}:{port}")

    def remember(self, host: str, port: int, key: str) -> None:
        hosts = self._hosts()
        hosts[f"{host}:{port}"] = key
        os.makedirs(self.dir, exist_ok=True)
        with open(self.hosts_path, "w", encoding="utf-8") as f:
            json.dump(hosts, f, indent=2)

    def forget(self, host: str, port: int) -> None:
        hosts = self._hosts()
        if hosts.pop(f"{host}:{port}", None) is not None:
            with open(self.hosts_path, "w", encoding="utf-8") as f:
                json.dump(hosts, f, indent=2)


class SshRunner(Runner):
    def __init__(self, target: str, keys: SshKeyStore) -> None:
        self.user, self.host, self.port = parse_ssh_target(target)
        self.keys = keys
        self.description = f"{self.user}@{self.host}:{self.port}"
        self._conn = None
        self._lock = asyncio.Lock()

    async def _connect(self):
        import asyncssh

        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    self.host,
                    port=self.port,
                    username=self.user,
                    client_keys=[self.keys.key_path],
                    known_hosts=None,  # проверяем сами ниже (запоминаем ключ при первом входе)
                    keepalive_interval=30,
                ),
                20,
            )
        except asyncssh.PermissionDenied as e:
            raise RunnerError(
                "SSH: доступ запрещён — добавьте ключ бота в ~/.ssh/authorized_keys на сервере"
            ) from e
        except (OSError, asyncssh.Error, asyncio.TimeoutError) as e:
            raise RunnerError(f"SSH: не удалось подключиться к {self.host}:{self.port}: {e or 'таймаут'}") from e

        server_key = conn.get_server_host_key()
        fingerprint = server_key.export_public_key().decode().strip() if server_key else ""
        known = self.keys.known_key(self.host, self.port)
        if known is None:
            self.keys.remember(self.host, self.port, fingerprint)
            log.info("Запомнен SSH-ключ сервера %s:%s", self.host, self.port)
        elif known != fingerprint:
            conn.close()
            raise RunnerError(
                f"SSH-ключ сервера {self.host} изменился! Возможна подмена сервера. "
                "Если вы переустанавливали сервер — нажмите «Сбросить SSH-ключ» в карточке сервера."
            )
        return conn

    async def run(self, args: list[str], stdin: bytes | None = None) -> tuple[int, str, str]:
        if self.user != "root":
            args = ["sudo", "-n", *args]
        command = shlex.join(args)
        for attempt in (1, 2):
            async with self._lock:
                if self._conn is None:
                    self._conn = await self._connect()
                conn = self._conn
            try:
                res = await asyncio.wait_for(self._exec(conn, command, stdin), COMMAND_TIMEOUT)
                out = res.stdout if isinstance(res.stdout, str) else (res.stdout or b"").decode(errors="replace")
                err = res.stderr if isinstance(res.stderr, str) else (res.stderr or b"").decode(errors="replace")
                return res.exit_status or 0, out or "", err or ""
            except asyncio.TimeoutError:
                raise RunnerError("SSH: команда выполнялась слишком долго")
            except Exception as e:  # соединение оборвалось — переподключаемся один раз
                await self.close()
                if attempt == 2:
                    raise RunnerError(f"SSH: {e}") from e
        raise RunnerError("SSH: не удалось выполнить команду")

    @staticmethod
    async def _exec(conn, command: str, stdin: bytes | None):
        # EOF отправляем всегда: иначе команда, читающая stdin, будет ждать вечно.
        process = await conn.create_process(command)
        if stdin:
            process.stdin.write(stdin.decode())
        process.stdin.write_eof()
        return await process.wait(check=False)

    async def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
