"""Получение токена ЮMoney для автоприёма оплат (один раз).

Запуск на сервере:  docker exec -it awg-bot python -m bot.yoomoney_auth
Локально:           python -m bot.yoomoney_auth
"""

from __future__ import annotations

import asyncio
import urllib.parse

import aiohttp

from .yoomoney import API, SCOPE, YooMoney, YooMoneyError

REDIRECT = "https://example.com/yoomoney"

INTRO = f"""
Получение токена ЮMoney
=======================
1. Откройте https://yoomoney.ru/myservices/new (войдите в свой кошелёк) и заполните:
     Название         — любое, например «VPN бот»
     Адрес сайта      — любой, например https://t.me/ваш_бот
     Почта            — ваша почта
     Redirect URI     — {REDIRECT}
     Notification URI — оставьте пустым
   Галочку «Проверять подлинность приложения (OAuth2 client_secret)» можно не ставить.
2. Нажмите «Подтвердить» и скопируйте client_id.
"""


def _extract_code(text: str) -> str:
    text = text.strip()
    if "code=" in text:
        query = urllib.parse.urlparse(text).query or text.split("?", 1)[-1]
        return urllib.parse.parse_qs(query).get("code", [""])[0]
    return text


async def main() -> None:
    print(INTRO)
    client_id = input("client_id: ").strip()
    secret = input("client_secret (Enter — если не включали): ").strip()
    params = {"client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPE}
    url = f"{API}/oauth/authorize?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"
    async with aiohttp.ClientSession() as session:
        # ЮMoney ждёт POST: берём адрес страницы входа, на который он перенаправит
        try:
            async with session.post(f"{API}/oauth/authorize", data=params) as resp:
                if resp.status == 200 and "yoomoney.ru" in str(resp.url):
                    url = str(resp.url)
        except aiohttp.ClientError:
            pass
        print(
            "\n3. Откройте ссылку в браузере и разрешите доступ (только просмотр баланса и истории — "
            "переводить деньги бот не сможет):\n\n" + url + "\n\n"
            f"4. Браузер откроет {REDIRECT}?code=… (страница может не загрузиться — это нормально).\n"
            "   Скопируйте адрес из адресной строки целиком."
        )
        code = _extract_code(input("\nАдрес или code: "))
        if not code:
            raise SystemExit("Не нашёл code в адресе")
        data = {"code": code, "client_id": client_id, "grant_type": "authorization_code", "redirect_uri": REDIRECT}
        if secret:
            data["client_secret"] = secret
        async with session.post(f"{API}/oauth/token", data=data) as resp:
            body = await resp.json(content_type=None)
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        raise SystemExit(f"ЮMoney не выдал токен: {body}")

    ym = YooMoney(token)
    try:
        wallet = await ym.account()
    except YooMoneyError as e:
        wallet = ""
        print(f"Токен получен, но проверить его не удалось: {e}")
    finally:
        await ym.close()
    print("\n✅ Готово! Добавьте в .env:\n")
    print(f"YOOMONEY_TOKEN={token}")
    if wallet:
        print(f"YOOMONEY_WALLET={wallet}")
    print("\nи перезапустите бота. Токен никому не показывайте.")


if __name__ == "__main__":
    asyncio.run(main())
