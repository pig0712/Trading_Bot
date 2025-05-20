#!/usr/bin/env python
"""
Trading_BOT/main.py
────────────────────────────────────────────────────────────────────────────
프로젝트 루트에서 바로 실행할 수 있는 런처입니다.

• src 경로를 PYTHONPATH에 자동 추가  
• .env 로드 (Gate.io 키 / 환경)  
• 기본 모드: 인터랙티브 CLI  
• --smoke 플래그: 네트워크 실가격으로 간단 자가-진단

$ python main.py                # CLI 실행
$ python main.py --smoke        # 연결·계산 빠른 진단
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv


# ──────────────────────────────────────────────────────────────────────────
# 초기화
# ──────────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent       # …/Trading_BOT
SRC = ROOT / "src"                           # …/Trading_BOT/src
load_dotenv(ROOT / ".env")                   # 환경변수 주입
sys.path.insert(0, str(SRC))                 # import trading_bot.* 가능


# ──────────────────────────────────────────────────────────────────────────
# util
# ──────────────────────────────────────────────────────────────────────────
def _smoke(contract: str = "BTC_USDT") -> None:
    """Gate.io 실시간 가격 호출 → 청산가 계산 → 모의 주문 1건."""
    from trading_bot.exchange_gateio import GateIOClient
    from trading_bot.config import BotConfig
    from trading_bot.liquidation import calculate_liquidation_price

    gate = GateIOClient()
    price = gate.fetch_last_price(contract)

    cfg = BotConfig(
        direction="long",
        symbol=contract,
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

    liq, drop = calculate_liquidation_price(
        cfg.entry_amount, cfg.split_amounts, cfg.leverage, cfg.margin_mode, price
    )
    order = gate.place_order(contract, size=1, side="long", price=None, leverage=5)

    print("💡 Smoke-test result")
    print(f"  spot price   : {price:,.2f} USDT")
    print(f"  liq price    : {liq:,.2f} USDT (↓{drop:.2f}%)")
    print(f"  mock order   : {order}")


def _run_cli() -> None:
    from trading_bot.cli import main as cli_main

    cli_main()


# ──────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────
def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Trading_BOT launcher")
    p.add_argument(
        "--smoke", action="store_true", help="연결·계산 간단 자가-진단 후 종료"
    )
    p.add_argument(
        "--contract",
        default="BTC_USDT",
        help="--smoke 모드에서 사용할 선물 계약명",
    )
    return p.parse_args()


def main() -> None:
    args = _parse()
    if args.smoke:
        _smoke(args.contract)
    else:
        _run_cli()


if __name__ == "__main__":
    main()
