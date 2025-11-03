# src/backtest/engine.py
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

import numpy as np
import pandas as pd
from tqdm.auto import tqdm

# ====== 선택적 C 가속 모듈 ======
try:
    import btcore  # C extension built in src/native_ext
    _HAS_NATIVE = True
except Exception:
    btcore = None
    _HAS_NATIVE = False


@dataclass
class BacktestResult:
    start_ts: pd.Timestamp
    end_ts: pd.Timestamp
    final_equity: float
    total_return: float
    cagr: float
    max_drawdown: float
    n_trades: int
    win_rate: float
    equity_series: Optional[pd.Series]  # may be None when using C fast path
    trades: Optional[List[Dict[str, Any]]]
    summary: Dict[str, Any]


class BacktestEngine:
    """
    Minimal backtester with optional C-accelerated core (btcore).
    Supports: MA cross long-only, taker fee, slippage (bps), TP/SL, benchmark, tqdm progress.
    """

    def __init__(self, df: pd.DataFrame, config: Dict[str, Any], show_progress: bool | str = False):
        # ---- data ----
        req_cols = ["ts", "open", "high", "low", "close", "volume"]
        for c in req_cols:
            if c not in df.columns:
                raise ValueError(f"Data is missing column: {c}")
        self.df = df.copy()
        if not pd.api.types.is_datetime64tz_dtype(self.df["ts"]):
            self.df["ts"] = pd.to_datetime(self.df["ts"], utc=True, errors="coerce")
        self.df = self.df.sort_values("ts").dropna(subset=["ts", "close"]).reset_index(drop=True)

        # ---- config ----
        self.cfg = config or {}
        strat = self.cfg.get("strategy", {})
        self.strategy_name = (strat.get("name") or "ma_cross").lower()

        # params for MA cross
        self.params = {
            "fast": int(strat.get("fast", 9)),
            "slow": int(strat.get("slow", 21)),
            "take_profit": strat.get("take_profit", None),  # e.g., 0.03 → +3%
            "stop_loss": strat.get("stop_loss", None),      # e.g., 0.01 → -1%
        }

        # fees / slippage
        fees = self.cfg.get("fees", {})
        self.taker_fee_rate = float(fees.get("taker_fee_rate", 0.0004))  # 4 bps
        self.slippage_bps   = float(fees.get("slippage_bps", 1.0))       # 1 bp

        # portfolio
        acct = self.cfg.get("account", {})
        self.initial_cash = float(acct.get("initial_cash", 10_000.0))

        # progress
        self.show_progress = show_progress  # False | True | "bar"

        # outputs (filled in run)
        self._res: Optional[BacktestResult] = None

    # ------------------ public API ------------------

    def run(self) -> BacktestResult:
        """
        Run single backtest using either C-accelerated core (if available & strategy supported)
        or Python fallback implementation.
        """
        if _HAS_NATIVE and self.strategy_name == "ma_cross":
            return self._run_native_ma_cross()
        else:
            return self._run_python_ma_cross()

    # ------------------ helpers ------------------

    def _calc_benchmark(self) -> float:
        close = self.df["close"].to_numpy(dtype=np.float64, copy=False)
        if len(close) < 2 or close[0] <= 0:
            return 0.0
        return (close[-1] / close[0]) - 1.0

    def _calc_cagr(self, tot_ret: float, start: pd.Timestamp, end: pd.Timestamp) -> float:
        # CAGR = (1+R)^(years) - 1
        dur_days = max(1e-9, (end - start).total_seconds() / 86400.0)
        years = dur_days / 365.0
        try:
            return (1.0 + tot_ret) ** (1.0 / years) - 1.0
        except Exception:
            return 0.0

    # ------------------ native path ------------------

    def _run_native_ma_cross(self) -> BacktestResult:
        """
        Use btcore.ma_cross_backtest on close prices.
        """
        t0 = time.time()
        close = self.df["close"].to_numpy(dtype=np.float64, copy=False)
        fast = int(self.params.get("fast", 9))
        slow = int(self.params.get("slow", 21))
        tp = float(self.params["take_profit"]) if self.params["take_profit"] is not None else -1.0
        sl = float(self.params["stop_loss"]) if self.params["stop_loss"] is not None else -1.0

        # tqdm: 원시 루프는 C에서 돌아가므로 여기선 파일 로딩/준비만 표시
        if self.show_progress == "bar":
            pbar = tqdm(total=1, desc="Native Core (MA Cross)", ncols=92, leave=False)
        else:
            pbar = None

        out = btcore.ma_cross_backtest(
            close,
            fast,
            slow,
            float(self.taker_fee_rate),
            float(self.slippage_bps),
            float(tp),
            float(sl),
        )

        if pbar:
            pbar.update(1)
            pbar.close()

        start_ts = pd.to_datetime(self.df["ts"].iloc[0], utc=True)
        end_ts   = pd.to_datetime(self.df["ts"].iloc[-1], utc=True)

        final_equity = float(out["final_equity"])
        total_return = float(out["total_return"])
        max_dd       = float(out["max_drawdown"])
        n_trades     = int(out["n_trades"])
        win_rate     = float(out["win_rate"])

        cagr = self._calc_cagr(total_return, start_ts, end_ts)
        bench = self._calc_benchmark()

        self._res = BacktestResult(
            start_ts=start_ts,
            end_ts=end_ts,
            final_equity=final_equity,
            total_return=total_return,
            cagr=cagr,
            max_drawdown=max_dd,
            n_trades=n_trades,
            win_rate=win_rate,
            equity_series=None,     # native 버전은 요약지표만
            trades=None,
            summary={
                "initial_cash": self.initial_cash,
                "benchmark_return": bench,
                "elapsed_sec": time.time() - t0,
                "engine": "native",
            },
        )
        return self._res

    # ------------------ python fallback ------------------

    def _run_python_ma_cross(self) -> BacktestResult:
        """
        Pure-Python MA cross long-only with fee/slippage/TP/SL.
        Uses pandas rolling means. Progress shown via tqdm.
        """
        t0 = time.time()
        df = self.df

        fast = int(self.params.get("fast", 9))
        slow = int(self.params.get("slow", 21))
        tp = self.params.get("take_profit", None)
        sl = self.params.get("stop_loss", None)

        # 지표 계산(여기서가 상대적으로 무거울 수 있음)
        if self.show_progress == "bar":
            pbar_prep = tqdm(total=3, desc="Prepare (indicators)", ncols=92, leave=False)
        else:
            pbar_prep = None

        s_fast = df["close"].rolling(window=fast, min_periods=fast).mean()
        if pbar_prep: pbar_prep.update(1)
        s_slow = df["close"].rolling(window=slow, min_periods=slow).mean()
        if pbar_prep: pbar_prep.update(1)

        # 시그널: 교차 (직전 <=, 현재 >) → 롱 진입 / (직전 >=, 현재 <) → 롱 청산
        # 첫 캔들은 의미 없으니 1부터 시작
        if pbar_prep: pbar_prep.update(1); pbar_prep.close()

        cash = self.initial_cash
        pos_qty = 0.0
        entry_price = np.nan
        peak = cash
        max_dd = 0.0
        n_trades = 0
        wins = 0

        equity_series = np.empty(len(df), dtype=np.float64)
        equity_series[:] = np.nan

        trades: List[Dict[str, Any]] = []

        iterator = range(1, len(df))
        if self.show_progress == "bar":
            iterator = tqdm(iterator, desc="Backtest (py)", ncols=92, leave=False)

        # 진행바 가벼운 postfix 업데이트(1000캔들마다)
        last_update = 0

        for i in iterator:
            price = float(df.at[i, "close"])
            prev_fast, prev_slow = s_fast.iat[i - 1], s_slow.iat[i - 1]
            cur_fast, cur_slow   = s_fast.iat[i], s_slow.iat[i]

            # 포지션 보유 시 TP/SL 확인
            if pos_qty > 0.0 and not math.isnan(entry_price):
                pnl = (price - entry_price) / entry_price
                hit_tp = (tp is not None) and (pnl >= float(tp))
                hit_sl = (sl is not None) and (pnl <= -float(sl))
                if hit_tp or hit_sl:
                    p_fill = price * (1.0 - self.slippage_bps / 10000.0)
                    proceeds = pos_qty * p_fill
                    fee = proceeds * self.taker_fee_rate
                    cash += (proceeds - fee)
                    wins += int(p_fill > entry_price)
                    n_trades += 1
                    trades.append({
                        "i": i, "side": "sell", "px": p_fill, "qty": pos_qty,
                        "reason": "tp" if hit_tp else "sl"
                    })
                    pos_qty = 0.0
                    entry_price = np.nan

            # 시그널 교차
            long_entry = (
                not np.isnan(cur_fast) and not np.isnan(cur_slow)
                and not np.isnan(prev_fast) and not np.isnan(prev_slow)
                and (prev_fast <= prev_slow) and (cur_fast > cur_slow)
            )
            long_exit = (
                not np.isnan(cur_fast) and not np.isnan(cur_slow)
                and not np.isnan(prev_fast) and not np.isnan(prev_slow)
                and (prev_fast >= prev_slow) and (cur_fast < cur_slow)
            )

            if long_entry and pos_qty <= 0.0:
                p_fill = price * (1.0 + self.slippage_bps / 10000.0)
                denom = p_fill * (1.0 + self.taker_fee_rate)
                if denom > 0.0:
                    qty = cash / denom
                    if qty > 0:
                        cost = qty * p_fill
                        fee  = cost * self.taker_fee_rate
                        cash -= (cost + fee)
                        pos_qty = qty
                        entry_price = p_fill
                        trades.append({"i": i, "side": "buy", "px": p_fill, "qty": qty, "reason": "cross_up"})

            elif long_exit and pos_qty > 0.0:
                p_fill = price * (1.0 - self.slippage_bps / 10000.0)
                proceeds = pos_qty * p_fill
                fee = proceeds * self.taker_fee_rate
                cash += (proceeds - fee)
                wins += int(p_fill > entry_price)
                n_trades += 1
                trades.append({"i": i, "side": "sell", "px": p_fill, "qty": pos_qty, "reason": "cross_down"})
                pos_qty = 0.0
                entry_price = np.nan

            equity = cash + pos_qty * price
            equity_series[i] = equity
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak)

            # tqdm postfix throttling
            if self.show_progress == "bar":
                if i - last_update >= 1000:
                    iterator.set_postfix({
                        "eq": f"{equity:,.0f}",
                        "cash": f"{cash:,.0f}",
                        "pos": f"{pos_qty:.4f}",
                        "dd": f"{max_dd*100:5.2f}%",
                        "trd": f"{n_trades:3d}",
                    })
                    last_update = i

        # 종료 시 포지션 정리(마지막 종가)
        if pos_qty > 0.0:
            last_price = float(df.iloc[-1]["close"])
            p_fill = last_price * (1.0 - self.slippage_bps / 10000.0)
            proceeds = pos_qty * p_fill
            fee = proceeds * self.taker_fee_rate
            cash += (proceeds - fee)
            wins += int(p_fill > entry_price)
            n_trades += 1
            trades.append({"i": len(df)-1, "side": "sell", "px": p_fill, "qty": pos_qty, "reason": "eod"})
            pos_qty = 0.0
            entry_price = np.nan
            equity_series[-1] = cash

        if self.show_progress == "bar":
            # type: ignore[attr-defined]
            iterator.close()  # tqdm

        final_equity = float(equity_series[~np.isnan(equity_series)][-1] if np.any(~np.isnan(equity_series)) else cash)
        total_return = (final_equity / self.initial_cash) - 1.0
        start_ts = pd.to_datetime(df["ts"].iloc[0], utc=True)
        end_ts   = pd.to_datetime(df["ts"].iloc[-1], utc=True)
        cagr = self._calc_cagr(total_return, start_ts, end_ts)
        bench = self._calc_benchmark()

        res = BacktestResult(
            start_ts=start_ts,
            end_ts=end_ts,
            final_equity=final_equity,
            total_return=total_return,
            cagr=cagr,
            max_drawdown=max_dd,
            n_trades=n_trades,
            win_rate=(wins / n_trades) if n_trades > 0 else 0.0,
            equity_series=pd.Series(equity_series, index=df["ts"]),
            trades=trades,
            summary={
                "initial_cash": self.initial_cash,
                "benchmark_return": bench,
                "elapsed_sec": time.time() - t0,
                "engine": "python",
            },
        )
        self._res = res
        return res
