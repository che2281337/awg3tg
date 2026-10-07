import json
import os

import pytest

from bot.awg.export import decode_vpn_url
from bot.awg.server import AwgServer
from bot.config import Settings
from bot.db import Database, now
from bot.service import ServiceError, VpnService

from .conftest import FAKEBIN, SERVER_PUB

ADMIN = 1


@pytest.fixture
async def svc(fake_container, tmp_path):
    settings = Settings(
        bot_token="x", admin_ids={ADMIN}, server_host="203.0.113.10", db_path=str(tmp_path / "bot.db"),
        ref_bonus_days=7,
    )
    db = Database(settings.db_path)
    await db.connect()
    service = VpnService(settings, db, AwgServer(docker=os.path.join(FAKEBIN, "docker")))
    await service.start()
    yield service
    await service.close()
    await db.close()


async def new_device(svc, user, name):
    """Устройство на первом (единственном) сервере."""
    server = (await svc.db.servers())[0]
    return await svc.create_device(user, name, server.id)


def server_conf(root) -> str:
    return (root / "opt" / "amnezia" / "awg" / "awg0.conf").read_text()


async def test_local_server_bootstrapped(svc):
    servers = await svc.db.servers()
    assert len(servers) == 1
    assert servers[0].conn == "local" and servers[0].host == "203.0.113.10" and servers[0].status_ok


async def test_default_plans_seeded(svc):
    plans = await svc.db.plans()
    assert [(p.days, p.devices, p.price) for p in plans] == [(30, 2, 100), (90, 2, 300), (180, 2, 600), (365, 2, 1200)]


async def test_no_subscription_no_device(svc):
    user, created = await svc.db.touch_user(10, "u10", "User")
    assert created
    with pytest.raises(ServiceError):
        await new_device(svc, user, "📱 Телефон")


async def test_device_limit_and_key(svc, fake_container):
    user, _ = await svc.db.touch_user(10, "u10", "User")
    await svc.extend(10, 30, devices=1)
    rk = await new_device(svc, user, "Мой айфон")
    assert rk.key.ip == "10.8.1.2"
    assert rk.filename == "AmneziaWG_1.conf"
    assert rk.location == "🌐 Основной"
    assert rk.key.public_key in server_conf(fake_container)
    data = decode_vpn_url(rk.vpn_url)
    assert json.loads(data["containers"][0]["awg"]["last_config"])["server_pub_key"] == SERVER_PUB
    assert data["description"] == "AmneziaWG 🌐 Основной"
    table = json.loads((fake_container / "opt/amnezia/awg/clientsTable").read_text())
    assert table[0]["userData"]["clientName"] == "Мой айфон | @u10"

    with pytest.raises(ServiceError, match="лимит"):
        await new_device(svc, user, "Ноутбук")


async def test_newbie_discount(svc):
    month, quarter, half, year = await svc.db.plans()
    user, _ = await svc.db.touch_user(10, "new", "New")
    assert await svc.price_for(user, month) == 50
    assert await svc.price_for(user, quarter) == 300  # скидка только на месяц
    assert await svc.price_for(user, half) == 600
    assert await svc.price_for(user, year) == 1200
    assert (await svc.first_offer(user)) == (month, 50)

    # админ выдал подписку вручную — человек всё ещё новичок (он не платил)
    await svc.extend(10, 10)
    assert await svc.price_for(user, month) == 50

    p = await svc.db.create_payment(10, month, "text", "чек", amount=50, title="1 месяц (скидка новичка)")
    await svc.confirm_payment(p.id, ADMIN)
    assert await svc.price_for(user, month) == 100
    assert await svc.first_offer(user) is None

    admin, _ = await svc.db.touch_user(ADMIN, "admin", "Admin")
    assert await svc.price_for(admin, month) == 100

    stats = await svc.db.stats(0, 0)
    assert stats["revenue_total"] == 50


async def test_expiry_disables_and_renewal_restores_same_key(svc, fake_container):
    user, _ = await svc.db.touch_user(10, "u10", "User")
    user = await svc.extend(10, 30, devices=2)
    rk1 = await new_device(svc, user, "📱 Телефон")
    rk2 = await new_device(svc, user, "💻 Компьютер")

    # подписка закончилась
    await svc.db.update_user(10, sub_until=now() - 1)
    expired = await svc.expire_subscriptions()
    assert [u.tg_id for u in expired] == [10]
    conf = server_conf(fake_container)
    assert rk1.key.public_key not in conf and rk2.key.public_key not in conf
    assert all(not k.enabled for k in await svc.db.user_keys(10))

    # пока ключи отключены, их IP не должны достаться другим
    other, _ = await svc.db.touch_user(20, "u20", "Other")
    await svc.extend(20, 30)
    rk_other = await new_device(svc, other, "📱 Телефон")
    assert rk_other.key.ip not in (rk1.key.ip, rk2.key.ip)

    # продление — те же ключи и IP снова на сервере
    await svc.extend(10, 30)
    conf = server_conf(fake_container)
    assert rk1.key.public_key in conf and rk2.key.public_key in conf
    keys = await svc.db.user_keys(10)
    assert [k.ip for k in keys] == [rk1.key.ip, rk2.key.ip] and all(k.enabled for k in keys)


async def test_device_limit_downgrade(svc, fake_container):
    user, _ = await svc.db.touch_user(10, "u10", "User")
    await svc.extend(10, 30, devices=3)
    for name in ("a", "b", "c"):
        await new_device(svc, user, name)
    await svc.set_device_limit(10, 1)
    keys = await svc.db.user_keys(10)
    assert [k.enabled for k in keys] == [1, 0, 0]  # старейшее устройство остаётся
    await svc.set_device_limit(10, 3)
    assert all(k.enabled for k in await svc.db.user_keys(10))


async def test_extend_adds_to_remaining(svc):
    await svc.db.touch_user(10, "u10", "User")
    u = await svc.extend(10, 10)
    first = u.sub_until
    u = await svc.extend(10, 5)
    assert u.sub_until == first + 5 * 86400


async def test_payment_and_referral(svc):
    await svc.db.touch_user(10, "ref", "Referrer")
    await svc.db.touch_user(11, "friend", "Friend")
    await svc.db.update_user(11, referrer_id=10)
    plan = (await svc.db.plans())[1]  # 3 месяца, 2 устройства
    friend = await svc.db.get_user(11)
    p = await svc.db.create_payment(11, plan, "text", "оплатил")
    res = await svc.confirm_payment(p.id, ADMIN)
    assert res.user.active and res.user.device_limit == 2
    assert res.user.sub_until >= now() + 89 * 86400
    assert res.referrer and res.referrer.tg_id == 10
    assert res.referrer.sub_until >= now() + 6 * 86400
    with pytest.raises(ServiceError):
        await svc.confirm_payment(p.id, ADMIN)  # двойное нажатие

    # второй платёж — бонус пригласившему повторно не начисляется
    p2 = await svc.db.create_payment(11, plan, "text", "ещё")
    res2 = await svc.confirm_payment(p2.id, ADMIN)
    assert res2.referrer is None
    assert (await svc.db.referrals_count(10)) == (1, 1)
    assert friend.tg_id == 11

    p3 = await svc.db.create_payment(11, plan, "text", "x")
    rejected = await svc.reject_payment(p3.id, ADMIN)
    assert rejected.status == "rejected"
    stats = await svc.db.stats(0, 0)
    assert stats["revenue_total"] == plan.price * 2


async def test_traffic_accumulates_across_counter_reset(svc, fake_container):
    user, _ = await svc.db.touch_user(10, "u10", "User")
    await svc.extend(10, 30)
    rk = await new_device(svc, user, "📱 Телефон")
    await svc.collect_traffic()  # fake awg: rx=1024, tx=2048
    await svc.collect_traffic()  # счётчики не изменились — дельта 0
    t = await svc.db.traffic(tg_id=10)
    assert (t.rx, t.tx) == (1024, 2048)
    # имитация перезапуска контейнера: счётчики стали меньше прошлых
    await svc.db.update_key(rk.key.id, last_rx=5000, last_tx=5000)
    await svc.collect_traffic()
    t = await svc.db.traffic(key_id=rk.key.id)
    assert (t.rx, t.tx) == (2048, 4096)
    assert (await svc.db.traffic(server_id=rk.key.server_id)).rx == 2048
    key = await svc.db.get_key(rk.key.id)
    assert key.last_handshake == 1700000000


async def test_ban_and_delete(svc, fake_container):
    user, _ = await svc.db.touch_user(10, "u10", "User")
    await svc.extend(10, 30)
    rk = await new_device(svc, user, "📱 Телефон")
    await svc.ban(10)
    assert rk.key.public_key not in server_conf(fake_container)
    await svc.unban(10)
    assert rk.key.public_key in server_conf(fake_container)
    await svc.delete_user(10)
    assert rk.key.public_key not in server_conf(fake_container)
    assert await svc.db.get_user(10) is None


async def test_admin_needs_no_subscription(svc):
    admin, _ = await svc.db.touch_user(ADMIN, "admin", "Admin")
    rk = await new_device(svc, admin, "📱 Телефон")
    assert rk.key.enabled
    assert await svc.expire_subscriptions() == []


async def test_inactive_users(svc):
    await svc.db.touch_user(10, "old", "Old")
    await svc.db.touch_user(11, "fresh", "Fresh")
    await svc.db.update_user(10, last_seen=now() - 40 * 86400)
    assert [u.tg_id for u in await svc.db.inactive_users(30)] == [10]
    await svc.extend(10, 5)  # с подпиской — не считается заброшенным
    assert await svc.db.inactive_users(30) == []


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


async def test_reminders_and_expiry_notifications(svc, fake_container):
    from bot.scheduler import check_subscriptions

    bot = FakeBot()
    user, _ = await svc.db.touch_user(10, "u10", "User")
    await svc.extend(10, 30)
    rk = await new_device(svc, user, "📱 Телефон")

    await svc.db.update_user(10, sub_until=now() + 2 * 86400)
    await check_subscriptions(bot, svc)
    await check_subscriptions(bot, svc)  # повторно не напоминает
    assert len(bot.sent) == 1 and "заканчивается" in bot.sent[0][1]

    await svc.db.update_user(10, sub_until=now() + 3600)
    await check_subscriptions(bot, svc)
    assert len(bot.sent) == 2

    await svc.db.update_user(10, sub_until=now() - 1)
    await check_subscriptions(bot, svc)
    await check_subscriptions(bot, svc)
    assert len(bot.sent) == 3 and "закончилась" in bot.sent[2][1]
    assert rk.key.public_key not in server_conf(fake_container)

    # после продления напоминания снова работают
    await svc.extend(10, 1)
    assert (await svc.db.get_user(10)).notified == 0


async def test_huge_values_are_rejected_or_clamped(svc):
    from bot.service import MAX_DAYS, MAX_DEVICES, MAX_SUB_UNTIL
    from bot.utils import fmt_dt

    await svc.db.touch_user(10, "u", "U")
    for days in (99999999999999999999999, MAX_DAYS + 1, -(10**30)):
        with pytest.raises(ServiceError):
            await svc.extend(10, days)
    assert (await svc.db.get_user(10)).sub_until is None  # ничего не сохранилось

    for _ in range(50):  # даже много продлений подряд не уходят за 2100 год
        await svc.extend(10, MAX_DAYS)
    user = await svc.db.get_user(10)
    assert user.sub_until == MAX_SUB_UNTIL and fmt_dt(user.sub_until).startswith("01.01.2100")

    await svc.set_sub_until(10, -(10**12))
    assert (await svc.db.get_user(10)).sub_until == 0
    await svc.set_device_limit(10, 10**30)
    assert (await svc.db.get_user(10)).device_limit == MAX_DEVICES

    # если в базе всё же оказалось мусорное значение — экран не ломается
    assert fmt_dt(10**17) == "∞" and fmt_dt(-(10**15)) == "—"


# ---------- несколько серверов (демо-эмуляция) ----------


@pytest.fixture
async def multi(tmp_path):
    settings = Settings(bot_token="x", admin_ids={ADMIN}, db_path=str(tmp_path / "bot.db"), awg_mock=True)
    db = Database(settings.db_path)
    await db.connect()
    service = VpnService(settings, db)
    await service.start()
    yield service
    await service.close()
    await db.close()


def mock_conf(server) -> str:
    return open(os.path.join(server.conn[5:], "opt/amnezia/awg/awg0.conf"), encoding="utf-8").read()


async def test_demo_creates_three_locations(multi):
    servers = await multi.db.servers()
    assert [s.title for s in servers] == ["🇩🇪 Германия", "🇳🇱 Нидерланды", "🇫🇮 Финляндия"]
    assert len(await multi.available_servers()) == 3


async def test_devices_on_different_servers_share_limit(multi):
    de, nl, fi = await multi.db.servers()
    user, _ = await multi.db.touch_user(10, "u", "U")
    await multi.extend(10, 30, devices=2)
    a = await multi.create_device(user, "Телефон", de.id)
    b = await multi.create_device(user, "Ноутбук", nl.id)
    assert a.key.public_key in mock_conf(de) and a.key.public_key not in mock_conf(nl)
    assert b.key.public_key in mock_conf(nl)
    assert decode_vpn_url(b.vpn_url)["hostName"] == nl.host
    # лимит общий на все серверы
    with pytest.raises(ServiceError, match="лимит"):
        await multi.create_device(user, "Планшет", fi.id)

    # окончание подписки отключает ключи на всех серверах
    await multi.db.update_user(10, sub_until=now() - 1)
    await multi.expire_subscriptions()
    assert a.key.public_key not in mock_conf(de) and b.key.public_key not in mock_conf(nl)
    await multi.extend(10, 30)
    assert a.key.public_key in mock_conf(de) and b.key.public_key in mock_conf(nl)


async def test_move_device(multi):
    de, nl, _ = await multi.db.servers()
    user, _ = await multi.db.touch_user(10, "u", "U")
    await multi.extend(10, 30)
    rk = await multi.create_device(user, "Телефон", de.id)
    moved = await multi.move_device(rk.key, nl.id)
    assert moved.key.server_id == nl.id and moved.location == "🇳🇱 Нидерланды"
    assert rk.key.public_key not in mock_conf(de) and rk.key.public_key in mock_conf(nl)
    assert decode_vpn_url(moved.vpn_url)["hostName"] == nl.host
    with pytest.raises(ServiceError):
        await multi.move_device(moved.key, nl.id)


async def test_server_down_is_hidden_and_reconciled(multi):
    de, nl, _ = await multi.db.servers()
    user, _ = await multi.db.touch_user(10, "u", "U")
    await multi.extend(10, 30, devices=2)
    rk = await multi.create_device(user, "Телефон", de.id)

    # «роняем» Германию
    down = os.path.join(de.conn[5:], "DOWN")
    open(down, "w").close()
    await multi.pool.drop(de.id)
    checks = {c.server.id: c for c in await multi.check_servers()}
    assert not checks[de.id].ok and checks[de.id].changed
    assert de.id not in {s.id for s in await multi.available_servers()}
    with pytest.raises(ServiceError):
        await multi.create_device(user, "Ноутбук", de.id)  # недоступная локация не выбирается

    # пока сервер лежит, клиент переезжает в Нидерланды — старый пир снять не получилось
    moved = await multi.move_device(rk.key, nl.id)
    assert moved.key.server_id == nl.id

    # сервер поднялся: проверка сама снимает «лишний» пир
    os.remove(down)
    checks = {c.server.id: c for c in await multi.check_servers()}
    assert checks[de.id].ok and checks[de.id].changed
    assert rk.key.public_key not in mock_conf(de)


async def test_add_and_delete_server(multi, tmp_path):
    row = await multi.add_server("Польша", "🇵🇱", "9.9.9.9", f"mock:{tmp_path / 'pl'}")
    assert row.status_ok and len(await multi.db.servers()) == 4
    user, _ = await multi.db.touch_user(10, "u", "U")
    await multi.extend(10, 30)
    rk = await multi.create_device(user, "Телефон", row.id)
    with pytest.raises(ServiceError, match="устройства"):
        await multi.delete_server(row)
    await multi.delete_device(rk.key)
    await multi.delete_server(row)
    assert len(await multi.db.servers()) == 3
    with pytest.raises(ServiceError, match="Неверное подключение"):
        await multi.add_server("X", "🏳", "1.1.1.1", "root@host:notaport")


async def test_orphan_keys_from_old_version_attach_to_first_server(tmp_path):
    from bot.awg.mock import MockAwgServer

    settings = Settings(bot_token="x", server_host="127.0.0.1", db_path=str(tmp_path / "bot.db"))
    db = Database(settings.db_path)
    await db.connect()
    await db.touch_user(10, "u", "U")
    old = await db.add_key(10, "Старый ключ", "PUBKEY=", "PRIVKEY=", "10.8.1.5")  # без server_id
    assert old.server_id is None
    service = VpnService(settings, db, MockAwgServer(str(tmp_path / "mock")))
    await service.start()
    server = (await db.servers())[0]
    assert (await db.get_key(old.id)).server_id == server.id
    await service.close()
    await db.close()


class FakeDocBot:
    def __init__(self):
        self.docs = []

    async def send_document(self, chat_id, document, **kw):
        self.docs.append((chat_id, document))


async def test_backup(multi):
    import io
    import sqlite3
    import zipfile

    from bot.scheduler import send_backup

    await multi.db.touch_user(10, "u", "U")
    multi.pool.ssh.ensure_key()
    bot = FakeDocBot()
    assert await send_backup(bot, multi)  # первый раз — сразу
    assert not await send_backup(bot, multi)  # второй — только через BACKUP_HOURS
    assert await send_backup(bot, multi, force=True)  # /backup
    assert len(bot.docs) == 2 and bot.docs[0][0] == ADMIN
    z = zipfile.ZipFile(io.BytesIO(bot.docs[0][1].data))
    assert {"data/bot.db", "data/ssh/id_ed25519"} <= set(z.namelist())
    restored = os.path.join(os.path.dirname(multi.settings.db_path), "restored.db")
    with open(restored, "wb") as f:
        f.write(z.read("data/bot.db"))
    assert sqlite3.connect(restored).execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_parse_ssh_target():
    from bot.awg.runner import parse_ssh_target

    assert parse_ssh_target("5.6.7.8") == ("root", "5.6.7.8", 22)
    assert parse_ssh_target("admin@5.6.7.8:2222") == ("admin", "5.6.7.8", 2222)
    assert parse_ssh_target("root@[2001:db8::1]:22") == ("root", "2001:db8::1", 22)
    for bad in ("root@", "host:port", "host:70000"):
        with pytest.raises(ValueError):
            parse_ssh_target(bad)


def test_ssh_key_generation(tmp_path):
    from bot.awg.runner import SshKeyStore

    store = SshKeyStore(str(tmp_path / "ssh"))
    pub = store.ensure_key()
    assert pub.startswith("ssh-ed25519 ") and store.ensure_key() == pub  # второй раз не пересоздаётся
    assert oct(os.stat(store.key_path).st_mode)[-3:] == "600"
    store.remember("1.2.3.4", 22, "ssh-ed25519 AAA")
    assert store.known_key("1.2.3.4", 22) == "ssh-ed25519 AAA"
    store.forget("1.2.3.4", 22)
    assert store.known_key("1.2.3.4", 22) is None


# ---------- VLESS (Xray от Amnezia) ----------


def xray_conf(root) -> dict:
    return json.loads((root / "opt" / "amnezia" / "xray" / "server.json").read_text())


def xray_ids(root) -> list[str]:
    return [c["id"] for c in xray_conf(root)["inbounds"][0]["settings"]["clients"]]


async def test_local_server_has_both_protocols(svc):
    server = (await svc.db.servers())[0]
    assert server.protocol_list == ["awg", "vless"]


async def test_vless_device(svc, fake_container):
    from urllib.parse import parse_qs, urlparse

    from .conftest import XRAY_PUB, XRAY_SID

    user, _ = await svc.db.touch_user(10, "u10", "User")
    await svc.extend(10, 30, devices=2)
    server = (await svc.db.servers())[0]
    rk = await svc.create_device(user, "Ноутбук", server.id, "vless")
    assert rk.is_vless and rk.conf == "" and rk.key.ip == ""
    url = urlparse(rk.vpn_url)
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.scheme == "vless" and url.username == rk.key.public_key
    assert url.hostname == "203.0.113.10" and url.port == 443
    assert q == {
        "type": "tcp", "encryption": "none", "security": "reality", "flow": "xtls-rprx-vision",
        "sni": "www.googletagmanager.com", "fp": "chrome", "pbk": XRAY_PUB, "sid": XRAY_SID,
    }
    cfg = xray_conf(fake_container)
    client = cfg["inbounds"][0]["settings"]["clients"][-1]
    assert client == {"id": rk.key.public_key, "email": rk.key.public_key, "flow": "xtls-rprx-vision"}
    assert cfg["inbounds"][0]["settings"]["clients"][0]["id"].startswith("11111111")  # клиент из приложения цел
    assert "StatsService" in cfg["api"]["services"]  # включена статистика
    assert (fake_container / "restarts").read_text().split() == ["amnezia-xray"]
    table = json.loads((fake_container / "opt/amnezia/xray/clientsTable").read_text())
    assert table[0] == {"clientId": rk.key.public_key, "userData": table[0]["userData"]}
    assert table[0]["userData"]["clientName"] == "Ноутбук | @u10"
    assert rk.qr_png()

    # трафик из Stats API
    await svc.collect_traffic()
    t = await svc.db.traffic(key_id=rk.key.id)
    assert (t.rx, t.tx) == (4096, 8192)
    assert (await svc.db.get_key(rk.key.id)).last_handshake > 0

    # окончание подписки — клиент снят; продление — тот же UUID вернулся
    await svc.db.update_user(10, sub_until=now() - 1)
    await svc.expire_subscriptions()
    assert rk.key.public_key not in xray_ids(fake_container)
    await svc.extend(10, 30)
    assert rk.key.public_key in xray_ids(fake_container)
    assert (await svc.render(await svc.db.get_key(rk.key.id))).vpn_url == rk.vpn_url

    await svc.delete_device(await svc.db.get_key(rk.key.id))
    assert rk.key.public_key not in xray_ids(fake_container)


async def test_vless_falls_back_when_xray_rejects_stats_api(svc, fake_container, monkeypatch):
    monkeypatch.setenv("FAKE_XRAY_REJECT_API", "1")
    user, _ = await svc.db.touch_user(10, "u", "U")
    await svc.extend(10, 30)
    server = (await svc.db.servers())[0]
    rk = await svc.create_device(user, "Телефон", server.id, "vless")
    cfg = xray_conf(fake_container)
    assert rk.key.public_key in xray_ids(fake_container) and "api" not in cfg


async def test_vless_broken_config_is_not_applied(svc, fake_container):
    xray_dir = fake_container / "opt" / "amnezia" / "xray"
    from bot.awg.xray import XrayServer

    xs = XrayServer(container="amnezia-xray", docker=os.path.join(FAKEBIN, "docker"))
    original = (xray_dir / "server.json").read_text()
    # «Xray» отклоняет всё: подменяем проверку
    async def always_bad(path):
        return "config error"
    xs._config_ok = always_bad
    from bot.awg.server import AwgError

    with pytest.raises(AwgError, match="не принял"):
        await xs.add_client("x")
    assert (xray_dir / "server.json").read_text() == original  # рабочий конфиг не тронут
    assert not (fake_container / "restarts").exists()


async def test_vless_unavailable_protocol(svc, fake_container):
    import shutil

    shutil.rmtree(fake_container / "opt" / "amnezia" / "xray")
    await svc.pool.drop((await svc.db.servers())[0].id)
    await svc.check_servers()
    server = (await svc.db.servers())[0]
    assert server.protocol_list == ["awg"] and server.status_ok  # AWG работает, VLESS пропал
    assert "VLESS" in (server.last_error or "")
    user, _ = await svc.db.touch_user(10, "u", "U")
    await svc.extend(10, 30)
    with pytest.raises(ServiceError, match="VLESS"):
        await svc.create_device(user, "Телефон", server.id, "vless")


async def test_multi_vless_move_and_preferred_server(multi):
    de, nl, _ = await multi.db.servers()
    assert de.protocol_list == ["awg", "vless"]
    user, _ = await multi.db.touch_user(10, "u", "U")
    plan = (await multi.db.plans())[0]
    p = await multi.db.create_payment(10, plan, "text", "чек", server_id=nl.id)
    await multi.confirm_payment(p.id, ADMIN)
    user = await multi.db.get_user(10)
    assert user.server_id == nl.id and (await multi.user_server(user)).id == nl.id

    rk = await multi.create_device(user, "Ноутбук", nl.id, "vless")
    moved = await multi.move_device(rk.key, de.id)
    assert moved.key.protocol == "vless" and moved.key.public_key == rk.key.public_key
    from bot.awg.mock import MockXrayServer

    de_ids = MockXrayServer.client_ids(json.load(open(os.path.join(de.conn[5:], "opt/amnezia/xray/server.json"))))
    nl_ids = MockXrayServer.client_ids(json.load(open(os.path.join(nl.conn[5:], "opt/amnezia/xray/server.json"))))
    assert rk.key.public_key in de_ids and rk.key.public_key not in nl_ids
    assert "@10.0.0.1:443" in moved.vpn_url


# ---------- доп. слоты устройств ----------


async def test_extra_device_slot(svc, fake_container):
    user, _ = await svc.db.touch_user(10, "u", "U")
    plan = (await svc.db.plans())[0]  # 1 месяц, 2 устройства
    p = await svc.db.create_payment(10, plan, "text", "чек")
    await svc.confirm_payment(p.id, ADMIN)
    user = await svc.db.get_user(10)
    a = await new_device(svc, user, "Телефон")
    b = await new_device(svc, user, "Ноутбук")
    with pytest.raises(ServiceError, match="лимит"):
        await new_device(svc, user, "Планшет")

    # покупка слота: +1 устройство на 30 дней
    sp = await svc.db.create_slot_payment(10, 100, 30, "text", "чек за слот")
    assert sp.kind == "slot" and sp.title == "Доп. слот устройства"
    res = await svc.confirm_payment(sp.id, ADMIN)
    assert res.referrer is None
    assert await svc.device_limit(await svc.db.get_user(10)) == 3
    c = await new_device(svc, user, "Планшет")
    slot = (await svc.db.user_slots(10))[0]
    assert slot.until >= now() + 29 * 86400

    # слот закончился — самое новое устройство отключается, остальные работают
    await svc.db.update_slot(slot.id, until=now() - 1)
    expired = await svc.expire_slots()
    assert [s.id for s in expired] == [slot.id]
    assert [k.enabled for k in await svc.db.user_keys(10)] == [1, 1, 0]
    assert c.key.public_key not in server_conf(fake_container)
    assert await svc.expire_slots() == []  # повторно не уведомляем

    # продление того же слота (оплата с slot_id) — устройство снова работает
    rp = await svc.db.create_slot_payment(10, 100, 30, "text", "продлеваю", slot_id=slot.id)
    assert rp.title == "Продление доп. слота устройства"
    await svc.confirm_payment(rp.id, ADMIN)
    assert len(await svc.db.user_slots(10)) == 1  # не новый слот, а продлённый
    assert all(k.enabled for k in await svc.db.user_keys(10))
    assert c.key.public_key in server_conf(fake_container)
    assert a.key.enabled and b.key.enabled

    # оплата слота не отменяет «новичка»-статус и не считается оплатой подписки
    assert await svc.db.has_paid(10)  # подписка оплачена выше
    await svc.db.touch_user(11, "n", "N")
    await svc.confirm_payment((await svc.db.create_slot_payment(11, 100, 30, "text", "x")).id, ADMIN)
    assert not await svc.db.has_paid(11)


async def test_admin_slots(svc):
    await svc.db.touch_user(10, "u", "U")
    user = await svc.extend(10, 30, devices=2)
    slot = await svc.add_slot(10, 30)
    assert slot.source == "admin" and await svc.device_limit(user) == 3
    await svc.extend_slot(slot.id, 30)
    assert (await svc.db.get_slot(slot.id)).until >= now() + 59 * 86400
    await svc.extend_slot(slot.id, -60)  # уменьшение срока — слот истёк
    assert await svc.device_limit(user) == 2
    second = await svc.add_slot(10, 7)
    await svc.remove_slot(second.id)
    assert await svc.db.get_slot(second.id) is None
    with pytest.raises(ServiceError):
        await svc.add_slot(10, 0)
    with pytest.raises(ServiceError):
        await svc.add_slot(10, 10**9)


async def test_slot_notifications(svc):
    from bot.scheduler import check_subscriptions

    bot = FakeBot()
    await svc.db.touch_user(10, "u", "U")
    await svc.extend(10, 60)
    slot = await svc.add_slot(10, 2)  # закончится через 2 дня
    await check_subscriptions(bot, svc)
    await check_subscriptions(bot, svc)
    texts = [t for _, t in bot.sent]
    assert sum("слот устройства заканчивается" in t for t in texts) == 1
    await svc.db.update_slot(slot.id, until=now() - 1)
    await check_subscriptions(bot, svc)
    await check_subscriptions(bot, svc)
    texts = [t for _, t in bot.sent]
    assert sum("слот устройства закончился" in t for t in texts) == 1
