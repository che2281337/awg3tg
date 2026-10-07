from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

BTN_PROFILE = "👤 Профиль"
BTN_DEVICES = "🔑 Мои устройства"
BTN_PLANS = "💳 Тарифы"
BTN_REF = "👥 Пригласить друга"
BTN_HELP = "❓ Помощь"
BTN_ADMIN = "🛠 Админка"
MENU_TEXTS = (BTN_PROFILE, BTN_DEVICES, BTN_PLANS, BTN_REF, BTN_HELP, BTN_ADMIN)



# ---------- callback data ----------


class Menu(CallbackData, prefix="m"):
    action: str  # profile | devices | plans | ref | add | help


class Dev(CallbackData, prefix="d"):
    action: str  # view | key | ren | del | delok
    id: int


class Loc(CallbackData, prefix="loc"):
    """Выбор локации: new — для нового устройства, pick/move — перенос устройства `key`."""

    action: str  # new | pick | move | home (сервер по умолчанию в профиле)
    key: int = 0
    sid: int = 0


class ASrv(CallbackData, prefix="as"):
    """Админ: серверы."""

    action: str  # list | view | add | edit | toggle | check | del | delok | resetkey | moveall | moveallok
    id: int = 0
    arg: int = 0


class AMove(CallbackData, prefix="amv"):
    """Админ: перенос устройства `key` на сервер `sid` (0 — выбрать)."""

    key: int
    sid: int = 0


class Buy(CallbackData, prefix="buy"):
    action: str  # plan | srv | paid
    plan_id: int
    sid: int = 0  # сервер, выбранный при покупке


class SlotCb(CallbackData, prefix="sl"):
    """Доп. слот устройства: buy — купить новый, renew — продлить слот id, paid — «Я оплатил»."""

    action: str
    id: int = 0


class ASlot(CallbackData, prefix="asl"):
    """Админ: доп. слоты пользователя uid (list | add | ext | del)."""

    action: str
    uid: int
    id: int = 0
    arg: int = 0


class DevProto(CallbackData, prefix="dp"):
    """Выбор протокола для нового устройства на сервере sid."""

    sid: int
    proto: str


class AAdd(CallbackData, prefix="aad"):
    """Админ: создать устройство пользователю uid на сервере sid протоколом proto."""

    uid: int
    sid: int = 0
    proto: str = ""


class Adm(CallbackData, prefix="a"):
    action: str
    arg: int = 0


class UList(CallbackData, prefix="ul"):
    flt: str
    page: int = 0


class AU(CallbackData, prefix="au"):
    """Действия админа над пользователем."""

    action: str
    uid: int
    arg: int = 0


class AK(CallbackData, prefix="ak"):
    """Действия админа над ключом пользователя."""

    action: str  # view | key | del | delok
    id: int


class APay(CallbackData, prefix="ap"):
    action: str  # view | ok | no
    id: int


class APlan(CallbackData, prefix="pl"):
    action: str  # view | edit | toggle | del | add
    id: int = 0


class Bcast(CallbackData, prefix="bc"):
    action: str  # target | stop
    target: str = "all"  # all | active | expired | s<id> (клиенты сервера)


# ---------- клавиатуры ----------


def main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_DEVICES)],
        [KeyboardButton(text=BTN_PLANS), KeyboardButton(text=BTN_REF)],
        [KeyboardButton(text=BTN_HELP)],
    ]
    if is_admin:
        rows[-1].append(KeyboardButton(text=BTN_ADMIN))
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def ikb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """Inline-клавиатура из [(текст, callback_data)]."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows if row]
    )


def slot_kb(slot_id: int, price: int, currency: str) -> InlineKeyboardMarkup:
    return ikb([[(f"🔁 Продлить слот — {price} {currency}", SlotCb(action="renew", id=slot_id).pack())]])


def renew_kb() -> InlineKeyboardMarkup:
    return ikb([[("💳 Продлить подписку", Menu(action="plans").pack())]])


def back(cb: CallbackData | str, text: str = "⬅️ Назад") -> tuple[str, str]:
    return text, cb if isinstance(cb, str) else cb.pack()


PROTO_BUTTONS = {
    "awg": "🛡 AmneziaWG — приложение AmneziaVPN",
    "vless": "⚡ VLESS — AmneziaVPN, v2rayNG, Hiddify, Streisand",
}
PROTO_ICONS = {"awg": "🛡", "vless": "⚡"}
