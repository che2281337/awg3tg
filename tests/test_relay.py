import json
import os

import pytest

from bot.awg.export import decode_vpn_url
from bot.awg.relay import PORT_RANGE_START, SCRIPT_PATH, Forward, RelayClient, allocate_ports, build_script
from bot.awg.runner import LocalRunner
from bot.service import ServiceError

from .conftest import FAKEBIN
from .test_service import multi  # noqa: F401  (фикстура: 3 демо-сервера)


def test_build_script():
    script = build_script([Forward("udp", 55424, "1.2.3.4", 55424), Forward("tcp", 20000, "5.6.7.8", 443)])
    assert script.startswith("#!/bin/sh\n")
    assert "iptables -t nat -A AWG3TG_PRE -p udp --dport 55424 -j DNAT --to-destination 1.2.3.4:55424" in script
    assert "iptables -t nat -A AWG3TG_PRE -p tcp --dport 20000 -j DNAT --to-destination 5.6.7.8:443" in script
    assert "iptables -t nat -A AWG3TG_POST -p tcp -d 5.6.7.8 --dport 443 -j MASQUERADE" in script
    assert "iptables -t filter -C FORWARD -j AWG3TG_FWD 2>/dev/null || iptables -t filter -I FORWARD -j AWG3TG_FWD" in script
    # порядок правил не зависит от порядка серверов — иначе бот перезаписывал бы файл впустую
    assert script == build_script([Forward("tcp", 20000, "5.6.7.8", 443), Forward("udp", 55424, "1.2.3.4", 55424)])


def test_allocate_ports():
    wanted = [((1, "awg"), "udp", 55424), ((2, "awg"), "udp", 55424), ((1, "vless"), "tcp", 443), ((2, "vless"), "tcp", 22)]
    ports = allocate_ports(wanted, {}, {("tcp", 22)})
    assert ports == {(1, "awg"): 55424, (2, "awg"): PORT_RANGE_START, (1, "vless"): 443, (2, "vless"): PORT_RANGE_START}
    # выданные порты сохраняются, даже если «родной» порт освободился
    again = allocate_ports(list(reversed(wanted)), ports, {("tcp", 22)})
    assert again == ports


async def test_relay_client_shell(tmp_path, monkeypatch):
    """Сгенерированные команды реально выполняются в sh (iptables/systemctl — фейковые)."""
    monkeypatch.setenv("PATH", FAKEBIN + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_ROOT", str(tmp_path))
    (tmp_path / "etc" / "sysctl.d").mkdir(parents=True)
    (tmp_path / "etc" / "systemd" / "system").mkdir(parents=True)
    client = RelayClient(LocalRunner(), root=str(tmp_path))
    forwards = [Forward("udp", 55424, "1.2.3.4", 55424), Forward("tcp", 443, "1.2.3.4", 443)]

    assert await client.sync(forwards) is True
    assert (tmp_path / SCRIPT_PATH.lstrip("/")).read_text() == build_script(forwards)
    assert "ExecStart=/bin/sh /etc/awg3tg-relay.sh" in (tmp_path / "etc/systemd/system/awg3tg-relay.service").read_text()
    assert "enable awg3tg-relay.service" in (tmp_path / "systemctl.log").read_text()
    rules = (tmp_path / "iptables.rules").read_text()
    assert "A nat AWG3TG_PRE -p udp --dport 55424 -j DNAT --to-destination 1.2.3.4:55424" in rules
    assert rules.count("A nat PREROUTING -j AWG3TG_PRE") == 1
    assert await client.active_rules() == 2

    assert await client.sync(forwards) is False  # всё уже настроено — ничего не трогаем
    # правила пропали (перезагрузка без службы) — бот применит заново, без дублей
    (tmp_path / "iptables.rules").write_text("")
    assert await client.sync(forwards) is True
    assert (tmp_path / "iptables.rules").read_text().count("A nat PREROUTING -j AWG3TG_PRE") == 1

    await client.remove()
    assert "AWG3TG" not in (tmp_path / "iptables.rules").read_text()
    assert not (tmp_path / SCRIPT_PATH.lstrip("/")).exists()


async def test_whitelist_keys(multi, tmp_path):  # noqa: F811
    svc = multi
    de, nl, fi = await svc.db.servers()
    user, _ = await svc.db.touch_user(10, "u", "U")
    await svc.extend(10, 30, devices=3)
    awg_key = (await svc.create_device(user, "Телефон", de.id, "awg")).key
    nl_key = (await svc.create_device(user, "Ноутбук", nl.id, "awg")).key
    vless_key = (await svc.create_device(user, "Планшет", fi.id, "vless")).key
    before = (await svc.render(awg_key)).vpn_url

    # без входа кнопки нет
    assert await svc.whitelist_route(awg_key) is None
    with pytest.raises(ServiceError):
        await svc.render(awg_key, whitelist=True)

    relay = await svc.add_relay("Москва", "7.7.7.7", f"mock:{tmp_path / 'relay'}")
    assert relay.is_main and relay.status_ok
    ports = {(p.server_id, p.protocol): p for p in await svc.db.relay_ports(relay.id)}
    # у всех демо-серверов AWG на 55424 — первый получает его, остальные — свободные порты
    assert ports[(de.id, "awg")].listen_port == 55424
    assert ports[(nl.id, "awg")].listen_port == PORT_RANGE_START
    assert ports[(nl.id, "awg")].target_ip == nl.host and ports[(nl.id, "awg")].target_port == 55424

    rk = await svc.render(nl_key, whitelist=True)
    assert rk.whitelist and rk.filename.endswith(f"WL_{nl_key.id}.conf")
    data = decode_vpn_url(rk.vpn_url)
    last = json.loads(data["containers"][0]["awg"]["last_config"])
    assert last["hostName"] == "7.7.7.7" and last["port"] == PORT_RANGE_START
    assert "белые списки" in data["description"]
    assert f"Endpoint = 7.7.7.7:{PORT_RANGE_START}" in rk.conf
    assert last["client_priv_key"] == nl_key.private_key  # тот же ключ — слот не тратится

    v = await svc.render(vless_key, whitelist=True)
    assert f"@7.7.7.7:{ports[(fi.id, 'vless')].listen_port}?" in v.vpn_url and vless_key.public_key in v.vpn_url

    # обычные ключи не изменились
    assert (await svc.render(awg_key)).vpn_url == before

    # повторная проверка ничего не перезаписывает, порты стабильны
    check = await svc.sync_relay(relay)
    assert check.ok and not check.applied
    assert {(p.server_id, p.protocol): p.listen_port for p in await svc.db.relay_ports(relay.id)} == {
        k: p.listen_port for k, p in ports.items()
    }


async def test_relay_down_and_server_down(multi, tmp_path):  # noqa: F811
    svc = multi
    de, nl, _ = await svc.db.servers()
    user, _ = await svc.db.touch_user(10, "u", "U")
    await svc.extend(10, 30, devices=2)
    key = (await svc.create_device(user, "Телефон", nl.id, "awg")).key
    relay_dir = tmp_path / "relay"
    relay = await svc.add_relay("Москва", "7.7.7.7", f"mock:{relay_dir}")
    port = (await svc.whitelist_route(key))[1].listen_port

    # сервер упал — направление на него сохраняется (порт клиентов не меняется)
    open(os.path.join(nl.conn[5:], "DOWN"), "w").close()
    await svc.check_servers()
    await svc.sync_relay(relay)
    assert (await svc.whitelist_route(key))[1].listen_port == port
    os.remove(os.path.join(nl.conn[5:], "DOWN"))
    await svc.check_servers()

    # вход упал — кнопка пропадает, админ получит changed=True
    (relay_dir / "DOWN").write_text("")
    [check] = await svc.check_relays()
    assert not check.ok and check.changed and "DOWN" in check.error
    assert await svc.whitelist_route(key) is None
    (relay_dir / "DOWN").unlink()
    [check] = await svc.check_relays()
    assert check.ok and check.changed
    assert (await svc.whitelist_route(key))[1].listen_port == port


async def test_spare_relay_and_delete(multi, tmp_path):  # noqa: F811
    svc = multi
    main = await svc.add_relay("Москва", "7.7.7.7", f"mock:{tmp_path / 'r1'}")
    spare = await svc.add_relay("Питер", "8.8.8.8", f"mock:{tmp_path / 'r2'}")
    assert not spare.is_main
    assert (tmp_path / "r2" / SCRIPT_PATH.lstrip("/")).exists()  # запасной настроен заранее
    user, _ = await svc.db.touch_user(10, "u", "U")
    await svc.extend(10, 30)
    key = (await svc.create_device(user, "Телефон", (await svc.db.servers())[0].id)).key
    assert (await svc.whitelist_route(key))[0].id == main.id

    await svc.db.set_main_relay(spare.id)
    assert (await svc.whitelist_route(key))[0].id == spare.id
    await svc.delete_relay(await svc.db.get_relay(spare.id))  # удалили основной — основным стал оставшийся
    assert (await svc.db.main_relay()).id == main.id
    assert not (tmp_path / "r2" / SCRIPT_PATH.lstrip("/")).exists()

    with pytest.raises(ServiceError):
        await svc.add_relay("Бот", "1.1.1.1", "local")
    with pytest.raises(ServiceError, match="Не удалось"):
        os.makedirs(tmp_path / "r3")
        (tmp_path / "r3" / "DOWN").write_text("")
        await svc.add_relay("Сломанный", "9.9.9.9", f"mock:{tmp_path / 'r3'}")
    assert len(await svc.db.relays()) == 1
