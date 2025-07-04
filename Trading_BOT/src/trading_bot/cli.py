# src/trading_bot/cli.py
import time
import click
import logging
import sys
import threading # 스레딩 기능 추가
from pathlib import Path
from typing import Optional, List, Dict, Any, Literal

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
            if order_purpose in ["take_profit", "stop_loss"]:
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

def prompt_config(gate_client: GateIOClient) -> Optional[BotConfig]:
    """사용자로부터 대화형으로 봇 설정을 입력받습니다."""
    click.secho("\n" + "="*10 + " 📈 신규 자동매매 전략 설정 " + "="*10, fg="yellow", bold=True)
    
    direction = click.prompt("👉 거래 방향 (long/short)", type=click.Choice(["long", "short"]), default="long")
    symbol = click.prompt("👉 거래 대상 코인 (예: BTC_USDT)", default="BTC_USDT").upper().strip()
    leverage = click.prompt("👉 레버리지 (예: 5)", type=int, default=15)
    margin_mode = click.prompt("👉 마진 모드 (cross/isolated)", type=click.Choice(["cross", "isolated"]), default="cross")
    entry_amount_usd = click.prompt("👉 첫 진입 금액 (USDT)", type=float, default=54.0)
    
    max_split_count = click.prompt("👉 분할매수 횟수", type=int, default=6)
    
    split_trigger_percents: List[float] = []
    split_amounts_usd: List[float] = []
    if max_split_count > 0:
        pct_header = "음수: 하락 기준" if direction == "long" else "양수: 상승 기준"
        click.secho(f"👉 {max_split_count}번의 분할매수 퍼센트를 입력하세요 ({pct_header})", fg="cyan")
        for i in range(max_split_count):
            trigger = click.prompt(f"  - {i+1}번째 분할 퍼센트 (%)", type=float)
            split_trigger_percents.append(trigger)
        
        click.secho(f"👉 {max_split_count}번의 분할매수 금액을 입력하세요 (예: 50, 100, ...)", fg="cyan")
        for i in range(max_split_count):
            amount = click.prompt(f"  - {i+1}번째 분할매수 금액 (USDT)", type=float)
            split_amounts_usd.append(amount)

    take_profit_pct_str = click.prompt("👉 익절 퍼센트 (평균 진입가 대비 %)", type=str, default="6.0")
    take_profit_pct = float(take_profit_pct_str) if take_profit_pct_str.strip() else None
    
    stop_loss_pct_str = click.prompt("👉 손절 퍼센트 (평균 진입가 대비 %)", type=str, default="5.0")
    stop_loss_pct = float(stop_loss_pct_str) if stop_loss_pct_str.strip() else None
    
    order_type = click.prompt("👉 주문 방식을 선택하세요 (market: 시장가 / limit: 지정가)", type=click.Choice(["market", "limit"]), default="market")
    
    click.echo("🔍 현재 코인 가격을 API로 조회합니다...")
    current_market_price = gate_client.fetch_last_price(symbol)
    if current_market_price is None:
        click.secho(f"❌ '{symbol}'의 현재 가격을 조회할 수 없습니다. 네트워크나 심볼 이름을 확인해주세요.", fg="red", bold=True)
        return None
    click.secho(f"  - 현재 {symbol} 가격: {current_market_price:.4f} USDT", fg="green")
    
    total_collateral_for_liq_calc = entry_amount_usd + sum(split_amounts_usd)
    liq_price, change_pct = calculate_liquidation_price(
        total_position_collateral_usd=total_collateral_for_liq_calc,
        leverage=leverage,
        margin_mode=margin_mode,
        avg_entry_price=current_market_price,
        position_direction=direction
    )

    if liq_price is not None and change_pct is not None:
        click.secho(f"\n📊 강제 청산가 계산 완료: {liq_price:.2f} USDT", fg="magenta", bold=True)
        change_direction_text = "하락" if direction == "long" else "상승"
        click.secho(f"💥 강제 청산가까지 {change_direction_text} %: {abs(change_pct):.2f}%", fg="magenta")
    else:
        click.secho("\n⚠️ 강제 청산가를 계산할 수 없습니다 (입력값 확인 필요).", fg="yellow")

    click.echo("")
    repeat_after_tp = click.confirm("익절 후 반복 실행하시겠습니까? (y/n)", default=True)
    stop_after_sl = click.confirm("손절 후 봇을 정지하시겠습니까? (y/n)", default=False)
    enable_sl = click.confirm("손절 기능을 활성화하시겠습니까? (y/n)", default=True)

    cfg_data = {
        "direction": direction, "symbol": symbol, "leverage": leverage, "margin_mode": margin_mode,
        "entry_amount_usd": entry_amount_usd, "max_split_count": max_split_count,
        "split_trigger_percents": split_trigger_percents, "split_amounts_usd": split_amounts_usd,
        "take_profit_pct": take_profit_pct, "stop_loss_pct": stop_loss_pct,
        "order_type": order_type,
        "repeat_after_take_profit": repeat_after_tp, "stop_bot_after_stop_loss": stop_after_sl,
        "enable_stop_loss": enable_sl
    }
    
    try:
        config = BotConfig(**cfg_data)
        click.secho("\n✅ 설정 완료. 자동매매 시작 준비 중...", fg="green", bold=True)
        return config
    except ValueError as e:
        _LOG.error(f"봇 설정 값 유효성 검사 실패: {e}", exc_info=True)
        click.secho(f"\n❌ 설정 오류: {e}", fg="red", bold=True)
        click.echo("설정을 처음부터 다시 시작합니다.")
        return None 

def show_summary_final(config: BotConfig):
    """최종 설정 요약을 출력합니다."""
    click.secho("\n" + "─"*18 + " 📊 설정 요약 " + "─"*18, fg="yellow", bold=True)
    click.echo(f"{'거래 방향:':<25} {config.direction}")
    click.echo(f"{'거래 대상 코인:':<25} {config.symbol}")
    click.echo(f"{'레버리지:':<25} {config.leverage}")
    click.echo(f"{'마진 모드:':<25} {config.margin_mode}")
    click.echo(f"{'첫 진입 금액:':<25} {config.entry_amount_usd}")
    click.echo(f"{'분할매수 횟수:':<25} {config.max_split_count}")
    click.echo(f"{'분할매수 퍼센트:':<25} {config.split_trigger_percents}")
    click.echo(f"{'분할매수 금액:':<25} {config.split_amounts_usd}")
    click.echo(f"{'익절 퍼센트 (평균가 대비):':<25} {config.take_profit_pct}%")
    click.echo(f"{'손절 퍼센트 (평균가 대비):':<25} {config.stop_loss_pct}%")
    click.echo(f"{'주문 방식:':<25} {config.order_type}")
    click.echo(f"{'익절 후 반복 실행:':<25} {'Yes' if config.repeat_after_take_profit else 'No'}")
    click.echo(f"{'손절 후 봇 정지:':<25} {'Yes' if config.stop_bot_after_stop_loss else 'No'}")
    click.echo(f"{'손절 기능 활성화:':<25} {'Yes' if config.enable_stop_loss else 'No'}")
    click.echo("─"*48)

def show_summary(config: BotConfig, current_market_price: Optional[float], gate_client: GateIOClient, current_bot_state: BotTradingState):
    """실시간 봇 상태 요약을 출력합니다."""
    click.secho("\n" + "="*15 + " 📊 봇 상태 및 설정 요약 " + "="*15, fg="yellow", bold=True)
    
    click.secho("[봇 설정]", fg="cyan")
    config_dict = config.to_dict()
    for k, v in config_dict.items():
        click.echo(f"  {k:<28}: {v}")
    
    click.secho("\n[시장 및 계산 정보]", fg="cyan")
    if current_market_price is not None:
        click.echo(f"  현재 시장가 ({config.symbol:<10}): {current_market_price:.4f} USDT")
    else:
        click.echo(f"  현재 시장가 ({config.symbol:<10}): 정보 없음")

    actual_position_info = None
    try:
        actual_position_info = gate_client.get_position(config.symbol)
    except ApiException as e:
        _LOG.warning(f"{config.symbol} 실제 포지션 정보 조회 중 API 오류: {e.body}", exc_info=True)
        click.secho(f"  (경고: {config.symbol} 실제 포지션 조회 실패 - API 오류)", fg="red")
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
        click.echo(f"  - 청산가 (API)  : {pos_liq_price_api} USDT")
        click.echo(f"  - 미실현 손익   : {pos_unreal_pnl} USDT")
    else:
        click.secho(f"\n[{config.symbol} 실제 거래소 포지션 없음 또는 정보 업데이트 중...]", fg="magenta")

    click.secho("\n[봇 내부 추적 상태]", fg="blue")
    if current_bot_state.is_in_position and current_bot_state.current_avg_entry_price is not None:
        bot_tracked_direction_consistent = \
            (config.direction == "long" and current_bot_state.total_position_contracts > 0) or \
            (config.direction == "short" and current_bot_state.total_position_contracts < 0)
        
        direction_display = config.direction.upper()
        if not bot_tracked_direction_consistent:
            direction_display += " (경고: 내부 상태와 설정 불일치!)"

        click.echo(f"  - 추적 방향     : {direction_display}")
        click.echo(f"  - 평균 진입가   : {current_bot_state.current_avg_entry_price:.4f} USDT")
        click.echo(f"  - 총 계약 수량  : {current_bot_state.total_position_contracts:.8f} {config.symbol.split('_')[0]}")
        click.echo(f"  - 총 투입 원금  : {current_bot_state.total_position_initial_usd:.2f} USDT (추정치)")
        click.echo(f"  - 분할매수 횟수 : {current_bot_state.current_split_order_count} / {config.max_split_count}")

        liq_price_calc, change_pct_calc = calculate_liquidation_price(
            total_position_collateral_usd=current_bot_state.total_position_initial_usd,
            leverage=config.leverage,
            margin_mode=config.margin_mode,
            avg_entry_price=current_bot_state.current_avg_entry_price,
            position_direction=config.direction
        )
        if liq_price_calc is not None and change_pct_calc is not None:
            change_display_char = '-' if config.direction == 'long' else '+'
            click.secho(f"  예상 청산가(계산): {liq_price_calc:.4f} USDT "
                        f"({change_display_char}{abs(change_pct_calc):.2f}% from avg entry)",
                        fg="magenta")
        else:
            click.secho("  예상 청산가(계산): 계산 불가", fg="magenta")
            
        if config.take_profit_pct:
            tp_target_price = current_bot_state.current_avg_entry_price * \
                              (1 + (config.take_profit_pct / 100.0) * (1 if config.direction == "long" else -1))
            click.echo(f"  익절 목표가     : {tp_target_price:.4f} USDT (+{config.take_profit_pct}%)")
        if config.enable_stop_loss and config.stop_loss_pct:
            sl_target_price = current_bot_state.current_avg_entry_price * \
                              (1 - (config.stop_loss_pct / 100.0) * (1 if config.direction == "long" else -1))
            click.echo(f"  손절 목표가     : {sl_target_price:.4f} USDT (-{config.stop_loss_pct}%)")
    else:
        click.echo("  (현재 봇 내부 추적 포지션 없음)")

    click.echo("="*50 + "\n")

def _execute_order_and_update_state(
    gate_client: GateIOClient,
    config: BotConfig,
    current_bot_state: BotTradingState,
    order_usd_amount: float,
    order_purpose: Literal["entry", "split", "take_profit", "stop_loss"]
) -> bool:
    """주문 실행 및 상태 업데이트 헬퍼 함수"""
    is_tp_sl_order = order_purpose in ["take_profit", "stop_loss"]
    reduce_only_flag = is_tp_sl_order
    
    if is_tp_sl_order:
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
    if is_tp_sl_order:
        current_market_price = gate_client.fetch_last_price(config.symbol)
        if current_market_price is None:
            _LOG.error(f"{order_purpose} 주문 위한 현재가 조회 실패. 주문 건너뜀.")
            return False
        usd_amount_for_api_call = abs(current_bot_state.total_position_contracts) * current_market_price
        _LOG.info(f"{order_purpose} 주문: 전체 포지션 청산 시도. "
                  f"계약수량={abs(current_bot_state.total_position_contracts):.8f}, "
                  f"추정USD가치=${usd_amount_for_api_call:.2f}")
        if usd_amount_for_api_call < 1e-2:
            _LOG.warning(f"{order_purpose} 주문 위한 포지션 가치가 너무 작음 (${usd_amount_for_api_call:.2f}). 주문 건너뜀.")
            if abs(current_bot_state.total_position_contracts) < 1e-8 :
                current_bot_state.reset()
            return False

    limit_order_price_for_api: Optional[float] = None
    effective_order_type = "market" if is_tp_sl_order else config.order_type
    
    if effective_order_type == "limit":
        if order_purpose == "take_profit" and current_bot_state.current_avg_entry_price and config.take_profit_pct:
            limit_order_price_for_api = current_bot_state.current_avg_entry_price * \
                (1 + (config.take_profit_pct / 100.0) * (1 if config.direction == "long" else -1))
        elif order_purpose == "stop_loss" and current_bot_state.current_avg_entry_price and config.stop_loss_pct:
             limit_order_price_for_api = current_bot_state.current_avg_entry_price * \
                (1 - (config.stop_loss_pct / 100.0) * (1 if config.direction == "long" else -1))
        elif not is_tp_sl_order:
            current_price_for_limit = gate_client.fetch_last_price(config.symbol)
            if current_price_for_limit is None:
                _LOG.error(f"{config.symbol} 현재가 조회 실패로 지정가 계산 불가. 주문 실패 처리.")
                return False
            slippage_factor = -1.0 if order_execution_side == "long" else 1.0
            limit_order_price_for_api = current_price_for_limit * \
                (1 + (slippage_factor * config.limit_order_slippage_pct / 100.0))
        
        if limit_order_price_for_api is not None:
             _LOG.info(f"{order_purpose} 지정가 주문 가격 계산됨: {limit_order_price_for_api:.4f}")
        else:
            _LOG.warning(f"{order_purpose} 지정가 주문 가격 계산 실패. 시장가로 강제 전환 또는 주문 실패 고려.")
            effective_order_type = "market"

    order_result = gate_client.place_order(
        contract_symbol=config.symbol,
        order_amount_usd=usd_amount_for_api_call,
        position_side=order_execution_side,
        leverage=config.leverage,
        order_type=effective_order_type,
        limit_price=limit_order_price_for_api if effective_order_type == "limit" else None,
        reduce_only=reduce_only_flag,
        order_id_prefix=full_order_id_prefix
    )

    if order_result and order_result.get("id"):
        order_id = order_result.get("id")
        _LOG.info(f"{order_purpose.upper()} 주문 성공적으로 API에 접수됨. 주문 ID: {order_id}, 상태: {order_result.get('status')}")
        
        if effective_order_type == "market":
            _LOG.info(f"시장가 {order_purpose} 주문 접수. 체결 가정하고 상태 업데이트 시도 (실제 체결 확인 필요).")
            filled_price_str = order_result.get('fill_price')
            filled_size_str = order_result.get('filled_size')

            if filled_price_str and filled_size_str and float(filled_price_str) > 0 and float(filled_size_str) != 0:
                actual_fill_price = float(filled_price_str)
                actual_filled_contracts = float(filled_size_str)
                actual_filled_usd = abs(actual_filled_contracts) * actual_fill_price
                _LOG.info(f"시장가 주문 체결 정보 (API 응답 기반): 가격=${actual_fill_price:.4f}, 계약수량={actual_filled_contracts:.8f}, USD가치=${actual_filled_usd:.2f}")
                current_bot_state.update_on_fill(
                    filled_contracts=actual_filled_contracts,
                    fill_price=actual_fill_price,
                    filled_usd_value=actual_filled_usd,
                    order_purpose=order_purpose
                )
            else:
                _LOG.warning(f"시장가 주문({order_id}) 체결 정보 즉시 확인 불가. 현재가 기준으로 임시 상태 업데이트.")
                temp_fill_price = gate_client.fetch_last_price(config.symbol) or \
                                  (current_bot_state.current_avg_entry_price if current_bot_state.is_in_position else 0)
                if temp_fill_price > 0 :
                    requested_contracts = (usd_amount_for_api_call / temp_fill_price) * (1 if order_execution_side == "long" else -1)
                    current_bot_state.update_on_fill(
                        filled_contracts=requested_contracts,
                        fill_price=temp_fill_price,
                        filled_usd_value=usd_amount_for_api_call,
                        order_purpose=order_purpose
                    )
                else:
                    _LOG.error("임시 체결가 계산 위한 현재가 조회 실패. 상태 업데이트 불가.")
        return True
    else:
        _LOG.error(f"{order_purpose.upper()} 주문 실패 또는 API로부터 유효한 응답 받지 못함.")
        return False

# --- 여기가 수정된 부분입니다 (1/3): run_strategy 함수에 stop_event 인자 추가 ---
def run_strategy(config: BotConfig, gate_client: GateIOClient, current_bot_state: BotTradingState, stop_event: threading.Event):
    """메인 거래 전략 실행 루프"""
    _LOG.info(f"'{config.symbol}'에 대한 거래 전략 시작. 설정: {config.to_dict()}")
    
    if not current_bot_state.is_in_position:
        click.secho(f"\n🚀 초기 진입 주문 시도 ({config.direction.upper()}) for {config.symbol}...", fg="green", bold=True)
        if not _execute_order_and_update_state(gate_client, config, current_bot_state, config.entry_amount_usd, "entry"):
            _LOG.critical("초기 진입 주문 실패. 이 심볼에 대한 전략을 시작할 수 없습니다.")
            click.secho(f"❌ {config.symbol} 초기 진입 주문 실패. 전략 실행 중지.", fg="red", bold=True)
            return

    # --- 여기가 수정된 부분입니다 (2/3): while 루프 조건에 stop_event 확인 추가 ---
    while not stop_event.is_set():
        try:
            _LOG.info(f"'{config.symbol}' 전략 루프 시작. 현재 분할매수 횟수: {current_bot_state.current_split_order_count}")
            current_market_price = gate_client.fetch_last_price(config.symbol)
            if current_market_price is None:
                _LOG.error(f"{config.symbol} 현재가 조회 실패. 다음 사이클까지 {config.check_interval_seconds}초 대기합니다.")
                time.sleep(config.check_interval_seconds)
                continue

            show_summary(config, current_market_price, gate_client, current_bot_state)

            if not current_bot_state.is_in_position:
                if config.repeat_after_take_profit:
                    _LOG.info(f"{config.symbol} 포지션 없음. '익절 후 반복' 설정에 따라 재진입 시도.")
                    click.secho(f"\n🔁 '{config.symbol}' 재진입 시도 ({config.direction.upper()})...", fg="blue")
                    current_bot_state.reset()
                    if not _execute_order_and_update_state(gate_client, config, current_bot_state, config.entry_amount_usd, "entry"):
                        _LOG.error(f"{config.symbol} 재진입 주문 실패. 다음 사이클까지 대기합니다.")
                else:
                    _LOG.info(f"{config.symbol} 포지션 없음. 반복 실행 설정 꺼져있으므로 이 심볼에 대한 전략 종료.")
                    break # 루프 종료
                if stop_event.is_set(): break

            # 익절 로직
            if config.take_profit_pct and current_bot_state.is_in_position and current_bot_state.current_avg_entry_price:
                profit_target_price = current_bot_state.current_avg_entry_price * (1 + (config.take_profit_pct / 100.0) * (1 if config.direction == "long" else -1))
                if (config.direction == "long" and current_market_price >= profit_target_price) or \
                   (config.direction == "short" and current_market_price <= profit_target_price):
                    _LOG.info(f"💰 {config.symbol} 익절 조건 충족!")
                    click.secho(f"💰 {config.symbol} 익절 주문 실행...", fg="green", bold=True)
                    if _execute_order_and_update_state(gate_client, config, current_bot_state, 0, "take_profit"):
                        if not config.repeat_after_take_profit and not current_bot_state.is_in_position:
                            _LOG.info(f"{config.symbol} 익절 후 반복 설정 꺼짐. 전략 종료.")
                            break
                    else:
                        _LOG.error(f"{config.symbol} 익절 주문 실패.")
                if stop_event.is_set(): break

            # 손절 로직
            if config.enable_stop_loss and config.stop_loss_pct and current_bot_state.is_in_position and current_bot_state.current_avg_entry_price:
                loss_target_price = current_bot_state.current_avg_entry_price * (1 - (config.stop_loss_pct / 100.0) * (1 if config.direction == "long" else -1))
                if (config.direction == "long" and current_market_price <= loss_target_price) or \
                   (config.direction == "short" and current_market_price >= loss_target_price):
                    _LOG.info(f"💣 {config.symbol} 손절 조건 충족!")
                    click.secho(f"💣 {config.symbol} 손절 주문 실행...", fg="red", bold=True)
                    if _execute_order_and_update_state(gate_client, config, current_bot_state, 0, "stop_loss"):
                        if config.stop_bot_after_stop_loss and not current_bot_state.is_in_position:
                            _LOG.info(f"{config.symbol} 손절 후 봇 중지 설정 켜짐. 전략 종료.")
                            break
                        elif not current_bot_state.is_in_position and not config.repeat_after_take_profit:
                             _LOG.info(f"{config.symbol} 손절로 포지션 청산됨. 반복 설정 꺼져있어 전략 종료.")
                             break
                    else:
                        _LOG.error(f"{config.symbol} 손절 주문 실패.")
                if stop_event.is_set(): break

            # 분할매수 로직
            if current_bot_state.current_split_order_count < config.max_split_count and current_bot_state.is_in_position and current_bot_state.current_avg_entry_price:
                trigger_pct = config.split_trigger_percents[current_bot_state.current_split_order_count]
                split_target_price = current_bot_state.current_avg_entry_price * (1 + trigger_pct / 100.0)
                if (config.direction == "long" and current_market_price <= split_target_price) or \
                   (config.direction == "short" and current_market_price >= split_target_price):
                    split_amount_usd = config.split_amounts_usd[current_bot_state.current_split_order_count]
                    _LOG.info(f"💧 {config.symbol} 분할매수 {current_bot_state.current_split_order_count + 1} 조건 충족!")
                    click.secho(f"💧 {config.symbol} 분할매수 {current_bot_state.current_split_order_count + 1} 주문 실행...", fg="cyan")
                    if _execute_order_and_update_state(gate_client, config, current_bot_state, split_amount_usd, "split"):
                        _LOG.info(f"{config.symbol} 분할매수 {current_bot_state.current_split_order_count}회 성공.")
                    else:
                        _LOG.error(f"{config.symbol} 분할매수 {current_bot_state.current_split_order_count + 1} 주문 실패.")
            
            if not stop_event.is_set():
                _LOG.debug(f"'{config.symbol}' 다음 전략 확인까지 {config.check_interval_seconds}초 대기...")
                # time.sleep을 여러 번으로 나누어 stop_event를 더 자주 확인할 수 있게 함
                for _ in range(config.check_interval_seconds):
                    if stop_event.is_set():
                        break
                    time.sleep(1)

        except KeyboardInterrupt:
            _LOG.warning("사용자 인터럽트 감지 (Ctrl+C). 종료 신호를 보냅니다.")
            click.secho("\n🛑 사용자 요청으로 봇을 종료합니다...", fg="yellow", bold=True)
            stop_event.set()
        except ApiException as e:
            _LOG.error(f"전략 실행 중 API 오류 발생: {e.body}", exc_info=True)
            click.secho(f"API 오류 발생: {e.reason}. 잠시 후 재시도합니다.", fg="red")
            time.sleep(config.check_interval_seconds * 2)
        except Exception as e:
            _LOG.error(f"전략 실행 중 예상치 못한 오류 발생: {e}", exc_info=True)
            click.secho(f"예상치 못한 오류 발생: {e}. 잠시 후 재시도합니다.", fg="red")
            time.sleep(config.check_interval_seconds * 2)

    # --- 루프가 종료된 후 실행되는 부분 ---
    _LOG.info(f"'{config.symbol}'에 대한 거래 전략 루프 종료.")
    
    if stop_event.is_set() and current_bot_state.is_in_position:
        _LOG.warning("종료 신호 수신. 최종 포지션 청산 시도...")
        click.secho("\n🛑 'stop' 명령 또는 Ctrl+C를 처리합니다. 포지션을 정리하고 봇을 종료합니다...", fg="yellow", bold=True)
        click.echo("   -> 현재 포지션을 시장가로 청산합니다...")
        
        if _execute_order_and_update_state(gate_client, config, current_bot_state, 0, "stop_loss"):
             click.secho("   -> ✅ 포지션이 성공적으로 청산되었습니다.", fg="green")
        else:
             click.secho("   -> ❌ 포지션 청산에 실패했습니다. 거래소에서 직접 확인해주세요.", fg="red")

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
        # ... (smoke_test 로직)
        sys.exit(0)

    bot_configuration: Optional[BotConfig] = None
    
    if config_file:
        try:
            bot_configuration = BotConfig.load(config_file)
            click.secho(f"\n✅ 설정 파일 로드 성공: {config_file.resolve()}", fg="green")
        except Exception as e:
            _LOG.error(f"지정된 설정 파일 '{config_file.resolve()}' 로드 실패: {e}", exc_info=True)
            click.secho(f"❌ 설정 파일 로드 오류: {e}", fg="red")
            sys.exit(1)
    else:
        project_root = Path(__file__).resolve().parents[2]
        config_dir = project_root / "Bot"
        
        while bot_configuration is None:
            user_choice = select_config(config_dir)
            if user_choice == "exit":
                _LOG.info("사용자가 메뉴에서 종료를 선택했습니다.")
                sys.exit(0)
            elif user_choice == "new":
                bot_configuration = prompt_config(gate_client)
                if bot_configuration is None:
                    if not click.confirm("\n설정 중 오류가 발생했습니다. 다시 시도하시겠습니까?", default=True):
                        _LOG.info("사용자가 설정 재시도를 원치 않아 종료합니다.")
                        sys.exit(0)
            elif isinstance(user_choice, BotConfig):
                bot_configuration = user_choice
                click.secho(f"\n✅ '{user_choice.symbol}' 설정 로드 완료.", fg="green")

    show_summary_final(bot_configuration)

    if click.confirm("\n❓ 이 설정을 파일로 저장하시겠습니까?", default=False):
        project_root = Path(__file__).resolve().parents[2]
        config_dir = project_root / "Bot"
        config_dir.mkdir(exist_ok=True)
        default_save_path = config_dir / f"{bot_configuration.symbol.lower()}_{bot_configuration.direction}_config.json"
        
        save_path_str = click.prompt("설정 저장 경로 또는 파일명 입력", default=str(default_save_path))
        
        save_path_obj = Path(save_path_str)
        if save_path_obj.is_dir():
            final_save_path = save_path_obj / default_save_path.name
            _LOG.warning(f"입력된 경로 '{save_path_str}'는 디렉토리입니다. 전체 저장 경로를 '{final_save_path}'로 설정합니다.")
        else:
            final_save_path = save_path_obj

        try:
            bot_configuration.save(final_save_path)
        except Exception as e:
            _LOG.error(f"설정 파일 저장 실패 ('{final_save_path}'): {e}", exc_info=True)
            click.secho(f"⚠️ 설정 파일 저장 실패: {e}", fg="yellow")

    # --- 여기가 수정된 부분입니다 (3/3): 스레드 기반 실행 로직 ---
    if click.confirm("\n▶️ 위 설정으로 자동매매를 시작하시겠습니까?", default=True):
        _LOG.info(f"사용자 확인. '{bot_configuration.symbol}' 자동매매 시작.")
        click.secho(f"🚀 '{bot_configuration.symbol}' 자동매매 시작...", fg="green", bold=True)
        
        current_bot_trading_state = BotTradingState(symbol=bot_configuration.symbol)
        
        # 스레드 종료를 위한 이벤트 객체 생성
        stop_event = threading.Event()
        
        # run_strategy 함수를 별도의 스레드에서 실행
        strategy_thread = threading.Thread(
            target=run_strategy, 
            args=(bot_configuration, gate_client, current_bot_trading_state, stop_event),
            daemon=True # 메인 스레드 종료 시 함께 종료되도록 설정
        )
        strategy_thread.start()
        
        click.secho("\n✅ 자동매매가 백그라운드에서 실행 중입니다.", fg="cyan")
        click.secho("🛑 종료하려면 'stop'을 입력하고 Enter를 누르세요.", fg="yellow", bold=True)
        
        try:
            # 메인 스레드는 사용자 입력을 기다림
            while strategy_thread.is_alive():
                user_input = input()
                if user_input.strip().lower() == 'stop':
                    stop_event.set() # 스레드에 종료 신호 보내기
                    break # 입력 대기 루프 탈출
                else:
                    click.echo("   (종료하시려면 'stop'을 입력해주세요...)")

        except KeyboardInterrupt:
            click.echo("\n🛑 Ctrl+C 감지. 봇 종료 신호를 보냅니다...")
            _LOG.warning("메인 스레드에서 Ctrl+C 감지. 전략 스레드에 종료 신호 전송.")
            stop_event.set()

        # 전략 스레드가 완전히 종료될 때까지 대기
        click.echo("   -> 포지션 정리 및 종료를 기다리는 중...")
        strategy_thread.join(timeout=30) # 최대 30초 대기
        
        if strategy_thread.is_alive():
            _LOG.error("전략 스레드가 제 시간 내에 종료되지 않았습니다. 강제 종료될 수 있습니다.")
            click.secho("⚠️ 스레드가 제 시간 내에 종료되지 않았습니다.", fg="red")

        click.secho(f"\n🏁 '{bot_configuration.symbol}' 자동매매 전략이 종료되었습니다.", fg="blue", bold=True)
    else:
        _LOG.info("사용자가 자동매매 시작을 선택하지 않았습니다.")
        click.secho("👋 자동매매가 시작되지 않았습니다. 프로그램을 종료합니다.", fg="yellow")

    _LOG.info("="*10 + " 자동매매 봇 CLI 종료 " + "="*10)

