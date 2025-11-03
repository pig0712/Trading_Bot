# src/ingest/fetch_1m_full_csv.py
# 2018-01-01부터 지금까지 1분봉을 "분할 수집"해서
# src/data/raw/BTCUSDT_YYMMDDhhmm.csv (현지시간=KST 기준)로 저장
#
# 실행: uv run python src/ingest/fetch_1m_full_csv.py

import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import ccxt
import pandas as pd
from dateutil import parser as dtparser
from tqdm import tqdm

# ===== 설정 =====
CONFIG = {
    "EXCHANGE": "binance",          # 장기 히스토리 수집 용이: "binance" 권장
    "SYMBOL": "BTC/USDT",
    "TIMEFRAME": "1m",
    "START": "2018-01-01",          # UTC 기준 시작일(naive면 UTC로 간주)
    "OUT_DIR": "src/data/raw",
    "LIMIT": 1000,                  # fetch_ohlcv per call
    "SLEEP_SEC": 0.2,               # 레이트리밋 여유
    "PRINT_EVERY": 50,              # 몇 배치마다 진행상황 로그
}
# ===============

# timeframe → ms
TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}

def to_fname(symbol: str) -> str:
    return symbol.replace("/", "")

def ohlcv_to_df(rows):
    # CCXT 포맷: [timestamp, open, high, low, close, volume]
    cols = ["ts", "open", "high", "low", "close", "volume"]
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows, columns=cols)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df

def build_exchange(name: str):
    name = name.lower()
    if name == "binance":
        return ccxt.binance({"enableRateLimit": True})
    if name == "gateio":
        return ccxt.gateio({"enableRateLimit": True})
    raise ValueError(f"Unsupported EXCHANGE: {name}")

def main():
    ex_name    = CONFIG["EXCHANGE"]
    symbol     = CONFIG["SYMBOL"]
    timeframe  = CONFIG["TIMEFRAME"]
    start_str  = CONFIG["START"]
    out_dir    = CONFIG["OUT_DIR"]
    limit      = int(CONFIG["LIMIT"])
    sleep_sec  = float(CONFIG["SLEEP_SEC"])
    print_every= int(CONFIG["PRINT_EVERY"])

    os.makedirs(out_dir, exist_ok=True)

    # 파일명: BTCUSDT_YYMMDDhhmm.csv (KST 기준)
    kst_now = datetime.now(ZoneInfo("Asia/Seoul"))
    stamp = kst_now.strftime("%y%m%d%H%M")
    csv_path = os.path.join(out_dir, f"{to_fname(symbol)}_{stamp}.csv")

    # CSV 헤더 작성(append 시에도 최초 1회만)
    wrote_header = False
    if os.path.exists(csv_path):
        os.remove(csv_path)

    # 시작 시각
    start_dt = dtparser.parse(start_str)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    since_ms = int(start_dt.timestamp() * 1000)

    # 종료 목표(now)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    tf_ms = TF_MS[timeframe]

    ex = build_exchange(ex_name)

    total_rows = 0
    batch_cnt = 0
    pbar = tqdm(unit="rows", dynamic_ncols=True)

    while since_ms <= now_ms:
        try:
            batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=limit)
        except ccxt.BaseError as e:
            print(f"[WARN] fetch error: {e}; retrying...")
            time.sleep(max(1.0, getattr(ex, "rateLimit", 1000) / 1000))
            continue

        if not batch:
            # 더 이상 데이터가 없으면 중단
            break

        df = ohlcv_to_df(batch)
        if df.empty:
            break

        # 다음 루프 시작 ms (중복 방지)
        last_ts_ms = int(df["ts"].max().timestamp() * 1000)
        # 간혹 동일 마지막 분이 연속 반환될 수 있어 +1ms
        since_ms = last_ts_ms + 1

        # CSV로 바로바로 append (메모리 절약)
        # 컬럼 순서 명시
        df = df[["ts", "open", "high", "low", "close", "volume"]]
        # ISO8601(UTC) 문자열로 저장
        out_df = df.copy()
        out_df["ts"] = out_df["ts"].dt.tz_convert(timezone.utc).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        out_df.to_csv(csv_path, mode="a", index=False, header=(not wrote_header))
        wrote_header = True

        # 진행상황
        got = len(df)
        total_rows += got
        batch_cnt += 1
        pbar.update(got)

        if batch_cnt % print_every == 0:
            latest_iso = df["ts"].max().isoformat()
            print(f"[INFO] {batch_cnt} batches, rows={total_rows}, latest={latest_iso}")

        # 종료 조건 (현재시각 도달한 경우)
        if last_ts_ms + tf_ms > now_ms:
            break

        time.sleep(max(sleep_sec, getattr(ex, "rateLimit", 1000) / 1000))

    pbar.close()
    print(f"[DONE] saved CSV: {csv_path} rows={total_rows}")

if __name__ == "__main__":
    main()
