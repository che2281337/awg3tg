import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAKEBIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakebin")

# Так выглядит awg0.conf, который создаёт приложение AmneziaVPN 5.x
# (client/server_scripts/awg/configure_container.sh) для AmneziaWG 3.1.
SERVER_CONF = """[Interface]
PrivateKey = sCQnTkkEYVtM/Bv0h0OMpkEBrQEJ0tNqCdjSAOFvrUc=
Address = 10.8.1.0/24
ListenPort = 55424
Jc = 4
Jmin = 12
Jmax = 50
S1 = 64
S2 = 120
S3 = 24
S4 = 16
H1 = 1020325451-1131148220
H2 = 1503522183-1641098421
H3 = 1964585130-2072433232
H4 = 2117612301-2147000000
HeaderProtectionKey = aGVhZGVyLXByb3RlY3Rpb24ta2V5LWJhc2U2NC0zMg==
ContentPaddingAddition = 10-100
RekeyAfterTime = 100-120
RekeyTimeout = 3-7
RejectAfterTime = 150-180
KeepaliveTimeout = 5-15
MaxHandshakeAttempts = 15-20
RandomTrailers = on
DisableCookies = on
# I1 = <r 2><b 0x858000010001000000000669636c6f756403636f6d0000010001c00c000100010000105a00044d583737>

[Peer]
PublicKey = 7jxs4R4cN6S3OkH5TbVpIDaPu1lXDFnxsI3cFeuUVlc=
PresharedKey = 2Dw0q8aBpd2cOHkNKj3yIKFxSuzCVk8h8iNYTmKCt5s=
AllowedIPs = 10.8.1.1/32

"""

SERVER_PUB = "xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg="
SERVER_PSK = "2Dw0q8aBpd2cOHkNKj3yIKFxSuzCVk8h8iNYTmKCt5s="


@pytest.fixture
def fake_container(tmp_path, monkeypatch):
    awg_dir = tmp_path / "opt" / "amnezia" / "awg"
    awg_dir.mkdir(parents=True)
    (awg_dir / "awg0.conf").write_text(SERVER_CONF)
    (awg_dir / "wireguard_server_public_key.key").write_text(SERVER_PUB + "\n")
    (awg_dir / "wireguard_psk.key").write_text(SERVER_PSK + "\n")
    xray_dir = tmp_path / "opt" / "amnezia" / "xray"
    xray_dir.mkdir(parents=True)
    (xray_dir / "server.json").write_text(json.dumps(XRAY_SERVER_JSON, indent=4))
    (xray_dir / "xray_public.key").write_text(XRAY_PUB + "\n")
    (xray_dir / "xray_short_id.key").write_text(XRAY_SID + "\n")
    monkeypatch.setenv("FAKE_ROOT", str(tmp_path))
    return tmp_path


# Так выглядит server.json, который пишет приложение AmneziaVPN для XRay (VLESS + Reality).
XRAY_PUB = "Zs8nT-p4bYp9e6kBQf3u0lX4yWZbq2Zc8m0e2fGJ1Ac"
XRAY_SID = "a1b2c3d4e5f60718"
XRAY_SERVER_JSON = {
    "log": {"loglevel": "error"},
    "inbounds": [
        {
            "port": 443,
            "protocol": "vless",
            "settings": {
                "clients": [{"id": "11111111-2222-3333-4444-555555555555", "flow": "xtls-rprx-vision"}],
                "decryption": "none",
            },
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "dest": "www.googletagmanager.com:443",
                    "fingerprint": "chrome",
                    "privateKey": "server-private-key",
                    "serverNames": ["www.googletagmanager.com"],
                    "shortIds": [XRAY_SID],
                },
            },
        }
    ],
    "outbounds": [{"protocol": "freedom"}],
}
