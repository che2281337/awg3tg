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

DEVICE_TYPES = {
    "phone": "📱 Телефон",
    "pc": "💻 Компьютер",
    "tablet": "📟 Планшет",
    "tv": "📺 Телевизор",
    "router": "📡 Роутер",
}


# ---------- callback data ----------


class Menu(CallbackData, prefix="m"):
    action: str  # profile | devices | plans | ref | trial | add | help


class Dev(CallbackData, prefix="d"):
    action: str  # view | key | ren | del | delok
    id: int


class DevType(CallbackData, prefix="dt"):
    kind: str


class Buy(CallbackData, prefix="buy"):
    action: str  # plan | paid
    plan_id: int


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
    action: str  # target | send | cancel
    target: str = "all"


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


def renew_kb() -> InlineKeyboardMarkup:
    return ikb([[("💳 Продлить подписку", Menu(action="plans").pack())]])


def back(cb: CallbackData | str, text: str = "⬅️ Назад") -> tuple[str, str]:
    return text, cb if isinstance(cb, str) else cb.pack()
