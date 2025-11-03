# src/trading_bot/native.py
import numpy as np

try:
    import btcore  # C extension
    _HAS_NATIVE = True
except Exception:
    btcore = None
    _HAS_NATIVE = False

def has_native() -> bool:
    return _HAS_NATIVE

def ma_cross_backtest_np(prices: np.ndarray, fast: int, slow: int,
                         fee_rate: float, slip_bps: float,
                         take_profit: float = -1.0, stop_loss: float = -1.0) -> dict:
    if not _HAS_NATIVE:
        raise RuntimeError("btcore not available (build C extension first)")
    if prices.dtype != np.float64:
        prices = prices.astype(np.float64, copy=False)
    return btcore.ma_cross_backtest(
        prices, fast, slow, fee_rate, slip_bps, take_profit, stop_loss
    )
