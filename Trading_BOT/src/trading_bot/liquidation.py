# src/trading_bot/liquidation.py
"""강제 청산가 계산 관련 유틸리티."""
import logging
from typing import Optional, Tuple, Literal # Literal 임포트 추가

_LOG = logging.getLogger(__name__)

def calculate_liquidation_price(
    total_position_collateral_usd: float, # 현재 포지션에 사용된 총 증거금 (USD)
    leverage: int,
    margin_mode: Literal["cross", "isolated"],
    avg_entry_price: float, # 현재 포지션의 평균 진입 가격
    position_direction: Literal["long", "short"],
    # Gate.io의 경우, 유지 증거금률(MMR)은 티어별로 다르고, 심볼마다 다를 수 있음.
    # 일반적으로 BTC/ETH 등 주요 코인은 낮은 MMR (예: 0.004 또는 0.005)을 가짐.
    # 정확한 계산을 위해서는 API에서 MMR을 가져오거나, Gate.io 문서를 참조해야 함.
    # 여기서는 일반적인 값을 기본으로 사용.
    maintenance_margin_rate: float = 0.005 # 기본값으로 0.5% 가정 (BTC 기준, 확인 필요)
) -> Tuple[Optional[float], Optional[float]]:
    """
    예상 강제 청산가 및 해당 가격 도달 시 변동률(%)을 반환합니다.

    이 함수는 일반적인 선물 청산 공식을 단순화하여 사용하며, 실제 거래소의
    정확한 청산 메커니즘(펀딩비, 보험 기금, 계층적 유지 증거금률 등)과는
    차이가 있을 수 있습니다. 특히 교차 마진의 경우 사용 가능한 전체 계좌 잔고에 따라 달라지므로,
    여기서 계산되는 교차 마진 청산가는 매우 개략적인 추정치입니다.

    Args:
        total_position_collateral_usd (float): 현재 포지션에 투입된 총 증거금 (USD).
                                              (초기 진입금액 + 모든 분할매수 금액의 합)
        leverage (int): 포지션 레버리지.
        margin_mode (str): "cross" 또는 "isolated".
        avg_entry_price (float): 현재 포지션의 평균 진입 가격.
        position_direction (str): "long" 또는 "short".
        maintenance_margin_rate (float): 유지 증거금률 (예: 0.005는 0.5%).

    Returns:
        Tuple[Optional[float], Optional[float]]: (예상 청산가, 예상 변동률), 계산 불가 시 (None, None).
    """
    _LOG.debug(
        f"청산가 계산 시작: 총증거금USD={total_position_collateral_usd:.2f}, 레버리지={leverage}x, "
        f"마진모드='{margin_mode}', 평균진입가=${avg_entry_price:.4f}, 방향='{position_direction}', MMR={maintenance_margin_rate*100:.2f}%"
    )

    if leverage <= 0:
        _LOG.error("레버리지는 0보다 커야 청산가 계산이 가능합니다.")
        return None, None
    if avg_entry_price <= 0:
        _LOG.error("평균 진입가는 0보다 커야 청산가 계산이 가능합니다.")
        return None, None
    if total_position_collateral_usd <= 0:
        _LOG.warning("총 포지션 증거금이 0 이하이므로 청산가를 계산할 수 없습니다.")
        return None, None
    if not (0 < maintenance_margin_rate < 1):
        _LOG.error(f"유지 증거금률(MMR)은 0과 1 사이의 값이어야 합니다. 입력값: {maintenance_margin_rate}")
        return None, None


    liq_price: Optional[float] = None

    # 청산 공식에서 사용될 핵심 비율: (1/Leverage - MaintenanceMarginRate)
    # 이 값이 음수 또는 0에 가까우면 청산이 발생하지 않거나 공식이 다르게 적용될 수 있음.
    # (즉, 유지 증거금률이 초기 증거금률보다 크거나 같은 경우)
    effective_margin_ratio_change = (1.0 / leverage) - maintenance_margin_rate
    
    if effective_margin_ratio_change <= 1e-9: # 거의 0 또는 음수
        _LOG.warning(f"레버리지({leverage}x)가 너무 낮거나 유지증거금률({maintenance_margin_rate*100:.2f}%)이 너무 높아 "
                     f"일반적인 청산 공식 적용이 어렵습니다 (1/L <= MMR). 청산이 거의 발생하지 않거나 다른 메커니즘이 적용될 수 있습니다.")
        return None, None # 청산가 계산 불가로 처리

    if margin_mode == "isolated":
        # 격리 마진 청산가 공식 (일반적인 형태):
        # 롱 포지션: P_liq = P_entry * (1 - (1/L - MMR))
        # 숏 포지션: P_liq = P_entry * (1 + (1/L - MMR))
        # 여기서 (1/L - MMR)은 effective_margin_ratio_change 와 동일.

        if position_direction == "long":
            liq_price = avg_entry_price * (1.0 - effective_margin_ratio_change)
        elif position_direction == "short":
            liq_price = avg_entry_price * (1.0 + effective_margin_ratio_change)
        else:
            _LOG.error(f"알 수 없는 포지션 방향: '{position_direction}'")
            return None, None

    elif margin_mode == "cross":
        _LOG.warning("교차 마진 청산가 계산은 단순 추정이며, 실제와 큰 차이가 있을 수 있습니다. "
                     "정확한 계산은 사용 가능한 전체 계좌 잔고와 다른 포지션 정보가 필요합니다.")
        # 교차 마진도 격리와 유사한 공식을 사용하되, 이는 매우 부정확함을 인지.
        if position_direction == "long":
            liq_price = avg_entry_price * (1.0 - effective_margin_ratio_change) # 단순 추정
        elif position_direction == "short":
            liq_price = avg_entry_price * (1.0 + effective_margin_ratio_change) # 단순 추정
        else:
            _LOG.error(f"알 수 없는 포지션 방향: '{position_direction}'")
            return None, None
    else:
        _LOG.error(f"지원되지 않는 마진 모드: '{margin_mode}'")
        return None, None

    if liq_price is not None and liq_price < 0:
        _LOG.warning(f"계산된 청산가가 음수입니다 ({liq_price:.4f}). 실제 청산은 0 또는 최소 가격 단위에서 발생할 수 있습니다. 청산가를 0.0으로 조정합니다.")
        liq_price = 0.0  # 청산가는 음수가 될 수 없음

    # 청산가 도달 시 가격 변동률 계산
    change_pct: Optional[float] = None
    if liq_price is not None and avg_entry_price > 1e-9 : # avg_entry_price가 0에 가까우면 변동률 계산 불가/무의미
        if position_direction == "long": # 가격 하락 시 청산
            change_pct = ((avg_entry_price - liq_price) / avg_entry_price) * 100.0
        else: # short, 가격 상승 시 청산
            change_pct = ((liq_price - avg_entry_price) / avg_entry_price) * 100.0
    
    if liq_price is not None and change_pct is not None:
        _LOG.info(f"계산된 예상 청산가: {liq_price:.4f} USDT, 예상 변동률: {change_pct:.2f}% "
                  f"(방향: {position_direction}, 평균진입가: {avg_entry_price:.4f}, 레버리지: {leverage}x, 모드: {margin_mode})")
    else:
        _LOG.warning("청산가 또는 변동률 계산에 실패했습니다.")
        return None, None # 둘 중 하나라도 None이면 실패로 간주
    
    return liq_price, change_pct
