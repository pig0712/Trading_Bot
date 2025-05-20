"""강제 청산가 계산기."""

from typing import Sequence, Tuple


def calculate_liquidation_price(                 # noqa: D401
    entry_amount: float,
    split_amounts: Sequence[float],
    leverage: int,
    margin_mode: str,
    market_price: float,
) -> Tuple[float, float]:
    """청산가 & 하락률(%) 반환."""
    if leverage <= 0:
        raise ValueError("leverage must be > 0")

    total_invested = entry_amount + sum(split_amounts)
    if total_invested == 0:
        raise ValueError("total position size must be > 0")

    position_size = total_invested * leverage

    if margin_mode == "cross":
        liq_price = market_price * (1 - (total_invested / position_size)) / leverage
    elif margin_mode == "isolated":
        liq_price = market_price * (1 - (total_invested / position_size))
    else:
        raise ValueError("margin_mode must be 'cross' or 'isolated'")

    drop_pct = ((market_price - liq_price) / market_price) * 100
    return liq_price, drop_pct
