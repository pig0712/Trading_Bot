# src/ingest/patch_missing_gaps.py
# 결측 구간만 재수집해서 병합 (상한필터/옵션 안정화)

import os
import time
from datetime import datetime, timezone, timedelta
from typing import List, Tuple

import pandas as pd
import ccxt
from tqdm import tqdm

# ===== 설정 =====
CONFIG = {
    "INPUT_PATH": "src/data/raw/BTCUSDT_2511031344.csv",
    "OUTPUT_PATH": None,

    "EXCHANGE": "binance",          # "binance" | "gateio"
    "SYMBOL": "BTC/USDT",
    "TIMEFRAME": "1m",

    "LIMIT": 1000,                  # binance 최대 1500; 1000은 안전
    "RETRY_MAX": 5,
    "RETRY_SLEEP": 1.5,
    "REQUEST_SLEEP": 0.35,
    "SAFETY_MS": 1,
    "PRINT_EVERY_GAP": 1,

    "SAVE_CSV": True,
    "SAVE_PARQUET": False,
    "TS_COL": "ts",
}
# =================

TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}
REQ_COLS = ["ts", "open", "high", "low", "close", "volume"]


def load_frame(path: str, ts_col: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if path.lower().endswith(".parquet"):
        df = pd.read_parquet(path)
        df[ts_col] = pd.to_datetime(df[ts_col], utc=True)
    elif path.lower().endswith(".csv"):
        df = pd.read_csv(path)
        df[ts_col] = pd.to_datetime(df[ts_col], utc=True)  # ISO8601 UTC 가정
    else:
        raise ValueError("지원 확장자: .csv, .parquet")
    for c in REQ_COLS:
        if c not in df.columns:
            raise ValueError(f"입력 데이터에 필요한 컬럼 누락: {c}")
    return df.sort_values(ts_col).drop_duplicates(subset=[ts_col]).reset_index(drop=True)


def detect_gaps(df: pd.DataFrame, ts_col: str, tf_ms: int) -> List[Tuple[pd.Timestamp, pd.Timestamp, int]]:
    gaps = []
    if len(df) < 2:
        return gaps
    one_tf = pd.Timedelta(milliseconds=tf_ms)
    deltas = df[ts_col].diff().dropna()
    idxs = deltas[deltas > one_tf].index
    for i in idxs:
        t_prev = df.loc[i - 1, ts_col]
        t_curr = df.loc[i, ts_col]
        miss = int((t_curr - t_prev) / one_tf) - 1
        gaps.append((t_prev, t_curr, miss))
    return gaps


def build_exchange(name: str):
    name = name.lower()
    if name == "binance":
        ex = ccxt.binance({
            "enableRateLimit": True,
            # spot 명시 (선물/마진 혼동 방지)
            "options": {"defaultType": "spot"},
        })
        ex.load_markets()
        return ex
    if name == "gateio":
        ex = ccxt.gateio({"enableRateLimit": True})
        ex.load_markets()
        return ex
    raise ValueError(f"Unsupported EXCHANGE: {name}")


def fetch_range(ex, symbol: str, timeframe: str, start_ms: int, end_ms: int, limit: int,
                retry_max: int, retry_sleep: float, request_sleep: float, safety_ms: int) -> pd.DataFrame:
    """[start_ms, end_ms] 범위 분할 수집"""
    all_parts = []
    since = start_ms
    end_dt = pd.to_datetime(end_ms, unit="ms", utc=True)
    pbar = tqdm(total=0, unit="rows", dynamic_ncols=True)

    while since <= end_ms:
        tries = 0
        while True:
            try:
                batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)
                break
            except ccxt.BaseError as e:
                tries += 1
                if tries > retry_max:
                    print(f"[ERROR] fetch failed permanently at since={since}: {e}")
                    if all_parts:
                        out = pd.concat(all_parts, ignore_index=True).sort_values("ts").drop_duplicates(subset=["ts"])
                        return out
                    return pd.DataFrame(columns=REQ_COLS)
                print(f"[WARN] fetch error: {e}; retry {tries}/{retry_max}")
                time.sleep(retry_sleep)

        if not batch:
            break

        df = pd.DataFrame(batch, columns=REQ_COLS)
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)

        # 상한 필터: tz-aware 비교 (안전)
        df = df[df["ts"] <= end_dt]

        if not df.empty:
            all_parts.append(df)
            pbar.total += len(df)
            pbar.refresh()
            last_ts_ms = int(df["ts"].max().timestamp() * 1000)
            next_since = last_ts_ms + safety_ms
            if next_since <= since:
                next_since = since + safety_ms
            since = next_since
        else:
            # 이번 배치가 상한필터로 비어서 진전이 없으면 종료
            break

        time.sleep(request_sleep)

    pbar.close()

    if all_parts:
        out = pd.concat(all_parts, ignore_index=True)
        out = out.sort_values("ts").drop_duplicates(subset=["ts"]).reset_index(drop=True)
        return out
    return pd.DataFrame(columns=REQ_COLS)


def main():
    path = CONFIG["INPUT_PATH"]
    out_path = CONFIG["OUTPUT_PATH"]
    ex_name = CONFIG["EXCHANGE"]
    symbol = CONFIG["SYMBOL"]
    timeframe = CONFIG["TIMEFRAME"]
    limit = int(CONFIG["LIMIT"])
    retry_max = int(CONFIG["RETRY_MAX"])
    retry_sleep = float(CONFIG["RETRY_SLEEP"])
    req_sleep = float(CONFIG["REQUEST_SLEEP"])
    safety_ms = int(CONFIG["SAFETY_MS"])
    save_csv = bool(CONFIG["SAVE_CSV"])
    save_parquet = bool(CONFIG["SAVE_PARQUET"])
    ts_col = CONFIG["TS_COL"]

    tf_ms = TF_MS[timeframe]

    print(f"[LOAD] {path}")
    base_df = load_frame(path, ts_col)

    gaps = detect_gaps(base_df, ts_col, tf_ms)
    if not gaps:
        print("[INFO] 결측 구간이 없습니다. 종료.")
        return
    print(f"[INFO] gap segments = {len(gaps)} | total missing minutes = {sum(m for *_ , m in gaps)}")

    ex = build_exchange(ex_name)

    # 스모크 테스트: 첫 gap 1개만 미리 호출해 실제로 데이터 오는지 확인
    t0_prev, t0_curr, m0 = gaps[0]
    test_start = int((t0_prev + timedelta(minutes=1)).timestamp() * 1000)
    test_end   = int((t0_curr - timedelta(minutes=1)).timestamp() * 1000)
    test_df = fetch_range(ex, symbol, timeframe, test_start, test_end, limit, retry_max, retry_sleep, req_sleep, safety_ms)
    print(f"[TEST] first gap fetch rows={len(test_df)} (should be >0 if exchange has data)")

    patched_parts = []
    for idx, (t_prev, t_curr, miss) in enumerate(gaps, 1):
        start_ms = int((t_prev + timedelta(minutes=1)).timestamp() * 1000)
        end_ms   = int((t_curr - timedelta(minutes=1)).timestamp() * 1000)
        if start_ms > end_ms:
            continue
        if CONFIG["PRINT_EVERY_GAP"] and (idx % CONFIG["PRINT_EVERY_GAP"] == 0):
            print(f"[GAP {idx:03d}] {t_prev.isoformat()} -> {t_curr.isoformat()}  miss={miss}m")
        df_gap = fetch_range(ex, symbol, timeframe, start_ms, end_ms, limit, retry_max, retry_sleep, req_sleep, safety_ms)
        if not df_gap.empty:
            patched_parts.append(df_gap)

    if not patched_parts:
        print("[INFO] API에서 결측을 메울 데이터가 반환되지 않았습니다. 원본 유지.")
        return

    fill_df = pd.concat(patched_parts, ignore_index=True).sort_values("ts").drop_duplicates(subset=["ts"])

    merged = pd.concat([base_df, fill_df], ignore_index=True)
    merged = merged.sort_values(ts_col).drop_duplicates(subset=[ts_col]).reset_index(drop=True)

    if out_path is None:
        base, ext = os.path.splitext(path)
        out_path = f"{base}_patched{ext}"

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    if out_path.lower().endswith(".csv"):
        out = merged.copy()
        out["ts"] = out["ts"].dt.tz_convert(timezone.utc).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        out.to_csv(out_path, index=False)
        print(f"[SAVE] CSV: {out_path} rows={len(out)}")
    elif out_path.lower().endswith(".parquet"):
        merged.to_parquet(out_path, index=False)
        print(f"[SAVE] Parquet: {out_path} rows={len(merged)}")
    else:
        # 입력 확장자 따라 저장
        if path.lower().endswith(".csv"):
            out = merged.copy()
            out["ts"] = out["ts"].dt.tz_convert(timezone.utc).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            out.to_csv(out_path, index=False)
            print(f"[SAVE] CSV: {out_path} rows={len(out)}")
        else:
            merged.to_parquet(out_path, index=False)
            print(f"[SAVE] Parquet: {out_path} rows={len(merged)}")

    if save_csv and not out_path.lower().endswith(".csv"):
        alt_csv = out_path.rsplit(".", 1)[0] + ".csv"
        out = merged.copy()
        out["ts"] = out["ts"].dt.tz_convert(timezone.utc).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        out.to_csv(alt_csv, index=False)
        print(f"[SAVE] CSV (secondary): {alt_csv} rows={len(out)}")
    if save_parquet and not out_path.lower().endswith(".parquet"):
        alt_pq = out_path.rsplit(".", 1)[0] + ".parquet"
        merged.to_parquet(alt_pq, index=False)
        print(f"[SAVE] Parquet (secondary): {alt_pq} rows={len(merged)}")

    print("[DONE] patch complete.")


if __name__ == "__main__":
    main()
