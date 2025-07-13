# src/trading_bot/cli.py
import time
import click
import logging
import sys
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any, Literal
import pandas as pd # 데이터 분석을 위해 pandas 추가

from .config import BotConfig
from .liquidation import calculate_liquidation_price
from .exchange_gateio import GateIOClient, ApiException

_LOG = logging.getLogger(__name__)

class BotTradingState:
    """봇의 현재 거래 관련 상태를 관리하는 클래스입니다."""
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.current_avg_entry_price: Optional[float] = None
        self.total_position_contracts: float = 0.0
        self.total_position_initial_usd: float = 0.0
        self.is_in_position: bool = False
        self.current_split_order_count: int = 0
        self.active_take_profit_order_id: Optional[str] = None
        self.active_stop_loss_order_id: Optional[str] = None
        self.last_known_liquidation_price: Optional[float] = None
        _LOG.info(f"BotTradingState for {self.symbol} initialized.")

    def reset(self):
        """봇 상태를 초기화합니다."""
        _LOG.info(f"BotTradingState for {self.symbol} resetting...")
        self.current_avg_entry_price = None
        self.total_position_contracts = 0.0
        self.total_position_initial_usd = 0.0
        self.is_in_position = False
        self.current_split_order_count = 0
        self.active_take_profit_order_id = None
        self.active_stop_loss_order_id = None
        self.last_known_liquidation_price = None
        _LOG.info(f"BotTradingState for {self.symbol} reset complete.")

    def update_on_fill(self, filled_contracts: float, fill_price: float, filled_usd_value: float, order_purpose: str):
        """주문 체결에 따라 포지션 상태를 업데이트합니다."""
        _LOG.info(f"Updating position state for {self.symbol} due to '{order_purpose}' fill: "
                  f"Contracts={filled_contracts:.8f}, Price=${fill_price:.4f}, USDValue=${filled_usd_value:.2f}")

        if not self.is_in_position:
            self.current_avg_entry_price = fill_price
            self.total_position_contracts = filled_contracts
            self.total_position_initial_usd = filled_usd_value
            self.is_in_position = True
            if order_purpose == "entry":
                 _LOG.info("Initial entry successful. Position opened.")
        else:
            if order_purpose in ["take_profit", "stop_loss", "emergency_close"]:
                new_total_contracts = self.total_position_contracts + filled_contracts
                if abs(new_total_contracts) < 1e-8:
                    _LOG.info(f"{order_purpose.upper()} resulted in full position closure for {self.symbol}.")
                    self.reset()
                else:
                    _LOG.warning(f"{order_purpose.upper()} resulted in partial closure. Remaining: {new_total_contracts:.8f}. Resetting state.")
                    self.reset()
                return

            prev_abs_contracts = abs(self.total_position_contracts)
            new_abs_contracts = abs(filled_contracts)
            new_total_contracts_abs = prev_abs_contracts + new_abs_contracts

            if new_total_contracts_abs > 1e-9:
                self.current_avg_entry_price = \
                    ((self.current_avg_entry_price or 0) * prev_abs_contracts + fill_price * new_abs_contracts) / \
                    new_total_contracts_abs

            self.total_position_contracts += filled_contracts
            self.total_position_initial_usd += filled_usd_value

            if order_purpose == "split":
                 self.current_split_order_count += 1
                 _LOG.info(f"Split order {self.current_split_order_count} successful.")

        avg_price_str = f"{self.current_avg_entry_price:.4f}" if self.current_avg_entry_price is not None else "N/A"

        _LOG.info(f"Position state updated for {self.symbol}: AvgEntryPrice=${avg_price_str}, "
                  f"TotalContracts={self.total_position_contracts:.8f}, TotalInitialUSD=${self.total_position_initial_usd:.2f}, "
                  f"IsInPosition={self.is_in_position}")

# === [새로운 기능] ===
def determine_trade_direction(gate_client: GateIOClient, symbol: str, timeframe: str = '1h', short_window: int = 20, long_window: int = 50) -> Optional[Literal["long", "short"]]:
    """
    이동평균선 교차를 기반으로 최적의 거래 방향을 결정합니다.
    - 단기 이평선 > 장기 이평선 (골든 크로스 상태) -> 'long' 반환
    - 단기 이평선 < 장기 이평선 (데드 크로스 상태) -> 'short' 반환
    """
    click.secho(f"\n🔍 {timeframe} 봉 기준, {symbol}의 추세를 분석하여 최적 포지션을 결정합니다...", fg="cyan")
    _LOG.info(f"거래 방향 결정을 위해 {symbol}의 {timeframe} 캔들 데이터 조회 시작.")
    
    try:
        # Gate.io API를 통해 OHLCV 데이터 가져오기
        candlesticks = gate_client.futures_api.list_futures_candlesticks(
            settle='usdt', 
            contract=symbol, 
            interval=timeframe,
            limit=long_window + 5 # 계산에 필요한 최소한의 데이터 + 여유분
        )
        if not candlesticks or len(candlesticks) < long_window:
            _LOG.error(f"방향 결정을 위한 캔들 데이터가 충분하지 않습니다 (필요: {long_window}, 확보: {len(candlesticks)}).")
            return None

        # pandas DataFrame으로 변환
        df = pd.DataFrame([c.to_dict() for c in candlesticks], columns=['t', 'v', 'c', 'h', 'l', 'o'])
        df['t'] = pd.to_datetime(df['t'], unit='s')
        df.rename(columns={'t': 'timestamp', 'v': 'volume', 'c': 'close', 'h': 'high', 'l': 'low', 'o': 'open'}, inplace=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col])
        
        df.set_index('timestamp', inplace=True)
        df.sort_index(inplace=True)

        # 이동평균선 계산
        df['sma_short'] = df['close'].rolling(window=short_window).mean()
        df['sma_long'] = df['close'].rolling(window=long_window).mean()

        # 최신 데이터로 방향 결정
        last_short_sma = df['sma_short'].iloc[-1]
        last_long_sma = df['sma_long'].iloc[-1]

        _LOG.info(f"최신 이동평균선 분석: 단기({short_window}) SMA = {last_short_sma:.4f}, 장기({long_window}) SMA = {last_long_sma:.4f}")

        if last_short_sma > last_long_sma:
            click.secho(f"📈 상승 추세 감지 (골든 크로스 상태). 'LONG' 포지션을 추천합니다.", fg="green")
            return "long"
        else:
            click.secho(f"📉 하락 추세 감지 (데드 크로스 상태). 'SHORT' 포지션을 추천합니다.", fg="red")
            return "short"

    except ApiException as e:
        _LOG.error(f"API 오류로 캔들 데이터 조회 실패: {e}", exc_info=True)
        return None
    except Exception as e:
        _LOG.error(f"거래 방향 결정 중 예상치 못한 오류 발생: {e}", exc_info=True)
        return None

def prompt_config(gate_client: GateIOClient) -> Optional[BotConfig]:
    """사용자로부터 대화형으로 봇 설정을 입력받습니다."""
    click.secho("\n" + "="*10 + " 📈 신규 동적 자금 관리 전략 설정 " + "="*10, fg="yellow", bold=True)
    # 방향(direction)은 자동으로 결정되므로 프롬프트에서 제거하거나 기본값으로 둡니다.
    # direction = click.prompt("👉 거래 방향 (long/short)", type=click.Choice(["long", "short"]), default="long")
    symbol = click.prompt("👉 거래 대상 코인 (예: BTC_USDT)", default="BTC_USDT").upper().strip()
    leverage = click.prompt("👉 레버리지 (예: 10)", type=int, default=10)
    margin_mode = click.prompt("👉 마진 모드 (cross/isolated)", type=click.Choice(["cross", "isolated"]), default="isolated")
    click.secho("\n--- 💰 동적 자금 설정 (사용 가능 잔액 기준) ---", fg="green")
    entry_amount_pct = click.prompt("👉 첫 진입 금액 (% of available balance)", type=float, default=10.0)
    max_split_count = click.prompt("👉 분할매수 횟수", type=int, default=5)
    split_trigger_percents: List[float] = []
    split_amounts_pct: List[float] = []
    if max_split_count > 0:
        click.secho(f"� {max_split_count}번의 분할매수 트리거 퍼센트를 입력하세요 (음수로 입력 시 자동으로 방향에 맞게 변환됩니다)", fg="cyan")
        for i in range(max_split_count):
            trigger = click.prompt(f"  - {i+1}번째 분할 퍼센트 (%)", type=float, default=round(-10.0 - i*5.0, 1))
            split_trigger_percents.append(trigger)
        click.secho(f"👉 {max_split_count}번의 분할매수 금액 비율을 입력하세요 (% of available balance)", fg="cyan")
        for i in range(max_split_count):
            amount_pct = click.prompt(f"  - {i+1}번째 분할매수 금액 비율 (%)", type=float, default=round(12.0 + i*2, 1))
            split_amounts_pct.append(amount_pct)
    take_profit_pct_str = click.prompt("👉 익절 퍼센트 (레버리지 적용된 포지션 수익률 %)", type=str, default="5.0")
    take_profit_pct = float(take_profit_pct_str) if take_profit_pct_str.strip() else None
    stop_loss_pct_str = click.prompt("👉 손절 퍼센트 (레버리지 적용된 포지션 손실률 %)", type=str, default="5.0")
    stop_loss_pct = float(stop_loss_pct_str) if stop_loss_pct_str.strip() else None
    order_type = click.prompt("👉 주문 방식을 선택하세요 (market: 시장가 / limit: 지정가)", type=click.Choice(["market", "limit"]), default="market")
    click.echo("")
    repeat_after_tp = click.confirm("익절 후 반복 실행하시겠습니까? (y/n)", default=True)
    stop_after_sl = click.confirm("손절 후 봇을 정지하시겠습니까? (y/n)", default=False)
    enable_sl = click.confirm("손절 기능을 활성화하시겠습니까? (y/n)", default=True)
    cfg_data = {
        "direction": "long", # 임시 기본값, 나중에 자동으로 덮어쓰여짐
        "symbol": symbol, "leverage": leverage, "margin_mode": margin_mode,
        "entry_amount_pct_of_balance": entry_amount_pct,
        "max_split_count": max_split_count,
        "split_trigger_percents": split_trigger_percents,
        "split_amounts_pct_of_balance": split_amounts_pct,
        "take_profit_pct": take_profit_pct, "stop_loss_pct": stop_loss_pct,
        "order_type": order_type,
        "repeat_after_take_profit": repeat_after_tp, "stop_bot_after_stop_loss": stop_after_sl,
        "enable_stop_loss": enable_sl
    }
    try:
        config = BotConfig(**cfg_data)
        click.secho("\n✅ 기본 설정 완료. 자동 방향 결정을 진행합니다...", fg="green", bold=True)
        return config
    except ValueError as e:
        _LOG.error(f"봇 설정 값 유효성 검사 실패: {e}", exc_info=True)
        click.secho(f"\n❌ 설정 오류: {e}", fg="red", bold=True)
        click.echo("설정을 처음부터 다시 시작합니다.")
        return None

def show_summary_final(config: BotConfig):
    """최종 설정 요약을 출력합니다."""
    click.secho("\n" + "─"*18 + " 📊 최종 실행 설정 요약 " + "─"*18, fg="yellow", bold=True)
    direction_color = "green" if config.direction == "long" else "red"
    click.secho(f"{'자동 결정된 거래 방향:':<35} {config.direction.upper()}", fg=direction_color, bold=True)
    click.echo(f"{'거래 대상 코인:':<35} {config.symbol}")
    click.echo(f"{'레버리지:':<35} {config.leverage}")
    click.echo(f"{'마진 모드:':<35} {config.margin_mode}")
    click.echo(f"{'첫 진입 금액 (% of available balance):':<35} {config.entry_amount_pct_of_balance}%")
    click.echo(f"{'분할매수 횟수:':<35} {config.max_split_count}")
    click.echo(f"{'분할매수 퍼센트 (레버리지 손익):':<35} {config.split_trigger_percents}")
    click.echo(f"{'분할매수 금액 (% of available balance):':<35} {config.split_amounts_pct_of_balance}")
    click.echo(f"{'익절 퍼센트 (레버리지 손익):':<35} {config.take_profit_pct}%")
    click.echo(f"{'손절 퍼센트 (레버리지 손익):':<35} {config.stop_loss_pct}%")
    click.echo(f"{'주문 방식:':<35} {config.order_type}")
    click.echo(f"{'익절 후 반복 실행:':<35} {'Yes' if config.repeat_after_take_profit else 'No'}")
    click.echo(f"{'손절 후 봇 정지:':<35} {'Yes' if config.stop_bot_after_stop_loss else 'No'}")
    click.echo(f"{'손절 기능 활성화:':<35} {'Yes' if config.enable_stop_loss else 'No'}")
    click.echo("─"*55)

def show_summary(config: BotConfig, current_market_price: Optional[float], gate_client: GateIOClient, current_bot_state: BotTradingState):
    """실시간 봇 상태 요약을 출력합니다."""
    click.secho("\n" + "="*15 + " 🤖 봇 상태 및 설정 요약 " + "="*15, fg="yellow", bold=True)
    click.secho("\n[시장 및 계산 정보]", fg="cyan")
    if current_market_price is not None:
        click.echo(f"  현재 시장가 ({config.symbol:<10}): {current_market_price:.4f} USDT")
    else:
        click.echo(f"  현재 시장가 ({config.symbol:<10}): 정보 없음")
    actual_position_info = None
    try:
        actual_position_info = gate_client.get_position(config.symbol)
    except Exception as e:
        _LOG.error(f"{config.symbol} 실제 포지션 정보 조회 중 예외 발생: {e}", exc_info=True)
        click.secho(f"  (에러: {config.symbol} 실제 포지션 조회 중 오류 발생)", fg="red")
    if actual_position_info and actual_position_info.get('size') is not None and float(actual_position_info.get('size', 0)) != 0:
        click.secho("\n[실제 거래소 포지션]", fg="magenta")
        pos_size = float(actual_position_info['size'])
        pos_entry_price_str = actual_position_info.get('entry_price')
        pos_entry_price = float(pos_entry_price_str) if pos_entry_price_str is not None else 0.0
        pos_leverage = actual_position_info.get('leverage', 'N/A')
        pos_liq_price_api = actual_position_info.get('liq_price', 'N/A')
        pos_unreal_pnl = actual_position_info.get('unrealised_pnl', 'N/A')
        click.echo(f"  - 방향          : {'LONG' if pos_size > 0 else 'SHORT'}")
        click.echo(f"  - 진입가 (API)  : {pos_entry_price:.4f} USDT")
        click.echo(f"  - 수량 (API)    : {pos_size} {config.symbol.split('_')[0]}")
        click.echo(f"  - 레버리지 (API): {pos_leverage}x")
        click.echo(f"  - 청산가 (API)  : {pos_liq_price_api if pos_liq_price_api else 'N/A'} USDT")
        click.echo(f"  - 미실현 손익   : {pos_unreal_pnl} USDT")
    else:
        click.secho(f"\n[{config.symbol} 실제 거래소 포지션 없음 또는 정보 업데이트 중...]", fg="magenta")
    click.secho("\n[봇 내부 추적 상태]", fg="blue")
    if current_bot_state.is_in_position and current_bot_state.current_avg_entry_price is not None and current_market_price is not None:
        direction_display = config.direction.upper()
        avg_price = current_bot_state.current_avg_entry_price
        total_contracts = current_bot_state.total_position_contracts
        click.echo(f"  - 추적 방향     : {direction_display}")
        click.echo(f"  - 평균 진입가   : {avg_price:.4f} USDT")
        click.echo(f"  - 총 계약 수량  : {total_contracts:.8f} {config.symbol.split('_')[0]}")
        click.echo(f"  - 총 투입 원금  : {current_bot_state.total_position_initial_usd:.2f} USDT (추정치)")
        current_position_value_usd = abs(total_contracts) * current_market_price
        if config.direction == "long":
            pnl_usd = (current_market_price - avg_price) * total_contracts
        else:
            pnl_usd = (avg_price - current_market_price) * abs(total_contracts)
        market_pnl_pct = (current_market_price - avg_price) / avg_price if avg_price > 0 else 0
        if config.direction == "short":
            market_pnl_pct *= -1
        leveraged_roe_pct = market_pnl_pct * config.leverage * 100
        click.echo(f"  - 현재 평가액    : {current_position_value_usd:,.2f} USDT")
        pnl_color = "green" if pnl_usd >= 0 else "red"
        click.secho(f"  - 손익 금액(추정): {pnl_usd:,.2f} USDT", fg=pnl_color)
        click.secho(f"  - 손익률(ROE)   : {leveraged_roe_pct:.2f}%", fg=pnl_color)
        click.echo(f"  - 분할매수 횟수 : {current_bot_state.current_split_order_count} / {config.max_split_count}")
        liq_price_calc, change_pct_calc = calculate_liquidation_price(   
            total_position_collateral_usd=current_bot_state.total_position_initial_usd,
            leverage=config.leverage, margin_mode=config.margin_mode,
            avg_entry_price=current_bot_state.current_avg_entry_price, position_direction=config.direction
        )
        if liq_price_calc is not None and change_pct_calc is not None:
            change_display_char = '-' if config.direction == 'long' else '+'
            click.secho(f"  예상 청산가(계산): {liq_price_calc:.4f} USDT ({change_display_char}{abs(change_pct_calc):.2f}% from avg entry)", fg="magenta")
        if config.take_profit_pct:
            market_move_pct = config.take_profit_pct / config.leverage
            tp_target_price = current_bot_state.current_avg_entry_price * (1 + (market_move_pct / 100.0) * (1 if config.direction == "long" else -1))
            click.echo(f"  익절 목표가 (ROE {config.take_profit_pct}%): {tp_target_price:.4f} USDT")
        if config.enable_stop_loss and config.stop_loss_pct:
            market_move_pct = config.stop_loss_pct / config.leverage
            sl_target_price = current_bot_state.current_avg_entry_price * (1 - (market_move_pct / 100.0) * (1 if config.direction == "long" else -1))
            click.echo(f"  손절 목표가 (ROE -{config.stop_loss_pct}%): {sl_target_price:.4f} USDT")
    else:
        click.echo("  (현재 봇 내부 추적 포지션 없음)")
    click.echo("="*50 + "\n")

def _execute_order_and_update_state(gate_client: GateIOClient, config: BotConfig, current_bot_state: BotTradingState, order_usd_amount: float, order_purpose: Literal["entry", "split", "take_profit", "stop_loss", "emergency_close"]) -> bool:
    """주문 실행 및 상태 업데이트 헬퍼 함수"""
    is_closing_order = order_purpose in ["take_profit", "stop_loss", "emergency_close"]
    reduce_only_flag = is_closing_order
    if is_closing_order:
        if not current_bot_state.is_in_position:
            _LOG.warning(f"{order_purpose} 주문 시도 중 포지션 없음. 주문 건너뜀.")
            return False
        order_execution_side = "short" if config.direction == "long" else "long"
    else:
        order_execution_side = config.direction
    order_id_suffix = f"{order_purpose}"
    if order_purpose == 'split':
        order_id_suffix += f"-{current_bot_state.current_split_order_count + 1}"
    full_order_id_prefix = config.order_id_prefix + order_id_suffix
    usd_amount_for_api_call = order_usd_amount
    if is_closing_order:
        current_market_price = gate_client.fetch_last_price(config.symbol)
        if current_market_price is None:
            _LOG.error(f"{order_purpose} 주문 위한 현재가 조회 실패. 주문 건너뜀.")
            return False
        position_value_usd = abs(current_bot_state.total_position_contracts) * current_market_price
        if position_value_usd < 1:
            _LOG.warning(f"{order_purpose} 주문 위한 포지션 가치(${position_value_usd:.4f})가 너무 작음. 주문 건너뜀.")
            if abs(current_bot_state.total_position_contracts) < 1e-8:
                current_bot_state.reset()
            return False
        usd_amount_for_api_call = position_value_usd
    effective_order_type = "market" if is_closing_order else config.order_type
    order_result = gate_client.place_order(
        contract_symbol=config.symbol, order_amount_usd=usd_amount_for_api_call,
        position_side=order_execution_side, leverage=config.leverage,
        order_type=effective_order_type, reduce_only=reduce_only_flag,
        order_id_prefix=full_order_id_prefix
    )
    if order_result and order_result.get("id"):
        order_id = order_result.get("id")
        _LOG.info(f"{order_purpose.upper()} 주문 성공적으로 API에 접수됨. ID: {order_id}, 상태: {order_result.get('status')}")
        if effective_order_type == "market":
            time.sleep(2)
            filled_order_info = gate_client.get_order_status(order_id)
            if filled_order_info and filled_order_info.get('size') is not None and float(filled_order_info.get('size', 0)) != 0:
                actual_fill_price_str = filled_order_info.get('fill_price')
                if not actual_fill_price_str:
                    _LOG.error(f"주문({order_id}) 체결 정보에 'fill_price'가 없어 상태 업데이트 불가.")
                    return False
                actual_fill_price = float(actual_fill_price_str)
                actual_filled_contracts = float(filled_order_info.get('size'))
                actual_filled_usd = abs(actual_filled_contracts) * actual_fill_price
                _LOG.info(f"체결 정보 확인: 가격=${actual_fill_price:.4f}, 계약수량={actual_filled_contracts:.8f}")
                current_bot_state.update_on_fill(actual_filled_contracts, actual_fill_price, actual_filled_usd, order_purpose)
            else:
                _LOG.error(f"시장가 주문({order_id}) 체결 정보 확인 실패. 상태 업데이트 불가.")
                return False
        return True
    else:
        _LOG.error(f"{order_purpose.upper()} 주문 실패 또는 API로부터 유효한 응답 받지 못함.")
        return False

def run_strategy(config: BotConfig, gate_client: GateIOClient, current_bot_state: BotTradingState, stop_event: threading.Event):
    """메인 거래 전략 실행 루프"""
    _LOG.info(f"'{config.symbol}'에 대한 거래 전략 시작. 설정: {config.to_dict()}")
    if not current_bot_state.is_in_position:
        click.secho(f"\n🚀 초기 진입 주문 시도 ({config.direction.upper()}) for {config.symbol}...", fg="green", bold=True)
        account_info = gate_client.get_account_info()
        if not account_info or not account_info.get('available'):
            _LOG.critical("초기 진입 위한 계좌 잔액 조회 실패. 전략 시작 불가.")
            return
        available_balance = float(account_info['available'])
        entry_usd_to_invest = available_balance * (config.entry_amount_pct_of_balance / 100.0)
        _LOG.info(f"첫 진입 투자 금액 계산: {entry_usd_to_invest:.4f} USDT")
        if not _execute_order_and_update_state(gate_client, config, current_bot_state, entry_usd_to_invest, "entry"):
            _LOG.critical("초기 진입 주문 실패.")
            return
    while not stop_event.is_set():
        try:
            if not current_bot_state.is_in_position:
                if config.repeat_after_take_profit:
                    _LOG.info(f"포지션 없음. '익절 후 반복' 설정에 따라 재진입 시도.")
                    # === [수정] 재진입 시에도 방향을 다시 결정 ===
                    base_config_dict = config.to_dict()
                    determined_direction = determine_trade_direction(gate_client, config.symbol)
                    if not determined_direction:
                        _LOG.error("재진입 방향을 결정할 수 없어 대기합니다.")
                        time.sleep(config.check_interval_seconds * 5)
                        continue
                    
                    base_config_dict["direction"] = determined_direction
                    # 롱/숏 방향에 관계없이 분할매수 트리거는 항상 음수여야 합니다.
                    base_config_dict["split_trigger_percents"] = [
                        abs(p) * -1 for p in config.split_trigger_percents
                    ]

                    new_config = BotConfig(**base_config_dict)
                    show_summary_final(new_config) # 사용자에게 새 방향 알림
                    
                    current_bot_state.reset()
                    account_info = gate_client.get_account_info()
                    if account_info and account_info.get('available'):
                        available_balance = float(account_info['available'])
                        entry_usd_to_invest = available_balance * (new_config.entry_amount_pct_of_balance / 100.0)
                        if not _execute_order_and_update_state(gate_client, new_config, current_bot_state, entry_usd_to_invest, "entry"):
                            _LOG.error("재진입 주문에 실패했습니다. 다음 루프에서 재시도합니다.")
                    else:
                        _LOG.error("재진입을 위한 계좌 정보 조회에 실패했습니다.")
                else:
                    _LOG.info("포지션 없음. 반복 설정이 꺼져있으므로 전략을 종료합니다.")
                    break
                time.sleep(config.check_interval_seconds)
                continue
            _LOG.info(f"'{config.symbol}' 전략 루프 시작. 분할매수 횟수: {current_bot_state.current_split_order_count}")
            current_market_price = gate_client.fetch_last_price(config.symbol)
            if current_market_price is None:
                _LOG.warning(f"현재가를 가져올 수 없습니다. 다음 루프에서 재시도합니다.")
                time.sleep(config.check_interval_seconds)
                continue
            show_summary(config, current_market_price, gate_client, current_bot_state)
            avg_price = current_bot_state.current_avg_entry_price
            if avg_price is None or avg_price <= 1e-9:
                _LOG.warning("평균 진입가가 없어 PnL 계산이 불가합니다. 다음 루프에서 재시도합니다.")
                time.sleep(config.check_interval_seconds)
                continue
            
            market_pnl_pct = (current_market_price - avg_price) / avg_price
            if config.direction == "short":
                market_pnl_pct *= -1
            
            leveraged_roe_pct = market_pnl_pct * config.leverage * 100
            _LOG.info(f"시장 변동률: {market_pnl_pct*100:.4f}%, 레버리지 적용 손익(ROE): {leveraged_roe_pct:.4f}%")

            if config.take_profit_pct and leveraged_roe_pct >= config.take_profit_pct:
                _LOG.info(f"익절 조건 도달 (현재 ROE: {leveraged_roe_pct:.2f}% >= 목표: {config.take_profit_pct}%)")
                _execute_order_and_update_state(gate_client, config, current_bot_state, 0, "take_profit")
                # 익절 후에는 루프 시작 부분에서 재진입 로직을 타게 됨
                continue

            if config.enable_stop_loss and config.stop_loss_pct and leveraged_roe_pct <= -config.stop_loss_pct:
                _LOG.warning(f"손절 조건 도달 (현재 ROE: {leveraged_roe_pct:.2f}% <= 목표: -{config.stop_loss_pct}%)")
                _execute_order_and_update_state(gate_client, config, current_bot_state, 0, "stop_loss")
                if config.stop_bot_after_stop_loss:
                    _LOG.warning("손절 후 봇 자동 정지 설정에 따라 봇을 종료합니다.")
                    break
                # 손절 후에는 루프 시작 부분에서 재진입 로직을 타게 됨
                continue
            
            if current_bot_state.current_split_order_count < config.max_split_count:
                next_split_trigger_pct = config.split_trigger_percents[current_bot_state.current_split_order_count]
                if leveraged_roe_pct <= next_split_trigger_pct:
                    _LOG.info(f"분할매수 조건 도달. (현재 ROE: {leveraged_roe_pct:.2f}%, 트리거: {next_split_trigger_pct}%)")
                    account_info = gate_client.get_account_info()
                    if account_info and account_info.get('available'):
                        available_balance = float(account_info['available'])
                        split_amount_pct = config.split_amounts_pct_of_balance[current_bot_state.current_split_order_count]
                        split_usd_to_invest = available_balance * (split_amount_pct / 100.0)
                        _execute_order_and_update_state(gate_client, config, current_bot_state, split_usd_to_invest, "split")
                    else:
                        _LOG.error("분할매수를 위한 계좌 정보 조회에 실패했습니다.")

            if not stop_event.is_set():
                _LOG.debug(f"{config.check_interval_seconds}초 대기...")
                for _ in range(config.check_interval_seconds):
                    if stop_event.is_set(): break
                    time.sleep(1)
        except Exception as e:
            _LOG.error(f"전략 실행 중 예상치 못한 오류: {e}", exc_info=True)
            time.sleep(config.check_interval_seconds)
    
    _LOG.info(f"'{config.symbol}' 전략 루프 종료.")
    if stop_event.is_set():
        _LOG.warning("종료 신호 수신. 최종 포지션 정리 상태를 확인합니다...")
        time.sleep(1)
        final_position = gate_client.get_position(config.symbol)
        if final_position and final_position.get('size') is not None and int(float(final_position.get('size', 0))) != 0:
            pos_size = int(float(final_position.get('size')))
            _LOG.info(f"아직 포지션({pos_size})이 남아있어 최종 청산을 시도합니다.")
            if gate_client.close_position_market(config.symbol, pos_size):
                click.secho(f"✅ {config.symbol} 포지션이 성공적으로 청산되었습니다.", fg="green")
            else:
                click.secho(f"❌ {config.symbol} 포지션 청산 실패. 거래소 확인 필요.", fg="red")
        else:
            _LOG.info("포지션이 이미 정리되었거나 없습니다. 추가 작업 없이 종료합니다.")

def handle_emergency_stop(gate_client: GateIOClient, stop_event: threading.Event):
    """모든 포지션을 조회하고 청산한 후, 종료 신호를 보냅니다."""
    click.secho("\n🚨 긴급 정지 명령 수신! 모든 포지션을 정리합니다...", fg="red", bold=True)
    try:
        open_positions = gate_client.list_all_positions()
        if not open_positions:
            click.secho("✅ 현재 보유 중인 포지션이 없습니다.", fg="green")
        else:
            click.echo(f"  -> {len(open_positions)}개의 포지션을 발견했습니다. 시장가로 청산을 시도합니다.")
            for pos in open_positions:
                contract = pos.get('contract')
                size_str = pos.get('size')
                size = int(float(size_str)) if size_str is not None else 0
                if contract and size != 0:
                    click.echo(f"    - 청산 시도: {contract} (수량: {size})")
                    close_order_result = gate_client.close_position_market(contract, size)
                    if close_order_result and close_order_result.get('id'):
                        click.secho(f"      -> ✅ 청산 주문 성공. 주문 ID: {close_order_result.get('id')}", fg="green")
                    else:
                        click.secho(f"      -> ❌ '{contract}' 청산 주문 실패. 거래소에서 직접 확인해주세요.", fg="red")
                else:
                    click.secho(f"    - ⚠️ 잘못된 포지션 데이터, 건너뜁니다: {pos}", fg="yellow")
    except Exception as e:
        _LOG.error(f"긴급 정지 중 오류 발생: {e}", exc_info=True)
        click.secho(f"❌ 포지션 정리 중 오류가 발생했습니다. 로그를 확인하고 거래소에서 직접 포지션을 확인해주세요.", fg="red")
    click.echo("   -> 실행 중인 전략 스레드에 종료 신호를 보냅니다...")
    stop_event.set()

def select_config(config_dir: Path) -> Optional[BotConfig | str]:
    """설정 파일 목록을 보여주고 사용자 선택을 받습니다."""
    config_dir.mkdir(exist_ok=True)
    config_files = sorted(list(config_dir.glob("*.json")))
    click.secho("\n" + "="*15 + " ⚙️ 거래 전략 설정 선택 " + "="*15, fg="yellow", bold=True)
    if not config_files:
        click.echo("저장된 설정 파일이 없습니다.")
    else:
        click.echo("저장된 설정 파일 목록:")
        for i, file in enumerate(config_files):
            click.echo(f"  [{i+1}] {file.name}")
    click.echo("-" * 50)
    click.echo(f"  [n] 📝 새 설정 만들기 (대화형)")
    click.echo(f"  [q] 🚪 종료")
    click.echo("=" * 50)
    choice = click.prompt("👉 실행할 설정 번호를 입력하거나, 'n' 또는 'q'를 입력하세요", type=str, default="n")
    if choice.lower() == 'q':
        return "exit"
    if choice.lower() == 'n':
        return "new"
    try:
        choice_index = int(choice) - 1
        if 0 <= choice_index < len(config_files):
            selected_file = config_files[choice_index]
            return BotConfig.load(selected_file)
        else:
            click.secho("❌ 잘못된 번호입니다. 다시 선택해주세요.", fg="red")
            return None
    except ValueError:
        click.secho("❌ 잘못된 입력입니다. 번호 또는 'n'/'q'를 입력해주세요.", fg="red")
        return None

@click.command(context_settings=dict(help_option_names=['-h', '--help']))
@click.option(
    '--config-file', '-c',
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help="JSON 설정 파일 경로. 지정하면 메뉴를 건너뛰고 바로 실행합니다."
)
@click.option(
    '--smoke-test',
    is_flag=True,
    help="실제 거래 없이 API 연결 및 기본 기능 테스트를 실행합니다."
)
@click.option(
    '--contract',
    default="BTC_USDT",
    show_default=True,
    help="--smoke-test 모드에서 사용할 선물 계약 심볼."
)
def main(config_file: Optional[Path], smoke_test: bool, contract: str) -> None:
    _LOG.info("="*10 + " 자동매매 봇 CLI 시작 " + "="*10)
    gate_client: GateIOClient
    try:
        gate_client = GateIOClient()
    except (EnvironmentError, ApiException, Exception) as e:
        _LOG.critical(f"GateIOClient 초기화 실패: {e}", exc_info=True)
        click.secho(f"❌ 치명적 오류: 봇 초기화에 실패했습니다. 로그를 확인해주세요.", fg="red", bold=True)
        sys.exit(1)
    if smoke_test:
        click.secho(f"\n🕵️ SMOKE TEST 모드 실행 (계약: {contract})...", fg="magenta", bold=True)
        sys.exit(0)
    
    # 1. 기본 설정 불러오기
    base_bot_configuration: Optional[BotConfig] = None
    if config_file:
        try:
            base_bot_configuration = BotConfig.load(config_file)
            click.secho(f"\n✅ 기본 설정 파일 로드 성공: {config_file.resolve()}", fg="green")
        except Exception as e:
            _LOG.error(f"지정된 설정 파일 '{config_file.resolve()}' 로드 실패: {e}", exc_info=True)
            click.secho(f"❌ 설정 파일 로드 오류: {e}", fg="red")
            sys.exit(1)
    else:
        project_root = Path(__file__).resolve().parents[2]
        config_dir = project_root / "Bot"
        while base_bot_configuration is None:
            user_choice = select_config(config_dir)
            if user_choice == "exit":
                _LOG.info("사용자가 메뉴에서 종료를 선택했습니다.")
                sys.exit(0)
            elif user_choice == "new":
                base_bot_configuration = prompt_config(gate_client)
                if base_bot_configuration is None:
                    if not click.confirm("\n설정 중 오류가 발생했습니다. 다시 시도하시겠습니까?", default=True):
                        _LOG.info("사용자가 설정 재시도를 원치 않아 종료합니다.")
                        sys.exit(0)
            elif isinstance(user_choice, BotConfig):
                base_bot_configuration = user_choice
                click.secho(f"\n✅ '{user_choice.symbol}' 기본 설정 로드 완료.", fg="green")

    # 2. 자동 방향 결정 로직
    determined_direction = determine_trade_direction(gate_client, base_bot_configuration.symbol)
    if not determined_direction:
        click.secho("❌ 추세 분석 실패로 거래 방향을 결정할 수 없습니다. 잠시 후 다시 시도해주세요.", fg="red", bold=True)
        sys.exit(1)

    # 3. 최종 설정 생성
    final_config_dict = base_bot_configuration.to_dict()
    final_config_dict['direction'] = determined_direction
    
    # 롱/숏 방향에 관계없이 분할매수 트리거는 항상 음수여야 합니다.
    final_config_dict['split_trigger_percents'] = [
        abs(p) * -1 for p in base_bot_configuration.split_trigger_percents
    ]
    
    final_bot_configuration = BotConfig(**final_config_dict)

    # 4. 최종 설정으로 실행
    show_summary_final(final_bot_configuration)

    if click.confirm("\n❓ 이 설정을 파일로 저장하시겠습니까?", default=False):
        project_root = Path(__file__).resolve().parents[2]
        config_dir = project_root / "Bot"
        config_dir.mkdir(exist_ok=True)
        default_save_path = config_dir / f"{final_bot_configuration.symbol.lower()}_autodir_config.json"
        save_path_str = click.prompt("설정 저장 경로 또는 파일명 입력", default=str(default_save_path))
        save_path_obj = Path(save_path_str)
        if save_path_obj.is_dir():
            final_save_path = save_path_obj / default_save_path.name
        else:
            final_save_path = save_path_obj
        try:
            final_bot_configuration.save(final_save_path)
        except Exception as e:
            _LOG.error(f"설정 파일 저장 실패 ('{final_save_path}'): {e}", exc_info=True)
            click.secho(f"⚠️ 설정 파일 저장 실패: {e}", fg="yellow")

    if click.confirm("\n▶️ 위 설정으로 자동매매를 시작하시겠습니까?", default=True):
        _LOG.info(f"사용자 확인. '{final_bot_configuration.symbol}' 자동매매 시작.")
        click.secho(f"🚀 '{final_bot_configuration.symbol}' 자동매매 시작...", fg="green", bold=True)
        
        current_bot_trading_state = BotTradingState(symbol=final_bot_configuration.symbol)
        
        stop_event = threading.Event()
        
        strategy_thread = threading.Thread(
            target=run_strategy, 
            args=(final_bot_configuration, gate_client, current_bot_trading_state, stop_event),
            daemon=True
        )
        strategy_thread.start()
        
        click.secho("\n✅ 자동매매가 백그라운드에서 실행 중입니다.", fg="cyan")
        click.secho("🛑 모든 포지션을 청산하고 종료하려면 'stop'을 입력하고 Enter를 누르세요.", fg="yellow", bold=True)
        
        try:
            while strategy_thread.is_alive():
                user_input = input()
                if user_input.strip().lower() == 'stop':
                    handle_emergency_stop(gate_client, stop_event)
                    break 
                else:
                    click.echo("   (종료하시려면 'stop'을 입력해주세요...)")

        except KeyboardInterrupt:
            click.echo("\n🛑 Ctrl+C 감지. 봇 종료 신호를 보냅니다...")
            _LOG.warning("메인 스레드에서 Ctrl+C 감지. 전략 스레드에 종료 신호 전송.")
            handle_emergency_stop(gate_client, stop_event)

        click.echo("   -> 포지션 정리 및 종료를 기다리는 중...")
        strategy_thread.join(timeout=30)
        
        if strategy_thread.is_alive():
            _LOG.error("전략 스레드가 제 시간 내에 종료되지 않았습니다. 강제 종료될 수 있습니다.")
            click.secho("⚠️ 스레드가 제 시간 내에 종료되지 않았습니다.", fg="red")

        click.secho(f"\n🏁 '{final_bot_configuration.symbol}' 자동매매 전략이 종료되었습니다.", fg="blue", bold=True)
    else:
        _LOG.info("사용자가 자동매매 시작을 선택하지 않았습니다.")
        click.secho("👋 자동매매가 시작되지 않았습니다. 프로그램을 종료합니다.", fg="yellow")

    _LOG.info("="*10 + " 자동매매 봇 CLI 종료 " + "="*10)