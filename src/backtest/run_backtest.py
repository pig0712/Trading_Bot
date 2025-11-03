# src/backtest/run_backtest.py
# Single-config backtest runner:
# - Loads one CSV and one YAML config
# - Runs engine (C-accelerated if available)
# - Appends result to history CSV
# - Prints benchmark move, current result, and historical Top-5 (with config path)
from __future__ import annotations

import sys
import os
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import yaml
from tqdm.auto import tqdm

# --- import path guard (allow `from backtest.engine import BacktestEngine`) ---
ROOT = Path(__file__).resolve().parents[2]  # project root
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
# ------------------------------------------------------------------------------

from backtest.engine import BacktestEngine


# ===================== Defaults =====================
CSV_PATH_DEFAULT = "src/data/raw/BTCUSDT_2511031344.csv"
CONFIG_PATH_DEFAULT = "configs/strategy/ma_cross.yaml"
RESULTS_CSV_DEFAULT = "reports/backtests/results.csv"
TOP_K_DEFAULT = 5
# ====================================================


def ensure_parent(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)


def pretty_pct(x: float, digits: int = 2) -> str:
    return f"{x * 100:.{digits}f}%"


def pretty_money(x: float, digits: int = 2) -> str:
    return f"${x:,.{digits}f}"


def hrule(width: int = 110):
    print("-" * width); sys.stdout.flush()


def draw_header(title: str, width: int = 110):
    print("\n" + "=" * width)
    print(title.center(width))
    print("=" * width)
    sys.stdout.flush()


def load_csv_with_progress(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    size_mb = os.path.getsize(path) / (1024 * 1024)
    draw_header("1) Loading CSV")
    print(f"Path : {path}")
    print(f"Size : {size_mb:.2f} MB")
    t0 = time.time()
    df = pd.read_csv(path)
    pbar = tqdm(total=3, desc="Preparing", ncols=92, leave=False)
    df["ts"] = pd.to_datetime(df["ts"], utc=True); pbar.update(1)
    df = df.sort_values("ts"); pbar.update(1)
    df = df.drop_duplicates(subset=["ts"]).reset_index(drop=True); pbar.update(1)
    pbar.close()
    print(f"Rows : {len(df):,}")
    print(f"Prep : {time.time() - t0:.2f}s"); sys.stdout.flush()
    return df


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    draw_header("2) Loading Config")
    print(f"Config : {path}")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    strat = (cfg.get("strategy") or {})
    fees = (cfg.get("fees") or {})
    acct = (cfg.get("account") or {})
    print(f"Strategy: {strat.get('name','ma_cross')}  "
          f"(fast={strat.get('fast',9)}, slow={strat.get('slow',21)}, "
          f"tp={strat.get('take_profit')}, sl={strat.get('stop_loss')})")
    print(f"Fees    : taker={fees.get('taker_fee_rate',0.0004)}  slip_bps={fees.get('slippage_bps',1.0)}")
    print(f"Account : initial_cash={acct.get('initial_cash',10000)}")
    sys.stdout.flush()
    return cfg


def append_and_rank(row: dict, results_csv: str, top_k: int = 5) -> pd.DataFrame:
    ensure_parent(results_csv)
    if os.path.exists(results_csv):
        hist = pd.read_csv(results_csv)
    else:
        hist = pd.DataFrame(columns=list(row.keys()))
    hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
    hist.to_csv(results_csv, index=False)
    ranked = hist.sort_values("total_return", ascending=False).reset_index(drop=True)
    return ranked.head(top_k)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=CSV_PATH_DEFAULT, help="1-minute OHLCV CSV path")
    ap.add_argument("--config", default=CONFIG_PATH_DEFAULT, help="YAML strategy config path")
    ap.add_argument("--results", default=RESULTS_CSV_DEFAULT, help="History results CSV")
    ap.add_argument("--topk", type=int, default=TOP_K_DEFAULT, help="Top-K to display from history")
    ap.add_argument("--no-bar", action="store_true", help="Disable progress bars")
    args = ap.parse_args()

    # Step 1: Load data
    df = load_csv_with_progress(args.csv)

    # Step 2: Load config
    cfg = load_config(args.config)

    # Step 3: Run backtest (single run)
    draw_header("3) Backtest Running")
    engine = BacktestEngine(df, cfg, show_progress=("bar" if not args.no_bar else False))
    res = engine.run()

    # Step 4: Save & Rank
    draw_header("4) Save & Rank")
    now_kst = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    cfg_name = (cfg.get("strategy") or {}).get("name", Path(args.config).stem)

    row = {
        "datetime": now_kst,
        "config_path": args.config,
        "config_name": cfg_name,
        "start_ts": res.start_ts.isoformat(),
        "end_ts": res.end_ts.isoformat(),
        "initial_cash": res.summary.get("initial_cash", 10000.0),
        "final_equity": res.final_equity,
        "total_return": res.total_return,
        "cagr": res.cagr,
        "max_drawdown": res.max_drawdown,
        "n_trades": res.n_trades,
        "win_rate": res.win_rate,
        "benchmark_return": res.summary.get("benchmark_return", 0.0),
        "engine": res.summary.get("engine", "python"),
        "elapsed_sec": res.summary.get("elapsed_sec", None),
    }
    topk = append_and_rank(row, args.results, args.topk)
    print(f"Saved results → {args.results}"); sys.stdout.flush()

    # Step 5: Nice summaries
    draw_header("5) Benchmark (Start ~ End)")
    print(f"Start    : {res.start_ts.isoformat()}")
    print(f"End      : {res.end_ts.isoformat()}")
    print(f"Benchmark: {pretty_pct(row['benchmark_return'])}")

    draw_header("6) Current Test Result")
    print(f"Config Name  : {row['config_name']}")
    print(f"Config Path  : {row['config_path']}")
    print(f"Initial Cash : {pretty_money(row['initial_cash'])}")
    print(f"Final Equity : {pretty_money(row['final_equity'])}")
    print(f"Total Return : {pretty_pct(row['total_return'])}")
    print(f"CAGR         : {pretty_pct(row['cagr'])}")
    print(f"Max Drawdown : {pretty_pct(row['max_drawdown'])}")
    print(f"Win Rate     : {pretty_pct(row['win_rate'])}")
    print(f"Trades       : {int(row['n_trades'])}")
    if row.get("elapsed_sec") is not None:
        print(f"Elapsed Sec  : {row['elapsed_sec']:.2f}s")
    print(f"Engine       : {row['engine']}")
    sys.stdout.flush()

    # Step 6: Historical Top-K (show config path, too)
    draw_header(f"7) Historical Top {args.topk} (by Total Return)")
    if len(topk) == 0:
        print("No historical runs recorded.")
    else:
        # columns: Rank | When | ConfigName | ConfigPath | Return | CAGR | MDD | WinR | Trades | Equity
        print(f"{'Rank':<6}{'When':<24}{'ConfigName':<18}{'ConfigPath':<36}"
              f"{'Return':>10}{'CAGR':>10}{'MDD':>10}{'WinR':>8}{'Trades':>8}{'Equity':>14}")
        hrule(136)
        for i, r in topk.reset_index(drop=True).iterrows():
            when = str(r.get('datetime',''))[:24]
            cfgname = str(r.get('config_name',''))[:18]
            cfgpath = str(r.get('config_path',''))[:36]
            print(f"{i+1:<6}{when:<24}"
                  f"{cfgname:<18}{cfgpath:<36}"
                  f"{float(r['total_return'])*100:>9.2f}%"
                  f"{float(r['cagr'])*100:>9.2f}%"
                  f"{float(r['max_drawdown'])*100:>9.2f}%"
                  f"{float(r.get('win_rate',0.0))*100:>7.2f}%"
                  f"{int(r['n_trades']):>8d}"
                  f"{float(r['final_equity']):>14.2f}")
    sys.stdout.flush()

    draw_header("Done")


if __name__ == "__main__":
    main()
