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
