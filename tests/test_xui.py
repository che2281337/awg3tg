"""VLESS через панель 3x-ui: эмуляция сервера (bash + x-ui + curl к API панели)."""

import json
import shlex
import urllib.parse

import pytest

from bot.awg.export import ClientParams, build_native_config
from bot.awg.keys import generate_keypair
from bot.awg.runner import Runner
from bot.awg.server import AwgError
from bot.awg.xray import build_vless_url
from bot.awg.xui import (
    AWG_PSK_FILE,
    CURL_CONFIG,
    AutoAwg,
    AutoVless,
    XuiAwgServer,
    XuiServer,
    awg_email,
    panel_url,
    parse_show,
    parse_xui_info,
)

# Сервер AmneziaWG в том виде, в каком его генерирует 3x-ui (defaultAmneziaWGServer)
AWG_SERVER = {
    "privateKey": "server-priv",
    "publicKey": "SERVERPUB=",
    "subnetIp": "10.8.1.0",
    "subnetCidr": 24,
    "primaryDns": "8.8.8.8",
    "secondaryDns": "8.8.4.4",
    "jc": 4, "jmin": 50, "jmax": 200, "s1": 30, "s2": 40, "s3": 20, "s4": 16,
    "h1": "100000-200000", "h2": "300000-400000", "h3": "500000-600000", "h4": "700000-800000",
    "i1": "<r 64>",
    "headerProtectionKey": "HPK=",
    "contentPaddingAddition": "10-30",
    "rekeyAfterTime": "110-130",
    "rekeyTimeout": "4-6",
    "rejectAfterTime": "170-220",
    "keepaliveTimeout": "9-14",
    "maxHandshakeAttempts": "18-30",
    "randomTrailers": True,
    "disableCookies": False,
}

REALITY_STREAM = {
    "network": "tcp",
    "security": "reality",
    "realitySettings": {
        "target": "127.0.0.1:8443",
        "serverNames": ["site.example.com"],
        "privateKey": "server-private",
        "shortIds": ["ab12", "cd34"],
        "settings": {"publicKey": "PUBKEY", "fingerprint": "firefox", "spiderX": "/"},
    },
}

XHTTP_STREAM = {
    "network": "xhttp",
    "security": "reality",
    "realitySettings": REALITY_STREAM["realitySettings"],
    "xhttpSettings": {
        "path": "/0123abcd/",
        "host": "",
        "mode": "auto",
        "xPaddingBytes": "100-1000",
        "scMaxEachPostBytes": "1000000",  # значение по умолчанию ядра — в ссылку не идёт
        "scMaxBufferedPosts": 30,  # серверное поле
        "noSSEHeader": False,
        "headers": {},
    },
}


class FakePanel(Runner):
    """Сервер с 3x-ui: отвечает на команды бота так, как ответили бы bash, x-ui и API панели."""

    description = "fake"

    def __init__(self, stream=REALITY_STREAM, docker=False, overrides=None, disable_flow=False):
        self.docker = docker
        self.overrides = overrides
        self.token = None  # токен, записанный в CURL_CONFIG
        self.valid_tokens = set()
        self.minted = 0
        self.records: dict[str, dict] = {}  # клиенты панели по email
        self.inbounds = {
            7: {"id": 7, "protocol": "vless", "port": 443, "enable": True, "remark": "XHTTP",
                "stream": stream, "clients": [], "disableFlow": disable_flow},
            8: {"id": 8, "protocol": "vless", "port": 8443, "enable": True, "remark": "other",
                "stream": REALITY_STREAM, "clients": [], "disableFlow": False},
            9: {"id": 9, "protocol": "trojan", "port": 2053, "enable": True, "remark": "trojan",
                "stream": REALITY_STREAM, "clients": [], "disableFlow": False},
            10: {"id": 10, "protocol": "amneziawg", "port": 51820, "enable": True, "remark": "AWG",
                 "server": dict(AWG_SERVER), "clients": [], "disableFlow": False},
        }
        self.traffic: dict[str, tuple[int, int]] = {}
        self.last_online: dict[str, int] = {}
        self.psk: str | None = None
        self.requests: list[tuple[str, str]] = []

    async def run(self, args, stdin=None):
        assert args[:2] == ["bash", "-c"], args
        script = args[2]
        if script.startswith("if [ -x /usr/local/x-ui/x-ui ]"):
            return 0, ("docker" if self.docker else "native") + "\n", ""
        if script.startswith("cat /etc/awg3tg/3x-ui.json"):
            return 0, json.dumps(self.overrides) if self.overrides else "", ""
        if "setting -show -getListen" in script:
            return 0, "current panel settings as follows:\nWarning: Panel is not secure with SSL\n" \
                      "hasDefaultCredential: false\nport: 2053\nwebBasePath: /secret/\nlistenIP: \n", ""
        if script.startswith(f"test -s {CURL_CONFIG}"):
            return 0, "yes\n" if self.token else "", ""
        if "-getApiToken" in script:
            assert "-tokenName awg3tg" in script
            self.valid_tokens.discard(self.token)
            self.minted += 1
            self.token = f"tok{self.minted}"
            self.valid_tokens.add(self.token)
            return 0, "", ""
        if script.startswith(f"cat {AWG_PSK_FILE}"):
            return 0, (self.psk + "\n") if self.psk else "", ""
        if script.startswith("umask 077; mkdir -p /etc/awg3tg; cat > " + AWG_PSK_FILE):
            self.psk = stdin.decode().strip()
            return 0, "", ""
        if script.startswith("curl "):
            return self._curl(shlex.split(script), stdin)
        raise AssertionError(f"неожиданная команда: {script}")

    # ---------- API панели ----------

    def _inbound_json(self, ib: dict) -> dict:
        clients = [dict(self.records[e]) for e in ib["clients"]]
        if ib["protocol"] == "amneziawg":
            settings = {"server": ib["server"], "clients": clients}  # панель отдаёт объект, не строку
            stream = {}
        else:
            settings = json.dumps({"clients": clients, "decryption": "none"})
            stream = json.dumps(ib["stream"])
        return {
            "id": ib["id"], "protocol": ib["protocol"], "port": ib["port"], "enable": ib["enable"],
            "remark": ib["remark"], "disableFlow": ib["disableFlow"],
            "settings": settings,
            "streamSettings": stream,
            "clientStats": [
                {"email": e, "up": self.traffic.get(e, (0, 0))[0], "down": self.traffic.get(e, (0, 0))[1],
                 "lastOnline": self.last_online.get(e, 0)}
                for e in ib["clients"]
            ],
        }

    def _curl(self, argv, stdin):
        assert "-K" in argv and argv[argv.index("-K") + 1] == CURL_CONFIG
        assert not any("Bearer" in a for a in argv), "токен не должен попадать в командную строку"
        method = argv[argv.index("-X") + 1]
        url = urllib.parse.urlsplit(argv[-1])
        assert url.netloc == "127.0.0.1:2053" and url.path.startswith("/secret/panel/api/"), argv[-1]
        path = url.path[len("/secret/panel/api/"):]
        self.requests.append((method, path))
        if self.token not in self.valid_tokens:
            return 0, "\n401", ""
        body = json.loads(stdin) if stdin else None
        code, obj, ok, msg = self._route(method, path, body)
        return 0, json.dumps({"success": ok, "msg": msg, "obj": obj}) + f"\n{code}", ""

    def _route(self, method, path, body):
        if method == "GET" and path == "inbounds/list":
            return 200, [self._inbound_json(i) for i in self.inbounds.values()], True, ""
        if method == "GET" and path.startswith("inbounds/get/"):
            ib = self.inbounds.get(int(path.rsplit("/", 1)[1]))
            return (200, self._inbound_json(ib), True, "") if ib else (200, None, False, "record not found")
        if method == "POST" and path == "clients/add":
            client, ids = body["client"], body["inboundIds"]
            if client["email"] in self.records:
                return 200, None, False, "Duplicate email"
            for i in ids:
                taken = {a for e in self.inbounds[i]["clients"] for a in self.records[e].get("allowedIPs") or []}
                if set(client.get("allowedIPs") or []) & taken:
                    return 200, None, False, "amneziawg: allowedIPs overlaps"
            self.records[client["email"]] = client
            for i in ids:
                self.inbounds[i]["clients"].append(client["email"])
            return 200, None, True, ""
        if method == "GET" and path.startswith("clients/get/"):
            email = urllib.parse.unquote(path.rsplit("/", 1)[1])
            return (200, self.records[email], True, "") if email in self.records else (200, None, False, "nf")
        if method == "POST" and path.endswith("/attach"):
            email = urllib.parse.unquote(path.split("/")[1])
            for i in body["inboundIds"]:
                self.inbounds[i]["clients"].append(email)
            return 200, None, True, ""
        if method == "POST" and path.startswith("clients/del/"):
            email = urllib.parse.unquote(path.split("/")[2].split("?")[0])
            self.records.pop(email, None)
            self.traffic.pop(email, None)
            for ib in self.inbounds.values():
                ib["clients"] = [e for e in ib["clients"] if e != email]
            return 200, None, True, ""
        return 404, None, False, "not found"


async def test_detect_mints_token_and_picks_443():
    panel = FakePanel()
    srv = XuiServer(panel)
    await srv.detect()
    assert srv.url == "http://127.0.0.1:2053/secret/"
    assert srv.inbound_id == 7  # VLESS на 443, а не первый попавшийся и не trojan
    assert panel.minted == 1
    # повторное подключение использует уже записанный токен
    await XuiServer(panel).detect()
    assert panel.minted == 1


async def test_inbound_override_and_docker():
    panel = FakePanel(docker=True, overrides={"inbound": "other"})
    srv = XuiServer(panel)
    await srv.detect()
    assert srv.cli[:3] == ["docker", "exec", "3x-ui"]
    assert srv.inbound_id == 8


async def test_add_remove_stats_roundtrip():
    panel = FakePanel()
    srv = XuiServer(panel)
    await srv.detect()
    cid, info = await srv.add_client("Мой айфон | @user")
    assert info.flow == "xtls-rprx-vision" and info.sni == "site.example.com" and info.public_key == "PUBKEY"
    rec = panel.records[cid]
    assert rec["id"] == cid and rec["comment"] == "Мой айфон | @user" and rec["flow"] == "xtls-rprx-vision"
    assert rec["expiryTime"] == 0 and rec["enable"] is True and len(rec["subId"]) == 16
    # повторное добавление того же UUID ничего не делает
    await srv.add_client("x", cid)
    assert panel.inbounds[7]["clients"] == [cid]
    panel.traffic[cid] = (100, 900)
    stats = await srv.stats()
    assert stats[cid].rx == 100 and stats[cid].tx == 900
    assert srv.client_ids(await srv.load_config()) == [cid]
    assert await srv.remove_client(cid) is True
    assert await srv.remove_client(cid) is False
    assert panel.inbounds[7]["clients"] == []


async def test_add_clients_restores_and_attaches_existing_email():
    panel = FakePanel()
    srv = XuiServer(panel)
    await srv.detect()
    # клиент уже есть в панели, но отвязан от подключения бота
    panel.records["u-1"] = {"id": "u-1", "email": "u-1"}
    assert await srv.add_clients([("A", "u-1"), ("B", "u-2"), ("B", "u-2")]) == 2
    assert sorted(panel.inbounds[7]["clients"]) == ["u-1", "u-2"]


async def test_revoked_token_is_reissued():
    panel = FakePanel()
    srv = XuiServer(panel)
    await srv.detect()
    panel.valid_tokens.clear()  # токен удалили в панели
    await srv.add_client("A", "u-1")
    assert panel.minted == 2 and "u-1" in panel.records


async def test_xhttp_link_has_client_extra_only():
    panel = FakePanel(stream=XHTTP_STREAM, disable_flow=True)
    srv = XuiServer(panel)
    await srv.detect()
    cid, info = await srv.add_client("A")
    assert info.network == "xhttp" and info.flow == "" and panel.records[cid]["flow"] == ""
    assert info.path == "/0123abcd/" and info.mode == "auto"
    assert info.xhttp_extra == {"xPaddingBytes": "100-1000"}
    url = build_vless_url(info, "152.0.0.1", cid, "test")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert q["type"] == ["xhttp"] and q["sni"] == ["site.example.com"] and q["pbk"] == ["PUBKEY"]
    assert q["sid"] == ["ab12"] and "flow" not in q
    assert "scMaxBufferedPosts" not in q["extra"][0]


def test_parse_rejects_unsupported():
    base = {"inbound": {"port": 443}, "settings": {"decryption": "none"}}
    with pytest.raises(AwgError, match="транспорт ws"):
        parse_xui_info({**base, "stream": {"network": "ws", "security": "tls"}})
    with pytest.raises(AwgError, match="VLESS Encryption"):
        parse_xui_info({**base, "settings": {"decryption": "mlkem768x25519plus..."}, "stream": REALITY_STREAM})
    no_key = json.loads(json.dumps(REALITY_STREAM))
    del no_key["realitySettings"]["settings"]["publicKey"]
    with pytest.raises(AwgError, match="publicKey"):
        parse_xui_info({**base, "stream": no_key})


def test_panel_url_and_show():
    show = parse_show("Panel is secure with SSL\nport: 54321\nwebBasePath: abc\nlistenIP: 10.0.0.5\n")
    assert panel_url(show) == "https://10.0.0.5:54321/abc/"
    assert panel_url({"port": 2053, "base_path": "/", "listen": "0.0.0.0"}) == "http://127.0.0.1:2053/"
    with pytest.raises(AwgError):
        parse_show("open database failed")


class NoDocker(FakePanel):
    async def run(self, args, stdin=None):
        if args[:2] == ["docker", "ps"]:
            return 127, "", "bash: docker: command not found"
        return await super().run(args, stdin)


async def test_auto_vless_falls_back_to_3xui():
    vless = AutoVless(NoDocker())
    await vless.detect()
    assert vless.backend == "3x-ui"
    cid, info = await vless.add_client("A")
    assert cid in vless.client_ids(await vless.load_config())


async def test_auto_vless_reports_both_errors():
    class Nothing(Runner):
        async def run(self, args, stdin=None):
            if args[0] == "docker":
                return 127, "", "docker: command not found"
            return 0, "none\n", ""

    with pytest.raises(AwgError, match="3x-ui не найден"):
        await AutoVless(Nothing()).detect()


# ---------------------------------------------------------------- AmneziaWG


async def test_awg_detect_and_server_info():
    panel = FakePanel()
    srv = XuiAwgServer(panel)
    await srv.detect()
    assert srv.inbound_id == 10  # AmneziaWG, а не VLESS-подключения
    info = await srv.server_info()
    assert info.container == "amnezia-awg2" and info.port == 51820 and info.server_public_key == "SERVERPUB="
    assert info.subnet_address == "10.8.1.0" and info.subnet_cidr == "24"
    assert info.mtu == str(1420 - 16)  # как EffectiveMTU в 3x-ui: 1420 - S4
    # общий PSK бота создан и сохранён на сервере
    assert info.preshared_key and panel.psk == info.preshared_key
    p = info.awg_params
    assert p["Jc"] == "4" and p["S3"] == "20" and p["S4"] == "16" and p["H1"] == "100000-200000"
    assert p["I1"] == "<r 64>" and p["HeaderProtectionKey"] == "HPK=" and p["RekeyAfterTime"] == "110-130"
    assert p["RandomTrailers"] == "on" and "DisableCookies" not in p and "I2" not in p


async def test_awg_add_remove_stats():
    panel = FakePanel()
    srv = XuiAwgServer(panel)
    await srv.detect()
    a, info = await srv.add_peer("Телефон | @u")
    assert a.ip == "10.8.1.2"  # .1 — у самого сервера
    rec = panel.records[awg_email(a.public_key)]
    assert rec["publicKey"] == a.public_key and rec["privateKey"] == a.private_key
    assert rec["allowedIPs"] == ["10.8.1.2/32"] and rec["preSharedKey"] == info.preshared_key
    assert rec["comment"] == "Телефон | @u" and rec["expiryTime"] == 0 and rec["enable"] is True
    b, _ = await srv.add_peer("Ноут", reserved={"10.8.1.3"})  # адрес отключённого ключа не занимаем
    assert b.ip == "10.8.1.4"
    # восстановление: тот же ключ и адрес
    again, _ = await srv.add_peer("Телефон", keypair=(a.private_key, a.public_key), ip=a.ip)
    assert again.ip == a.ip and len(panel.inbounds[10]["clients"]) == 2
    # адрес занят другим — выдаётся новый
    priv, pub = generate_keypair()
    c, _ = await srv.add_peer("Планшет", keypair=(priv, pub), ip="10.8.1.2")
    assert c.ip == "10.8.1.3" and c.public_key == pub

    panel.traffic[awg_email(a.public_key)] = (500, 7000)
    panel.last_online[awg_email(a.public_key)] = 1_700_000_000_123
    st = (await srv.stats())[a.public_key]
    assert (st.latest_handshake, st.rx, st.tx) == (1_700_000_000, 500, 7000)
    cfg = await srv.load_config()
    assert {p["PublicKey"] for p in cfg.peers} == {a.public_key, b.public_key, pub}
    assert await srv.remove_peer(b.public_key) is True
    assert await srv.remove_peer(b.public_key) is False
    assert awg_email(b.public_key) not in panel.records


async def test_awg_psk_recovered_from_bot_clients():
    panel = FakePanel()
    priv, pub = generate_keypair()
    email = awg_email(pub)
    panel.records[email] = {"email": email, "publicKey": pub, "privateKey": priv,
                            "allowedIPs": ["10.8.1.9/32"], "preSharedKey": "OLDPSK="}
    panel.inbounds[10]["clients"].append(email)
    srv = XuiAwgServer(panel)
    await srv.detect()
    assert (await srv.server_info()).preshared_key == "OLDPSK=" and panel.psk == "OLDPSK="


async def test_awg_client_config_from_3xui():
    panel = FakePanel()
    srv = XuiAwgServer(panel)
    await srv.detect()
    peer, info = await srv.add_peer("A")
    conf = build_native_config(ClientParams(
        host="152.0.0.1", port=info.port, client_ip=peer.ip, client_private_key=peer.private_key,
        client_public_key=peer.public_key, server_public_key=info.server_public_key,
        preshared_key=info.preshared_key, awg_params=info.awg_params, dns1="1.1.1.1", dns2="1.0.0.1",
        container=info.container, mtu=info.mtu,
    ))
    for line in ("Address = 10.8.1.2/32", "MTU = 1404", "S3 = 20", "S4 = 16", "H4 = 700000-800000",
                 "I1 = <r 64>", "HeaderProtectionKey = HPK=", "RandomTrailers = on",
                 "PublicKey = SERVERPUB=", f"PresharedKey = {info.preshared_key}", "Endpoint = 152.0.0.1:51820",
                 "PersistentKeepalive = 25-35"):
        assert line in conf, line


async def test_auto_awg_falls_back_to_3xui():
    awg = AutoAwg(NoDocker())
    await awg.detect()
    assert awg.backend == "3x-ui"
    peer, info = await awg.add_peer("A")
    assert info.port == 51820 and peer.ip == "10.8.1.2"


async def test_awg_without_inbound_is_reported():
    panel = FakePanel()
    del panel.inbounds[10]
    with pytest.raises(AwgError, match="нет включённого AmneziaWG"):
        await XuiAwgServer(panel).detect()
