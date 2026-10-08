"""Получение токена ЮMoney для автоприёма оплат (один раз).

Запуск на сервере:  docker exec -it awg-bot python -m bot.yoomoney_auth
Локально:           python -m bot.yoomoney_auth
"""

from __future__ import annotations

import asyncio
import html
import os
import urllib.parse

import aiohttp

from .yoomoney import API, SCOPE, YooMoney, YooMoneyError

DEFAULT_REDIRECT = "https://example.com/yoomoney"

INTRO = f"""
Получение токена ЮMoney
=======================
1. Откройте https://yoomoney.ru/myservices/new (войдите в свой кошелёк) и заполните:
     Название         — любое, например «VPN бот»
     Адрес сайта      — любой, например https://t.me/ваш_бот
     Почта            — ваша почта
     Redirect URI     — {DEFAULT_REDIRECT}
     Notification URI — оставьте пустым
   Галочку «Проверять подлинность приложения (OAuth2 client_secret)» НЕ ставьте.
2. Нажмите «Подтвердить» и скопируйте client_id.
"""

ERRORS = {
    "invalid_request": "в запросе не хватает параметров или Redirect URI не совпадает с указанным в приложении",
    "invalid_scope": "ЮMoney не принял запрошенные права",
    "unauthorized_client": "неверный client_id или приложение заблокировано",
    "access_denied": "доступ не разрешён",
}

FORM = """<!doctype html>
<html><head><meta charset="utf-8"><title>ЮMoney</title></head>
<body onload="document.forms[0].submit()">
<form method="POST" action="{action}">
{fields}
<button type="submit">Перейти в ЮMoney</button>
</form></body></html>
"""


def _extract_code(text: str) -> str:
    text = text.strip()
    if "code=" in text:
        query = urllib.parse.urlparse(text).query or text.split("?", 1)[-1]
        return urllib.parse.parse_qs(query).get("code", [""])[0]
    return text


def _error_of(url: str) -> str:
    query = urllib.parse.urlparse(url).query
    return urllib.parse.parse_qs(query).get("error", [""])[0]


def _explain(error: str) -> str:
    return f"{error}: {ERRORS.get(error, 'см. сообщение ЮMoney')}"


def _save_form(params: dict) -> tuple[str, str]:
    """HTML-страница, которая отправит запрос авторизации из браузера (POST, как требует ЮMoney)."""
    fields = "\n".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">' for k, v in params.items()
    )
    page = FORM.format(action=f"{API}/oauth/authorize", fields=fields)
    path = "data/yoomoney_auth.html" if os.path.isdir("data") else "yoomoney_auth.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return page, os.path.abspath(path)


async def _login_url(session: aiohttp.ClientSession, params: dict) -> str:
    """ЮMoney принимает запрос авторизации только методом POST и отвечает переадресацией на страницу входа."""
    url = f"{API}/oauth/authorize"
    for _ in range(5):
        async with session.post(url, data=params, allow_redirects=False) as resp:
            location = resp.headers.get("Location", "")
            if resp.status not in (301, 302, 303, 307, 308) or not location:
                raise RuntimeError(f"ЮMoney ответил HTTP {resp.status} без переадресации")
        url = urllib.parse.urljoin(url, location)
        error = _error_of(url)
        if error:
            raise ValueError(_explain(error))
        if not url.startswith(f"{API}/oauth/authorize"):
            return url
        params = {}
    raise RuntimeError("слишком много переадресаций")


async def main() -> None:
    print(INTRO)
    client_id = input("client_id: ").strip()
    redirect = input(f"Redirect URI [Enter — {DEFAULT_REDIRECT}]: ").strip() or DEFAULT_REDIRECT
    secret = input("client_secret (Enter — если галочку не ставили): ").strip()
    params = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect, "scope": SCOPE}

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        try:
            url = await _login_url(session, params)
            print(
                "\n3. Откройте ссылку в браузере и разрешите доступ (только просмотр баланса и истории — "
                "переводить деньги бот не сможет):\n\n" + url
            )
        except ValueError as e:
            raise SystemExit(
                f"\n❌ ЮMoney отклонил запрос — {e}.\n"
                f"Проверьте в https://yoomoney.ru/myservices: Redirect URI должен быть ровно {redirect}\n"
                "(без пробелов и слеша в конце), а client_id — скопирован целиком."
            )
        except (aiohttp.ClientError, RuntimeError, TimeoutError) as e:
            page, path = _save_form(params)
            print(
                f"\nС сервера не получилось открыть ЮMoney ({e or type(e).__name__}).\n"
                "Сделаем из браузера: сохраните текст ниже в файл yoomoney.html на компьютере "
                f"(он же сохранён на сервере: {path}) и откройте его в браузере —\n"
                "он сам перейдёт в ЮMoney, там разрешите доступ.\n\n" + page
            )
        print(
            f"\n4. Браузер откроет {redirect}?code=… (страница может не загрузиться — это нормально).\n"
            "   Скопируйте адрес из адресной строки целиком."
        )
        answer = input("\nАдрес или code: ")
        if _error_of(answer):
            raise SystemExit(f"❌ ЮMoney вернул ошибку — {_explain(_error_of(answer))}")
        code = _extract_code(answer)
        if not code:
            raise SystemExit("Не нашёл code в адресе")
        data = {"code": code, "client_id": client_id, "grant_type": "authorization_code", "redirect_uri": redirect}
        if secret:
            data["client_secret"] = secret
        async with session.post(f"{API}/oauth/token", data=data) as resp:
            body = await resp.json(content_type=None)
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        error = body.get("error", "") if isinstance(body, dict) else ""
        raise SystemExit(
            f"❌ ЮMoney не выдал токен: {body}\n"
            + ("code одноразовый и живёт меньше минуты — запустите скрипт заново и вставьте адрес сразу."
               if error == "invalid_grant" else "")
        )

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
