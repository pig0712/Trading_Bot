# src/ingest/compact_time_axis.py
# 결측 구간을 "압축"하여 앞/뒤를 붙여 연속 1분 시계열로 만드는 스크립트
# - 가격/거래량 데이터는 그대로, 시간축만 갭만큼 앞으로 당김(누적)
# - 원본 ts는 ts_original로 보존
#
# 실행 예:
#   ./.venv/bin/python src/ingest/compact_time_axis.py

import os
from datetime import timedelta
from typing import List, Tuple

import pandas as pd

# ===== 설정 =====
CONFIG = {
    "INPUT_PATH": "src/data/raw/BTCUSDT_2511031344_fixed.csv",  # CSV 또는 Parquet
    "OUTPUT_PATH": None,                                        # None이면 *_compact.(csv/parquet)
    "TS_COL": "ts",                                             # UTC 타임스탬프 컬럼
    "TIMEFRAME": "1m",                                          # 고정 1m 가정
    "KEEP_ORIGINAL_TS": True,                                   # 원본 ts를 ts_original로 보존
    "SAVE_CSV": True,                                           # 출력 확장자와 별개로 보조 저장 옵션
    "SAVE_PARQUET": False,
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
    # 최소 컬럼 체크(있으면 좋음)
    missing = [c for c in REQ_COLS if c not in df.columns]
    if missing:
        print(f"[WARN] 누락 컬럼: {missing} (진행은 ts 기준으로 계속)")
    return df.sort_values(ts_col).drop_duplicates(subset=[ts_col]).reset_index(drop=True)


def detect_gaps(df: pd.DataFrame, ts_col: str, tf_ms: int) -> List[Tuple[int, pd.Timestamp, pd.Timestamp, int]]:
    """(행 인덱스, prev_ts, curr_ts, gap_minutes) 리스트 반환"""
    gaps = []
    one_tf = pd.Timedelta(milliseconds=tf_ms)
    deltas = df[ts_col].diff().dropna()
    idxs = deltas[deltas > one_tf].index
    for i in idxs:
        t_prev = df.loc[i - 1, ts_col]
        t_curr = df.loc[i, ts_col]
        miss = int((t_curr - t_prev) / one_tf) - 1
        gaps.append((i, t_prev, t_curr, miss))
    return gaps


def compact_time(df: pd.DataFrame, ts_col: str, tf_ms: int) -> pd.DataFrame:
    gaps = detect_gaps(df, ts_col, tf_ms)
    print(f"[INFO] gap segments = {len(gaps)} | total missing minutes = {sum(g[3] for g in gaps)}")

    if CONFIG["KEEP_ORIGINAL_TS"]:
        df = df.copy()
        df["ts_original"] = df[ts_col]
    else:
        df = df.copy()

    # 누적 시프트(분 → timedelta) 계산
    cumulative_shift = 0  # minutes
    shift_series = pd.Series(0, index=df.index, dtype="int64")

    gap_iter = iter(gaps)
    current_gap = next(gap_iter, None)

    # 각 행을 순회하며, 갭의 시작 인덱스 이후 행들에 누적 시프트 적용
    for idx in range(len(df)):
        while current_gap is not None and idx >= current_gap[0]:
            # 이 갭부터 이후 전부에 누적 시프트 추가
            cumulative_shift += current_gap[3]
            current_gap = next(gap_iter, None)
        shift_series.iloc[idx] = cumulative_shift

    # 실제 시프트 적용: ts_new = ts - cumulative_shift(minutes)
    df[ts_col] = df[ts_col] - shift_series.map(lambda m: timedelta(minutes=int(m)))

    # 검증: 이제 diff가 정확히 1분이어야 함(처음 행 제외)
    one_min = pd.Timedelta(minutes=1)
    if len(df) > 1:
        bad = (df[ts_col].diff().dropna() != one_min).sum()
        print(f"[CHECK] non-1m steps after compaction: {bad}")

    return df


def main():
    path = CONFIG["INPUT_PATH"]
    out_path = CONFIG["OUTPUT_PATH"]
    ts_col = CONFIG["TS_COL"]
    tf_ms = TF_MS[CONFIG["TIMEFRAME"]]

    print(f"[LOAD] {path}")
    df = load_frame(path, ts_col)

    compact = compact_time(df, ts_col, tf_ms)

    # 출력 경로
    if out_path is None:
        base, ext = os.path.splitext(path)
        out_path = f"{base}_compact{ext}"

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    # 저장 (입력 확장자에 맞춤)
    if out_path.lower().endswith(".csv"):
        out = compact.copy()
        # CSV는 UTC ISO8601로
        for col in [c for c in [ts_col, "ts_original"] if c in out.columns]:
            out[col] = out[col].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        out.to_csv(out_path, index=False)
        print(f"[SAVE] CSV: {out_path} rows={len(out)}")
    elif out_path.lower().endswith(".parquet"):
        compact.to_parquet(out_path, index=False)
        print(f"[SAVE] Parquet: {out_path} rows={len(compact)}")
    else:
        # 기본은 입력 확장자 따라감
        if path.lower().endswith(".csv"):
            out = compact.copy()
            for col in [c for c in [ts_col, "ts_original"] if c in out.columns]:
                out[col] = out[col].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            out.to_csv(out_path, index=False)
            print(f"[SAVE] CSV: {out_path} rows={len(out)}")
        else:
            compact.to_parquet(out_path, index=False)
            print(f"[SAVE] Parquet: {out_path} rows={len(compact)}")

    # 보조 저장 옵션
    if CONFIG["SAVE_CSV"] and not out_path.lower().endswith(".csv"):
        alt_csv = out_path.rsplit(".", 1)[0] + ".csv"
        out = compact.copy()
        for col in [c for c in [ts_col, "ts_original"] if c in out.columns]:
            out[col] = out[col].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        out.to_csv(alt_csv, index=False)
        print(f"[SAVE] CSV (secondary): {alt_csv} rows={len(out)}")
    if CONFIG["SAVE_PARQUET"] and not out_path.lower().endswith(".parquet"):
        alt_pq = out_path.rsplit(".", 1)[0] + ".parquet"
        compact.to_parquet(alt_pq, index=False)
        print(f"[SAVE] Parquet (secondary): {alt_pq} rows={len(compact)}")

    print("[DONE] compact complete.")


if __name__ == "__main__":
    main()
