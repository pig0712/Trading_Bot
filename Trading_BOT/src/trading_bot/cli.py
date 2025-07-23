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
        self.current_pyramiding_order_count: int = 0
        self.last_entry_attempt_time: Optional[float] = None

        # ✅ --- 추적 익절을 위한 상태 변수 추가 ---
        self.is_in_trailing_mode: bool = False
        self.highest_unrealised_pnl_usd: float = 0.0
        
        _LOG.info(f"BotTradingState for {self.symbol} initialized.")

    def reset(self):
        """봇 상태를 초기화합니다."""
        _LOG.info(f"BotTradingState for {self.symbol} resetting...")
        self.current_avg_entry_price = None
        self.total_position_contracts = 0.0
        self.total_position_initial_usd = 0.0
        self.is_in_position = False
        self.current_split_order_count = 0
        self.current_pyramiding_order_count = 0
        self.last_entry_attempt_time = None

        # ✅ --- 리셋 시 추적 익절 상태도 초기화 ---
        self.is_in_trailing_mode = False
        self.highest_unrealised_pnl_usd = 0.0
        
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
            elif order_purpose == "pyramiding":
                self.current_pyramiding_order_count += 1
                _LOG.info(f"Pyramiding order {self.current_pyramiding_order_count} successful.")

        avg_price_str = f"{self.current_avg_entry_price:.4f}" if self.current_avg_entry_price is not None else "N/A"
        _LOG.info(f"Position state updated for {self.symbol}: AvgEntryPrice=${avg_price_str}, "
                  f"TotalContracts={self.total_position_contracts:.8f}, TotalInitialUSD=${self.total_position_initial_usd:.2f}, "
                  f"IsInPosition={self.is_in_position}")

def prompt_config(gate_client: GateIOClient) -> Optional[BotConfig]:
    """사용자로부터 대화형으로 봇 설정을 입력받습니다."""
    click.secho("\n" + "="*10 + " 📈 신규 전략 설정 " + "="*10, fg="yellow", bold=True)
    
    auto_determine_direction = click.confirm("🤖 자동으로 포지션 방향(Long/Short)을 결정하시겠습니까?", default=False)
    
    direction = "long"
    if not auto_determine_direction:
        direction = click.prompt("👉 거래 방향 (long/short)", type=click.Choice(["long", "short"]), default="long")

    symbol = click.prompt("👉 거래 대상 코인 (예: BTC_USDT)", default="BTC_USDT").upper().strip()
    leverage = click.prompt("👉 레버리지 (예: 10)", type=int, default=10)
    margin_mode = click.prompt("👉 마진 모드 (cross/isolated)", type=click.Choice(["cross", "isolated"]), default="isolated")
    
    click.secho("\n--- 💰 자금 설정 (사용 가능 잔액 기준) ---", fg="green")
    entry_amount_pct = click.prompt("👉 첫 진입 금액 (% of available balance)", type=float, default=12.0)
    
    click.secho("\n--- 💧 분할매수(물타기) 설정 ---", fg="blue")
    max_split_count = click.prompt("👉 분할매수 횟수", type=int, default=5)
    split_trigger_percents: List[float] = []
    split_amounts_pct: List[float] = []
    if max_split_count > 0:
        click.secho(f"👉 {max_split_count}번의 분할매수 트리거 퍼센트를 입력하세요 (손실률이므로 음수로 입력)", fg="cyan")
        for i in range(max_split_count):
            trigger = click.prompt(f"  - {i+1}번째 분할매수 손실률 (%)", type=float, default=round(-2.0 - i*2.0, 1))
            split_trigger_percents.append(trigger)
        click.secho(f"👉 {max_split_count}번의 분할매수 금액 비율을 입력하세요 (% of available balance)", fg="cyan")
        for i in range(max_split_count):
            amount_pct = click.prompt(f"  - {i+1}번째 분할매수 금액 비율 (%)", type=float, default=round(12.0 + i*2, 1))
            split_amounts_pct.append(amount_pct)

    click.secho("\n--- 🔥 피라미딩(불타기) 설정 ---", fg="magenta")
    enable_pyramiding = click.confirm("수익이 날 때 추가 매수(피라미딩) 기능을 사용하시겠습니까?", default=False)
    pyramiding_max_count = 0
    pyramiding_trigger_percents = []
    pyramiding_amounts_pct = []
    if enable_pyramiding:
        pyramiding_max_count = click.prompt("👉 피라미딩 횟수", type=int, default=3)
        click.secho(f"👉 {pyramiding_max_count}번의 피라미딩 트리거 퍼센트를 입력하세요 (수익률이므로 양수로 입력)", fg="cyan")
        for i in range(pyramiding_max_count):
            trigger = click.prompt(f"  - {i+1}번째 추가 매수 수익률 (%)", type=float, default=round(2.0 + i*2.0, 1))
            pyramiding_trigger_percents.append(trigger)
        click.secho(f"👉 {pyramiding_max_count}번의 추가 매수 금액 비율을 입력하세요 (% of available balance)", fg="cyan")
        for i in range(pyramiding_max_count):
            amount_pct = click.prompt(f"  - {i+1}번째 추가 매수 금액 비율 (%)", type=float, default=10.0)
            pyramiding_amounts_pct.append(amount_pct)

    click.secho("\n--- ⚙️ 청산(Exit) 및 기타 설정 ---", fg="yellow")
    
    use_trailing_tp = click.confirm("💸 수익금 기준 추적 익절(Trailing Take Profit) 기능을 사용하시겠습니까?", default=True)
    
    trailing_tp_trigger_pct = None
    trailing_tp_offset_pct = None
    take_profit_pct = None

    if use_trailing_tp:
        trailing_tp_trigger_pct = click.prompt("  - 추적 익절 시작 ROE (%)", type=float, default=4.0)
        trailing_tp_offset_pct = click.prompt("  - 최고 수익금 대비 하락 허용치 (%)", type=float, default=5.0)
    else:
        take_profit_pct_str = click.prompt("👉 일반 익절 ROE (%)", type=str, default="5.0")
        take_profit_pct = float(take_profit_pct_str) if take_profit_pct_str.strip() else None

    stop_loss_pct_str = click.prompt("👉 손절 ROE (%)", type=str, default="2.5")
    stop_loss_pct = float(stop_loss_pct_str) if stop_loss_pct_str.strip() else None
    
    order_type = click.prompt("👉 주문 방식을 선택하세요 (market: 시장가 / limit: 지정가)", type=click.Choice(["market", "limit"]), default="market")
    click.echo("")
    repeat_after_tp = click.confirm("익절 후 반복 실행하시겠습니까? (y/n)", default=True)
    stop_after_sl = click.confirm("손절 후 봇을 정지하시겠습니까? (y/n)", default=False)
    enable_sl = click.confirm("손절 기능을 활성화하시겠습니까? (y/n)", default=True)
    
    cfg_data = {
        "auto_determine_direction": auto_determine_direction,
        "direction": direction, "symbol": symbol, "leverage": leverage, "margin_mode": margin_mode,
        "entry_amount_pct_of_balance": entry_amount_pct,
        "max_split_count": max_split_count,
        "split_trigger_percents": split_trigger_percents,
        "split_amounts_pct_of_balance": split_amounts_pct,
        "enable_pyramiding": enable_pyramiding,
        "pyramiding_max_count": pyramiding_max_count,
        "pyramiding_trigger_percents": pyramiding_trigger_percents,
        "pyramiding_amounts_pct_of_balance": pyramiding_amounts_pct,
        "take_profit_pct": take_profit_pct, 
        "stop_loss_pct": stop_loss_pct,
        "trailing_take_profit_trigger_pct": trailing_tp_trigger_pct,
        "trailing_take_profit_offset_pct": trailing_tp_offset_pct,
        "order_type": order_type,
        "repeat_after_take_profit": repeat_after_tp, 
        "stop_bot_after_stop_loss": stop_after_sl,
        "enable_stop_loss": enable_sl,
    }
    try:
        config = BotConfig(**cfg_data)
        click.secho("\n✅ 설정 완료.", fg="green", bold=True)
        return config
    except ValueError as e:
        _LOG.error(f"봇 설정 값 유효성 검사 실패: {e}", exc_info=True)
        click.secho(f"\n❌ 설정 오류: {e}", fg="red", bold=True)
        click.echo("설정을 처음부터 다시 시작합니다.")
        return None

def show_summary_final(config: BotConfig):
    """최종 설정 요약을 출력합니다."""
    click.secho("\n" + "─"*18 + " 📊 최종 실행 설정 요약 " + "─"*18, fg="yellow", bold=True)
    
    # --- 거래 기본 설정 ---
    if config.auto_determine_direction:
        direction_title = "자동 결정된 거래 방향:"
        direction_color = "cyan"
    else:
        direction_title = "거래 방향:"
        direction_color = "green" if config.direction == "long" else "red"
    click.secho(f"{direction_title:<35} {config.direction.upper()}", fg=direction_color, bold=True)
    click.echo(f"{'거래 대상 코인:':<35} {config.symbol}")
    click.echo(f"{'레버리지:':<35} {config.leverage}x")
    click.echo(f"{'마진 모드:':<35} {config.margin_mode}")
    click.echo(f"{'주문 방식:':<35} {config.order_type}")
    
    click.echo("─" * 55)

    # --- 자금 운용 설정 ---
    click.echo(f"{'첫 진입 금액 (% of available balance):':<35} {config.entry_amount_pct_of_balance}%")
    
    # 분할매수(물타기) 설정 표시
    click.secho(f"{'분할매수(물타기) 횟수:':<35} {config.max_split_count}회", fg="blue")
    if config.max_split_count > 0:
        click.echo(f"{'  - 트리거 손실률(%):':<35} {config.split_trigger_percents}")
        click.echo(f"{'  - 추가 투입 비율(%):':<35} {config.split_amounts_pct_of_balance}")

    # ✅ 피라미딩(불타기) 설정 표시
    pyramiding_enabled_str = 'Yes' if config.enable_pyramiding else 'No'
    pyramiding_color = "magenta" if config.enable_pyramiding else "default"
    click.secho(f"{'피라미딩(불타기) 활성화:':<35} {pyramiding_enabled_str}", fg=pyramiding_color)
    
    if config.enable_pyramiding:
        click.echo(f"{'  - 피라미딩 횟수:':<35} {config.pyramiding_max_count}회")
        click.echo(f"{'  - 트리거 수익률(%):':<35} {config.pyramiding_trigger_percents}")
        click.echo(f"{'  - 추가 투입 비율(%):':<35} {config.pyramiding_amounts_pct_of_balance}")
        
    click.echo("─" * 55)

    # --- 리스크 관리 설정 ---
    click.echo(f"{'익절 퍼센트 (레버리지 손익):':<35} {config.take_profit_pct}%")
    click.secho(f"{'손절 기능 활성화:':<35} {'Yes' if config.enable_stop_loss else 'No'}", fg="red" if config.enable_stop_loss else "default")
    if config.enable_stop_loss:
        click.echo(f"{'손절 퍼센트 (레버리지 손익):':<35} {config.stop_loss_pct}%")
    
    click.echo("─" * 55)

    # --- 봇 운영 정책 ---
    click.echo(f"{'익절 후 반복 실행:':<35} {'Yes' if config.repeat_after_take_profit else 'No'}")
    click.echo(f"{'손절 후 봇 정지:':<35} {'Yes' if config.stop_bot_after_stop_loss else 'No'}")

    click.echo("─"*55)

def show_summary(config: BotConfig, current_market_price: Optional[float], gate_client: GateIOClient, current_bot_state: BotTradingState):
    """실시간 봇 상태 요약을 출력합니다."""
    click.secho("\n" + "="*15 + " 🤖 봇 상태 및 설정 요약 " + "="*15, fg="yellow", bold=True)
    click.secho("\n[시장 및 계산 정보]", fg="cyan")
    if current_market_price is not None:
        click.echo(f" 	현재 시장가 ({config.symbol:<10}): {current_market_price:.4f} USDT")
    else:
        click.echo(f" 	현재 시장가 ({config.symbol:<10}): 정보 없음")
    actual_position_info = None
    try:
        actual_position_info = gate_client.get_position(config.symbol)
    except Exception as e:
        _LOG.error(f"{config.symbol} 실제 포지션 정보 조회 중 예외 발생: {e}", exc_info=True)
        click.secho(f" 	(에러: {config.symbol} 실제 포지션 조회 중 오류 발생)", fg="red")
    if actual_position_info and actual_position_info.get('size') is not None and float(actual_position_info.get('size', 0)) != 0:
        click.secho("\n[실제 거래소 포지션]", fg="magenta")
        pos_size = float(actual_position_info['size'])
        pos_entry_price_str = actual_position_info.get('entry_price')
        pos_entry_price = float(pos_entry_price_str) if pos_entry_price_str is not None else 0.0
        pos_leverage = actual_position_info.get('leverage', 'N/A')
        pos_liq_price_api = actual_position_info.get('liq_price', 'N/A')
        pos_unreal_pnl = actual_position_info.get('unrealised_pnl', 'N/A')
        click.echo(f" 	- 방향 		: {'LONG' if pos_size > 0 else 'SHORT'}")
        click.echo(f" 	- 진입가 (API) 	: {pos_entry_price:.4f} USDT")
        click.echo(f" 	- 수량 (API) 		: {pos_size} {config.symbol.split('_')[0]}")
        click.echo(f" 	- 레버리지 (API): {pos_leverage}x")
        click.echo(f" 	- 청산가 (API) 	: {pos_liq_price_api if pos_liq_price_api else 'N/A'} USDT")
        click.echo(f" 	- 미실현 손익 	 : {pos_unreal_pnl} USDT")
    else:
        click.secho(f"\n[{config.symbol} 실제 거래소 포지션 없음 또는 정보 업데이트 중...]", fg="magenta")
    click.secho("\n[봇 내부 추적 상태]", fg="blue")
    if current_bot_state.is_in_position and current_bot_state.current_avg_entry_price is not None and current_market_price is not None:
        direction_display = config.direction.upper()
        avg_price = current_bot_state.current_avg_entry_price
        total_contracts = current_bot_state.total_position_contracts
        click.echo(f" 	- 추적 방향 		: {direction_display}")
        click.echo(f" 	- 평균 진입가 	: {avg_price:.4f} USDT")
        click.echo(f" 	- 총 계약 수량 	: {total_contracts:.8f} {config.symbol.split('_')[0]}")
        click.echo(f" 	- 총 투입 원금 	: {current_bot_state.total_position_initial_usd:.2f} USDT (추정치)")
        current_position_value_usd = abs(total_contracts) * current_market_price
        if config.direction == "long":
            pnl_usd = (current_market_price - avg_price) * total_contracts
        else:
            pnl_usd = (avg_price - current_market_price) * abs(total_contracts)
        market_pnl_pct = (current_market_price - avg_price) / avg_price if avg_price > 0 else 0
        if config.direction == "short":
            market_pnl_pct *= -1
        leveraged_roe_pct = market_pnl_pct * config.leverage * 100
        click.echo(f" 	- 현재 평가액 		: {current_position_value_usd:,.2f} USDT")
        pnl_color = "green" if pnl_usd >= 0 else "red"
        click.secho(f" 	- 손익 금액(추정): {pnl_usd:,.2f} USDT", fg=pnl_color)
        click.secho(f" 	- 손익률(ROE) 	: {leveraged_roe_pct:.2f}%", fg=pnl_color)
        click.echo(f" 	- 분할매수 횟수 : {current_bot_state.current_split_order_count} / {config.max_split_count}")
        liq_price_calc, change_pct_calc = calculate_liquidation_price(
            total_position_collateral_usd=current_bot_state.total_position_initial_usd,
            leverage=config.leverage, margin_mode=config.margin_mode,
            avg_entry_price=current_bot_state.current_avg_entry_price, position_direction=config.direction
        )
        if liq_price_calc is not None and change_pct_calc is not None:
            change_display_char = '-' if config.direction == 'long' else '+'
            click.secho(f" 	예상 청산가(계산): {liq_price_calc:.4f} USDT ({change_display_char}{abs(change_pct_calc):.2f}% from avg entry)", fg="magenta")
        if config.take_profit_pct:
            market_move_pct = config.take_profit_pct / config.leverage
            tp_target_price = current_bot_state.current_avg_entry_price * (1 + (market_move_pct / 100.0) * (1 if config.direction == "long" else -1))
            click.echo(f" 	익절 목표가 (ROE {config.take_profit_pct}%): {tp_target_price:.4f} USDT")
        if config.enable_stop_loss and config.stop_loss_pct:
            market_move_pct = config.stop_loss_pct / config.leverage
            sl_target_price = current_bot_state.current_avg_entry_price * (1 - (market_move_pct / 100.0) * (1 if config.direction == "long" else -1))
            click.echo(f" 	손절 목표가 (ROE -{config.stop_loss_pct}%): {sl_target_price:.4f} USDT")
    else:
        click.echo(" 	(현재 봇 내부 추적 포지션 없음)")
    click.echo("="*50 + "\n")

def _execute_order_and_update_state(gate_client: GateIOClient, config: BotConfig, current_bot_state: BotTradingState, order_usd_amount: float, order_purpose: Literal["entry", "split", "pyramiding", "take_profit", "stop_loss", "emergency_close"]) -> bool:
    """주문 실행 및 상태 업데이트 헬퍼 함수 (피라미딩 기능 추가)"""
    is_closing_order = order_purpose in ["take_profit", "stop_loss", "emergency_close"]
    
    if order_purpose in ["entry", "split", "pyramiding"]:
        account_info = gate_client.get_account_info()
        if not account_info or 'available' not in account_info:
            _LOG.error(f"주문을 위한 계좌 정보 조회 실패 ({order_purpose})")
            return False
        available_balance = float(account_info['available'])
        
        pct_of_balance = 0.0
        if order_purpose == "entry":
            pct_of_balance = config.entry_amount_pct_of_balance
        elif order_purpose == "split":
            pct_of_balance = config.split_amounts_pct_of_balance[current_bot_state.current_split_order_count]
        elif order_purpose == "pyramiding":
            pct_of_balance = config.pyramiding_amounts_pct_of_balance[current_bot_state.current_pyramiding_order_count]
        
        order_usd_amount = available_balance * (pct_of_balance / 100.0)
        _LOG.info(f"'{order_purpose}' 투자 금액 계산: {order_usd_amount:.4f} USDT")

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
    elif order_purpose == 'pyramiding':
        order_id_suffix += f"-{current_bot_state.current_pyramiding_order_count + 1}"

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
        
        if order_purpose in ["entry", "split", "pyramiding"]:
            current_bot_state.last_entry_attempt_time = time.time()
            _LOG.info(f"'{order_purpose}' 주문 타임스탬프 기록: {current_bot_state.last_entry_attempt_time}")

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
    """(최종 수정) 봇의 내부 상태를 신뢰하여, API 지연 시 재진입하지 않고 대기하는 최종 버전"""
    _LOG.info(f"'{config.symbol}'에 대한 거래 전략 시작. 설정: {config.to_dict()}")

    if not current_bot_state.is_in_position:
        if not _execute_order_and_update_state(gate_client, config, current_bot_state, 0, "entry"):
            _LOG.critical("초기 진입 주문 실패.")
            return

    while not stop_event.is_set():
        try:
            click.clear()
            actual_position = gate_client.get_position(config.symbol)
            
            # ✅ 새로 만든 UI 함수가 모든 표시를 담당합니다.
            pretty_show_summary(config, current_bot_state, actual_position)
            
            position_size_raw = actual_position.get('size') if actual_position else None
            actual_pos_size = float(position_size_raw) if position_size_raw is not None else 0.0

            # --- CASE 1: 실제 포지션이 "있을" 경우 ---
            if actual_pos_size != 0:
                if not current_bot_state.is_in_position:
                    _LOG.warning("상태 불일치 복구: 실제 포지션이 있으므로 내부 상태를 '진입'으로 변경합니다.")
                    current_bot_state.is_in_position = True
                
                margin_used = float(actual_position.get('margin', 0))
                current_unrealised_pnl = float(actual_position.get('unrealised_pnl', 0))
                leveraged_roe_pct = (current_unrealised_pnl / margin_used) * 100 if margin_used > 1e-9 else 0.0

                if current_bot_state.is_in_trailing_mode:
                    current_bot_state.highest_unrealised_pnl_usd = max(
                        current_bot_state.highest_unrealised_pnl_usd, current_unrealised_pnl
                    )
                    exit_profit_level = current_bot_state.highest_unrealised_pnl_usd * (1 - (config.trailing_take_profit_offset_pct / 100.0))
                    final_exit_level = max(exit_profit_level, 0.1)
                    if current_unrealised_pnl <= final_exit_level:
                        _LOG.info(f"💸 추적 익절 실행! 최고수익:${current_bot_state.highest_unrealised_pnl_usd:.2f}, 익절라인:${final_exit_level:.2f}")
                        _execute_order_and_update_state(gate_client, config, current_bot_state, 0, "take_profit")
                        continue
                else: # 일반 모드
                    if config.trailing_take_profit_trigger_pct and leveraged_roe_pct >= config.trailing_take_profit_trigger_pct:
                        _LOG.info(f"🔥 추적 익절 모드로 전환! (현재 ROE: {leveraged_roe_pct:.2f}%)")
                        current_bot_state.is_in_trailing_mode = True
                        current_bot_state.highest_unrealised_pnl_usd = current_unrealised_pnl
                        if config.enable_pyramiding:
                            _execute_order_and_update_state(gate_client, config, current_bot_state, 0, "pyramiding")
                        continue
                    elif config.take_profit_pct and leveraged_roe_pct >= config.take_profit_pct:
                        _execute_order_and_update_state(gate_client, config, current_bot_state, 0, "take_profit")
                        continue

                # 공통 로직: 손절, 분할매수, 피라미딩
                if config.enable_stop_loss and config.stop_loss_pct and leveraged_roe_pct <= -config.stop_loss_pct:
                    if _execute_order_and_update_state(gate_client, config, current_bot_state, 0, "stop_loss"):
                        if config.stop_bot_after_stop_loss: break
                    continue
                if current_bot_state.current_split_order_count < config.max_split_count:
                    next_split_trigger_pct = config.split_trigger_percents[current_bot_state.current_split_order_count]
                    if leveraged_roe_pct <= next_split_trigger_pct:
                        _execute_order_and_update_state(gate_client, config, current_bot_state, 0, "split")
                if config.enable_pyramiding and current_bot_state.is_in_trailing_mode and current_bot_state.current_pyramiding_order_count < config.pyramiding_max_count:
                    next_pyramiding_trigger = config.pyramiding_trigger_percents[current_bot_state.current_pyramiding_order_count]
                    if leveraged_roe_pct >= next_pyramiding_trigger:
                        _execute_order_and_update_state(gate_client, config, current_bot_state, 0, "pyramiding")

            # ✅ CASE 2: 실제 포지션이 "없을" 경우 -> 봇의 내부 상태(예측)를 확인
            else:
                # 봇이 포지션에 "있다"고 기억하는 경우 (API 지연)
                if current_bot_state.is_in_position:
                    _LOG.info("주문 체결 확인. 거래소 API에서 포지션 상세 정보가 업데이트되기를 기다립니다...")
                    # 아무 행동도 하지 않고 다음 루프를 기다립니다.
                
                # 봇도 포지션이 "없다"고 기억하는 경우 (정상적인 포지션 없음)
                else:
                    if config.repeat_after_take_profit:
                        _LOG.info("포지션 없음 확인. 재진입을 시도합니다.")
                        if not _execute_order_and_update_state(gate_client, config, current_bot_state, 0, "entry"):
                            _LOG.error("재진입 주문에 실패했습니다.")
                    else:
                        _LOG.info("반복 설정이 꺼져있으므로 전략을 종료합니다.")
                        break

            # --- 대기 시간 ---
            if not stop_event.is_set():
                wait_seconds = config.check_interval_seconds
                label = f" 다음 확인까지 [{wait_seconds}초] 대기 중..."
                with click.progressbar(length=wait_seconds, label=label, fill_char='█', empty_char='-') as bar:
                    for _ in range(wait_seconds):
                        if stop_event.is_set(): break
                        time.sleep(1)
                        bar.update(1)
                        
        except Exception as e:
            _LOG.error(f"전략 실행 중 예상치 못한 오류: {e}", exc_info=True)
            click.secho(f"\n❌ 오류 발생: {e}. 10초 후 재시도...", fg="red")
            time.sleep(10)
    
    _LOG.info(f"'{config.symbol}' 전략 루프 종료.")

def determine_trade_direction(
    gate_client: GateIOClient, 
    symbol: str, 
    major_timeframe: str = '1h', 
    trade_timeframe: str = '15m',
    short_window: int = 20, 
    long_window: int = 50, 
    rsi_period: int = 14
) -> Optional[Literal["long", "short"]]:
    """
    (초정밀) 다중 타임프레임, SMA, RSI, MACD를 결합하여 거래 방향을 결정합니다.
    """
    click.secho(f"\n🔍 {major_timeframe}/{trade_timeframe} 봉 기준, {symbol}의 추세를 정밀 분석합니다...", fg="cyan")
    
    try:
        # --- 1. 장기 추세 필터 (Major Trend Filter - 1h) ---
        _LOG.info(f"장기 추세 분석 ({major_timeframe})...")
        candles_major = gate_client.futures_api.list_futures_candlesticks(
            settle='usdt', contract=symbol, interval=major_timeframe, limit=long_window
        )
        if not candles_major or len(candles_major) < long_window:
            _LOG.error(f"장기 추세 분석을 위한 데이터가 충분하지 않습니다.")
            return None
        
        df_major = pd.DataFrame([c.to_dict() for c in candles_major], columns=['t', 'c'])
        df_major['c'] = pd.to_numeric(df_major['c'])
        sma_long_major = df_major['c'].rolling(window=long_window).mean().iloc[-1]
        last_price = float(candles_major[-1].c)

        is_major_trend_up = last_price > sma_long_major
        is_major_trend_down = last_price < sma_long_major
        _LOG.info(f"장기 추세 판단: 현재가({last_price:.2f}) vs {major_timeframe} {long_window}SMA({sma_long_major:.2f}) -> {'상승' if is_major_trend_up else '하락'}")

        # --- 2. 단기 진입 신호 분석 (Trade Signal - 15m) ---
        _LOG.info(f"단기 진입 신호 분석 ({trade_timeframe})...")
        candles_trade = gate_client.futures_api.list_futures_candlesticks(
            settle='usdt', contract=symbol, interval=trade_timeframe, limit=long_window + rsi_period + 34 # MACD 계산을 위한 충분한 데이터
        )
        if not candles_trade or len(candles_trade) < long_window:
            _LOG.error(f"단기 추세 분석을 위한 데이터가 충분하지 않습니다.")
            return None

        df_trade = pd.DataFrame([c.to_dict() for c in candles_trade], columns=['t', 'c'])
        df_trade['c'] = pd.to_numeric(df_trade['c'])
        
        # SMA 계산
        df_trade['sma_short'] = df_trade['c'].rolling(window=short_window).mean()
        df_trade['sma_long'] = df_trade['c'].rolling(window=long_window).mean()

        # RSI 계산
        delta = df_trade['c'].diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/rsi_period, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/rsi_period, adjust=False).mean()
        rs = gain / loss
        df_trade['rsi'] = 100 - (100 / (1 + rs))

        # MACD 계산
        ema_12 = df_trade['c'].ewm(span=12, adjust=False).mean()
        ema_26 = df_trade['c'].ewm(span=26, adjust=False).mean()
        df_trade['macd'] = ema_12 - ema_26
        df_trade['macd_signal'] = df_trade['macd'].ewm(span=9, adjust=False).mean()

        # 최종 데이터 추출
        last = df_trade.iloc[-1]
        _LOG.info(f"단기 지표: 단기SMA={last['sma_short']:.2f}, 장기SMA={last['sma_long']:.2f}, RSI={last['rsi']:.2f}, MACD={last['macd']:.2f}, Signal={last['macd_signal']:.2f}")

        # --- 3. 모든 조건 결합하여 최종 결정 ---
        is_golden_cross = last['sma_short'] > last['sma_long']
        is_dead_cross = last['sma_short'] < last['sma_long']
        is_macd_bullish = last['macd'] > last['macd_signal']
        is_macd_bearish = last['macd'] < last['macd_signal']

        # 롱 포지션 진입 조건: (장기 추세 상승) AND (단기 골든크로스) AND (RSI > 50) AND (MACD 상승)
        if is_major_trend_up and is_golden_cross and last['rsi'] > 50 and is_macd_bullish:
            click.secho(f"📈 모든 조건 충족. 'LONG' 포지션을 추천합니다.", fg="green", bold=True)
            return "long"
        
        # 숏 포지션 진입 조건: (장기 추세 하락) AND (단기 데드크로스) AND (RSI < 50) AND (MACD 하락)
        elif is_major_trend_down and is_dead_cross and last['rsi'] < 50 and is_macd_bearish:
            click.secho(f"📉 모든 조건 충족. 'SHORT' 포지션을 추천합니다.", fg="red", bold=True)
            return "short"
            
        else:
            click.secho("불확실성 높음. 진입 신호가 발견되지 않았습니다. 대기합니다.", fg="yellow")
            return None

    except Exception as e:
        _LOG.error(f"거래 방향 결정 중 예상치 못한 오류 발생: {e}", exc_info=True)
        return None
    
def handle_emergency_stop(gate_client: GateIOClient, stop_event: threading.Event):
    """모든 포지션을 조회하고 청산한 후, 종료 신호를 보냅니다."""
    click.secho("\n🚨 긴급 정지 명령 수신! 모든 포지션을 정리합니다...", fg="red", bold=True)
    try:
        open_positions = gate_client.list_all_positions()
        if not open_positions:
            click.secho("✅ 현재 보유 중인 포지션이 없습니다.", fg="green")
        else:
            click.echo(f" 	-> {len(open_positions)}개의 포지션을 발견했습니다. 시장가로 청산을 시도합니다.")
            for pos in open_positions:
                contract = pos.get('contract')
                size_str = pos.get('size')
                size = int(float(size_str)) if size_str is not None else 0
                if contract and size != 0:
                    click.echo(f" 		- 청산 시도: {contract} (수량: {size})")
                    close_order_result = gate_client.close_position_market(contract, size)
                    if close_order_result and close_order_result.get('id'):
                        click.secho(f" 			-> ✅ 청산 주문 성공. 주문 ID: {close_order_result.get('id')}", fg="green")
                    else:
                        click.secho(f" 			-> ❌ '{contract}' 청산 주문 실패. 거래소에서 직접 확인해주세요.", fg="red")
                else:
                    click.secho(f" 		- ⚠️ 잘못된 포지션 데이터, 건너뜁니다: {pos}", fg="yellow")
    except Exception as e:
        _LOG.error(f"긴급 정지 중 오류 발생: {e}", exc_info=True)
        click.secho(f"❌ 포지션 정리 중 오류가 발생했습니다. 로그를 확인하고 거래소에서 직접 포지션을 확인해주세요.", fg="red")
    click.echo(" 	-> 실행 중인 전략 스레드에 종료 신호를 보냅니다...")
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
            click.echo(f" 	[{i+1}] {file.name}")
    click.echo("-" * 50)
    click.echo(f" 	[n] 📝 새 설정 만들기 (대화형)")
    click.echo(f" 	[q] 🚪 종료")
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

def pretty_show_summary(config: BotConfig, current_bot_state: BotTradingState, actual_position: Optional[Dict[str, Any]]):
    """
    (최종 수정) API 우선, 실패 시 내부 추정치를 보여주는 UI 함수
    """
    click.echo() 
    
    position_size_raw = actual_position.get('size') if actual_position else None
    is_api_position_valid = position_size_raw is not None and float(position_size_raw) != 0

    # CASE 1: API를 통해 실제 포지션이 확인될 때 (가장 좋은 경우)
    if is_api_position_valid:
        try:
            pos_size = float(position_size_raw)
            entry_price = float(actual_position.get('entry_price', 0))
            margin_used = float(actual_position.get('margin', 0))
            leverage = float(actual_position.get('leverage', 1))
            unrealised_pnl = float(actual_position.get('unrealised_pnl', 0))
            roe_pct = (unrealised_pnl / margin_used) * 100 if margin_used > 1e-9 else 0.0
            pnl_color = "green" if unrealised_pnl >= 0 else "red"
            direction_str, direction_color, direction_icon = ("LONG", "green", "📈") if pos_size > 0 else ("SHORT", "red", "📉")

            click.secho(" ╭" + "─" * 25 + "┬" + "─" * 27 + "╮")
            title = f" {direction_icon} {config.symbol} | {direction_str} "
            click.secho(f" │{title:^25}│ {'현재 손익 (ROE)':^27} │", fg=direction_color, bold=True)
            click.secho(" ├" + "─" * 25 + "┼" + "─" * 27 + "┤")
            pnl_str = f"{unrealised_pnl:,.2f} USDT"
            roe_str = f"{roe_pct:.2f}%"
            click.secho(f" │ {'P L':<10}  {pnl_str:>12} │ {roe_str:^27} │", fg=pnl_color)
            click.secho(" ├" + "─" * 25 + "┴" + "─" * 27 + "┤")
            click.echo(f" │ {'평균 진입가':<12} {f'{entry_price:,.2f}':>11} │")
            click.echo(f" │ {'포지션 크기':<12} {f'{pos_size}':>11} │")
            click.echo(f" │ {'레버리지':<12} {f'{leverage:.0f}x':>11} │")
            # ... (이하 익절/손절 목표가 표시 로직은 이전과 동일)
            click.secho(" ╰" + "─" * 53 + "╯")
            return
        except (ValueError, TypeError) as e:
            _LOG.error(f"API 포지션 데이터 파싱 오류: {e}", exc_info=True)
            # 파싱 오류 시 아래 Fallback 로직으로 넘어감

    # CASE 2: API 포지션은 없지만, 봇 내부에 기록이 있을 때 (주문 직후 등)
    if current_bot_state.is_in_position:
        click.secho(" ╭" + "─" * 53 + "╮", fg="yellow")
        click.secho(" │ ⚠️  포지션 정보 업데이트 대기 중 (내부 추정치)         │", fg="yellow", bold=True)
        click.secho(" ├" + "─" * 53 + "┤", fg="yellow")
        
        avg_price = current_bot_state.current_avg_entry_price
        total_contracts = current_bot_state.total_position_contracts
        if avg_price and total_contracts:
            click.echo(f" │ {'추정 진입가':<12} {f'{avg_price:,.2f}':>11} USDT" + " "*25 + "│")
            click.echo(f" │ {'추정 수량':<12} {f'{total_contracts}':>11}" + " "*25 + "│")
        else:
             click.echo(" │ 내부 데이터 오류. 상태 확인 필요." + " "*25 + "│")
        click.secho(" ╰" + "─" * 53 + "╯", fg="yellow")
        return

    # CASE 3: API와 봇 내부 모두 포지션이 없을 때
    click.secho(" " * 2 + "╭" + "─" * 45 + "╮", fg="cyan")
    click.secho(f" │ 💤 {config.symbol:<15} 현재 포지션 없음 │", fg="cyan")
    click.secho(" " * 2 + "╰" + "─" * 45 + "╯", fg="cyan")

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
def main(config_file: Optional[Path] = None, smoke_test: bool = False, contract: str = "BTC_USDT") -> None:
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
    
    # 1. 설정 불러오기 또는 생성하기
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

    # 2. (조건부) 자동 방향 결정 및 무한 재시도 로직
    if bot_configuration.auto_determine_direction:
        click.secho("\n🤖 자동 방향 결정 기능 활성화됨. 추세를 분석합니다...", fg="cyan")
        
        retry_delay_seconds = 10  # 60초(1분) 대기

        while True: # ✅ 방향이 결정될 때까지 무한 반복
            determined_direction = determine_trade_direction(gate_client, bot_configuration.symbol)
            if determined_direction:
                bot_configuration.direction = determined_direction
                break  # 방향 결정 성공 시 루프 탈출
            
            click.secho(f"   -> 추세 불확실. {retry_delay_seconds}초 후 다시 분석합니다...", fg="yellow")
            time.sleep(retry_delay_seconds)

    # 3. 설정 값 보정
    bot_configuration.split_trigger_percents = [
        abs(p) * -1 for p in bot_configuration.split_trigger_percents
    ]
    
    # 4. 최종 설정으로 실행
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
        else:
            final_save_path = save_path_obj
        try:
            bot_configuration.save(final_save_path)
        except Exception as e:
            _LOG.error(f"설정 파일 저장 실패 ('{final_save_path}'): {e}", exc_info=True)
            click.secho(f"⚠️ 설정 파일 저장 실패: {e}", fg="yellow")

    if click.confirm("\n▶️ 위 설정으로 자동매매를 시작하시겠습니까?", default=True):
        _LOG.info(f"사용자 확인. '{bot_configuration.symbol}' 자동매매 시작.")
        click.secho(f"🚀 '{bot_configuration.symbol}' 자동매매 시작...", fg="green", bold=True)
        
        current_bot_trading_state = BotTradingState(symbol=bot_configuration.symbol)
        
        stop_event = threading.Event()
        
        strategy_thread = threading.Thread(
            target=run_strategy, 
            args=(bot_configuration, gate_client, current_bot_trading_state, stop_event),
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
                    click.echo("    (종료하시려면 'stop'을 입력해주세요...)")

        except KeyboardInterrupt:
            click.echo("\n🛑 Ctrl+C 감지. 봇 종료 신호를 보냅니다...")
            _LOG.warning("메인 스레드에서 Ctrl+C 감지. 전략 스레드에 종료 신호 전송.")
            handle_emergency_stop(gate_client, stop_event)

        click.echo("    -> 포지션 정리 및 종료를 기다리는 중...")
        strategy_thread.join(timeout=30)
        
        if strategy_thread.is_alive():
            _LOG.error("전략 스레드가 제 시간 내에 종료되지 않았습니다. 강제 종료될 수 있습니다.")
            click.secho("⚠️ 스레드가 제 시간 내에 종료되지 않았습니다.", fg="red")

        click.secho(f"\n🏁 '{bot_configuration.symbol}' 자동매매 전략이 종료되었습니다.", fg="blue", bold=True)
    else:
        _LOG.info("사용자가 자동매매 시작을 선택하지 않았습니다.")
        click.secho("👋 자동매매가 시작되지 않았습니다. 프로그램을 종료합니다.", fg="yellow")

    _LOG.info("="*10 + " 자동매매 봇 CLI 종료 " + "="*10)