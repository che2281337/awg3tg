"""Скрытие протокола, смена протокола устройства, восстановление стёртых клиентов, защита от дублей."""

import json
import os

import pytest

from bot.service import ServiceError

from .test_service import multi  # noqa: F401  (фикстура: 3 демо-сервера)


def awg_conf(server) -> str:
    return open(os.path.join(server.conn[5:], "opt/amnezia/awg/awg0.conf"), encoding="utf-8").read()


def xray_ids(server) -> list[str]:
    with open(os.path.join(server.conn[5:], "opt/amnezia/xray/server.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    return [c["id"] for c in cfg["inbounds"][0]["settings"]["clients"]]


async def user_with_sub(svc, devices=3):
    user, _ = await svc.db.touch_user(10, "u", "U")
    await svc.extend(10, 30, devices=devices)
    return await svc.db.get_user(10)


async def test_hide_protocol(multi):  # noqa: F811
    svc = multi
    de, nl, _ = await svc.db.servers()
    user = await user_with_sub(svc)
    old = (await svc.create_device(user, "Старый", de.id, "vless")).key

    await svc.set_protocol_hidden(de, "vless", True)
    de = await svc.db.get_server(de.id)
    assert de.visible_protocols == ["awg"] and de.hidden_list == ["vless"]
    assert de.id not in {s.id for s in await svc.available_servers("vless")}
    assert de.id in {s.id for s in await svc.available_servers("awg")}
    with pytest.raises(ServiceError, match="недоступен"):
        await svc.create_device(user, "Новый", de.id, "vless")
    assert (await svc.create_device(user, "Админ", de.id, "vless", force=True)).key.protocol == "vless"
    # у кого VLESS уже есть — работает как прежде
    assert old.public_key in (await svc.render(old)).vpn_url
    with pytest.raises(ServiceError):
        await svc.move_device(old, de.id)  # на тот же сервер — и так нельзя
    other = (await svc.create_device(user, "Другой", nl.id, "vless")).key
    with pytest.raises(ServiceError, match="нет"):
        await svc.move_device(other, de.id)  # на скрытый протокол клиент не переносит

    await svc.set_protocol_hidden(de, "vless", False)
    de = await svc.db.get_server(de.id)
    assert de.visible_protocols == ["awg", "vless"]


async def test_change_protocol_keeps_device(multi):  # noqa: F811
    svc = multi
    de, nl, _ = await svc.db.servers()
    user = await user_with_sub(svc)
    key = (await svc.create_device(user, "Телефон", de.id, "awg")).key
    await svc.db.c.execute(
        "INSERT INTO traffic_daily (key_id, tg_id, server_id, day, rx, tx) VALUES (?, 10, ?, '2026-11-01', 5, 7)",
        (key.id, de.id),
    )
    await svc.db.c.commit()

    # AWG → VLESS на том же сервере
    assert [s.id for s in await svc.protocol_options(key)][0] == de.id  # текущий сервер первым
    rk = await svc.change_protocol(key, "vless")
    moved = await svc.db.get_key(key.id)
    assert moved.protocol == "vless" and moved.server_id == de.id and moved.enabled
    assert moved.public_key in xray_ids(de) and key.public_key not in awg_conf(de)
    assert rk.vpn_url.startswith("vless://" + moved.public_key)
    assert (await svc.db.traffic(key_id=key.id)).rx == 5  # история трафика устройства сохранилась
    assert len(await svc.db.user_keys(10)) == 1  # слот не тратится

    # VLESS → AWG на другом сервере
    rk = await svc.change_protocol(moved, "awg", nl.id)
    back = await svc.db.get_key(key.id)
    assert back.protocol == "awg" and back.server_id == nl.id and back.ip
    assert back.public_key in awg_conf(nl) and moved.public_key not in xray_ids(de)
    assert back.public_key != key.public_key  # ключ новый
    assert "[Peer]" in rk.conf

    with pytest.raises(ServiceError, match="уже"):
        await svc.change_protocol(back, "awg")


async def test_change_protocol_disabled_and_rollback(multi):  # noqa: F811
    svc = multi
    de, nl, _ = await svc.db.servers()
    user = await user_with_sub(svc, devices=1)
    first = (await svc.create_device(user, "Первый", de.id, "awg")).key
    second = (await svc.create_device(user, "Второй", de.id, "awg", force=True)).key
    await svc.sync_user(10)  # лимит 1 — второе устройство отключено
    second = await svc.db.get_key(second.id)
    assert not second.enabled

    changed = (await svc.change_protocol(second, "vless")).key
    assert changed.protocol == "vless" and not changed.enabled  # осталось отключённым
    assert changed.public_key not in xray_ids(de)

    # старый сервер не отвечает — смена отменяется целиком, новый ключ не остаётся висеть
    before = xray_ids(nl)
    open(os.path.join(de.conn[5:], "DOWN"), "w").close()
    await svc.pool.drop(de.id)
    with pytest.raises(ServiceError, match="не отвечает"):
        await svc.change_protocol(first, "vless", nl.id)
    assert xray_ids(nl) == before
    assert (await svc.db.get_key(first.id)).protocol == "awg"
    os.remove(os.path.join(de.conn[5:], "DOWN"))


async def test_restore_wiped_clients(multi):  # noqa: F811
    svc = multi
    de, *_ = await svc.db.servers()
    user = await user_with_sub(svc)
    awg = (await svc.create_device(user, "Телефон", de.id, "awg")).key
    vless = (await svc.create_device(user, "Ноутбук", de.id, "vless")).key
    off = (await svc.create_device(user, "Планшет", de.id, "vless")).key
    await svc.disable_key(off)

    assert (await svc.check_server(de)).restored == 0  # всё на месте — ничего не трогаем

    # «переустановили» AWG и XRay в приложении Amnezia: конфиги созданы заново, клиентов нет
    root = de.conn[5:]
    os.remove(os.path.join(root, "opt/amnezia/awg/awg0.conf"))
    os.remove(os.path.join(root, "opt/amnezia/xray/server.json"))
    await svc.pool.drop(de.id)

    check = await svc.check_server(de)
    assert check.ok and check.restored == 2
    assert awg.public_key in awg_conf(de) and f"{awg.ip}/32" in awg_conf(de)
    assert vless.public_key in xray_ids(de)
    assert off.public_key not in xray_ids(de)  # отключённый не возвращается
    assert (await svc.check_server(de)).restored == 0

    # ключ, с которым идёт операция (удаление/отключение), не восстанавливается
    await svc._remove_from_server(vless)
    svc._busy[vless.id] = 1
    assert (await svc.check_server(de)).restored == 0
    del svc._busy[vless.id]
    assert (await svc.check_server(de)).restored == 1


async def test_duplicate_server_rejected(multi):  # noqa: F811
    svc = multi
    de, nl, _ = await svc.db.servers()
    with pytest.raises(ServiceError, match="уже добавлен"):
        await svc.add_server("Копия", "🇩🇪", "203.0.113.99", de.conn)
    with pytest.raises(ServiceError, match="уже добавлен"):
        await svc.add_server("Копия", "🇩🇪", nl.host, "mock:/tmp/elsewhere")
    await svc.check_duplicate(de.host, de.conn, exclude_id=de.id)  # сам с собой — не дубль


async def test_server_protocol_counts(multi):  # noqa: F811
    svc = multi
    de, nl, _ = await svc.db.servers()
    user = await user_with_sub(svc)
    await svc.create_device(user, "A", de.id, "awg")
    await svc.create_device(user, "B", de.id, "vless")
    k = (await svc.create_device(user, "C", de.id, "vless")).key
    await svc.db.update_key(k.id, last_handshake=svc_now())
    counts = await svc.db.server_protocol_counts()
    assert counts[de.id] == {"awg": (1, 0), "vless": (2, 1)}
    assert nl.id not in counts


def svc_now():
    from bot.db import now

    return now()


class FakeCall:
    def __init__(self, uid=1, data="a:ban:5"):
        self.from_user = type("U", (), {"id": uid})()
        self.data = data
        self.alerts = []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append((text, show_alert))


async def test_confirm_tap(monkeypatch):
    from bot.handlers import common

    call = FakeCall()
    assert await common.confirm_tap(call, "Забанить?") is False
    text, alert = call.alerts[0]
    assert alert and text.startswith("⚠️ Забанить?") and "ещё раз" in text
    assert await common.confirm_tap(call, "Забанить?") is True  # второе нажатие — выполняем
    assert await common.confirm_tap(call, "Забанить?") is False  # и снова спрашиваем
    # другая кнопка или другой админ — отдельное подтверждение
    assert await common.confirm_tap(FakeCall(uid=2), "Забанить?") is False
    assert await common.confirm_tap(FakeCall(data="a:del:5"), "Удалить?") is False
    # длинный вопрос обрезается под лимит Telegram (200 символов)
    long_call = FakeCall(data="x")
    await common.confirm_tap(long_call, "очень " * 100)
    assert len(long_call.alerts[0][0]) <= 200
    # подтверждение «протухает»
    monkeypatch.setattr(common, "CONFIRM_WINDOW", -1)
    assert await common.confirm_tap(FakeCall(data="y"), "?") is False
    assert await common.confirm_tap(FakeCall(data="y"), "?") is False


async def test_vless_client_tweaks(multi):  # noqa: F811
    """Ссылки VLESS: отпечаток firefox вместо chrome; XHTTP_MODE=stream-one подменяет auto, явный режим — как есть."""
    import urllib.parse as up

    svc = multi
    de, *_ = await svc.db.servers()
    user = await user_with_sub(svc)
    key = (await svc.create_device(user, "Ноут", de.id, "vless")).key

    def params(url):
        return dict(up.parse_qsl(up.urlparse(url).query))

    q = params((await svc.render(key)).vpn_url)
    assert q["fp"] == "firefox" and q["type"] == "tcp" and "mode" not in q

    path = os.path.join(de.conn[5:], "opt/amnezia/xray/server.json")
    cfg = json.load(open(path, encoding="utf-8"))
    stream = cfg["inbounds"][0]["streamSettings"]
    stream["network"] = "xhttp"
    stream["xhttpSettings"] = {"path": "/api/v2/sync", "mode": "auto"}
    json.dump(cfg, open(path, "w", encoding="utf-8"))
    assert params((await svc.render(key)).vpn_url)["mode"] == "auto"  # по умолчанию — как на сервере
    svc.settings.xhttp_mode = "stream-one"
    q = params((await svc.render(key)).vpn_url)
    assert q["type"] == "xhttp" and q["mode"] == "stream-one" and q["path"] == "/api/v2/sync" and "flow" not in q

    stream["xhttpSettings"]["mode"] = "packet-up"  # режим задан на сервере явно — клиент обязан совпасть
    json.dump(cfg, open(path, "w", encoding="utf-8"))
    assert params((await svc.render(key)).vpn_url)["mode"] == "packet-up"

    svc.settings.xray_fingerprint = ""  # пусто — как в настройках сервера
    svc.settings.xhttp_mode = ""
    stream["xhttpSettings"]["mode"] = "auto"
    json.dump(cfg, open(path, "w", encoding="utf-8"))
    q = params((await svc.render(key)).vpn_url)
    assert q["fp"] == "chrome" and q["mode"] == "auto"


# Конфиг клиента, который выдаёт приложение Amnezia (экспорт «для AmneziaVPN», ноябрь 2026) для
# сервера с XHTTP + Reality. ID, адрес и ключи обезличены.
AMNEZIA_CLIENT = {
    "inbounds": [{"listen": "127.0.0.1", "port": 10808, "protocol": "socks", "settings": {"udp": True}}],
    "log": {"loglevel": "error"},
    "outbounds": [
        {
            "protocol": "vless",
            "settings": {"vnext": [{"address": "ADDR", "port": 443, "users": [{"encryption": "none", "id": "UUID"}]}]},
            "streamSettings": {
                "network": "xhttp",
                "realitySettings": {
                    "fingerprint": "firefox",
                    "publicKey": "PBK",
                    "serverName": "www.helsinki.fi",
                    "shortId": "SID",
                    "spiderX": "",
                },
                "security": "reality",
                "xhttpSettings": {
                    "host": "www.helsinki.fi",
                    "mode": "auto",
                    "noGRPCHeader": True,
                    "noSSEHeader": True,
                    "path": "/api/v2/sync7f3a",
                    "scMaxEachPostBytes": "1000000-1000000",
                    "scMinPostsIntervalMs": "100-800",
                    "scStreamUpServerSecs": "1-100",
                    "seqPlacement": "path",
                    "sessionIDPlacement": "path",
                    "uplinkDataPlacement": "body",
                    "uplinkHTTPMethod": "POST",
                    "xPaddingHeader": "X-Padding",
                    "xPaddingKey": "x_padding",
                    "xPaddingMethod": "repeat-x",
                    "xPaddingObfsMode": True,
                    "xPaddingPlacement": "cookie",
                },
            },
        }
    ],
}
# На сервер Amnezia пишет те же xhttpSettings, кроме чисто клиентских полей (xrayProtocolConfig.cpp)
AMNEZIA_XHTTP = {
    k: v
    for k, v in AMNEZIA_CLIENT["outbounds"][0]["streamSettings"]["xhttpSettings"].items()
    if k not in ("uplinkHTTPMethod", "scMinPostsIntervalMs", "uplinkChunkSize", "xmux")
}


async def _amnezia_xhttp_server(svc):
    de, *_ = await svc.db.servers()
    await svc.check_server(de)
    path = os.path.join(de.conn[5:], "opt/amnezia/xray/server.json")
    cfg = json.load(open(path, encoding="utf-8"))
    stream = cfg["inbounds"][0]["streamSettings"]
    stream["network"] = "xhttp"
    stream["xhttpSettings"] = dict(AMNEZIA_XHTTP)
    stream["realitySettings"]["serverNames"] = ["www.helsinki.fi"]
    json.dump(cfg, open(path, "w", encoding="utf-8"))
    return de, path


async def test_vless_xhttp_matches_amnezia(multi):  # noqa: F811
    """Ключ vpn:// из бота совпадает с экспортом приложения Amnezia один в один."""
    from bot.awg.export import decode_vpn_url

    svc = multi
    de, path = await _amnezia_xhttp_server(svc)
    user = await user_with_sub(svc)
    key = (await svc.create_device(user, "Ноут", de.id, "vless")).key
    rk = await svc.render(key)

    data = decode_vpn_url(rk.amnezia_url)
    assert data["defaultContainer"] == "amnezia-xray" and data["hostName"] == de.host
    client = json.loads(data["containers"][0]["xray"]["last_config"])
    reality = client["outbounds"][0]["streamSettings"]["realitySettings"]
    assert reality["publicKey"] and reality["shortId"]
    expected = json.loads(
        json.dumps(AMNEZIA_CLIENT)
        .replace('"ADDR"', json.dumps(de.host))
        .replace('"UUID"', json.dumps(key.public_key))
        .replace('"PBK"', json.dumps(reality["publicKey"]))
        .replace('"SID"', json.dumps(reality["shortId"]))
    )
    assert client == expected


async def test_vless_xhttp_link_extra(multi):  # noqa: F811
    """Ссылка vless:// для других приложений несёт все настройки XHTTP в параметре extra."""
    import urllib.parse as up

    svc = multi
    de, path = await _amnezia_xhttp_server(svc)
    user = await user_with_sub(svc)
    key = (await svc.create_device(user, "Ноут", de.id, "vless")).key
    rk = await svc.render(key)
    q = dict(up.parse_qsl(up.urlparse(rk.vpn_url).query))
    assert q["type"] == "xhttp" and q["path"] == "/api/v2/sync7f3a" and q["host"] == "www.helsinki.fi"
    assert q["mode"] == "auto" and q["fp"] == "firefox" and q["sni"] == "www.helsinki.fi" and "flow" not in q
    expected_extra = {
        k: v
        for k, v in AMNEZIA_CLIENT["outbounds"][0]["streamSettings"]["xhttpSettings"].items()
        if k not in ("host", "path", "mode")
    }
    assert json.loads(q["extra"]) == expected_extra

    # через вход для белых списков — тот же конфиг, но адрес и порт входа
    await svc.add_relay("Москва", "7.7.7.7", f"mock:{os.path.join(os.path.dirname(path), 'relay')}")
    from bot.awg.export import decode_vpn_url

    wl = await svc.render(key, whitelist=True)
    wl_out = json.loads(decode_vpn_url(wl.amnezia_url)["containers"][0]["xray"]["last_config"])["outbounds"][0]
    assert wl_out["settings"]["vnext"][0]["address"] == "7.7.7.7"
    assert "@7.7.7.7:" in wl.vpn_url and "extra=" in wl.vpn_url


async def test_vless_tcp_has_amnezia_key(multi):  # noqa: F811
    from bot.awg.export import decode_vpn_url

    svc = multi
    de, *_ = await svc.db.servers()
    await svc.check_server(de)  # демо-сервер создаёт конфиг XRay
    path = os.path.join(de.conn[5:], "opt/amnezia/xray/server.json")
    cfg = json.load(open(path, encoding="utf-8"))
    # как на сервере Amnezia: её собственный клиент с flow vision
    cfg["inbounds"][0]["settings"]["clients"] = [{"id": "11111111-2222-3333-4444-555555555555", "flow": "xtls-rprx-vision"}]
    json.dump(cfg, open(path, "w", encoding="utf-8"))
    user = await user_with_sub(svc)
    rk = await svc.create_device(user, "Телефон", de.id, "vless")
    assert rk.amnezia_url.startswith("vpn://") and "extra=" not in rk.vpn_url
    out = json.loads(decode_vpn_url(rk.amnezia_url)["containers"][0]["xray"]["last_config"])["outbounds"][0]
    assert out["streamSettings"]["network"] == "tcp" and "xhttpSettings" not in out["streamSettings"]
    assert out["settings"]["vnext"][0]["users"][0]["flow"] == "xtls-rprx-vision"
