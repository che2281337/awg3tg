"""Приём оплаты картой через кошелёк ЮMoney физлица.

Клиент оплачивает форму ЮMoney (quickpay) с меткой (label) счёта. Бот раз в N секунд
читает историю входящих операций кошелька (API operation-history) и подтверждает счёт,
у которого совпала метка, — домен и вебхук не нужны.

Токен с правами только на чтение (account-info, operation-history): с ним нельзя
ничего перевести с кошелька. Получить: python -m bot.yoomoney_auth
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)

API = "https://yoomoney.ru"
SCOPE = "account-info operation-history"


class YooMoneyError(Exception):
    pass


@dataclass
class Operation:
    operation_id: str
    status: str  # success | refused | in_progress
    direction: str  # in | out
    amount: float  # сколько зачислено на кошелёк (уже за вычетом комиссии)
    label: str
    datetime: str = ""


def quickpay_url(receiver: str, amount: int, label: str, targets: str, payment_type: str = "AC", success_url: str = "") -> str:
    """Ссылка на страницу оплаты ЮMoney. payment_type: AC — картой, PC — из кошелька ЮMoney."""
    params = {
        "receiver": receiver,
        "quickpay-form": "button",
        "targets": targets,
        "paymentType": payment_type,
        "sum": str(amount),
        "label": label,
    }
    if success_url:
        params["successURL"] = success_url
    return f"{API}/quickpay/confirm.xml?{urllib.parse.urlencode(params)}"


class YooMoney:
    def __init__(self, token: str, base_url: str = API, timeout: float = 20) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _post(self, method: str, data: dict | None = None) -> dict:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        try:
            async with self._session.post(
                f"{self.base_url}/api/{method}",
                data=data or {},
                headers={"Authorization": f"Bearer {self.token}"},
            ) as resp:
                if resp.status == 401:
                    raise YooMoneyError("Токен ЮMoney недействителен — получите новый: python -m bot.yoomoney_auth")
                if resp.status != 200:
                    raise YooMoneyError(f"ЮMoney ответил HTTP {resp.status}")
                body = await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise YooMoneyError(f"ЮMoney недоступен: {e}") from e
        except TimeoutError as e:
            raise YooMoneyError("ЮMoney не ответил вовремя") from e
        if not isinstance(body, dict):
            raise YooMoneyError("Непонятный ответ ЮMoney")
        if body.get("error"):
            raise YooMoneyError(f"Ошибка ЮMoney: {body['error']}")
        return body

    async def account(self) -> str:
        """Номер кошелька."""
        return str((await self._post("account-info")).get("account") or "")

    async def operations(self, label: str | None = None, records: int = 100) -> list[Operation]:
        """Последние входящие операции (по метке — только операции этого счёта)."""
        data = {"type": "deposition", "records": str(records)}
        if label:
            data["label"] = label
        result = []
        for op in (await self._post("operation-history", data)).get("operations") or []:
            try:
                result.append(
                    Operation(
                        operation_id=str(op.get("operation_id", "")),
                        status=str(op.get("status", "")),
                        direction=str(op.get("direction", "")),
                        amount=float(op.get("amount") or 0),
                        label=str(op.get("label") or ""),
                        datetime=str(op.get("datetime", "")),
                    )
                )
            except (TypeError, ValueError):
                continue
        return result

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
