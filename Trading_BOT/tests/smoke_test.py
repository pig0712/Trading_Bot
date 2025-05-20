# tests/smoke_test.py
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # …/Trading_BOT
SRC  = ROOT / "src"
sys.path.insert(0, str(SRC))

from trading_bot.exchange_gateio import GateIOClient
from trading_bot.config import BotConfig
from trading_bot.liquidation import calculate_liquidation_price

gate = GateIOClient()
gate.fetch_last_price = lambda _sym: 50_000.0
gate.place_order      = lambda *a, **k: {"id": 1, "status": "ok"}

cfg = BotConfig(
    direction="long",
    symbol="BTC_USDT",
    leverage=5,
    margin_mode="cross",
    entry_amount=100,
    split_trigger_percents=[-1, -2],
    split_amounts=[50, 50],
    take_profit_pct=5,
    stop_loss_pct=3,
    order_type="market",
    max_split_count=2,
)

price = gate.fetch_last_price(cfg.symbol)
liq, drop = calculate_liquidation_price(
    cfg.entry_amount, cfg.split_amounts, cfg.leverage, cfg.margin_mode, price
)
order = gate.place_order(cfg.symbol, 1, cfg.direction, price=None, leverage=cfg.leverage)

print("price:", price)
print("liq  :", liq, f"({drop:.2f}%)")
print("order:", order)
