# src/ingest/check_integrity.py
# 1분봉 데이터 무결성 점검/선택적 수정 스크립트 (CLI 인자 없음)
# - 지원 포맷: Parquet(.parquet) 또는 CSV(.csv)
# - 중복/결측(분 단위 간격), 시간대(UTC), 정렬 여부 점검
# - 옵션에 따라 정리본 저장

import os
from datetime import timedelta
import pandas as pd

# ===== 설정 =====
CONFIG = {
    "PATH": "src/data/raw/BTCUSDT_2511031344.csv", 
    "TS_COL": "ts",
    "IS_CSV_UTC_STR": True,
    "SAVE_FIXED": True,
    "FIX_OUTPUT_PATH": None,
    "PRINT_MISSING_LIMIT": 200,
}
# ==============


def _load_df(path: str, ts_col: str, is_csv_utc_str: bool) -> pd.DataFrame:
    if path.lower().endswith(".parquet"):
        df = pd.read_parquet(path)
        df[ts_col] = pd.to_datetime(df[ts_col], utc=True)
    elif path.lower().endswith(".csv"):
        df = pd.read_csv(path)
        if is_csv_utc_str:
            # 예: "2025-10-31T12:34:00Z"
            df[ts_col] = pd.to_datetime(df[ts_col], utc=True)
        else:
            # 숫자 ms 등인 경우 여기에 맞게 조정
            df[ts_col] = pd.to_datetime(df[ts_col], unit="ms", utc=True)
    else:
        raise ValueError("지원하지 않는 파일 확장자입니다. (.csv 또는 .parquet)")
    return df


def _save_df(df: pd.DataFrame, path: str):
    if path.lower().endswith(".parquet"):
        df.to_parquet(path, index=False)
    elif path.lower().endswith(".csv"):
        df.to_csv(path, index=False)
    else:
        raise ValueError("저장 확장자는 .csv 또는 .parquet 이어야 합니다.")


def main():
    path = CONFIG["PATH"]
    ts_col = CONFIG["TS_COL"]
    is_csv_utc_str = CONFIG["IS_CSV_UTC_STR"]
    save_fixed = CONFIG["SAVE_FIXED"]
    fix_out = CONFIG["FIX_OUTPUT_PATH"]
    print_missing_limit = CONFIG["PRINT_MISSING_LIMIT"]

    if not os.path.exists(path):
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {path}")

    print(f"[LOAD] {path}")
    df = _load_df(path, ts_col, is_csv_utc_str)

    required_cols = {ts_col, "open", "high", "low", "close", "volume"}
    missing_cols = required_cols - set(df.columns)
    if missing_cols:
        print(f"[WARN] 누락된 컬럼: {sorted(missing_cols)} (점검은 ts 기준으로 계속 진행)")

    # 정렬 & 중복 제거 전 원본 기준 지표
    n0 = len(df)
    dup0 = df.duplicated(subset=[ts_col]).sum()

    # UTC 확인(정보용)
    tz_ok = pd.api.types.is_datetime64tz_dtype(df[ts_col])
    print(f"[INFO] tz-aware(UTC)={tz_ok}, rows={n0}, dup_ts(before)={dup0}")

    # 정렬 → 중복제거 → 다시 정렬 보장
    df = df.sort_values(ts_col)
    before = len(df)
    df = df.drop_duplicates(subset=[ts_col], keep="first")
    after = len(df)
    dropped_dups = before - after

    # 연속성 점검 (1분 간격)
    df = df.reset_index(drop=True)
    if len(df) >= 2:
        deltas = df[ts_col].diff().dropna()
        # 결측(간격이 1분보다 큰 곳)
        one_min = pd.Timedelta(minutes=1)
        gaps = deltas[deltas > one_min]
        gap_count = len(gaps)
    else:
        gaps = pd.Series([], dtype="timedelta64[ns]")
        gap_count = 0

    print(f"[CHECK] sorted asc by {ts_col}=True")
    print(f"[CHECK] duplicates removed={dropped_dups}")

    # 결측 구간 상세
    total_missing = 0
    missing_ranges = []
    if gap_count > 0:
        # 각 gap 구간에 빠진 타임스탬프 개수 = (gap/1분 - 1)
        for idx in gaps.index:
            t_prev = df.loc[idx - 1, ts_col]
            t_curr = df.loc[idx, ts_col]
            miss = int((t_curr - t_prev) / timedelta(minutes=1)) - 1
            total_missing += miss
            missing_ranges.append((t_prev, t_curr, miss))

    print(f"[CHECK] gap_segments={gap_count}, total_missing_minutes={total_missing}")
    if missing_ranges:
        print(f"[DETAIL] first {min(print_missing_limit, len(missing_ranges))} missing segments:")
        for i, (a, b, m) in enumerate(missing_ranges[:print_missing_limit], 1):
            print(f"  {i:03d}) {a.isoformat()} -> {b.isoformat()}  missing={m} min")

    # 간단 통계 (있으면)
    if {"close", "volume"}.issubset(df.columns):
        desc = df[["close", "volume"]].describe().to_string()
        print("[STATS]\n" + desc)

    # 저장 (옵션)
    if save_fixed:
        base, ext = os.path.splitext(path)
        out_path = fix_out if fix_out else f"{base}_fixed{ext}"
        # 고정: 정렬 + 중복제거만 수행(결측은 채우지 않음)
        to_save = df.copy()
        # 필요시 여기에서 리샘플/결측보간 로직을 추가할 수 있음(현재는 '점검용'으로만 저장)
        _save_df(to_save, out_path)
        print(f"[SAVE] cleaned file: {out_path} (rows={len(to_save)})")

    print("[DONE] integrity check complete.")


if __name__ == "__main__":
    main()
