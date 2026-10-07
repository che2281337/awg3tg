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
        trial_days=3, trial_devices=1, ref_bonus_days=7,
    )
    db = Database(settings.db_path)
    await db.connect()
    service = VpnService(settings, db, AwgServer(docker=os.path.join(FAKEBIN, "docker")))
    await service.start()
    yield service
    await db.close()


def server_conf(root) -> str:
    return (root / "opt" / "amnezia" / "awg" / "awg0.conf").read_text()


async def test_default_plans_seeded(svc):
    plans = await svc.db.plans()
    assert len(plans) == 4 and plans[0].days == 30


async def test_no_subscription_no_device(svc):
    user, created = await svc.db.touch_user(10, "u10", "User")
    assert created
    with pytest.raises(ServiceError):
        await svc.create_device(user, "📱 Телефон")


async def test_trial_and_devices(svc, fake_container):
    user, _ = await svc.db.touch_user(10, "u10", "User")
    assert svc.trial_available(user)
    user = await svc.start_trial(user)
    assert user.active and user.trial_used and user.device_limit == 1
    assert not svc.trial_available(user)
    with pytest.raises(ServiceError):
        await svc.start_trial(user)

    rk = await svc.create_device(user, "📱 Телефон")
    assert rk.key.ip == "10.8.1.2"
    assert rk.filename == "AmneziaWG_1.conf"
    assert rk.key.public_key in server_conf(fake_container)
    data = decode_vpn_url(rk.vpn_url)
    assert json.loads(data["containers"][0]["awg"]["last_config"])["server_pub_key"] == SERVER_PUB
    table = json.loads((fake_container / "opt/amnezia/awg/clientsTable").read_text())
    assert table[0]["userData"]["clientName"] == "📱 Телефон | @u10"

    with pytest.raises(ServiceError, match="лимит"):
        await svc.create_device(user, "💻 Компьютер")


async def test_expiry_disables_and_renewal_restores_same_key(svc, fake_container):
    user, _ = await svc.db.touch_user(10, "u10", "User")
    user = await svc.extend(10, 30, devices=2)
    rk1 = await svc.create_device(user, "📱 Телефон")
    rk2 = await svc.create_device(user, "💻 Компьютер")

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
    rk_other = await svc.create_device(other, "📱 Телефон")
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
        await svc.create_device(user, name)
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
    plan = (await svc.db.plans())[1]  # 3 месяца, 3 устройства
    friend = await svc.db.get_user(11)
    p = await svc.db.create_payment(11, plan, "text", "оплатил")
    res = await svc.confirm_payment(p.id, ADMIN)
    assert res.user.active and res.user.device_limit == 3
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
    rk = await svc.create_device(user, "📱 Телефон")
    await svc.collect_traffic()  # fake awg: rx=1024, tx=2048
    await svc.collect_traffic()  # счётчики не изменились — дельта 0
    t = await svc.db.traffic(tg_id=10)
    assert (t.rx, t.tx) == (1024, 2048)
    # имитация перезапуска контейнера: счётчики стали меньше прошлых
    await svc.db.update_key(rk.key.id, last_rx=5000, last_tx=5000)
    await svc.collect_traffic()
    t = await svc.db.traffic(key_id=rk.key.id)
    assert (t.rx, t.tx) == (2048, 4096)
    key = await svc.db.get_key(rk.key.id)
    assert key.last_handshake == 1700000000


async def test_ban_and_delete(svc, fake_container):
    user, _ = await svc.db.touch_user(10, "u10", "User")
    await svc.extend(10, 30)
    rk = await svc.create_device(user, "📱 Телефон")
    await svc.ban(10)
    assert rk.key.public_key not in server_conf(fake_container)
    await svc.unban(10)
    assert rk.key.public_key in server_conf(fake_container)
    await svc.delete_user(10)
    assert rk.key.public_key not in server_conf(fake_container)
    assert await svc.db.get_user(10) is None


async def test_admin_needs_no_subscription(svc):
    admin, _ = await svc.db.touch_user(ADMIN, "admin", "Admin")
    rk = await svc.create_device(admin, "📱 Телефон")
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
    rk = await svc.create_device(user, "📱 Телефон")

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


async def test_mock_server(tmp_path):
    from bot.awg.mock import MockAwgServer

    settings = Settings(bot_token="x", server_host="127.0.0.1", db_path=str(tmp_path / "bot.db"))
    db = Database(settings.db_path)
    await db.connect()
    service = VpnService(settings, db, MockAwgServer(str(tmp_path / "mock")))
    await service.start()
    user, _ = await db.touch_user(10, "u", "U")
    await service.extend(10, 30, devices=2)
    rk = await service.create_device(user, "📱 Телефон")
    assert "HeaderProtectionKey" in rk.conf and rk.key.ip == "10.8.1.1"
    for _ in range(5):
        await service.collect_traffic()
    await service.delete_device(rk.key)
    assert rk.key.public_key not in (tmp_path / "mock/opt/amnezia/awg/awg0.conf").read_text()
    await db.close()


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
