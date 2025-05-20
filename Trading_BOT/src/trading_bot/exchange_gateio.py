# src/trading_bot/exchange_gateio.py
import os
import time
import logging
from pathlib import Path
from typing import Dict, Any, Literal, Optional

from dotenv import load_dotenv
from gate_api import Configuration, ApiClient, FuturesApi, ApiException

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

_BASE = (
    "https://api.gateio.ws"
    if os.getenv("GATE_ENV", "live") == "live"
    else "https://fx-api-testnet.gateio.ws"
)

_cfg = Configuration(
    host=_BASE,
    key=os.getenv("GATE_API_KEY"),
    secret=os.getenv("GATE_API_SECRET"),
)
_client = ApiClient(_cfg)
_futures = FuturesApi(_client)

_LOG = logging.getLogger(__name__)


class GateIOClient:
    def __init__(self, settle: str = "usdt") -> None:
        self.settle = settle

    # ── REST 주문 ───────────────────────────────────────────────
    def place_order(
        self,
        contract: str,
        size: int,
        side: Literal["long", "short"],
        price: Optional[float] = None,
        tif: str = "gtc",
        reduce_only: bool = False,
        leverage: int = 20,
    ) -> Dict[str, Any]:
        order = {
            "contract": contract,
            "size": size if side == "long" else -size,
            "price": "0" if price is None else str(price),
            "tif": tif,
            "reduce_only": reduce_only,
            "text": "bot",
            "leverage": str(leverage),
        }
        try:
            return _futures.create_futures_order(self.settle, order)
        except ApiException as e:
            _LOG.error("Gate.io order error: %s", e.body)
            raise

    # ── 포지션/잔고 ──────────────────────────────────────────────
    def get_position(self, contract: str) -> Dict[str, Any]:
        return _futures.get_position(self.settle, contract)

    def get_account(self) -> Dict[str, Any]:
        return _futures.get_futures_accounts(self.settle)

    # ── 현재가 ───────────────────────────────────────────────────
    def fetch_last_price(self, contract: str) -> float:
        tick = _futures.list_futures_tickers(self.settle, contract=contract)[0]
        return float(tick.last)
