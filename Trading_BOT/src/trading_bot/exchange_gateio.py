# src/trading_bot/exchange_gateio.py
import os
import time
import logging
import json
from typing import Dict, Any, Literal, Optional, List

from gate_api import Configuration, ApiClient, FuturesApi, ApiException, FuturesOrder, Position, FuturesAccount, FuturesTicker

_LOG = logging.getLogger(__name__)

GATE_API_KEY = os.getenv("GATE_API_KEY")
GATE_API_SECRET = os.getenv("GATE_API_SECRET")
GATE_ENV = os.getenv("GATE_ENV", "live")

if not GATE_API_KEY or not GATE_API_SECRET:
    _LOG.critical("CRITICAL: Gate.io API Key or Secret not found.")
    raise EnvironmentError("GATE_API_KEY and GATE_API_SECRET must be set for GateIOClient.")

_BASE_URL = (
    "https://api.gateio.ws/api/v4"
    if GATE_ENV == "live"
    else "https://fx-api-testnet.gateio.ws/api/v4"
)

_API_CFG_DEFAULTS = {"host": _BASE_URL, "key": GATE_API_KEY, "secret": GATE_API_SECRET}


class GateIOClient:
    def __init__(self, settle_currency: str = "usdt") -> None:
        self.settle = settle_currency.lower()
        current_api_config = Configuration(**_API_CFG_DEFAULTS)
        self.api_client = ApiClient(current_api_config)
        self.futures_api = FuturesApi(self.api_client)
        
        _LOG.info(f"GateIOClient 초기화 완료. 정산 통화: '{self.settle}', 환경: '{GATE_ENV}', API 호스트: '{_BASE_URL}'")
        self._test_connectivity()

    def _test_connectivity(self) -> None:
        _LOG.debug("Testing API connectivity and authentication...")
        try:
            account_info = self.get_account_info()
            if account_info and account_info.get('currency'):
                 _LOG.info(f"Successfully connected to Gate.io API and authenticated. Currency: {account_info['currency']}")
            else:
                _LOG.error("API connectivity test failed: Account info could not be retrieved or is invalid.")
                raise ApiException(status=0, reason="Failed to retrieve valid account info during connectivity test.")
        except ApiException as e:
            _LOG.error(f"Failed to connect/authenticate with Gate.io API during connectivity test. Status: {e.status}, Body: {e.body}")
            raise

    def get_contract_multiplier(self, contract_symbol: str) -> Optional[float]:
        """심볼에 맞는 계약 단위를 반환합니다. 향후 API에서 동적으로 가져오도록 개선할 수 있습니다."""
        symbol_upper = contract_symbol.upper()
        if "BTC" in symbol_upper:
            return 0.0001  # 1 계약 = 0.0001 BTC
        elif "ETH" in symbol_upper:
            return 0.001   # 1 계약 = 0.001 ETH (예시)
        else:
            _LOG.warning(f"'{contract_symbol}'에 대한 계약 단위를 알 수 없습니다. 기본값 1을 사용합니다 (위험!).")
            return 1.0

    def place_order(
        self,
        contract_symbol: str,
        order_amount_usd: float,
        position_side: Literal["long", "short"],
        leverage: int,
        order_type: Literal["market", "limit"] = "market",
        limit_price: Optional[float] = None,
        reduce_only: bool = False,
        time_in_force: str = "gtc",
        order_id_prefix: str = "t-bot-"
    ) -> Optional[Dict[str, Any]]:
        
        if not reduce_only:
            _LOG.info(f"주문 전 {contract_symbol}의 레버리지를 {leverage}x로 설정합니다.")
            try:
                self.update_position_leverage(contract_symbol, leverage)
            except Exception as e:
                _LOG.error(f"레버리지 설정 실패로 주문을 진행할 수 없습니다: {e}")
                return None

        if order_amount_usd <= 0:
            _LOG.error(f"주문 금액(USD)은 0보다 커야 합니다: {order_amount_usd}")
            return None

        current_market_price = self.fetch_last_price(contract_symbol)
        if current_market_price is None:
            _LOG.error(f"{contract_symbol}의 현재가를 가져올 수 없어 주문 수량을 계산할 수 없습니다.")
            return None

        contract_multiplier = self.get_contract_multiplier(contract_symbol)
        if contract_multiplier is None:
            return None

        coin_quantity_to_order = order_amount_usd / current_market_price
        num_contracts_to_order = int(coin_quantity_to_order / contract_multiplier)

        min_order_size = 1 # 1 계약
        if abs(num_contracts_to_order) < min_order_size:
            _LOG.error(f"계산된 계약 개수({num_contracts_to_order})가 최소 주문 단위({min_order_size} 계약)보다 작습니다.")
            _LOG.error("주문 금액(USD)을 늘리거나, 코인 가격이 변할 때까지 기다려야 합니다. 주문을 취소합니다.")
            return None

        api_order_size = num_contracts_to_order if position_side == "long" else -num_contracts_to_order
        
        timestamp_ms = int(time.time() * 1000)
        client_order_id = f"{order_id_prefix}{timestamp_ms}"
        if not client_order_id.startswith("t-"):
            client_order_id = "t-" + client_order_id
        client_order_id = client_order_id[:30]

        # --- 여기가 수정된 부분입니다 ---
        # 주문 유형에 따라 올바른 Time in Force(tif) 값을 설정합니다.
        effective_tif = time_in_force
        if order_type == "market":
            # Gate.io API 규칙: 시장가 주문은 'ioc' 또는 'fok'여야 합니다.
            if time_in_force not in ["ioc", "fok"]:
                _LOG.info(f"시장가 주문 감지. 주문 유효 기간(tif)을 기본값 '{time_in_force}'에서 'ioc'로 강제 변경합니다.")
                effective_tif = "ioc"
        
        futures_order_payload = FuturesOrder(
            contract=contract_symbol,
            size=api_order_size,
            tif=effective_tif, # 수정된 tif 값 사용
            text=client_order_id,
            reduce_only=reduce_only
        )

        if order_type == "limit":
            if limit_price is None or limit_price <= 0:
                _LOG.error("지정가 주문 시 유효한 limit_price(양수)가 필요합니다.")
                return None
            futures_order_payload.price = str(limit_price)
        else:
            futures_order_payload.price = "0"

        _LOG.info(f"주문 시도: 심볼={contract_symbol}, 방향={position_side}, 유형={order_type}, "
                  f"계약수량(size)={futures_order_payload.size}, TIF='{effective_tif}', 지정가={limit_price if limit_price else 'N/A'}, "
                  f"ReduceOnly={reduce_only}, ClientOrderID={client_order_id}")
        try:
            created_order: FuturesOrder = self.futures_api.create_futures_order(
                settle=self.settle, 
                futures_order=futures_order_payload
            )
            _LOG.info(f"주문 성공: ID={created_order.id}, 계약={created_order.contract}, 상태={created_order.status}")
            return created_order.to_dict()
        except ApiException as e:
            _LOG.error(f"Gate.io 주문 API 오류: Status={e.status}, Body='{e.body}'")
            return None

    def get_account_info(self) -> Optional[Dict[str, Any]]:
        _LOG.debug(f"선물 계좌({self.settle}) 정보 조회 시도.")
        try:
            api_response = self.futures_api.list_futures_accounts(settle=self.settle)
            _LOG.info(f"DEBUG: list_futures_accounts API 응답 수신. 타입: {type(api_response)}, 값: {api_response}")

            futures_account_obj = None

            if isinstance(api_response, list):
                if not api_response:
                    _LOG.warning(f"API가 {self.settle} 선물 계좌에 대한 빈 리스트를 반환했습니다.")
                    return None
                else:
                    futures_account_obj = api_response[0]
                    _LOG.debug("API 응답이 리스트 형태이므로 첫 번째 항목을 사용합니다.")
            else:
                futures_account_obj = api_response
                _LOG.debug("API 응답이 단일 객체 형태(또는 None)입니다.")

            if futures_account_obj and hasattr(futures_account_obj, 'currency'):
                _LOG.info(f"계좌 정보 ({self.settle}): Currency={futures_account_obj.currency}, "
                          f"사용가능잔액={futures_account_obj.available} {self.settle.upper()}, "
                          f"총잔액={futures_account_obj.total} {self.settle.upper()}")
                return futures_account_obj.to_dict()
            else:
                _LOG.error(f"Gate.io {self.settle} 선물 계좌 정보를 찾을 수 없거나 응답 객체가 유효하지 않습니다. 최종 확인된 객체: {futures_account_obj}")
                return None

        except ApiException as e:
            if "USER_NOT_FOUND" in str(e.body):
                _LOG.error(f"Gate.io API 오류: 선물 계정이 활성화되지 않았습니다. 웹사이트에서 선물 지갑으로 소액을 이체해주세요. Body: {e.body}")
            else:
                _LOG.error(f"Gate.io 계좌 정보 조회 API 오류: Status={e.status}, Body='{e.body}'")
            raise
        except Exception as e:
            _LOG.error(f"계좌 정보 처리 중 예상치 못한 오류: {e}", exc_info=True)
            raise
            
    def get_position(self, contract_symbol: str) -> Optional[Dict[str, Any]]:
        _LOG.debug(f"포지션 정보 조회 시도: {contract_symbol}")
        try:
            position: Position = self.futures_api.get_position(settle=self.settle, contract=contract_symbol)
            if position.size == 0:
                _LOG.info(f"{contract_symbol}에 대한 활성 포지션 없음 (Size: 0).")
            else:
                _LOG.info(f"포지션 정보 ({contract_symbol}): Size={position.size}, EntryPrice={position.entry_price}, "
                          f"Leverage={position.leverage}, LiqPrice={position.liq_price}, UnrealisedPNL={position.unrealised_pnl}")
            return position.to_dict()
        except ApiException as e:
            error_body_str = e.body if isinstance(e.body, str) else str(e.body)
            if e.status == 400 and ("POSITION_NOT_FOUND" in error_body_str.upper() or "CONTRACT_NOT_FOUND" in error_body_str.upper()):
                 _LOG.info(f"{contract_symbol}에 대한 포지션 없음 (API 응답: Status={e.status}, Body='{e.body}')")
                 return {"contract": contract_symbol, "size": 0, "message": "No active position found"}
            _LOG.error(f"Gate.io 포지션 조회 API 오류: Status={e.status}, Body='{e.body}'")
            return None
            
    def fetch_last_price(self, contract_symbol: str) -> Optional[float]:
        _LOG.debug(f"현재가 조회 시도: {contract_symbol}")
        try:
            tickers: List[FuturesTicker] = self.futures_api.list_futures_tickers(settle=self.settle, contract=contract_symbol)
            if not tickers:
                _LOG.warning(f"{contract_symbol}에 대한 Ticker 정보 없음 (API 응답이 비어 있음).")
                return None
            
            if tickers[0].last is None:
                _LOG.warning(f"{contract_symbol} Ticker 정보에 최근 체결가(last) 없음.")
                return None
            
            last_price = float(tickers[0].last)
            _LOG.debug(f"현재가 ({contract_symbol}): {last_price}")
            return last_price
        except ApiException as e:
            _LOG.error(f"Gate.io 현재가 조회 API 오류: Status={e.status}, Body='{e.body}'")
            return None
        except (IndexError, AttributeError, ValueError) as e:
            _LOG.error(f"{contract_symbol} Ticker 정보 파싱 오류: {e}", exc_info=True)
            return None

    def get_order_status(self, order_id: str) -> Optional[Dict[str, Any]]:
        _LOG.debug(f"주문 상태 조회 시도: OrderID='{order_id}'")
        try:
            order_status: FuturesOrder = self.futures_api.get_futures_order(settle=self.settle, order_id=order_id)
            _LOG.info(f"주문 상태 (ID: {order_id}): Status='{order_status.status}', FilledSize='{order_status.filled_size}', "
                      f"AvgFillPrice='{order_status.fill_price}', Price='{order_status.price}'")
            return order_status.to_dict()
        except ApiException as e:
            if e.status == 404:
                 _LOG.warning(f"주문을 찾을 수 없음: OrderID='{order_id}' (Status 404)")
                 return None
            _LOG.error(f"Gate.io 주문 조회 API 오류 (OrderID: {order_id}): Status={e.status}, Body='{e.body}'")
            return None

    def cancel_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        _LOG.info(f"주문 취소 시도: OrderID='{order_id}'")
        try:
            cancelled_order: FuturesOrder = self.futures_api.cancel_futures_order(settle=self.settle, order_id=order_id)
            _LOG.info(f"주문 취소 결과 (ID: {order_id}): API_OrderID={cancelled_order.id}, 상태='{cancelled_order.status}'")
            return cancelled_order.to_dict()
        except ApiException as e:
            error_body_str = e.body if isinstance(e.body, str) else str(e.body)
            if e.status == 400 and ("ORDER_NOT_FOUND" in error_body_str.upper() or \
                                    "ORDER_FINISHED" in error_body_str.upper() or \
                                    "ORDER_CANCELLED" in error_body_str.upper() or \
                                    "ORDER_CLOSED" in error_body_str.upper()):
                _LOG.warning(f"주문(ID: {order_id})을 취소할 수 없거나 이미 처리됨: Status={e.status}, Body='{e.body}'")
                return {"id": order_id, "status": "already_processed_or_not_found", "message": e.body}
            _LOG.error(f"Gate.io 주문 취소 API 오류 (OrderID: {order_id}): Status={e.status}, Body='{e.body}'")
            return None

    def cancel_all_open_orders(self, contract_symbol: str) -> List[Dict[str, Any]]:
        _LOG.info(f"{contract_symbol}에 대한 모든 미체결 주문 취소 시도.")
        try:
            cancelled_orders_sdk_list: List[FuturesOrder] = self.futures_api.cancel_futures_orders(
                settle=self.settle, 
                contract=contract_symbol
            )
            
            results = []
            if isinstance(cancelled_orders_sdk_list, list):
                for co_sdk_obj in cancelled_orders_sdk_list:
                    results.append(co_sdk_obj.to_dict())
                _LOG.info(f"{contract_symbol}에 대해 {len(results)}개의 주문 취소 성공 (API 응답 기준).")
            else:
                _LOG.warning(f"cancel_futures_orders API 응답이 리스트가 아님: Type='{type(cancelled_orders_sdk_list)}', Value='{cancelled_orders_sdk_list}'")
            return results
        except ApiException as e:
            _LOG.error(f"Gate.io {contract_symbol} 전체 주문 취소 API 오류: Status={e.status}, Body='{e.body}'")
            return []

    def update_position_leverage(self, contract_symbol: str, new_leverage: int) -> Optional[Dict[str, Any]]:
        if not (0 < new_leverage <= 125):
            _LOG.error(f"잘못된 레버리지 값: {new_leverage}. 유효 범위 내여야 합니다.")
            return None

        _LOG.info(f"{contract_symbol} 포지션 레버리지를 {new_leverage}x로 업데이트 시도.")
        try:
            updated_position: Position = self.futures_api.update_position_leverage(
                settle=self.settle,
                contract=contract_symbol,
                leverage=str(new_leverage)
            )
            _LOG.info(f"{contract_symbol} 레버리지 업데이트 성공. 새 레버리지: {updated_position.leverage}, 모드: {updated_position.mode}")
            return updated_position.to_dict()
        except ApiException as e:
            _LOG.error(f"Gate.io {contract_symbol} 레버리지 업데이트 API 오류: Status={e.status}, Body='{e.body}'")
            raise
        except AttributeError:
            _LOG.error("현재 설치된 gate-api SDK 버전에 'update_position_leverage' 함수가 없거나 이름이 다를 수 있습니다. SDK 문서를 확인하세요.")
            raise

    def get_open_orders(self, contract_symbol: str) -> List[Dict[str, Any]]:
        _LOG.debug(f"미체결 주문 목록 조회 시도: {contract_symbol}")
        try:
            open_orders_sdk_list: List[FuturesOrder] = self.futures_api.list_futures_orders(
                settle=self.settle,
                contract=contract_symbol,
                status="open"
            )
            orders_list = [order.to_dict() for order in open_orders_sdk_list]
            _LOG.debug(f"{contract_symbol}에 대해 {len(orders_list)}개의 미체결 주문 발견.")
            return orders_list
        except ApiException as e:
            _LOG.error(f"Gate.io {contract_symbol} 미체결 주문 조회 API 오류: Status={e.status}, Body='{e.body}'")
            return []
