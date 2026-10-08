import urllib.parse

import pytest
from aiohttp import web

from bot.db import now
from bot.service import INVOICE_TTL
from bot.yoomoney import Operation, YooMoney, YooMoneyError, quickpay_url

from .test_service import ADMIN, svc  # noqa: F401  (фикстура)


class FakeYooMoney:
    """История кошелька в памяти вместо API ЮMoney."""

    def __init__(self):
        self.ops: list[Operation] = []
        self.calls = 0

    def pay(self, label, amount, op_id=None, status="success"):
        self.ops.insert(0, Operation(op_id or f"op{len(self.ops) + 1}", status, "in", amount, label))

    async def operations(self, label=None, records=100):
        self.calls += 1
        return [o for o in self.ops if label is None or o.label == label][:records]

    async def close(self):
        pass


@pytest.fixture
def ym(svc):  # noqa: F811
    fake = FakeYooMoney()
    svc.yoomoney = fake
    svc.yoomoney_wallet = "4100111222333"
    return fake


def test_quickpay_url():
    url = quickpay_url("4100111", 50, "abc", "VPN: 1 месяц", "AC", "https://t.me/bot")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert url.startswith("https://yoomoney.ru/quickpay/confirm.xml?")
    assert q["receiver"] == ["4100111"] and q["sum"] == ["50"] and q["label"] == ["abc"]
    assert q["paymentType"] == ["AC"] and q["targets"] == ["VPN: 1 месяц"] and q["successURL"] == ["https://t.me/bot"]


async def test_plan_autopay_with_newbie_discount_and_referral(svc, ym):  # noqa: F811
    await svc.db.touch_user(1000, "ref", "Ref")
    user, _ = await svc.db.touch_user(10, "u", "U")
    await svc.db.update_user(10, referrer_id=1000)
    month = (await svc.db.plans())[0]

    inv = await svc.plan_invoice(user, month)
    assert inv.status == "invoice" and inv.amount == 50 and inv.method == "yoomoney" and inv.label
    assert (await svc.plan_invoice(user, month)).id == inv.id  # повторное открытие — тот же счёт
    assert "label=" + inv.label in svc.pay_url(inv)

    assert await svc.check_invoices() == []  # ещё не оплачен
    assert await svc.db.has_paid(10) is False
    assert await svc.db.user_payments(10) == []  # неоплаченные счета не засоряют историю

    ym.pay(inv.label, 48.5)  # минус комиссия ЮMoney 3%
    ym.pay("чужая-метка", 1000)
    [res] = await svc.check_invoices()
    assert res.result and not res.review
    assert res.payment.status == "paid" and res.payment.admin_id == 0 and res.payment.paid_amount == 48.5
    u = await svc.db.get_user(10)
    assert u.active and u.device_limit == 2
    assert res.result.referrer and res.result.referrer.tg_id == 1000
    assert await svc.db.has_paid(10)

    assert await svc.check_invoices() == []  # операция не засчитывается второй раз
    assert await svc.check_invoices(inv) == []
    # новый счёт после оплаты — уже без скидки
    inv2 = await svc.plan_invoice(await svc.db.get_user(10), month)
    assert inv2.id != inv.id and inv2.amount == 100


async def test_underpaid_goes_to_admin(svc, ym):  # noqa: F811
    user, _ = await svc.db.touch_user(10, "u", "U")
    year = (await svc.db.plans())[-1]
    inv = await svc.plan_invoice(user, year)
    ym.pay(inv.label, 1.0)  # клиент поправил сумму в ссылке
    [res] = await svc.check_invoices(inv)
    assert res.review and res.result is None
    p = await svc.db.get_payment(inv.id)
    assert p.status == "pending" and "1.00" in p.receipt
    assert not (await svc.db.get_user(10)).active
    # админ может подтвердить вручную
    await svc.confirm_payment(inv.id, ADMIN)
    assert (await svc.db.get_user(10)).active


async def test_slot_autopay_and_ignored_operations(svc, ym):  # noqa: F811
    user, _ = await svc.db.touch_user(10, "u", "U")
    await svc.extend(10, 30, devices=2)
    inv = await svc.slot_invoice(user, None)
    assert inv.kind == "slot" and inv.amount == 100
    ym.pay(inv.label, 97, status="refused")
    ym.ops.insert(0, Operation("out1", "success", "out", 97, inv.label))
    assert await svc.check_invoices() == []
    ym.pay(inv.label, 97)
    [res] = await svc.check_invoices()
    assert res.result and await svc.device_limit(await svc.db.get_user(10)) == 3


async def test_expired_invoice_still_counts(svc, ym):  # noqa: F811
    user, _ = await svc.db.touch_user(10, "u", "U")
    inv = await svc.plan_invoice(user, (await svc.db.plans())[0])
    await svc.db.c.execute("UPDATE payments SET created_at = ? WHERE id = ?", (now() - INVOICE_TTL - 10, inv.id))
    await svc.db.c.commit()
    calls = ym.calls
    assert await svc.check_invoices() == []
    assert ym.calls == calls  # открытых счетов нет — ЮMoney не дёргаем
    assert (await svc.db.get_payment(inv.id)).status == "expired"
    # новый счёт создаётся, а оплата по старой ссылке всё равно засчитывается
    fresh = await svc.plan_invoice(user, (await svc.db.plans())[0])
    assert fresh.id != inv.id
    ym.pay(inv.label, 50)
    [res] = await svc.check_invoices()
    assert res.payment.id == inv.id and res.result


async def test_http_client():
    seen = {}

    async def history(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["form"] = dict(await request.post())
        if seen["auth"] != "Bearer good":
            return web.Response(status=401)
        return web.json_response(
            {
                "operations": [
                    {"operation_id": "1", "status": "success", "direction": "in", "amount": 97.0, "label": "L1",
                     "datetime": "2026-10-08T10:00:00Z"},
                    {"operation_id": "2", "status": "success", "direction": "in", "amount": "bad"},
                ]
            }
        )

    async def account(request):
        return web.json_response({"account": "4100111222333", "balance": 0})

    app = web.Application()
    app.router.add_post("/api/operation-history", history)
    app.router.add_post("/api/account-info", account)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        client = YooMoney("good", base_url=f"http://127.0.0.1:{port}")
        assert await client.account() == "4100111222333"
        [op] = await client.operations(label="L1", records=5)
        assert op == Operation("1", "success", "in", 97.0, "L1", "2026-10-08T10:00:00Z")
        assert seen["form"] == {"type": "deposition", "records": "5", "label": "L1"}
        await client.close()
        bad = YooMoney("bad", base_url=f"http://127.0.0.1:{port}")
        with pytest.raises(YooMoneyError, match="Токен"):
            await bad.operations()
        await bad.close()
    finally:
        await runner.cleanup()
