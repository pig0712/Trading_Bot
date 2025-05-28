# src/trading_bot/exchange_gateio.py
import os
import time
import logging
from typing import Dict, Any, Literal, Optional, List

# Gate.io 공식 SDK 임포트
# FuturesOrder: 주문 생성 시 사용, Position: 포지션 정보 조회 시 사용 등
from gate_api import Configuration, ApiClient, FuturesApi, ApiException, FuturesOrder, Position, FuturesAccount

_LOG = logging.getLogger(__name__)

# .env 파일은 main.py에서 로드됨. 여기서는 os.getenv를 통해 환경 변수 사용.
GATE_API_KEY = os.getenv("GATE_API_KEY")
GATE_API_SECRET = os.getenv("GATE_API_SECRET")
GATE_ENV = os.getenv("GATE_ENV", "live")  # 기본값을 "live"로 설정

if not GATE_API_KEY or not GATE_API_SECRET:
    # 이 로그는 main.py에서 로깅이 설정된 후에야 파일/콘솔에 제대로 출력됨.
    # 만약 이 모듈이 로깅 설정 전에 임포트되면, 이 critical 로그는 기본 핸들러(stderr)로 갈 수 있음.
    _LOG.critical("CRITICAL: Gate.io API Key or Secret not found. Please set them in the .env file or as environment variables.")
    # 라이브러리 로드 시점에 바로 exit()는 부적절하므로, 사용하는 쪽에서 처리하도록 예외 발생
    raise EnvironmentError("GATE_API_KEY and GATE_API_SECRET must be set for GateIOClient.")

# API v4 엔드포인트 사용 확인
_BASE_URL = (
    "https://api.gateio.ws/api/v4"
    if GATE_ENV == "live"
    else "https://fx-api-testnet.gateio.ws/api/v4"
)

# API 설정 객체 (모듈 레벨에서 한 번만 생성하여 공유 가능, 또는 인스턴스별 생성도 가능)
# 공유 시 스레드 안전성 문제 발생 가능성 있으므로 주의 (ApiClient는 스레드 안전하지 않을 수 있음)
# 여기서는 인스턴스별로 ApiClient를 생성하도록 변경하여 안정성 확보.
_API_CFG_DEFAULTS = {"host": _BASE_URL, "key": GATE_API_KEY, "secret": GATE_API_SECRET}


class GateIOClient:
    """
    Gate.io Futures API와 통신하기 위한 클라이언트 클래스입니다.
    API 키, 시크릿, 환경(live/testnet)은 환경 변수를 통해 설정됩니다.
    """
    def __init__(self, settle_currency: str = "usdt") -> None:
        """
        GateIOClient를 초기화합니다.

        Args:
            settle_currency (str): 정산 통화 (예: "usdt", "btc"). 기본값 "usdt".
                                   Gate.io API에서는 이 값을 'settle' 파라미터로 사용합니다.
        """
        self.settle = settle_currency.lower()
        
        # 각 GateIOClient 인스턴스가 자체 ApiClient 및 FuturesApi를 갖도록 하여,
        # 여러 전략 또는 스레드에서 클라이언트를 사용할 경우의 잠재적 충돌 방지.
        current_api_config = Configuration(**_API_CFG_DEFAULTS)
        self.api_client = ApiClient(current_api_config)
        self.futures_api = FuturesApi(self.api_client)
        
        _LOG.info(f"GateIOClient initialized. Settle Currency: '{self.settle}', Environment: '{GATE_ENV}', API Host: '{_BASE_URL}'")
        self._test_connectivity()

    def _test_connectivity(self) -> None:
        """API 연결 상태 및 인증을 테스트합니다 (계좌 정보 조회 시도)."""
        _LOG.debug("Testing API connectivity and authentication...")
        try:
            account_info = self.get_account_info() # 잔고 조회로 연결 테스트
            if account_info and account_info.get('user_id'):
                 _LOG.info(f"Successfully connected to Gate.io API and authenticated. User ID: {account_info['user_id']}")
            else:
                _LOG.error("API connectivity test failed: Account info could not be retrieved or user_id missing.")
                # 이 경우, 생성자에서 예외를 발생시켜 봇 시작을 막는 것이 좋을 수 있음.
                raise ApiException(status=0, reason="Failed to retrieve valid account info during connectivity test.")
        except ApiException as e:
            _LOG.error(f"Failed to connect/authenticate with Gate.io API during connectivity test. Status: {e.status}, Body: {e.body}")
            raise  # 호출한 쪽에서 처리할 수 있도록 예외를 다시 발생

    def place_order(
        self,
        contract_symbol: str,
        order_amount_usd: float, # 주문할 금액 (USD 기준)
        position_side: Literal["long", "short"], # 진입할 포지션 방향
        leverage: int,
        order_type: Literal["market", "limit"] = "market",
        limit_price: Optional[float] = None, # 지정가 주문 시 가격
        reduce_only: bool = False, # 포지션 축소 전용 주문 여부
        time_in_force: str = "gtc",  # 주문 유효 기간: gtc, ioc, fok, poc (Gate.io 지원 확인 필요)
        order_id_prefix: str = "t-bot-" # 사용자 정의 주문 ID 접두사
    ) -> Optional[Dict[str, Any]]:
        """
        선물 주문을 전송합니다. USD 금액을 기반으로 계약 수량을 계산합니다 (시장가 주문 시).

        Args:
            contract_symbol (str): 계약 심볼 (예: "BTC_USDT").
            order_amount_usd (float): 주문할 금액 (USD 기준). 양수여야 합니다.
                                      TP/SL의 경우, 이 값은 현재 포지션의 전체 USD 가치를 의미할 수 있습니다.
            position_side (Literal["long", "short"]): 주문으로 생성/변경하려는 포지션의 방향.
                                                     TP/SL 주문 시에는 현재 포지션과 반대 방향이 됩니다.
            leverage (int): 사용할 레버리지.
            order_type (Literal["market", "limit"]): 주문 유형.
            limit_price (Optional[float]): 지정가 주문 시 가격.
            reduce_only (bool): True이면 포지션 크기를 늘리지 않고 줄이기만 하는 주문.
            time_in_force (str): 주문 유효 기간.
            order_id_prefix (str): 사용자 정의 주문 ID 생성 시 사용할 접두사.

        Returns:
            Optional[Dict[str, Any]]: 성공 시 API로부터 받은 주문 결과 딕셔너리, 실패 시 None.
        """
        if order_amount_usd <= 0:
            _LOG.error(f"주문 금액(USD)은 0보다 커야 합니다. 입력값: {order_amount_usd}")
            return None

        current_market_price = self.fetch_last_price(contract_symbol)
        if current_market_price is None:
            _LOG.error(f"{contract_symbol}의 현재가를 가져올 수 없어 주문 수량을 계산할 수 없습니다. 주문을 진행할 수 없습니다.")
            return None

        # 계약 수량(size) 계산: Gate.io API의 'size'는 계약 단위 수량입니다.
        # (주문금액 USD / 현재가) 가 실제 구매/판매할 계약 수량입니다.
        # 레버리지는 증거금 계산에 사용되며, 실제 주문 size는 레버리지를 곱하지 않은 순수 계약량입니다.
        # 단, reduce_only 주문 시 size는 청산할 계약 수량이 되어야 함. 이 부분은 호출하는 쪽에서 관리.
        # 여기서는 order_amount_usd를 기준으로 신규 진입/추가 계약 수량을 계산.
        contracts_to_order = round(order_amount_usd / current_market_price, 8) # 심볼별 정밀도 확인 필요

        if abs(contracts_to_order) < 1e-8: # 매우 작은 수량 (거의 0)
            _LOG.warning(f"{contract_symbol}에 대해 {order_amount_usd} USD로 계산된 계약 수량이 너무 작습니다 (거의 0). "
                         f"최소 주문 수량을 충족하지 못할 수 있습니다. (계산된 수량: {contracts_to_order})")
            # Gate.io의 최소 주문 수량 정책 확인 필요. 너무 작으면 API에서 거부됨.
            # 여기서는 일단 진행하고 API 오류로 확인하거나, 미리 최소 주문량 체크 로직 추가 가능.


        # API에 전달할 size: long일 경우 양수, short일 경우 음수
        api_order_size = contracts_to_order if position_side == "long" else -contracts_to_order

        # 사용자 정의 주문 ID 생성 (Gate.io는 "t-" 접두사 필요, 최대 길이 제한 확인)
        # 고유성을 위해 타임스탬프 사용, 접두사 포함 전체 길이가 너무 길지 않도록 주의
        timestamp_ms = int(time.time() * 1000)
        client_order_id = f"{order_id_prefix}{timestamp_ms}"[-30:] # 예시: 마지막 30자 사용 (Gate.io 제한 확인)
        if not client_order_id.startswith("t-"):
            client_order_id = "t-" + client_order_id[-28:]
            _LOG.warning(f"Order ID prefix '{order_id_prefix}' did not start with 't-'. Adjusted to '{client_order_id}'.")


        # FuturesOrder 객체 생성 (SDK 모델 사용)
        futures_order_payload = FuturesOrder(
            contract=contract_symbol,
            size=api_order_size, # 부호가 있는 계약 수량
            # price: 시장가는 "0", 지정가는 실제 가격 문자열. SDK가 내부적으로 처리할 수 있음.
            #        명시적으로 "0"을 설정하거나, 지정가일 경우 가격 설정.
            leverage=str(leverage), # API는 문자열 레버리지 요구
            tif=time_in_force,
            text=client_order_id, # 사용자 정의 주문 ID
            reduce_only=reduce_only
            # iceberg, close 등의 옵션은 필요시 추가
        )

        if order_type == "limit":
            if limit_price is None or limit_price <= 0:
                _LOG.error("지정가 주문 시 유효한 limit_price(양수)가 필요합니다.")
                return None
            futures_order_payload.price = str(limit_price) # 지정가 설정
        else: # 시장가 주문
            futures_order_payload.price = "0" # API 문서에 따라 시장가는 가격을 "0"으로 설정


        _LOG.info(f"주문 시도: 심볼={contract_symbol}, 방향={position_side}, 유형={order_type}, "
                  f"계약수량={api_order_size:.8f} (계산근거: {order_amount_usd} USD @ {current_market_price:.4f}), "
                  f"지정가={limit_price if limit_price else 'N/A'}, 레버리지={leverage}x, ReduceOnly={reduce_only}, ClientOrderID={client_order_id}")
        try:
            created_order: FuturesOrder = self.futures_api.create_futures_order(
                settle=self.settle, 
                futures_order=futures_order_payload
            )
            _LOG.info(f"주문 성공: ID={created_order.id}, 계약={created_order.contract}, 상태={created_order.status}, ClientOrderID={created_order.text}")
            return created_order.to_dict() # 응답 객체를 dict로 변환하여 일관성 유지
        except ApiException as e:
            # ApiException 객체는 status, reason, body, headers 등의 속성을 가짐
            error_body = e.body
            error_label = ""
            if isinstance(error_body, str): # body가 JSON 문자열일 수 있음
                try:
                    error_data = json.loads(error_body)
                    error_label = error_data.get("label", "")
                    _LOG.error(f"Gate.io 주문 API 오류: Status={e.status}, Label='{error_label}', Reason='{e.reason}', Body='{error_body}'")
                except json.JSONDecodeError:
                    _LOG.error(f"Gate.io 주문 API 오류 (body 파싱 불가): Status={e.status}, Reason='{e.reason}', Body='{error_body}'")
            else: # body가 이미 dict이거나 다른 타입일 수 있음 (SDK 버전에 따라 다름)
                 _LOG.error(f"Gate.io 주문 API 오류: Status={e.status}, Reason='{e.reason}', Body (type: {type(error_body)})='{error_body}'")
            
            # 특정 오류 레이블에 따라 추가 정보 로깅 또는 처리 가능
            if error_label == "BALANCE_NOT_ENOUGH":
                _LOG.error("잔고 부족으로 주문 실패.")
            elif error_label == "ORDER_SIZE_NOT_ENOUGH" or error_label == "MIN_ORDER_SIZE_NOT_MET": # 예시 레이블
                _LOG.error("최소 주문 수량 미달로 주문 실패.")

            return None # 실패 시 None 반환

    def get_position(self, contract_symbol: str) -> Optional[Dict[str, Any]]:
        """지정된 계약의 현재 포지션 정보를 조회합니다."""
        _LOG.debug(f"포지션 정보 조회 시도: {contract_symbol}")
        try:
            position: Position = self.futures_api.get_position(settle=self.settle, contract=contract_symbol)
            # 포지션이 없는 경우 size가 0일 수 있음 (또는 API가 404 대신 size 0 객체 반환)
            if position.size == 0:
                _LOG.info(f"{contract_symbol}에 대한 활성 포지션 없음 (Size: 0).")
            else:
                _LOG.info(f"포지션 정보 ({contract_symbol}): Size={position.size}, EntryPrice={position.entry_price}, "
                          f"Leverage={position.leverage}, LiqPrice={position.liq_price}, UnrealisedPNL={position.unrealised_pnl}")
            return position.to_dict()
        except ApiException as e:
            # Gate.io에서 포지션이 없을 때 400 에러와 함께 "POSITION_NOT_FOUND" 또는 "CONTRACT_NOT_FOUND" 레이블을 반환할 수 있음
            # 또는 HTTP 404를 반환할 수도 있음. SDK가 이를 어떻게 처리하는지 확인 필요.
            # 일반적으로 SDK는 404를 ApiException으로 변환함.
            error_body_str = e.body if isinstance(e.body, str) else str(e.body)
            if e.status == 400 and ("POSITION_NOT_FOUND" in error_body_str.upper() or "CONTRACT_NOT_FOUND" in error_body_str.upper()):
                 _LOG.info(f"{contract_symbol}에 대한 포지션 없음 (API 응답: Status={e.status}, Body='{e.body}')")
                 # 포지션이 없는 것을 나타내는 빈 딕셔너리 또는 특정 구조 반환 가능
                 return {"contract": contract_symbol, "size": 0, "message": "No active position found"}
            _LOG.error(f"Gate.io 포지션 조회 API 오류: Status={e.status}, Body='{e.body}'")
            return None # 그 외 오류

    def get_account_info(self) -> Optional[Dict[str, Any]]:
        """선물 계좌의 전반적인 정보를 조회합니다 (예: 사용 가능 잔액 등)."""
        _LOG.debug(f"선물 계좌({self.settle}) 정보 조회 시도.")
        try:
            # FuturesApi.get_futures_accounts(settle) 사용
            futures_account: FuturesAccount = self.futures_api.get_futures_accounts(settle=self.settle)
            _LOG.info(f"계좌 정보 ({self.settle}): UserID={futures_account.user_id}, "
                      f"사용가능잔액={futures_account.available} {self.settle.upper()}, "
                      f"총잔액={futures_account.total} {self.settle.upper()}")
            return futures_account.to_dict()
        except ApiException as e:
            _LOG.error(f"Gate.io 계좌 정보 조회 API 오류: Status={e.status}, Body='{e.body}'")
            return None

    def fetch_last_price(self, contract_symbol: str) -> Optional[float]:
        """지정된 계약의 최근 체결가를 조회합니다."""
        _LOG.debug(f"현재가 조회 시도: {contract_symbol}")
        try:
            # list_futures_tickers는 리스트를 반환하며, 특정 계약 조회 시에도 리스트의 첫 번째 요소 사용
            tickers: List[FuturesTicker] = self.futures_api.list_futures_tickers(settle=self.settle, contract=contract_symbol)
            if not tickers: # 빈 리스트 반환 시
                _LOG.warning(f"{contract_symbol}에 대한 Ticker 정보 없음 (API 응답이 비어 있음).")
                return None
            
            # tickers[0].last 가 최근 체결가 (문자열일 수 있으므로 float 변환)
            if tickers[0].last is None:
                _LOG.warning(f"{contract_symbol} Ticker 정보에 최근 체결가(last) 없음.")
                return None
            
            last_price = float(tickers[0].last)
            _LOG.debug(f"현재가 ({contract_symbol}): {last_price}")
            return last_price
        except ApiException as e:
            _LOG.error(f"Gate.io 현재가 조회 API 오류: Status={e.status}, Body='{e.body}'")
            return None
        except (IndexError, AttributeError, ValueError) as e: # 리스트가 비었거나, 'last' 속성이 없거나, float 변환 실패
            _LOG.error(f"{contract_symbol} Ticker 정보 파싱 오류: {e}", exc_info=True)
            return None

    def get_order_status(self, order_id: str) -> Optional[Dict[str, Any]]:
        """주문 ID를 사용하여 특정 주문의 상태를 조회합니다."""
        _LOG.debug(f"주문 상태 조회 시도: OrderID='{order_id}'")
        try:
            # get_futures_order는 contract 인자를 받지 않음 (order_id가 고유 식별자)
            order_status: FuturesOrder = self.futures_api.get_futures_order(settle=self.settle, order_id=order_id)
            _LOG.info(f"주문 상태 (ID: {order_id}): Status='{order_status.status}', FilledSize='{order_status.filled_size}', "
                      f"AvgFillPrice='{order_status.fill_price}', Price='{order_status.price}'")
            return order_status.to_dict()
        except ApiException as e:
            if e.status == 404: # 주문을 찾을 수 없는 경우
                 _LOG.warning(f"주문을 찾을 수 없음: OrderID='{order_id}' (Status 404)")
                 return None
            _LOG.error(f"Gate.io 주문 조회 API 오류 (OrderID: {order_id}): Status={e.status}, Body='{e.body}'")
            return None

    def cancel_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """특정 주문을 취소합니다."""
        _LOG.info(f"주문 취소 시도: OrderID='{order_id}'")
        try:
            # cancel_futures_order는 contract 인자를 받지 않음
            cancelled_order: FuturesOrder = self.futures_api.cancel_futures_order(settle=self.settle, order_id=order_id)
            _LOG.info(f"주문 취소 결과 (ID: {order_id}): API_OrderID={cancelled_order.id}, 상태='{cancelled_order.status}'")
            return cancelled_order.to_dict()
        except ApiException as e:
            # 이미 체결되었거나 존재하지 않는 주문 취소 시 오류 발생 가능
            error_body_str = e.body if isinstance(e.body, str) else str(e.body)
            if e.status == 400 and ("ORDER_NOT_FOUND" in error_body_str.upper() or \
                                    "ORDER_FINISHED" in error_body_str.upper() or \
                                    "ORDER_CANCELLED" in error_body_str.upper() or \
                                    "ORDER_CLOSED" in error_body_str.upper()): # 추가적인 에러 레이블 확인
                _LOG.warning(f"주문(ID: {order_id})을 취소할 수 없거나 이미 처리됨: Status={e.status}, Body='{e.body}'")
                # 이 경우, 클라이언트가 이미 주문이 없다고 판단할 수 있도록 특정 값 반환 또는 None 반환
                return {"id": order_id, "status": "already_processed_or_not_found", "message": e.body} # 예시
            _LOG.error(f"Gate.io 주문 취소 API 오류 (OrderID: {order_id}): Status={e.status}, Body='{e.body}'")
            return None

    def cancel_all_open_orders(self, contract_symbol: str) -> List[Dict[str, Any]]:
        """특정 계약에 대한 모든 미체결 주문을 취소합니다."""
        _LOG.info(f"{contract_symbol}에 대한 모든 미체결 주문 취소 시도.")
        try:
            # FuturesApi.cancel_futures_orders(settle, contract, side=None) 사용
            # side를 명시하지 않으면 해당 계약의 모든 주문 취소
            cancelled_orders_sdk_list: List[FuturesOrder] = self.futures_api.cancel_futures_orders(
                settle=self.settle, 
                contract=contract_symbol
            )
            
            results = []
            if isinstance(cancelled_orders_sdk_list, list):
                for co_sdk_obj in cancelled_orders_sdk_list:
                    results.append(co_sdk_obj.to_dict())
                _LOG.info(f"{contract_symbol}에 대해 {len(results)}개의 주문 취소 성공 (API 응답 기준).")
            else: # 예상치 못한 응답 타입
                _LOG.warning(f"cancel_futures_orders API 응답이 리스트가 아님: Type='{type(cancelled_orders_sdk_list)}', Value='{cancelled_orders_sdk_list}'")

            return results
        except ApiException as e:
            _LOG.error(f"Gate.io {contract_symbol} 전체 주문 취소 API 오류: Status={e.status}, Body='{e.body}'")
            return [] # 실패 시 빈 리스트 반환

    def update_position_leverage(self, contract_symbol: str, new_leverage: int) -> Optional[Dict[str, Any]]:
        """
        지정된 계약 포지션의 레버리지를 업데이트합니다.
        주의: 이 기능은 실제 포지션이 존재하고, 해당 마진 모드(격리/교차)에서 지원될 때만 작동합니다.
        Gate.io API 문서 및 SDK를 정확히 확인해야 합니다. 주문 시 레버리지를 지정하는 것이 더 일반적입니다.
        Gate.io SDK에는 `update_position_leverage` 와 같은 함수가 있을 수 있습니다. (v0.19.0 기준 확인 필요)
        """
        if not (0 < new_leverage <= 125): # Gate.io의 일반적인 레버리지 범위 (최대 100x 또는 125x, 심볼별 확인)
            _LOG.error(f"잘못된 레버리지 값: {new_leverage}. 유효 범위 내여야 합니다.")
            return None

        _LOG.info(f"{contract_symbol} 포지션 레버리지를 {new_leverage}x로 업데이트 시도.")
        try:
            # Gate.io Python SDK v0.19.0 기준, FuturesApi에 update_position_leverage 메서드가 존재함.
            # update_position_leverage(settle, contract, leverage, cross_leverage_limit=None)
            # leverage는 문자열로 전달.
            updated_position: Position = self.futures_api.update_position_leverage(
                settle=self.settle,
                contract=contract_symbol,
                leverage=str(new_leverage)
                # cross_leverage_limit 파라미터는 교차 마진 시 사용될 수 있음 (필요시 설정)
            )
            _LOG.info(f"{contract_symbol} 레버리지 업데이트 성공. 새 레버리지: {updated_position.leverage}, 모드: {updated_position.mode}")
            return updated_position.to_dict()
        except ApiException as e:
            _LOG.error(f"Gate.io {contract_symbol} 레버리지 업데이트 API 오류: Status={e.status}, Body='{e.body}'")
            # 오류 메시지 예: "leverage cannot be changed when position is not clear" (포지션 있을 때 격리 레버리지 변경 불가 등)
            return None
        except AttributeError:
            _LOG.error("현재 설치된 gate-api SDK 버전에 'update_position_leverage' 함수가 없거나 이름이 다를 수 있습니다. SDK 문서를 확인하세요.")
            return None


    def get_open_orders(self, contract_symbol: str) -> List[Dict[str, Any]]:
        """특정 계약에 대한 모든 '미체결(open)' 주문 목록을 가져옵니다."""
        _LOG.debug(f"미체결 주문 목록 조회 시도: {contract_symbol}")
        try:
            # status="open"으로 필터링
            open_orders_sdk_list: List[FuturesOrder] = self.futures_api.list_futures_orders(
                settle=self.settle,
                contract=contract_symbol,
                status="open" # "open": 미체결, "finished": 체결 완료 또는 취소
            )
            orders_list = [order.to_dict() for order in open_orders_sdk_list]
            _LOG.debug(f"{contract_symbol}에 대해 {len(orders_list)}개의 미체결 주문 발견.")
            return orders_list
        except ApiException as e:
            _LOG.error(f"Gate.io {contract_symbol} 미체결 주문 조회 API 오류: Status={e.status}, Body='{e.body}'")
            return [] # 오류 발생 시 빈 리스트 반환
