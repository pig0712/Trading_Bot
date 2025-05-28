# src/trading_bot/prices.py
"""CoinGecko API를 이용한 가격 조회 모듈 (동기 & 비동기, 재시도 로직 포함)."""
import time
import logging
import httpx # 외부 API 호출용 HTTP 클라이언트
import asyncio # 비동기 sleep을 위해 추가
import json # JSON 파싱 오류 처리를 위해 추가
from typing import Optional, Dict, Any # 타입 힌트 추가

_LOG = logging.getLogger(__name__)

# CoinGecko API 설정
_COINGECKO_API_BASE_URL = "https://api.coingecko.com/api/v3"
_COINGECKO_SIMPLE_PRICE_ENDPOINT = f"{_COINGECKO_API_BASE_URL}/simple/price"

# API 요청 기본 설정
_DEFAULT_TIMEOUT = 10  # 초 단위
_DEFAULT_RETRIES = 3   # 최대 재시도 횟수
_DEFAULT_BACKOFF_FACTOR = 1.5 # 재시도 간격 증가 배수 (초기 1초 * 1.5, 2.25초 * 1.5 ...)

# 사용자 에이전트 설정 (API 요청 시 권장, 일부 API는 이를 요구할 수 있음)
_HEADERS = {"User-Agent": "Trading_Bot/1.0 (Python; like Gecko)"}


def _parse_coingecko_price_response(
    response_data: Optional[Dict[str, Any]], 
    symbol_id: str, 
    vs_currency: str
) -> Optional[float]:
    """
    CoinGecko의 /simple/price API 응답에서 특정 암호화폐의 가격을 파싱합니다.

    Args:
        response_data (Optional[Dict[str, Any]]): API 응답 JSON을 파싱한 딕셔너리.
        symbol_id (str): CoinGecko에서 사용하는 암호화폐 ID (예: "bitcoin").
        vs_currency (str): 비교 대상 통화 (예: "usd").

    Returns:
        Optional[float]: 성공 시 가격(float), 실패 시 None.
    """
    if not response_data:
        _LOG.warning(f"CoinGecko 응답 데이터가 비어있습니다 (심볼: {symbol_id}, 통화: {vs_currency}).")
        return None
    try:
        # CoinGecko API는 요청한 id와 currency를 소문자로 키로 사용한 딕셔너리를 반환합니다.
        # 예: {"bitcoin": {"usd": 60000.0}}
        price_data = response_data.get(symbol_id.lower())
        if price_data is None:
            _LOG.error(f"CoinGecko 응답에 '{symbol_id.lower()}' 키가 없습니다. 응답: {response_data}")
            return None
        
        price = price_data.get(vs_currency.lower())
        if price is None:
            _LOG.error(f"CoinGecko 응답 '{symbol_id.lower()}'에 '{vs_currency.lower()}' 통화 정보가 없습니다. 응답: {price_data}")
            return None
            
        return float(price)
    except (KeyError, ValueError, TypeError) as e:
        _LOG.error(f"CoinGecko 응답 데이터 파싱 중 오류 발생 (심볼: {symbol_id}, 통화: {vs_currency}): {e}. 응답 데이터: {response_data}", exc_info=True)
        return None

def fetch_price_coingecko(
    symbol_id: str = "bitcoin",  # CoinGecko에서 사용하는 ID (예: "bitcoin", "ethereum")
    vs_currency: str = "usd",    # 비교 대상 통화 (예: "usd", "krw")
    timeout: int = _DEFAULT_TIMEOUT,
    retries: int = _DEFAULT_RETRIES,
    backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
) -> Optional[float]:
    """
    CoinGecko API를 사용하여 특정 암호화폐의 현재 가격을 조회합니다 (동기 방식).
    네트워크 오류 발생 시 지수 백오프(exponential backoff)를 사용한 재시도 로직이 포함됩니다.
    """
    params = {"ids": symbol_id, "vs_currencies": vs_currency}
    
    last_exception: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            _LOG.debug(f"CoinGecko 동기 가격 조회 시도 ({attempt}/{retries}): URL='{_COINGECKO_SIMPLE_PRICE_ENDPOINT}', Params={params}")
            with httpx.Client(timeout=timeout, headers=_HEADERS, follow_redirects=True) as client:
                response = client.get(_COINGECKO_SIMPLE_PRICE_ENDPOINT, params=params)
                response.raise_for_status()  # HTTP 4xx/5xx 오류 발생 시 예외 발생
                
                response_data = response.json()
                _LOG.debug(f"CoinGecko 동기 응답 수신 ({attempt}): {response_data}")
                return _parse_coingecko_price_response(response_data, symbol_id, vs_currency)

        except httpx.HTTPStatusError as e: # HTTP 오류 (예: 404, 500, 429 Too Many Requests)
            _LOG.warning(f"CoinGecko API HTTP 오류 (시도 {attempt}): Status={e.response.status_code}, URL='{e.request.url}'. 응답: '{e.response.text}'")
            last_exception = e
            # 클라이언트 오류(4xx, 429 제외)는 재시도하지 않음 (예: 잘못된 심볼 ID)
            if 400 <= e.response.status_code < 500 and e.response.status_code != 429: # 429는 재시도 가치 있음
                _LOG.error(f"복구 불가능한 클라이언트 오류 ({e.response.status_code}). 재시도를 중단합니다.")
                break 
        except httpx.RequestError as e: # 타임아웃, 네트워크 연결 오류 등 httpx 라이브러리에서 발생하는 요청 관련 오류
            _LOG.warning(f"CoinGecko API 요청 오류 (시도 {attempt}): {type(e).__name__} - '{e}', URL='{e.request.url if e.request else 'N/A'}'")
            last_exception = e
        except json.JSONDecodeError as e: # 응답이 JSON 형식이 아닐 경우
            _LOG.error(f"CoinGecko API 응답 JSON 파싱 오류 (시도 {attempt}): {e}. 응답 텍스트: '{response.text if 'response' in locals() else 'N/A'}'")
            last_exception = e # 파싱 오류는 보통 재시도해도 동일하므로 break 가능
            break # 재시도 중단

        if attempt < retries:
            # 지수 백오프: 첫 재시도는 1 * backoff_factor, 두 번째는 2 * backoff_factor 등 또는 (backoff_factor ** (attempt -1))
            sleep_time = (backoff_factor ** (attempt -1)) * 1.0 # 초기 1초에서 시작하여 점차 증가
            _LOG.info(f"CoinGecko API 요청 실패. {sleep_time:.2f}초 후 재시도합니다... (시도 {attempt}/{retries})")
            time.sleep(sleep_time)
    
    _LOG.error(f"CoinGecko로부터 {symbol_id}/{vs_currency} 가격 조회 최종 실패 ({retries}회 시도 후). 마지막 오류: {type(last_exception).__name__ if last_exception else 'N/A'}")
    return None


async def fetch_price_coingecko_async(
    symbol_id: str = "bitcoin",
    vs_currency: str = "usd",
    timeout: int = _DEFAULT_TIMEOUT,
    retries: int = _DEFAULT_RETRIES,
    backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
) -> Optional[float]:
    """
    CoinGecko API를 사용하여 특정 암호화폐의 현재 가격을 조회합니다 (비동기 방식).
    """
    params = {"ids": symbol_id, "vs_currencies": vs_currency}

    last_exception: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=timeout, headers=_HEADERS, follow_redirects=True) as client:
        for attempt in range(1, retries + 1):
            try:
                _LOG.debug(f"CoinGecko 비동기 가격 조회 시도 ({attempt}/{retries}): URL='{_COINGECKO_SIMPLE_PRICE_ENDPOINT}', Params={params}")
                response = await client.get(_COINGECKO_SIMPLE_PRICE_ENDPOINT, params=params)
                response.raise_for_status()
                
                response_data = response.json()
                _LOG.debug(f"CoinGecko 비동기 응답 수신 ({attempt}): {response_data}")
                return _parse_coingecko_price_response(response_data, symbol_id, vs_currency)

            except httpx.HTTPStatusError as e:
                _LOG.warning(f"CoinGecko API 비동기 HTTP 오류 (시도 {attempt}): Status={e.response.status_code}, URL='{e.request.url}'. 응답: '{e.response.text}'")
                last_exception = e
                if 400 <= e.response.status_code < 500 and e.response.status_code != 429:
                    _LOG.error(f"복구 불가능한 클라이언트 오류 ({e.response.status_code}). 재시도를 중단합니다.")
                    break
            except httpx.RequestError as e:
                _LOG.warning(f"CoinGecko API 비동기 요청 오류 (시도 {attempt}): {type(e).__name__} - '{e}', URL='{e.request.url if e.request else 'N/A'}'")
                last_exception = e
            except json.JSONDecodeError as e:
                _LOG.error(f"CoinGecko API 비동기 응답 JSON 파싱 오류 (시도 {attempt}): {e}. 응답 텍스트: '{response.text if 'response' in locals() else 'N/A'}'")
                last_exception = e
                break 
            
            if attempt < retries:
                sleep_time = (backoff_factor ** (attempt - 1)) * 1.0
                _LOG.info(f"CoinGecko API 비동기 요청 실패. {sleep_time:.2f}초 후 재시도합니다... (시도 {attempt}/{retries})")
                await asyncio.sleep(sleep_time)

    _LOG.error(f"CoinGecko로부터 {symbol_id}/{vs_currency} 비동기 가격 조회 최종 실패 ({retries}회 시도 후). 마지막 오류: {type(last_exception).__name__ if last_exception else 'N/A'}")
    return None

# 예제 사용법 (이 파일을 직접 실행 시 테스트)
if __name__ == "__main__":
    # 이 테스트는 main.py에서 설정된 로깅 핸들러가 아닌, 여기서 설정한 기본 로깅을 사용함.
    # 좀 더 정교한 테스트를 위해서는 main.py의 로깅 설정을 공유하거나 별도 테스트용 로깅 설정 필요.
    if not logging.getLogger().hasHandlers(): # 핸들러가 없으면 기본 설정
        logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)-8s - %(message)s')

    _LOG.info("--- CoinGecko 가격 조회 모듈 직접 실행 테스트 ---")

    # 동기 테스트
    btc_price_sync = fetch_price_coingecko(symbol_id="bitcoin", vs_currency="usd")
    if btc_price_sync:
        _LOG.info(f"동기 조회 - Bitcoin 현재가: ${btc_price_sync:,.2f}")
    else:
        _LOG.error("동기 조회 - Bitcoin 가격 정보를 가져오지 못했습니다.")

    eth_price_sync_krw = fetch_price_coingecko(symbol_id="ethereum", vs_currency="krw", retries=2)
    if eth_price_sync_krw:
        _LOG.info(f"동기 조회 - Ethereum 현재가: ₩{eth_price_sync_krw:,.0f}")
    else:
        _LOG.error("동기 조회 - Ethereum/KRW 가격 정보를 가져오지 못했습니다.")

    # 비동기 테스트
    async def run_async_tests():
        _LOG.info("--- 비동기 테스트 시작 ---")
        btc_price_async_eur = await fetch_price_coingecko_async(symbol_id="bitcoin", vs_currency="eur")
        if btc_price_async_eur:
            _LOG.info(f"비동기 조회 - Bitcoin 현재가: €{btc_price_async_eur:,.2f}")
        else:
            _LOG.error("비동기 조회 - Bitcoin/EUR 가격 정보를 가져오지 못했습니다.")

        # 여러 개 동시 요청 예시
        _LOG.info("--- 비동기 동시 요청 테스트 시작 ---")
        results = await asyncio.gather(
            fetch_price_coingecko_async("solana", "usd"),
            fetch_price_coingecko_async("dogecoin", "usd", retries=1),
            fetch_price_coingecko_async("nonexistentsymbol", "usd"), # 실패 예상
            return_exceptions=True # 개별 작업의 예외를 반환하도록 설정
        )
        
        symbols_for_gather = ["Solana", "Dogecoin", "NonExistentSymbol"]
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                _LOG.error(f"비동기 동시 조회 - {symbols_for_gather[i]}: 오류 발생 - {type(res).__name__}: {res}")
            elif res is not None:
                 _LOG.info(f"비동기 동시 조회 - {symbols_for_gather[i]}: ${res:,.4f}")
            else:
                _LOG.warning(f"비동기 동시 조회 - {symbols_for_gather[i]}: 가격 정보 없음 (None 반환)")
        _LOG.info("--- 비동기 테스트 완료 ---")

    asyncio.run(run_async_tests())
    _LOG.info("--- 모든 테스트 완료 ---")
