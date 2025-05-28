# src/trading_bot/cli.py
import time
import click # CLI 생성을 위한 라이브러리
import logging
import sys # sys.exit() 사용
from pathlib import Path
from typing import Optional, List, Dict, Any, Literal # 타입 힌트용

# 내부 모듈 임포트
from .config import BotConfig
from .liquidation import calculate_liquidation_price
from .exchange_gateio import GateIOClient, ApiException # GateIOClient 및 ApiException 임포트

_LOG = logging.getLogger(__name__)

# --- 봇 상태 변수 (전략 실행 간 유지) ---
# 더 복잡한 봇에서는 이들을 클래스 멤버로 관리하는 것이 좋음
class BotTradingState:
    """봇의 현재 거래 관련 상태를 관리하는 클래스입니다."""
    def __init__(self, symbol: str):
        self.symbol = symbol # 이 상태가 어떤 심볼에 대한 것인지 명시
        self.current_avg_entry_price: Optional[float] = None
        self.total_position_contracts: float = 0.0  # 계약 수량 (BTC, ETH 등). 롱은 양수, 숏은 음수.
        self.total_position_initial_usd: float = 0.0 # 포지션 진입에 사용된 총 USD (수수료 제외 추정치)
        
        # 미체결 주문 ID 추적 (지정가 익절/손절 주문용)
        self.active_take_profit_order_id: Optional[str] = None
        self.active_stop_loss_order_id: Optional[str] = None
        
        self.current_split_order_count: int = 0 # 현재까지 성공적으로 실행된 분할매수 횟수
        self.last_known_liquidation_price: Optional[float] = None
        self.is_in_position: bool = False # 현재 포지션을 보유하고 있는지 여부

        _LOG.info(f"BotTradingState for {self.symbol} initialized.")

    def reset(self):
        """봇 상태를 초기화합니다 (새로운 거래 사이클 시작 또는 포지션 완전 종료 시)."""
        _LOG.info(f"BotTradingState for {self.symbol} resetting...")
        self.current_avg_entry_price = None
        self.total_position_contracts = 0.0
        self.total_position_initial_usd = 0.0
        self.active_take_profit_order_id = None
        self.active_stop_loss_order_id = None
        self.current_split_order_count = 0
        self.last_known_liquidation_price = None
        self.is_in_position = False
        _LOG.info(f"BotTradingState for {self.symbol} reset complete.")

    def update_on_fill(self, filled_contracts: float, fill_price: float, filled_usd_value: float, order_purpose: str):
        """
        주문 체결(fill)에 따라 포지션 상태를 업데이트합니다.

        Args:
            filled_contracts (float): 체결된 계약 수량 (롱은 양수, 숏은 음수).
            fill_price (float): 체결 가격.
            filled_usd_value (float): 체결된 주문의 USD 가치 (abs(filled_contracts) * fill_price 와 유사).
            order_purpose (str): 주문 목적 ("entry", "split", "take_profit", "stop_loss").
        """
        _LOG.info(f"Updating position state for {self.symbol} due to '{order_purpose}' fill: "
                  f"Contracts={filled_contracts:.8f}, Price=${fill_price:.4f}, USDValue=${filled_usd_value:.2f}")

        if not self.is_in_position: # 첫 진입 (entry)
            self.current_avg_entry_price = fill_price
            self.total_position_contracts = filled_contracts
            self.total_position_initial_usd = filled_usd_value
            self.is_in_position = True
            if order_purpose == "entry":
                 _LOG.info("Initial entry successful. Position opened.")
        else: # 포지션에 추가 (split) 또는 부분/전체 청산 (tp/sl)
            if order_purpose in ["take_profit", "stop_loss"]: # 포지션 청산
                # 청산 주문이므로 filled_contracts는 현재 포지션과 반대 부호
                new_total_contracts = self.total_position_contracts + filled_contracts
                if abs(new_total_contracts) < 1e-8: # 포지션 전체 청산됨
                    _LOG.info(f"{order_purpose.upper()} resulted in full position closure for {self.symbol}.")
                    self.reset() # 상태 초기화
                else: # 부분 청산 (일반적으로 TP/SL은 전체 청산을 목표로 함)
                    _LOG.warning(f"{order_purpose.upper()} resulted in partial closure. "
                                 f"Remaining contracts: {new_total_contracts:.8f}. State may be inconsistent.")
                    # 부분 청산 시 평균 단가, 총 투입 USD 등 재계산 필요 (여기서는 단순화)
                    self.total_position_contracts = new_total_contracts
                    # total_position_initial_usd도 비례적으로 줄여야 함 (복잡)
                    # 여기서는 일단 reset으로 처리하거나, 더 정교한 로직 필요.
                    # 지금은 전체 청산만 가정하고 reset 호출.
                    self.reset() # TP/SL은 전체 청산으로 가정하고 상태 리셋
                return # TP/SL 후에는 아래 로직 실행 안 함

            # 분할 매수 (split)
            # 새 평균 단가 = (기존 총 USD 가치 + 신규 주문 USD 가치) / (기존 총 계약 수량 + 신규 주문 계약 수량)
            # 기존 총 USD 가치 = 기존 평균단가 * 기존 계약수량(절대값)
            # 신규 주문 USD 가치 = filled_usd_value
            
            # 부호를 고려한 계약 수량 및 USD 가치 계산
            # (기존 총 계약 가치 + 신규 계약 가치) / (새로운 총 계약 수량)
            # 계약 가치 = 계약 수량 * 진입가 (숏일 경우 음의 가치로 볼 수도 있으나, 계산 복잡)
            # 여기서는 USD 투입액 기준으로 평균 단가 계산
            
            new_total_initial_usd = self.total_position_initial_usd + filled_usd_value
            new_total_contracts_abs = abs(self.total_position_contracts + filled_contracts)

            if new_total_contracts_abs > 1e-9: # 0으로 나누기 방지
                # (기존 평단 * 기존 계약수 + 신규 체결가 * 신규 계약수) / (기존 계약수 + 신규 계약수)
                # 여기서 계약수는 절대값으로 사용
                prev_abs_contracts = abs(self.total_position_contracts)
                new_abs_contracts = abs(filled_contracts)
                
                self.current_avg_entry_price = \
                    ((self.current_avg_entry_price or 0) * prev_abs_contracts + fill_price * new_abs_contracts) / \
                    (prev_abs_contracts + new_abs_contracts)
            else: # 모든 포지션이 정확히 0이 된 경우 (이론상)
                self.current_avg_entry_price = None # 평균 단가 의미 없음

            self.total_position_contracts += filled_contracts # 부호 유지
            self.total_position_initial_usd = new_total_initial_usd
            
            if order_purpose == "split":
                 self.current_split_order_count += 1
                 _LOG.info(f"Split order {self.current_split_order_count} successful.")

        _LOG.info(f"Position state updated for {self.symbol}: AvgEntryPrice=${self.current_avg_entry_price:.4f if self.current_avg_entry_price else 'N/A'}, "
                  f"TotalContracts={self.total_position_contracts:.8f}, TotalInitialUSD=${self.total_position_initial_usd:.2f}, "
                  f"IsInPosition={self.is_in_position}")

# 각 심볼별 거래 상태를 관리하기 위한 딕셔너리
# key: symbol (str), value: BotTradingState 인스턴스
# 이 방식은 단일 프로세스에서 여러 심볼을 순차적으로 관리할 때 사용 가능.
# 동시에 여러 심볼을 독립적으로 관리하려면 각 심볼마다 별도 스레드/프로세스 또는 비동기 작업 필요.
# 여기서는 단일 심볼 거래를 가정하고, cli_main에서 BotTradingState 객체를 생성하여 run_strategy에 전달.
# global bot_state # 이전 방식 -> 인스턴스 전달 방식으로 변경


def prompt_config() -> BotConfig:
    """사용자로부터 대화형으로 봇 설정을 입력받습니다."""
    click.echo("\n" + "="*10 + " 📈 Gate.io 선물 자동매매 봇 설정 시작 " + "="*10)
    
    direction = click.prompt("👉 포지션 방향 (long/short)", type=click.Choice(["long", "short"]), default="long")
    symbol = click.prompt("👉 거래 심볼 (예: BTC_USDT)", default="BTC_USDT").upper().strip()
    leverage = click.prompt("👉 레버리지 (예: 10)", type=int, default=10)
    margin_mode = click.prompt("👉 마진 모드 (cross/isolated)", type=click.Choice(["cross", "isolated"]), default="isolated")
    entry_amount_usd = click.prompt("👉 첫 진입 금액 (USDT)", type=float, default=100.0)
    max_split_count = click.prompt("👉 최대 분할매수 횟수 (0이면 안 함)", type=int, default=0)

    split_trigger_percents: List[float] = []
    split_amounts_usd: List[float] = []
    if max_split_count > 0:
        click.secho(f"\n💧 {max_split_count}회 분할매수 상세 설정:", fg="cyan")
        for i in range(max_split_count):
            default_trigger = (-(i + 1.0) * 0.5) if direction == "long" else ((i + 1.0) * 0.5)
            trigger_prompt_msg = (f"  - {i+1}번째 분할매수 트리거 가격 변동률 (%) "
                                  f"(현재 평균단가 대비, 예: {default_trigger:.1f} for {direction.upper()})")
            trigger = click.prompt(trigger_prompt_msg, type=float, default=default_trigger)
            split_trigger_percents.append(trigger)
            
            default_amount = round(entry_amount_usd * (0.5 + i * 0.25), 2) # 예시 기본값
            amount_prompt_msg = f"  - {i+1}번째 분할매수 추가 진입 금액 (USDT)"
            amount = click.prompt(amount_prompt_msg, type=float, default=default_amount)
            split_amounts_usd.append(amount)

    tp_default = "5.0" # 익절 기본값 문자열
    take_profit_pct_str = click.prompt(f"👉 익절 수익률 (%) (평균단가 대비. 비워두면 미사용. 예: {tp_default})",
                                       type=str, default=tp_default, show_default=True) # show_default=True로 하여 기본값 표시
    take_profit_pct = float(take_profit_pct_str) if take_profit_pct_str.strip() else None # 빈 문자열 입력 시 None
    
    sl_default = "2.5" # 손절 기본값 문자열
    # 익절 설정 시 손절도 기본 활성화 제안, 아니면 기본 비활성화
    enable_sl_default_suggestion = True if take_profit_pct is not None else False
    enable_stop_loss = click.confirm("🛡️ 손절 기능을 활성화할까요?", default=enable_sl_default_suggestion)
    
    stop_loss_pct = None
    if enable_stop_loss:
        stop_loss_pct_str = click.prompt(f"👉 손절 손실률 (%) (평균단가 대비. 예: {sl_default})",
                                         type=str, default=sl_default, show_default=True)
        stop_loss_pct = float(stop_loss_pct_str) if stop_loss_pct_str.strip() else None
        if stop_loss_pct is None: # 사용자가 손절률을 입력하지 않으면 비활성화
            enable_stop_loss = False # 명시적으로 비활성화
            _LOG.info("손절률이 입력되지 않아 손절 기능이 비활성화됩니다.")
    else: # 손절 기능 비활성화 선택 시
        stop_loss_pct = None # 명시적으로 None 설정


    order_type = click.prompt("👉 주문 방식 (market/limit)", type=click.Choice(["market", "limit"]), default="market")
    limit_slippage_default = 0.05 
    limit_order_slippage_pct = limit_slippage_default
    if order_type == "limit":
        limit_order_slippage_pct = click.prompt(f"👉 지정가 주문 시 슬리피지 (%) (예: {limit_slippage_default})", 
                                                type=float, default=limit_slippage_default)

    cfg_data = {
        "direction": direction,
        "symbol": symbol,
        "leverage": leverage,
        "margin_mode": margin_mode,
        "entry_amount_usd": entry_amount_usd,
        "max_split_count": max_split_count,
        "split_trigger_percents": split_trigger_percents,
        "split_amounts_usd": split_amounts_usd,
        "take_profit_pct": take_profit_pct,
        "stop_loss_pct": stop_loss_pct,
        "order_type": order_type,
        "limit_order_slippage_pct": limit_order_slippage_pct,
        "repeat_after_take_profit": click.confirm("📈 익절 후 동일 설정으로 자동 반복할까요?", default=False),
        "stop_bot_after_stop_loss": click.confirm("🛑 손절 발생 시 봇을 완전히 중지할까요?", default=True),
        "enable_stop_loss": enable_stop_loss, # 사용자의 최종 선택 반영
        "check_interval_seconds": click.prompt("⏱️ 가격 및 전략 확인 주기 (초)", type=int, default=60),
        "order_id_prefix": click.prompt("🆔 주문 ID 접두사 (t-로 시작 권장)", default="t-tradingbot-").strip() or "t-tradingbot-",
    }
    try:
        # BotConfig 생성 시 __post_init__에서 유효성 검사 실행
        config_obj = BotConfig(**cfg_data)
        _LOG.info(f"사용자 입력으로부터 BotConfig 생성 완료: {config_obj.symbol}")
        return config_obj
    except ValueError as e: # BotConfig의 __post_init__에서 발생한 유효성 검사 오류
        _LOG.error(f"봇 설정 값 유효성 검사 실패: {e}", exc_info=True)
        click.secho(f"오류: {e}", fg="red", bold=True)
        click.secho("설정을 처음부터 다시 시작합니다.", fg="yellow")
        return prompt_config() # 오류 시 설정 프롬프트 재시도


def show_summary(
    config: BotConfig, 
    current_market_price: Optional[float], 
    gate_client: GateIOClient,
    current_bot_state: BotTradingState # 현재 봇 상태 객체 전달
) -> None:
    """현재 봇 설정, 시장 상황, 포지션 정보를 요약하여 표시합니다."""
    click.secho("\n" + "="*15 + " 📊 봇 상태 및 설정 요약 " + "="*15, fg="yellow", bold=True)
    
    # 설정 정보 출력
    click.secho("[봇 설정]", fg="cyan")
    config_dict = config.to_dict()
    for k, v_val in config_dict.items(): # 변수명 변경 (v -> v_val)
        click.echo(f"  {k:<28}: {v_val}") # 항목명 정렬
    
    # 시장 및 계산 정보
    click.secho("\n[시장 및 계산 정보]", fg="cyan")
    if current_market_price is not None:
        click.echo(f"  현재 시장가 ({config.symbol:<10}): {current_market_price:.4f} USDT")
    else:
        click.echo(f"  현재 시장가 ({config.symbol:<10}): 정보 없음 (API 조회 실패 가능성)")

    # 실제 포지션 정보 조회 (API 호출)
    actual_position_info: Optional[Dict[str, Any]] = None
    try:
        actual_position_info = gate_client.get_position(config.symbol)
    except ApiException as e:
        _LOG.warning(f"{config.symbol} 실제 포지션 정보 조회 중 API 오류: {e.body}", exc_info=True)
        click.secho(f"  (경고: {config.symbol} 실제 포지션 조회 실패 - API 오류)", fg="red")
    except Exception as e: # 네트워크 오류 등
        _LOG.error(f"{config.symbol} 실제 포지션 정보 조회 중 예외 발생: {e}", exc_info=True)
        click.secho(f"  (에러: {config.symbol} 실제 포지션 조회 중 오류 발생)", fg="red")

    if actual_position_info and actual_position_info.get('size', 0) != 0:
        click.secho("\n[실제 거래소 포지션]", fg="magenta")
        pos_size = float(actual_position_info['size']) # 부호 있는 계약 수량
        pos_entry_price = float(actual_position_info['entry_price'])
        pos_leverage = actual_position_info.get('leverage', 'N/A') # 문자열일 수 있음
        pos_liq_price_api = actual_position_info.get('liq_price', 'N/A') # API가 제공하는 청산가
        pos_unreal_pnl = actual_position_info.get('unrealised_pnl', 'N/A') # 미실현 손익
        pos_real_pnl = actual_position_info.get('realised_pnl', 'N/A') # 실현 손익
        
        click.echo(f"  - 방향          : {'LONG' if pos_size > 0 else 'SHORT'}")
        click.echo(f"  - 진입가 (API)  : {pos_entry_price:.4f} USDT")
        click.echo(f"  - 수량 (API)    : {pos_size:.8f} {config.symbol.split('_')[0]}")
        click.echo(f"  - 레버리지 (API): {pos_leverage}x")
        click.echo(f"  - 청산가 (API)  : {pos_liq_price_api if pos_liq_price_api else 'N/A'} USDT")
        click.echo(f"  - 미실현 손익   : {pos_unreal_pnl} USDT")
        click.echo(f"  - 실현 손익     : {pos_real_pnl} USDT")
    else: # API 응답이 None이거나 size가 0인 경우
        click.secho(f"\n[{config.symbol} 실제 거래소 포지션 없음 또는 조회 실패]", fg="magenta")


    # 봇 내부 상태 기반 정보
    click.secho("\n[봇 내부 추적 상태]", fg="blue")
    if current_bot_state.is_in_position and current_bot_state.current_avg_entry_price is not None:
        # 봇 내부 추적 방향과 설정 방향 일치 여부 확인 (중요)
        bot_tracked_direction_consistent = \
            (config.direction == "long" and current_bot_state.total_position_contracts > 0) or \
            (config.direction == "short" and current_bot_state.total_position_contracts < 0)
        
        direction_display = config.direction.upper()
        if not bot_tracked_direction_consistent:
            direction_display += " (경고: 내부 상태와 설정 불일치!)"
            _LOG.warning(f"봇 내부 추적 포지션 방향(계약수량 부호: {current_bot_state.total_position_contracts})과 "
                         f"설정된 방향({config.direction})이 일치하지 않습니다.")

        click.echo(f"  - 추적 방향     : {direction_display}")
        click.echo(f"  - 평균 진입가   : {current_bot_state.current_avg_entry_price:.4f} USDT")
        click.echo(f"  - 총 계약 수량  : {current_bot_state.total_position_contracts:.8f} {config.symbol.split('_')[0]}")
        click.echo(f"  - 총 투입 원금  : {current_bot_state.total_position_initial_usd:.2f} USDT (추정치)")
        click.echo(f"  - 분할매수 횟수 : {current_bot_state.current_split_order_count} / {config.max_split_count}")

        # 예상 청산가 계산 (봇 내부 상태 기준)
        liq_price_calc, change_pct_calc = calculate_liquidation_price(
            total_position_collateral_usd=current_bot_state.total_position_initial_usd,
            leverage=config.leverage,
            margin_mode=config.margin_mode,
            avg_entry_price=current_bot_state.current_avg_entry_price,
            position_direction=config.direction # 설정된 방향 기준
        )
        current_bot_state.last_known_liquidation_price = liq_price_calc # 상태 업데이트
        
        if liq_price_calc is not None and change_pct_calc is not None:
            # 변동률 부호: 롱은 음수(하락), 숏은 양수(상승)일 때 청산 위험
            change_display_char = '-' if config.direction == 'long' else '+'
            click.secho(f"  예상 청산가(계산): {liq_price_calc:.4f} USDT "
                        f"({change_display_char}{abs(change_pct_calc):.2f}% from avg entry)",
                        fg="magenta")
        else:
            click.secho("  예상 청산가(계산): 계산 불가 (데이터 부족 또는 조건 미충족)", fg="magenta")
            
        # 익절/손절 목표가 표시
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
    current_bot_state: BotTradingState, # 현재 봇 상태 객체
    order_usd_amount: float, # 이 주문에 사용할 USD 금액 (Entry/Split 시)
    order_purpose: Literal["entry", "split", "take_profit", "stop_loss"]
) -> bool:
    """
    주문을 실행하고 성공 시 봇의 내부 상태를 업데이트합니다.
    TP/SL 주문은 항상 reduce_only=True로, 포지션 방향과 반대로 실행됩니다.
    Entry/Split 주문은 reduce_only=False로, 설정된 포지션 방향으로 실행됩니다.

    Args:
        order_usd_amount: 
            - Entry/Split 시: 신규로 투입할 USD 금액.
            - TP/SL 시: 이 값은 무시되고, 현재 포지션 전체를 청산 시도.
    Returns:
        bool: 주문이 성공적으로 API에 접수되었으면 True. (체결 보장은 아님)
    """
    is_tp_sl_order = order_purpose in ["take_profit", "stop_loss"]
    reduce_only_flag = is_tp_sl_order
    
    order_execution_side: Literal["long", "short"]
    if is_tp_sl_order: # TP/SL 주문 시 주문 방향은 현재 포지션과 반대
        if not current_bot_state.is_in_position:
            _LOG.warning(f"{order_purpose} 주문 시도 중 포지션 없음. 주문 건너뜀.")
            return False
        order_execution_side = "short" if config.direction == "long" else "long"
    else: # Entry 또는 Split 주문
        order_execution_side = config.direction

    # 주문 ID 접두사 설정
    order_id_suffix = f"{order_purpose}"
    if order_purpose == 'split':
        order_id_suffix += f"-{current_bot_state.current_split_order_count + 1}" # 다음 분할매수 번호
    
    full_order_id_prefix = config.order_id_prefix + order_id_suffix

    # TP/SL 주문 시, order_usd_amount는 무시하고 전체 포지션 청산을 위한 USD 가치 계산
    usd_amount_for_api_call = order_usd_amount
    if is_tp_sl_order:
        current_market_price = gate_client.fetch_last_price(config.symbol)
        if current_market_price is None:
            _LOG.error(f"{order_purpose} 주문 위한 현재가 조회 실패. 주문 건너뜀.")
            return False
        # 전체 포지션 청산을 위한 USD 가치 (계약 수량 * 현재가)
        usd_amount_for_api_call = abs(current_bot_state.total_position_contracts) * current_market_price
        _LOG.info(f"{order_purpose} 주문: 전체 포지션 청산 시도. "
                  f"계약수량={abs(current_bot_state.total_position_contracts):.8f}, "
                  f"추정USD가치=${usd_amount_for_api_call:.2f}")
        if usd_amount_for_api_call < 1e-2: # 매우 작은 금액이면 주문 의미 없음
            _LOG.warning(f"{order_purpose} 주문 위한 포지션 가치가 너무 작음 (${usd_amount_for_api_call:.2f}). 주문 건너뜀.")
            # 이 경우, 이미 포지션이 거의 없다고 보고 상태를 리셋할 수도 있음.
            if abs(current_bot_state.total_position_contracts) < 1e-8 : # 계약 수량이 0에 가까우면 리셋
                current_bot_state.reset()
            return False


    # 지정가 주문 시 가격 계산
    limit_order_price_for_api: Optional[float] = None
    # TP/SL은 보통 시장가로 즉시 체결, Entry/Split은 설정에 따름
    effective_order_type = "market" if is_tp_sl_order else config.order_type
    
    if effective_order_type == "limit":
        # 지정가 계산: TP/SL의 경우, 목표 가격을 지정가로 사용. Entry/Split은 슬리피지 적용.
        if order_purpose == "take_profit" and current_bot_state.current_avg_entry_price and config.take_profit_pct:
            limit_order_price_for_api = current_bot_state.current_avg_entry_price * \
                (1 + (config.take_profit_pct / 100.0) * (1 if config.direction == "long" else -1))
        elif order_purpose == "stop_loss" and current_bot_state.current_avg_entry_price and config.stop_loss_pct:
             limit_order_price_for_api = current_bot_state.current_avg_entry_price * \
                (1 - (config.stop_loss_pct / 100.0) * (1 if config.direction == "long" else -1))
        elif not is_tp_sl_order: # Entry 또는 Split
            current_price_for_limit = gate_client.fetch_last_price(config.symbol)
            if current_price_for_limit is None:
                _LOG.error(f"{config.symbol} 현재가 조회 실패로 지정가 계산 불가. 주문 실패 처리.")
                return False
            # 롱 주문(매수) 시 현재가보다 약간 낮게, 숏 주문(매도) 시 현재가보다 약간 높게 지정가 설정 (유리한 방향)
            slippage_factor = -1.0 if order_execution_side == "long" else 1.0
            limit_order_price_for_api = current_price_for_limit * \
                (1 + (slippage_factor * config.limit_order_slippage_pct / 100.0))
        
        if limit_order_price_for_api is not None:
             _LOG.info(f"{order_purpose} 지정가 주문 가격 계산됨: {limit_order_price_for_api:.4f}")
        else: # 지정가 계산 실패 (TP/SL인데 평단가 없거나, Entry/Split인데 현재가 조회 실패)
            _LOG.warning(f"{order_purpose} 지정가 주문 가격 계산 실패. 시장가로 강제 전환 또는 주문 실패 고려.")
            effective_order_type = "market" # 안전하게 시장가로 전환

    # 주문 실행
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
        
        # 중요: 실제 체결(fill)은 비동기적으로 발생할 수 있음.
        # 시장가 주문은 비교적 빨리 체결되지만, 지정가는 대기할 수 있음.
        # 이 함수는 주문 '접수' 성공 여부만 반환. 체결 확인 및 상태 업데이트는 별도 로직 필요.
        # 여기서는 단순화를 위해, 시장가 주문은 즉시 체결되었다고 가정하고 상태 업데이트 시도.
        # 지정가 주문은 active_xxx_order_id에 저장하고, run_strategy 루프에서 상태 확인.

        if effective_order_type == "market":
            _LOG.info(f"시장가 {order_purpose} 주문 접수. 체결 가정하고 상태 업데이트 시도 (실제 체결 확인 필요).")
            # 체결 가격 및 수량은 API 응답에서 가져와야 함.
            # order_result에 'avg_fill_price' 또는 'filled_size' 등이 있을 수 있음 (Gate.io API 문서 확인)
            # 여기서는 임시로 주문 시점의 현재가를 체결가로, 요청된 USD를 체결액으로 가정.
            # 실제로는 get_order_status(order_id)를 호출하여 체결 정보 확인해야 함.
            
            # Gate.io FuturesOrder 객체는 'fill_price' (평균 체결가), 'filled_size' (체결 수량, 부호 있음) 필드를 가짐.
            # 주문 즉시 이 값이 채워지지 않을 수 있음.
            filled_price_str = order_result.get('fill_price') # 평균 체결가
            filled_size_str = order_result.get('filled_size') # 체결된 계약 수량 (부호 있음)

            if filled_price_str and filled_size_str and float(filled_price_str) > 0 and float(filled_size_str) != 0:
                actual_fill_price = float(filled_price_str)
                actual_filled_contracts = float(filled_size_str) # 부호 있는 계약 수량
                actual_filled_usd = abs(actual_filled_contracts) * actual_fill_price # 체결된 USD 가치

                _LOG.info(f"시장가 주문 체결 정보 (API 응답 기반): 가격=${actual_fill_price:.4f}, 계약수량={actual_filled_contracts:.8f}, USD가치=${actual_filled_usd:.2f}")
                current_bot_state.update_on_fill(
                    filled_contracts=actual_filled_contracts,
                    fill_price=actual_fill_price,
                    filled_usd_value=actual_filled_usd,
                    order_purpose=order_purpose
                )
            else: # 체결 정보가 즉시 없으면, 일단 현재가를 기준으로 추정 (나중에 보정 필요)
                _LOG.warning(f"시장가 주문({order_id}) 체결 정보 즉시 확인 불가. 현재가 기준으로 임시 상태 업데이트.")
                temp_fill_price = gate_client.fetch_last_price(config.symbol) or \
                                  (current_bot_state.current_avg_entry_price if current_bot_state.is_in_position else 0) # fallback
                if temp_fill_price > 0 :
                    # 주문 요청된 계약 수량 (부호 있음)
                    requested_contracts = (usd_amount_for_api_call / temp_fill_price) * (1 if order_execution_side == "long" else -1)
                    current_bot_state.update_on_fill(
                        filled_contracts=requested_contracts, # 요청된 계약 수량으로 가정
                        fill_price=temp_fill_price,           # 현재가로 가정
                        filled_usd_value=usd_amount_for_api_call, # 요청된 USD로 가정
                        order_purpose=order_purpose
                    )
                else:
                    _LOG.error("임시 체결가 계산 위한 현재가 조회 실패. 상태 업데이트 불가.")


        elif effective_order_type == "limit": # 지정가 주문
            if order_purpose == "take_profit":
                current_bot_state.active_take_profit_order_id = order_id
                _LOG.info(f"지정가 익절 주문({order_id}) 대기 중. 목표가: {limit_order_price_for_api:.4f}")
            elif order_purpose == "stop_loss":
                current_bot_state.active_stop_loss_order_id = order_id
                _LOG.info(f"지정가 손절 주문({order_id}) 대기 중. 목표가: {limit_order_price_for_api:.4f}")
            else: # 지정가 Entry/Split (여기서는 일단 시장가처럼 즉시 체결 가정 단순화. 실제로는 체결 대기 로직 필요)
                 _LOG.warning(f"지정가 {order_purpose} 주문({order_id}) 접수. 즉시 체결 가정하고 상태 업데이트 (실제 체결 확인 필요).")
                 # 위 시장가와 유사한 임시 상태 업데이트 (실제로는 체결 대기해야 함)
                 temp_fill_price = limit_order_price_for_api or gate_client.fetch_last_price(config.symbol) or 0
                 if temp_fill_price > 0:
                    requested_contracts = (usd_amount_for_api_call / temp_fill_price) * (1 if order_execution_side == "long" else -1)
                    current_bot_state.update_on_fill(requested_contracts, temp_fill_price, usd_amount_for_api_call, order_purpose)
                 else:
                     _LOG.error("지정가 주문 임시 체결가 계산 실패. 상태 업데이트 불가.")


        return True # 주문 접수 성공
    else:
        _LOG.error(f"{order_purpose.upper()} 주문 실패 또는 API로부터 유효한 응답 받지 못함.")
        return False


def _check_and_handle_limit_orders(gate_client: GateIOClient, config: BotConfig, current_bot_state: BotTradingState):
    """미체결 지정가 익절/손절 주문 상태를 확인하고 처리합니다."""
    if current_bot_state.active_take_profit_order_id:
        order_id = current_bot_state.active_take_profit_order_id
        _LOG.debug(f"미체결 지정가 익절 주문({order_id}) 상태 확인 중...")
        status = gate_client.get_order_status(order_id)
        if status and status.get('status') == 'closed': # 'closed'는 완전 체결 의미 (Gate.io 확인)
            _LOG.info(f"지정가 익절 주문({order_id}) 체결 확인!")
            fill_price = float(status.get('fill_price', 0)) # 평균 체결가
            filled_contracts = float(status.get('filled_size', 0)) # 체결된 계약 수량 (부호 있음)
            if fill_price > 0 and filled_contracts != 0:
                current_bot_state.update_on_fill(filled_contracts, fill_price, abs(filled_contracts)*fill_price, "take_profit")
            else:
                _LOG.error(f"익절 주문({order_id}) 체결 정보 부족. 상태 업데이트 실패. Status: {status}")
                # 이 경우, 포지션 정보를 직접 조회하여 상태를 보정해야 할 수 있음.
            current_bot_state.active_take_profit_order_id = None # 주문 ID 제거
        elif status and status.get('status') in ['cancelled', 'expired']: # 취소 또는 만료
            _LOG.warning(f"지정가 익절 주문({order_id})이 '{status.get('status')}' 상태입니다. 주문 ID 제거.")
            current_bot_state.active_take_profit_order_id = None
        elif not status: # 주문 조회 실패
            _LOG.error(f"지정가 익절 주문({order_id}) 상태 조회 실패. 주문 ID 유지하고 다음 사이클에 재확인.")

    if current_bot_state.active_stop_loss_order_id:
        order_id = current_bot_state.active_stop_loss_order_id
        _LOG.debug(f"미체결 지정가 손절 주문({order_id}) 상태 확인 중...")
        status = gate_client.get_order_status(order_id)
        if status and status.get('status') == 'closed':
            _LOG.info(f"지정가 손절 주문({order_id}) 체결 확인!")
            fill_price = float(status.get('fill_price', 0))
            filled_contracts = float(status.get('filled_size', 0))
            if fill_price > 0 and filled_contracts != 0:
                current_bot_state.update_on_fill(filled_contracts, fill_price, abs(filled_contracts)*fill_price, "stop_loss")
            else:
                _LOG.error(f"손절 주문({order_id}) 체결 정보 부족. 상태 업데이트 실패. Status: {status}")
            current_bot_state.active_stop_loss_order_id = None
        elif status and status.get('status') in ['cancelled', 'expired']:
            _LOG.warning(f"지정가 손절 주문({order_id})이 '{status.get('status')}' 상태입니다. 주문 ID 제거.")
            current_bot_state.active_stop_loss_order_id = None
        elif not status:
            _LOG.error(f"지정가 손절 주문({order_id}) 상태 조회 실패. 주문 ID 유지하고 다음 사이클에 재확인.")


def run_strategy(config: BotConfig, gate_client: GateIOClient, current_bot_state: BotTradingState) -> None:
    """메인 거래 전략 실행 로직."""
    _LOG.info(f"'{config.symbol}'에 대한 거래 전략 시작. 설정: {config.to_dict()}")
    
    # 전략 시작 시, 기존 미체결 TP/SL 주문이 있다면 취소 시도 (봇 재시작 시 등)
    # 이는 선택적. 여기서는 일단 상태 초기화만.
    # current_bot_state.reset() # run_main_cli에서 호출하므로 여기서는 생략 또는 조건부 호출

    # --- 1. 초기 진입 주문 (봇 상태가 포지션 없음을 나타낼 경우) ---
    if not current_bot_state.is_in_position:
        click.secho(f"\n🚀 초기 진입 주문 시도 ({config.direction.upper()}) for {config.symbol}...", fg="green", bold=True)
        if not _execute_order_and_update_state(gate_client, config, current_bot_state, config.entry_amount_usd, "entry"):
            _LOG.critical("초기 진입 주문 실패. 이 심볼에 대한 전략을 시작할 수 없습니다.")
            click.secho(f"❌ {config.symbol} 초기 진입 주문 실패. 전략 실행 중지.", fg="red", bold=True)
            return # 초기 진입 실패 시 해당 심볼 전략 종료

    strategy_active_for_this_symbol = True
    while strategy_active_for_this_symbol:
        try:
            _LOG.info(f"'{config.symbol}' 전략 루프 시작. 현재 분할매수 횟수: {current_bot_state.current_split_order_count}")
            current_market_price = gate_client.fetch_last_price(config.symbol)
            if current_market_price is None:
                _LOG.error(f"{config.symbol} 현재가 조회 실패. 다음 사이클까지 {config.check_interval_seconds}초 대기합니다.")
                time.sleep(config.check_interval_seconds)
                continue # 루프 계속

            show_summary(config, current_market_price, gate_client, current_bot_state)

            # --- 0. 미체결 지정가 주문 상태 확인 ---
            _check_and_handle_limit_orders(gate_client, config, current_bot_state)

            # 포지션 상태 재확인 (지정가 주문 체결로 상태가 변경되었을 수 있음)
            if not current_bot_state.is_in_position:
                if config.repeat_after_take_profit: # 익절 후 반복 설정 시
                    _LOG.info(f"{config.symbol} 포지션 없음 (이전 TP/SL로 청산된 듯). '익절 후 반복' 설정에 따라 재진입 시도.")
                    click.secho(f"\n🔁 '{config.symbol}' 재진입 시도 ({config.direction.upper()})...", fg="blue")
                    current_bot_state.reset() # 재진입 전 상태 완전 초기화
                    if not _execute_order_and_update_state(gate_client, config, current_bot_state, config.entry_amount_usd, "entry"):
                        _LOG.error(f"{config.symbol} 재진입 주문 실패. 다음 사이클까지 대기합니다.")
                    # 재진입 성공/실패 후 루프는 계속됨 (다음 반복에서 가격 다시 체크)
                else: # 반복 설정 없으면 종료
                    _LOG.info(f"{config.symbol} 포지션 없음. 반복 실행 설정 꺼져있으므로 이 심볼에 대한 전략 종료.")
                    strategy_active_for_this_symbol = False
                if not strategy_active_for_this_symbol: continue # while 루프 조건 검사로 이동


            # --- 2. 익절(Take Profit) 로직 ---
            # 미체결 TP 주문이 없고, 익절 조건 충족 시 신규 TP 주문 시도
            if strategy_active_for_this_symbol and config.take_profit_pct and \
               current_bot_state.is_in_position and current_bot_state.current_avg_entry_price and \
               current_bot_state.active_take_profit_order_id is None: # 기존 TP 주문 없을 때만
                
                profit_target_price = current_bot_state.current_avg_entry_price * \
                                      (1 + (config.take_profit_pct / 100.0) * (1 if config.direction == "long" else -1))
                
                tp_condition_met = (config.direction == "long" and current_market_price >= profit_target_price) or \
                                   (config.direction == "short" and current_market_price <= profit_target_price)

                if tp_condition_met:
                    _LOG.info(f"💰 {config.symbol} 익절 조건 충족! 현재가: {current_market_price:.4f}, 익절 목표가: {profit_target_price:.4f}")
                    click.secho(f"💰 {config.symbol} 익절 조건 충족 (현재가: {current_market_price:.4f}). 익절 주문 실행...", fg="green", bold=True)
                    # TP 주문 시 order_usd_amount는 무시됨 (_execute_order_and_update_state 내부에서 전체 포지션 가치로 계산)
                    if _execute_order_and_update_state(gate_client, config, current_bot_state, 0, "take_profit"):
                        # 성공적으로 TP 주문 접수 (시장가면 즉시 체결 가정, 지정가면 ID 저장됨)
                        # BotTradingState.is_in_position은 update_on_fill에서 false로 설정됨 (시장가 체결 시)
                        # 또는 active_take_profit_order_id가 설정됨 (지정가 시)
                        if not config.repeat_after_take_profit and not current_bot_state.is_in_position: # 반복 안 하고, 포지션 청산됐으면
                            _LOG.info(f"{config.symbol} 익절 후 반복 설정 꺼짐. 이 심볼에 대한 전략 종료.")
                            strategy_active_for_this_symbol = False
                    else:
                        _LOG.error(f"{config.symbol} 익절 주문 실행/접수 실패.")
                    if not strategy_active_for_this_symbol: continue


            # --- 3. 손절(Stop Loss) 로직 ---
            if strategy_active_for_this_symbol and config.enable_stop_loss and config.stop_loss_pct and \
               current_bot_state.is_in_position and current_bot_state.current_avg_entry_price and \
               current_bot_state.active_stop_loss_order_id is None: # 기존 SL 주문 없을 때만

                loss_target_price = current_bot_state.current_avg_entry_price * \
                                    (1 - (config.stop_loss_pct / 100.0) * (1 if config.direction == "long" else -1))

                sl_condition_met = (config.direction == "long" and current_market_price <= loss_target_price) or \
                                   (config.direction == "short" and current_market_price >= loss_target_price)
                
                if sl_condition_met:
                    _LOG.info(f"💣 {config.symbol} 손절 조건 충족! 현재가: {current_market_price:.4f}, 손절 목표가: {loss_target_price:.4f}")
                    click.secho(f"💣 {config.symbol} 손절 조건 충족 (현재가: {current_market_price:.4f}). 손절 주문 실행...", fg="red", bold=True)
                    if _execute_order_and_update_state(gate_client, config, current_bot_state, 0, "stop_loss"):
                        if config.stop_bot_after_stop_loss and not current_bot_state.is_in_position: # 봇 중지 설정 및 포지션 청산 시
                            _LOG.info(f"{config.symbol} 손절 후 봇 중지 설정 켜짐. 이 심볼에 대한 전략 종료.")
                            strategy_active_for_this_symbol = False
                        elif not current_bot_state.is_in_position: # 봇 중지 설정은 꺼져있지만 포지션 청산 시
                             _LOG.info(f"{config.symbol} 손절로 포지션 청산됨. 반복 실행 설정 확인 중...")
                             # repeat_after_take_profit이 손절 후 재시작에도 적용될지 여부 결정 필요.
                             # 여기서는 손절 시에는 repeat_after_take_profit과 무관하게 재시작 안 한다고 가정.
                             # 필요시 별도 설정 (예: repeat_after_stop_loss) 추가.
                             if not config.repeat_after_take_profit: # 임시로 이 설정 사용
                                strategy_active_for_this_symbol = False


                    else:
                        _LOG.error(f"{config.symbol} 손절 주문 실행/접수 실패.")
                    if not strategy_active_for_this_symbol: continue


            # --- 4. 분할매수(Split Order / Scale-in) 로직 ---
            # TP/SL이 발생하지 않았고, 아직 최대 분할매수 횟수에 도달하지 않았으며, 포지션 보유 중일 때
            if strategy_active_for_this_symbol and \
               current_bot_state.current_split_order_count < config.max_split_count and \
               current_bot_state.is_in_position and current_bot_state.current_avg_entry_price:
                
                trigger_pct = config.split_trigger_percents[current_bot_state.current_split_order_count]
                # 분할매수 목표 가격 (현재 평균 단가 기준)
                split_target_price = current_bot_state.current_avg_entry_price * (1 + trigger_pct / 100.0)
                
                _LOG.debug(f"{config.symbol} 분할매수 {current_bot_state.current_split_order_count + 1} 조건 확인: "
                           f"현재가={current_market_price:.4f}, 평균단가={current_bot_state.current_avg_entry_price:.4f}, "
                           f"분할매수 목표가={split_target_price:.4f} (트리거 {trigger_pct}%)")

                split_condition_met = (config.direction == "long" and current_market_price <= split_target_price) or \
                                      (config.direction == "short" and current_market_price >= split_target_price)

                if split_condition_met:
                    split_amount_usd = config.split_amounts_usd[current_bot_state.current_split_order_count]
                    _LOG.info(f"💧 {config.symbol} 분할매수 {current_bot_state.current_split_order_count + 1} 조건 충족! "
                              f"현재가: {current_market_price:.4f}, 목표가: {split_target_price:.4f}")
                    click.secho(f"💧 {config.symbol} 분할매수 {current_bot_state.current_split_order_count + 1} 조건 충족. 주문 실행...", fg="cyan")
                    
                    if _execute_order_and_update_state(gate_client, config, current_bot_state, split_amount_usd, "split"):
                        # current_split_order_count는 BotTradingState.update_on_fill 내부에서 증가됨 (split 경우)
                        _LOG.info(f"{config.symbol} 분할매수 {current_bot_state.current_split_order_count}회 성공. "
                                  f"새 평균단가: {current_bot_state.current_avg_entry_price:.4f if current_bot_state.current_avg_entry_price else 'N/A'}")
                        # 분할매수 후 익절/손절 목표가 재계산은 다음 루프에서 show_summary 통해 확인 및 로직 적용
                    else:
                        _LOG.error(f"{config.symbol} 분할매수 {current_bot_state.current_split_order_count + 1} 주문 실행/접수 실패.")
            
            if strategy_active_for_this_symbol: # 익절/손절로 중지되지 않았다면 다음 체크까지 대기
                _LOG.debug(f"'{config.symbol}' 다음 전략 확인까지 {config.check_interval_seconds}초 대기...")
                time.sleep(config.check_interval_seconds)

        except KeyboardInterrupt:
            _LOG.warning("사용자 인터럽트 감지 (Ctrl+C). 봇을 안전하게 종료합니다.")
            click.secho("\n🛑 사용자 요청으로 봇을 종료합니다...", fg="yellow", bold=True)
            # TODO: 미체결 주문(TP/SL 등)이 있다면 취소 시도
            # if current_bot_state.active_take_profit_order_id:
            #     gate_client.cancel_order(current_bot_state.active_take_profit_order_id)
            # if current_bot_state.active_stop_loss_order_id:
            #     gate_client.cancel_order(current_bot_state.active_stop_loss_order_id)
            strategy_active_for_this_symbol = False # 루프 종료
            # break # while 루프 직접 탈출
        except ApiException as e:
            _LOG.error(f"전략 실행 중 Gate.io API 오류 발생 (심볼: {config.symbol}): Status={e.status}, Body='{e.body}'", exc_info=True)
            click.secho(f"API 오류 발생 (심볼: {config.symbol}): {e.status} - {e.reason}. 로그를 확인하세요. 잠시 후 재시도합니다.", fg="red")
            time.sleep(config.check_interval_seconds * 2) # 오류 발생 시 좀 더 길게 대기 후 재시도
        except Exception as e:
            _LOG.error(f"전략 실행 중 예상치 못한 오류 발생 (심볼: {config.symbol}): {e}", exc_info=True)
            click.secho(f"예상치 못한 오류 발생 (심볼: {config.symbol}): {e}. 로그를 확인하세요. 잠시 후 재시도합니다.", fg="red")
            time.sleep(config.check_interval_seconds * 2) # 오류 발생 시 좀 더 길게 대기 후 재시도
    
    _LOG.info(f"'{config.symbol}'에 대한 거래 전략 루프 종료.")


@click.command(context_settings=dict(help_option_names=['-h', '--help']))
@click.option(
    '--config-file', '-c',
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path), # path_type=Path로 변경
    help="JSON 설정 파일 경로. 지정하지 않으면 대화형으로 설정합니다."
)
@click.option(
    '--smoke-test', # 옵션명 변경 (하이픈 사용)
    is_flag=True,
    help="실제 거래 없이 API 연결, 가격 조회, 청산가 계산 등 간단한 테스트를 실행합니다."
)
@click.option(
    '--contract', # smoke-test 시 사용할 계약 심볼
    default="BTC_USDT",
    show_default=True,
    help="--smoke-test 모드에서 사용할 선물 계약 심볼."
)
def main(config_file: Optional[Path], smoke_test: bool, contract: str) -> None:
    """
    Gate.io 선물 자동매매 봇 CLI (명령줄 인터페이스)
    """
    # 로깅은 main.py에서 이미 설정됨
    _LOG.info("="*10 + " 자동매매 봇 CLI 시작 " + "="*10)
    
    gate_client: GateIOClient
    try:
        # GateIOClient 생성 시 API 키 존재 여부 및 연결 테스트 수행
        gate_client = GateIOClient() # .env 파일은 main.py에서 로드됨
    except EnvironmentError as e: # API 키 누락 등 환경 문제
        _LOG.critical(f"GateIOClient 초기화 실패 (환경 오류): {e}")
        click.secho(f"치명적 오류: {e}. .env 파일에 API 키와 시크릿을 올바르게 설정했는지 확인하세요.", fg="red", bold=True)
        sys.exit(1)
    except ApiException as e: # API 연결 또는 인증 실패
        _LOG.critical(f"Gate.io API 연결/인증 실패 (초기화 중): Status={e.status}, Body='{e.body}'", exc_info=True)
        click.secho(f"치명적 오류: Gate.io API에 연결할 수 없습니다. Status: {e.status}, Reason: {e.reason}", fg="red", bold=True)
        click.secho("API 키 권한, 네트워크 연결, Gate.io API 상태를 확인하세요.", fg="red")
        sys.exit(1)
    except Exception as e: # 기타 예외
        _LOG.critical(f"GateIOClient 초기화 중 예상치 못한 오류: {e}", exc_info=True)
        click.secho(f"치명적 오류: 초기화 중 예상치 못한 오류 발생 - {e}", fg="red", bold=True)
        sys.exit(1)


    if smoke_test:
        click.secho(f"\n🕵️ SMOKE TEST 모드 실행 (계약: {contract})...", fg="magenta", bold=True)
        _LOG.info(f"Smoke test 시작 (계약: {contract})")
        try:
            price = gate_client.fetch_last_price(contract)
            if price:
                click.secho(f"  ✅ 현재 시장가 ({contract}): {price:.4f} USDT", fg="green")
                
                # 간단한 청산가 계산 테스트 (기본값 사용)
                dummy_entry_usd = 1000.0
                dummy_leverage = 10
                dummy_mode = "isolated"
                dummy_direction_long: Literal["long", "short"] = "long" # 타입 명시
                
                liq_p, liq_pct = calculate_liquidation_price(
                    total_position_collateral_usd=dummy_entry_usd,
                    leverage=dummy_leverage,
                    margin_mode=dummy_mode,
                    avg_entry_price=price,
                    position_direction=dummy_direction_long
                )
                if liq_p is not None and liq_pct is not None:
                    click.secho(f"  ✅ 예상 청산가 (1000 USD, 10x LONG 기준): "
                                f"~${liq_p:.4f} USDT ({'-' if dummy_direction_long == 'long' else '+'}{abs(liq_pct):.2f}%)", fg="green")
                else:
                    click.secho(f"  ⚠️ {contract} 예상 청산가 계산 실패.", fg="yellow")
            else:
                click.secho(f"  ❌ {contract} 현재가 조회 실패.", fg="red")
            
            acc_info = gate_client.get_account_info()
            if acc_info and acc_info.get('user_id'):
                click.secho(f"  ✅ 계좌 정보 조회 성공 (UserID: {acc_info['user_id']}). API 연결 및 인증 정상.", fg="green")
            else:
                click.secho(f"  ❌ 계좌 정보 조회 실패. API 키 또는 연결 상태를 확인하세요.", fg="red")

        except ApiException as e:
            _LOG.error(f"Smoke test 중 API 오류: {e.body}", exc_info=True)
            click.secho(f"  ❌ Smoke Test API 오류: Status {e.status} - {e.reason}", fg="red")
        except Exception as e:
            _LOG.error(f"Smoke test 중 예상치 못한 오류: {e}", exc_info=True)
            click.secho(f"  ❌ Smoke Test 중 예상치 못한 오류: {e}", fg="red")
        _LOG.info("Smoke test 완료.")
        sys.exit(0) # Smoke test 후 정상 종료

    # --- 설정 로드 또는 프롬프트 ---
    bot_configuration: Optional[BotConfig] = None
    if config_file: # config_file은 Path 객체로 전달됨
        try:
            bot_configuration = BotConfig.load(config_file)
            click.secho(f"\n✅ 설정 파일 로드 성공: {config_file.resolve()}", fg="green")
        except (FileNotFoundError, ValueError, Exception) as e: # 모든 로드 관련 예외 포괄
            _LOG.error(f"설정 파일 '{config_file.resolve()}' 로드 실패: {e}", exc_info=True)
            click.secho(f"❌ 설정 파일 '{config_file.resolve()}' 로드 오류: {e}", fg="red")
            if not click.confirm("대화형 설정으로 계속 진행하시겠습니까?", default=True):
                _LOG.info("사용자가 설정 파일 로드 실패 후 종료 선택.")
                sys.exit(1)
            # bot_configuration은 None으로 유지되어 아래에서 프롬프트 실행
    
    if not bot_configuration: # 설정 파일이 없거나 로드 실패 시 대화형 프롬프트 실행
        _LOG.info("대화형 설정 시작.")
        try:
            bot_configuration = prompt_config()
        except ValueError as e: # BotConfig의 __post_init__에서 발생한 유효성 검사 오류
            _LOG.critical(f"봇 설정 중 유효성 검사 실패: {e}", exc_info=True) # 스택 트레이스 포함
            click.secho(f"봇 설정 실패: {e}. 올바른 파라미터로 다시 시작해주세요.", fg="red", bold=True)
            sys.exit(1)
        except Exception as e: # 기타 예외 (예: click.prompt 내부 오류 등)
             _LOG.critical(f"대화형 설정 중 예상치 못한 오류: {e}", exc_info=True)
             click.secho(f"설정 중 예상치 못한 오류 발생: {e}. 프로그램을 종료합니다.", fg="red", bold=True)
             sys.exit(1)


    # --- 초기 요약 정보 표시 및 실행 확인 ---
    try:
        initial_market_price = gate_client.fetch_last_price(bot_configuration.symbol)
        if initial_market_price is None:
            _LOG.critical(f"{bot_configuration.symbol} 초기 가격 조회 실패. 봇을 시작할 수 없습니다.")
            click.secho(f"❌ {bot_configuration.symbol} 초기 가격 조회 실패. 봇 시작 불가.", fg="red", bold=True)
            sys.exit(1)
        
        # 각 심볼에 대한 BotTradingState 객체 생성
        current_bot_trading_state = BotTradingState(symbol=bot_configuration.symbol)
        show_summary(bot_configuration, initial_market_price, gate_client, current_bot_trading_state)

    except ApiException as e:
        _LOG.critical(f"초기 요약 정보 표시 중 API 오류: {e.body}", exc_info=True)
        click.secho(f"❌ API 오류 발생 (초기 설정 중): Status={e.status}, Reason='{e.reason}'. 봇 시작 불가.", fg="red", bold=True)
        sys.exit(1)
    except Exception as e: # 기타 예외
        _LOG.critical(f"초기 요약 정보 표시 중 예상치 못한 오류: {e}", exc_info=True)
        click.secho(f"❌ 예상치 못한 오류 발생 (초기 설정 중): {e}. 봇 시작 불가.", fg="red", bold=True)
        sys.exit(1)


    if click.confirm("\n❓ 이 설정을 파일로 저장하시겠습니까?", default=False): # 기본값 False로 변경
        default_save_path_str = f"{bot_configuration.symbol.lower()}_{bot_configuration.direction}_config.json"
        save_path_str = click.prompt("설정 저장 경로 입력 (예: my_strategy.json)", default=default_save_path_str)
        try:
            bot_configuration.save(save_path_str)
            # click.secho(f"✅ 설정 저장 완료: {Path(save_path_str).resolve()}", fg="green") # 이미 BotConfig.save에서 로깅함
        except Exception as e: # 저장 실패 시에도 계속 진행할 수 있도록
            _LOG.error(f"설정 파일 저장 실패 ('{save_path_str}'): {e}", exc_info=True)
            click.secho(f"⚠️ 설정 파일 저장 실패: {e}", fg="yellow")


    if click.confirm("\n▶️ 위 설정으로 자동매매를 시작하시겠습니까?", default=True):
        _LOG.info(f"사용자 확인. '{bot_configuration.symbol}'에 대한 자동매매 전략 시작. 설정: {bot_configuration.to_dict()}")
        click.secho(f"🚀 '{bot_configuration.symbol}' 자동매매 시작...", fg="green", bold=True)
        run_strategy(bot_configuration, gate_client, current_bot_trading_state) # 생성된 상태 객체 전달
        click.secho(f"\n🏁 '{bot_configuration.symbol}' 자동매매 전략이 종료되었거나 중지되었습니다.", fg="blue", bold=True)
    else:
        _LOG.info("사용자가 자동매매 시작을 선택하지 않았습니다. 프로그램 종료.")
        click.secho("👋 자동매매가 시작되지 않았습니다. 프로그램을 종료합니다.", fg="yellow")

    _LOG.info("="*10 + " 자동매매 봇 CLI 종료 " + "="*10)

# 이 파일이 직접 실행될 때 (python src/trading_bot/cli.py) click이 알아서 main을 호출함.
# 따라서 if __name__ == '__main__': main() 불필요.
# 패키지 외부에서 python main.py로 실행 시, main.py 내부에서 이 cli.main을 호출.
