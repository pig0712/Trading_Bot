#!/usr/bin/env python
"""
Trading_BOT/main.py
────────────────────────────────────────────────────────────────────────────
메인 애플리케이션 진입점입니다.
환경 변수 로드, 로깅 설정, CLI 실행을 담당합니다.
"""
from __future__ import annotations

import sys
import logging
import os # os.getenv 사용을 위해 추가
from pathlib import Path

from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────────────────
# 경로 및 환경 변수 설정
# ──────────────────────────────────────────────────────────────────────────
# 이 파일(main.py)이 있는 디렉토리를 프로젝트 루트로 간주합니다.
# (예: /path/to/your/Trading_Bot/)
ROOT_DIR = Path(__file__).resolve().parent
ENV_PATH = ROOT_DIR / ".env"

# .env 파일 로드 시도 (프로그램 시작 시 한 번만)
if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH, verbose=True)
    # 초기 로깅 설정 전이므로 print 사용
    print(f"INFO: main.py - Loaded environment variables from {ENV_PATH}", file=sys.stderr)
else:
    print(f"WARNING: main.py - .env file not found at {ENV_PATH}. API keys might be missing or need to be set as environment variables.", file=sys.stderr)

# src 디렉토리를 Python 경로에 추가 (다른 모듈 import 전에 수행)
SRC_DIR = ROOT_DIR / "src"
if SRC_DIR.exists() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
    print(f"INFO: main.py - Added {SRC_DIR} to sys.path", file=sys.stderr)

# ──────────────────────────────────────────────────────────────────────────
# 로깅 설정 (다른 모듈 import 전에 기본 설정 완료)
# ──────────────────────────────────────────────────────────────────────────
LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True) # 로그 디렉토리 생성 (이미 있으면 무시)
LOG_FILE = LOG_DIR / "trading_bot.log"

# 환경 변수에서 로그 레벨 가져오기 (기본값: INFO)
LOG_LEVEL_STR = os.getenv("LOG_LEVEL", "INFO").upper()
# getattr을 사용하여 문자열로부터 logging 레벨 객체 가져오기
LOG_LEVEL = getattr(logging, LOG_LEVEL_STR, logging.INFO)
if not isinstance(LOG_LEVEL, int): # getattr 실패 시 기본값 INFO 사용
    print(f"WARNING: main.py - Invalid LOG_LEVEL '{LOG_LEVEL_STR}'. Defaulting to INFO.", file=sys.stderr)
    LOG_LEVEL = logging.INFO

# 로깅 기본 설정
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(name)-25s - %(levelname)-8s - %(filename)s:%(lineno)d - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),  # 콘솔 출력 핸들러
        logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')  # 파일 출력 핸들러 (이어쓰기 모드)
    ]
)

# 이 파일 자체의 로거 (basicConfig 이후에 getLogger 호출)
_MAIN_LOG = logging.getLogger(__name__) # 이제 로거 사용 가능

# .env 로드 및 sys.path 추가에 대한 로그 (basicConfig 이후)
if ENV_PATH.exists():
    _MAIN_LOG.info(f"Successfully loaded environment variables from {ENV_PATH}")
else:
    _MAIN_LOG.warning(f".env file not found at {ENV_PATH}. API keys might be missing.")
if SRC_DIR.exists() and str(SRC_DIR) in sys.path:
     _MAIN_LOG.info(f"Successfully added {SRC_DIR} to sys.path")

_MAIN_LOG.info(f"Logging initialized. Application log level set to: {LOG_LEVEL_STR} ({LOG_LEVEL}). Log file: {LOG_FILE.resolve()}")


def run_main_cli():
    """
    trading_bot.cli 모듈의 메인 CLI 명령을 가져와 실행합니다.
    이 함수는 모든 초기 설정(경로, .env, 로깅)이 완료된 후 호출됩니다.
    """
    try:
        # src 경로가 sys.path에 추가된 후 import 수행
        from trading_bot.cli import main as cli_main_command # cli.py의 main 함수를 가져옴
        _MAIN_LOG.info("Invoking trading_bot.cli.main command...")
        cli_main_command()  # Click이 sys.argv를 파싱하여 적절한 명령 실행
    except ImportError:
        _MAIN_LOG.critical("Failed to import trading_bot.cli. Is 'src' directory in PYTHONPATH and all dependencies installed?", exc_info=True)
        sys.exit(1) # 심각한 오류로 종료
    except Exception as e: # click 명령 실행 중 발생할 수 있는 모든 예외 포괄
        _MAIN_LOG.critical(f"An unexpected error occurred when trying to run the CLI: {e}", exc_info=True)
        sys.exit(1) # 심각한 오류로 종료


if __name__ == "__main__":
    _MAIN_LOG.info(f"Trading_BOT/main.py executed as script. Current working directory: {Path.cwd()}")
    run_main_cli()
    _MAIN_LOG.info("Trading_BOT/main.py finished.")

