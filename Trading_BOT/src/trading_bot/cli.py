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
class BotTradingState:
    """봇의 현재 거래 관련 상태를 관리하는 클래스입니다."""
    def __init__(self, symbol: str):
        self.symbol = symbol # 이 상태가 어떤 심볼에 대한 것인지 명시
        self.current_avg_entry_price: Optional[float] = None
        self.total_position_contracts: float = 0.0  # 계약 수량. 롱은 양수, 숏은 음수.
        self.total_position_initial_usd: float = 0.0 # 포지션 진입에 사용된 총 USD (수수료 제외 추정치)
        self.is_in_position: bool = False # 현재 포지션을 보유하고 있는지 여부
        self.current_split_order_count: int = 0 # 현재까지 성공적으로 실행된 분할매수 횟수
        _LOG.info(f"BotTradingState for {self.symbol} initialized.")

    def reset(self):
        """봇 상태를 초기화합니다 (새로운 거래 사이클 시작 또는 포지션 완전 종료 시)."""
        _LOG.info(f"BotTradingState for {self.symbol} resetting...")
        self.current_avg_entry_price = None
        self.total_position_contracts = 0.0
        self.total_position_initial_usd = 0.0
        self.is_in_position = False
        self.current_split_order_count = 0
        _LOG.info(f"BotTradingState for {self.symbol} reset complete.")

    def update_on_fill(self, filled_contracts: float, fill_price: float, filled_usd_value: float, order_purpose: str):
        """주문 체결(fill)에 따라 포지션 상태를 업데이트합니다."""
        _LOG.info(f"Updating position state for {self.symbol} due to '{order_purpose}' fill: "
                  f"Contracts={filled_contracts:.8f}, Price=${fill_price:.4f}, USDValue=${filled_usd_value:.2f}")

        if not self.is_in_position: # 첫 진입 (entry)
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
                    _LOG.warning(f"{order_purpose.upper()} resulted in partial closure. Remaining: {new_total_contracts:.8f}.")
                    self.reset() # TP/SL은 전체 청산으로 가정하고 상태 리셋
                return

            # 분할 매수 (split)
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

        _LOG.info(f"Position state updated for {self.symbol}: AvgEntryPrice=${self.current_avg_entry_price:.4f if self.current_avg_entry_price else 'N/A'}, "
                  f"TotalContracts={self.total_position_contracts:.8f}, TotalInitialUSD=${self.total_position_initial_usd:.2f}, "
                  f"IsInPosition={self.is_in_position}")

# 이 클래스는 더 이상 사용하지 않고, run_strategy 외부에서 생성하여 전달하는 방식으로 변경
# global bot_state 

def prompt_config(gate_client: GateIOClient) -> Optional[BotConfig]:
    """사용자로부터 대화형으로 봇 설정을 입력받아 BotConfig 객체를 생성합니다."""
    click.secho("\n" + "="*10 + " 📈 비트코인 선물 분할매수 자동매매 봇 설정 " + "="*10, fg="yellow", bold=True)
    
    # 1단계: 기본 설정
    direction = click.prompt("👉 거래 방향 (long/short)", type=click.Choice(["long", "short"]), default="long")
    symbol = click.prompt("👉 거래 대상 코인 (예: BTC_USDT)", default="BTC_USDT").upper().strip()
    leverage = click.prompt("👉 레버리지 (예: 5)", type=int, default=15)
    margin_mode = click.prompt("👉 마진 모드 (cross/isolated)", type=click.Choice(["cross", "isolated"]), default="cross")
    entry_amount_usd = click.prompt("👉 첫 진입 금액 (USDT)", type=float, default=54.0)
    
    # 2단계: 분할매수 설정
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

    # 3단계: 익절/손절 설정
    # 예시에서 '선물 기준', '현물 기준'은 혼동을 줄 수 있으므로, '평균 진입가 대비'로 통일
    take_profit_pct_str = click.prompt("👉 익절 퍼센트 (평균 진입가 대비 %)", type=str, default="6.0")
    take_profit_pct = float(take_profit_pct_str) if take_profit_pct_str.strip() else None
    
    stop_loss_pct_str = click.prompt("👉 손절 퍼센트 (평균 진입가 대비 %)", type=str, default="5.0")
    stop_loss_pct = float(stop_loss_pct_str) if stop_loss_pct_str.strip() else None
    
    order_type = click.prompt("👉 주문 방식을 선택하세요 (market: 시장가 / limit: 지정가)", type=click.Choice(["market", "limit"]), default="market")
    
    # --- 중요: 현재 가격은 API로 자동 조회 ---
    click.echo("🔍 현재 코인 가격을 API로 조회합니다...")
    current_market_price = gate_client.fetch_last_price(symbol)
    if current_market_price is None:
        click.secho(f"❌ '{symbol}'의 현재 가격을 조회할 수 없습니다. 네트워크나 심볼 이름을 확인해주세요.", fg="red", bold=True)
        return None
    click.secho(f"  - 현재 {symbol} 가격: {current_market_price:.4f} USDT", fg="green")
    
    # 4단계: 청산가 계산 및 표시
    total_collateral_for_liq_calc = entry_amount_usd + sum(split_amounts_usd)
    liq_price, change_pct = calculate_liquidation_price(
        total_position_collateral_usd=total_collateral_for_liq_calc,
        leverage=leverage,
        margin_mode=margin_mode,
        avg_entry_price=current_market_price, # 초기 계산은 현재가 기준
        position_direction=direction
    )

    if liq_price is not None and change_pct is not None:
        click.secho(f"\n📊 강제 청산가 계산 완료: {liq_price:.2f} USDT", fg="magenta", bold=True)
        change_direction_text = "하락" if direction == "long" else "상승"
        click.secho(f"💥 강제 청산가까지 {change_direction_text} %: {abs(change_pct):.2f}%", fg="magenta")
    else:
        click.secho("\n⚠️ 강제 청산가를 계산할 수 없습니다 (입력값 확인 필요).", fg="yellow")


    # 5단계: 최종 운영 설정 확인
    click.echo("") # 한 줄 띄우기
    repeat_after_tp = click.confirm("익절 후 반복 실행하시겠습니까? (y/n)", default=True)
    stop_after_sl = click.confirm("손절 후 봇을 정지하시겠습니까? (y/n)", default=False)
    enable_sl = click.confirm("손절 기능을 활성화하시겠습니까? (y/n)", default=True)


    # 설정 객체 생성
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
        # 재귀 호출보다는 None을 반환하여 main 루프에서 다시 시도하도록 하는 것이 더 안정적일 수 있음
        return None 


def show_summary_final(config: BotConfig):
    """최종 설정 요약을 예시와 같은 형식으로 출력합니다."""
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


# ... (이하 run_strategy, _execute_order_and_update_state 등 나머지 함수는 이전 버전과 거의 동일하게 유지)
# ... 단, show_summary는 show_summary_final로 대체될 수 있으며, main 루프에서 호출 방식 변경 필요

# 나머지 함수들은 이전 버전의 코드를 그대로 사용한다고 가정하고, main 함수만 수정합니다.
# 실제로는 `run_strategy`와 `show_summary`도 새로운 프롬프트 흐름에 맞게 일부 조정이 필요할 수 있습니다.
# 여기서는 `main` 함수의 흐름을 예시에 맞게 재구성하는 데 집중합니다.

# 아래는 main 함수의 새로운 버전입니다. 기존 cli.py의 main 함수를 이것으로 교체하세요.

# --- 나머지 함수들은 이전 최종 버전의 코드를 그대로 사용한다고 가정합니다 ---
# _execute_order_and_update_state, run_strategy, show_summary 등...
# show_summary는 여기서는 사용하지 않고, show_summary_final을 사용합니다.
# 기존 show_summary는 실시간 정보를 보여주는 역할이었고,
# show_summary_final은 최종 확인용입니다. 둘 다 목적에 맞게 사용할 수 있습니다.

@click.command(context_settings=dict(help_option_names=['-h', '--help']))
@click.option(
    '--config-file', '-c',
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help="JSON 설정 파일 경로. 지정하면 대화형 설정을 건너뜁니다."
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
    """
    Gate.io 선물 자동매매 봇 CLI (명령줄 인터페이스)
    """
    _LOG.info("="*10 + " 자동매매 봇 CLI 시작 " + "="*10)
    
    gate_client: GateIOClient
    try:
        gate_client = GateIOClient()
    except (EnvironmentError, ApiException, Exception) as e:
        _LOG.critical(f"GateIOClient 초기화 실패: {e}", exc_info=True)
        click.secho(f"❌ 치명적 오류: 봇 초기화에 실패했습니다. 로그를 확인해주세요.", fg="red", bold=True)
        sys.exit(1)

    if smoke_test:
        # smoke_test 로직은 이전과 동일하게 유지
        click.secho(f"\n🕵️ SMOKE TEST 모드 실행 (계약: {contract})...", fg="magenta", bold=True)
        # ... (이전 smoke_test 코드)
        sys.exit(0)

    # --- 설정 로드 또는 프롬프트 ---
    bot_configuration: Optional[BotConfig] = None
    if config_file:
        try:
            bot_configuration = BotConfig.load(config_file)
            click.secho(f"\n✅ 설정 파일 로드 성공: {config_file.resolve()}", fg="green")
        except Exception as e:
            _LOG.error(f"설정 파일 '{config_file.resolve()}' 로드 실패: {e}", exc_info=True)
            click.secho(f"❌ 설정 파일 로드 오류: {e}", fg="red")
            if not click.confirm("대화형 설정으로 계속 진행하시겠습니까?", default=True):
                sys.exit(1)
            bot_configuration = None
    
    if not bot_configuration:
        # 대화형 설정 루프 (유효한 설정이 입력될 때까지)
        while bot_configuration is None:
            bot_configuration = prompt_config(gate_client)
            if bot_configuration is None:
                if not click.confirm("\n설정 중 오류가 발생했습니다. 다시 시도하시겠습니까?", default=True):
                    _LOG.info("사용자가 설정 재시도를 원치 않아 종료합니다.")
                    sys.exit(0)

    # --- 최종 요약 정보 표시 및 실행 확인 ---
    show_summary_final(bot_configuration)

    if click.confirm("\n❓ 이 설정을 파일로 저장하시겠습니까?", default=False):
        default_save_path = f"{bot_configuration.symbol.lower()}_{bot_configuration.direction}_config.json"
        save_path = click.prompt("설정 저장 경로 입력", default=default_save_path)
        try:
            bot_configuration.save(save_path)
        except Exception as e:
            _LOG.error(f"설정 파일 저장 실패 ('{save_path}'): {e}", exc_info=True)
            click.secho(f"⚠️ 설정 파일 저장 실패: {e}", fg="yellow")

    if click.confirm("\n▶️ 위 설정으로 자동매매를 시작하시겠습니까?", default=True):
        _LOG.info(f"사용자 확인. '{bot_configuration.symbol}' 자동매매 시작.")
        click.secho(f"🚀 '{bot_configuration.symbol}' 자동매매 시작...", fg="green", bold=True)
        
        # 각 심볼에 대한 BotTradingState 객체 생성 및 전략 실행
        current_bot_trading_state = BotTradingState(symbol=bot_configuration.symbol)
        # run_strategy(bot_configuration, gate_client, current_bot_trading_state) # 실제 전략 실행 (주석 처리)
        click.echo("... (실제 run_strategy 함수 호출 부분) ...") # 테스트용 출력
        click.secho(f"\n🏁 '{bot_configuration.symbol}' 자동매매 전략이 종료되었거나 중지되었습니다.", fg="blue", bold=True)
    else:
        _LOG.info("사용자가 자동매매 시작을 선택하지 않았습니다.")
        click.secho("👋 자동매매가 시작되지 않았습니다. 프로그램을 종료합니다.", fg="yellow")

    _LOG.info("="*10 + " 자동매매 봇 CLI 종료 " + "="*10)


# 이 파일이 직접 실행될 때 click이 main 함수를 호출하게 됩니다.
# `if __name__ == '__main__':` 블록은 click command 사용 시 필요하지 않습니다.
