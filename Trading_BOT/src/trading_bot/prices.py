"""가격 조회 모듈 (동기 & 비동기, 간단한 재시도 포함)."""
import time
from typing import Literal, Optional

import httpx

_API = "https://api.coingecko.com/api/v3/simple/price"
_DEFAULT_TIMEOUT = 10


def _get(params: dict, timeout: int) -> httpx.Response:
    with httpx.Client(timeout=timeout, headers={"User-Agent": "Trading_BOT/1.0"}) as c:
        return c.get(_API, params=params)


def fetch_price(
    symbol_id: str = "bitcoin",
    vs_currency: str = "usd",
    timeout: int = _DEFAULT_TIMEOUT,
    retries: int = 3,
    backoff: float = 1.5,
) -> float:
    """현재가 조회 (동기).

    재시도(back-off) 로직이 있어 네트워크 일시 장애에 조금 더 강합니다.
    """
    params = {"ids": symbol_id, "vs_currencies": vs_currency}
    for attempt in range(1, retries + 1):
        try:
            resp = _get(params, timeout)
            resp.raise_for_status()
            return resp.json()[symbol_id][vs_currency]
        except Exception as e:  # noqa: BLE001
            if attempt == retries:
                raise
            time.sleep(backoff ** attempt)


async def fetch_price_async(
    symbol_id: str = "bitcoin",
    vs_currency: str = "usd",
    timeout: int = _DEFAULT_TIMEOUT,
) -> float:
    """현재가 조회 (비동기)."""
    async with httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": "Trading_BOT/1.0"}
    ) as client:
        resp = await client.get(_API, params={"ids": symbol_id, "vs_currencies": vs_currency})
        resp.raise_for_status()
        return resp.json()[symbol_id][vs_currency]
