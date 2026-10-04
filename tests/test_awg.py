import json

from bot.awg import conf as wgconf
from bot.awg.export import ClientParams, build_native_config, build_vpn_url, decode_vpn_url, q_uncompress
from bot.awg.keys import generate_keypair, public_from_private
from bot.awg.server import AwgServer

from .conftest import SERVER_CONF, SERVER_PSK, SERVER_PUB


def test_parse_awg3_params():
    cfg = wgconf.parse(SERVER_CONF)
    params = cfg.awg_params()
    assert params["HeaderProtectionKey"].startswith("aGVh")
    assert params["H1"] == "1020325451-1131148220"
    assert params["I1"].startswith("<r 2><b 0x8580")  # из закомментированной строки
    assert params["RandomTrailers"] == "on"
    assert "PrivateKey" not in params and "ListenPort" not in params
    assert wgconf.protocol_version(params) == "3.1"


def test_protocol_version_legacy():
    assert wgconf.protocol_version({"Jc": "3", "H1": "1", "H2": "2"}) == ""
    assert wgconf.protocol_version({"H1": "1-5"}) == "2"
    assert wgconf.protocol_version({"I1": "<r 2>"}) == "1.5"
    assert wgconf.protocol_version({"RandomTrailers": "off", "S3": "10"}) == "2"


def test_add_remove_peer_roundtrip():
    cfg = wgconf.parse(SERVER_CONF)
    cfg.add_peer("PUB", "PSK", "10.8.1.2/32")
    text = cfg.dump()
    assert "# I1 = <r 2>" in text  # комментарии сервера не теряются
    cfg2 = wgconf.parse(text)
    assert len(cfg2.peers) == 2
    assert cfg2.remove_peer("PUB")
    assert len(cfg2.peers) == 1
    assert not cfg2.remove_peer("PUB")


def test_allocate_ip_reuses_gaps():
    cfg = wgconf.parse(SERVER_CONF)
    assert AwgServer.allocate_ip(cfg) == "10.8.1.2"
    cfg.add_peer("A", None, "10.8.1.3/32")
    assert AwgServer.allocate_ip(cfg) == "10.8.1.2"
    cfg.add_peer("B", None, "10.8.1.2/32")
    cfg.add_peer("C", None, "0.0.0.0/0")  # не должен «занять» всю подсеть
    assert AwgServer.allocate_ip(cfg) == "10.8.1.4"


def test_keys():
    priv, pub = generate_keypair()
    assert public_from_private(priv) == pub
    assert len(pub) == 44


def _params(**kw):
    cfg = wgconf.parse(SERVER_CONF)
    priv, pub = generate_keypair()
    base = dict(
        host="203.0.113.10",
        port=55424,
        client_ip="10.8.1.2",
        client_private_key=priv,
        client_public_key=pub,
        server_public_key=SERVER_PUB,
        preshared_key=SERVER_PSK,
        awg_params=cfg.awg_params(),
        dns1="1.1.1.1",
        dns2="1.0.0.1",
        subnet_address="10.8.1.0",
        subnet_cidr="24",
        description="Test",
    )
    base.update(kw)
    return ClientParams(**base)


def test_native_config_contains_awg3():
    text = build_native_config(_params())
    parsed = wgconf.parse(text)
    iface = parsed.interface
    for key in ("HeaderProtectionKey", "ContentPaddingAddition", "RekeyAfterTime", "S3", "S4", "I1", "DisableCookies"):
        assert iface.get(key), key
    peer = parsed.peers[0]
    assert peer.get("Endpoint") == "203.0.113.10:55424"
    assert peer.get("PersistentKeepalive") == "25-35"
    assert peer.get("PresharedKey") == SERVER_PSK


def test_ipv6_endpoint():
    assert "Endpoint = [2001:db8::1]:55424" in build_native_config(_params(host="2001:db8::1"))


def test_vpn_url_format():
    p = _params()
    url = build_vpn_url(p)
    assert url.startswith("vpn://") and "=" not in url and "+" not in url and "/" not in url[6:]
    data = decode_vpn_url(url)
    assert data["defaultContainer"] == "amnezia-awg2"
    assert data["hostName"] == "203.0.113.10"
    assert data["format_version"] == 1
    awg = data["containers"][0]["awg"]
    assert data["containers"][0]["container"] == "amnezia-awg2"
    assert awg["port"] == "55424" and awg["protocol_version"] == "3.1"
    assert awg["HeaderProtectionKey"] == p.awg_params["HeaderProtectionKey"]
    assert awg["I2"] == ""
    last = json.loads(awg["last_config"])
    assert last["client_priv_key"] == p.client_private_key
    assert last["clientId"] == p.client_public_key
    assert last["port"] == 55424
    assert last["psk_key"] == SERVER_PSK
    assert last["RekeyTimeout"] == "3-7"
    assert last["persistent_keep_alive"] == "25-35"
    assert "HeaderProtectionKey = " in last["config"]


def test_qcompress_header():
    import base64

    url = build_vpn_url(_params())
    raw = base64.urlsafe_b64decode(url[6:] + "=" * (-len(url[6:]) % 4))
    # qCompress: 4 байта длины big-endian, затем zlib-заголовок 0x78
    assert raw[4] == 0x78
    assert json.loads(q_uncompress(raw))




def test_allocate_ip_respects_reserved():
    cfg = wgconf.parse(SERVER_CONF)
    assert AwgServer.allocate_ip(cfg, {"10.8.1.2", "10.8.1.3"}) == "10.8.1.4"
    assert AwgServer.ip_in_use(cfg, "10.8.1.1")  # занят пиром из приложения
    assert AwgServer.ip_in_use(cfg, "10.8.1.0")  # адрес сервера
    assert AwgServer.ip_in_use(cfg, "10.9.0.5")  # вне подсети
    assert not AwgServer.ip_in_use(cfg, "10.8.1.7")
