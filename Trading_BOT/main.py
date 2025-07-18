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
import os
from logging.handlers import RotatingFileHandler # 로그 파일 자동 관리를 위해 임포트
from pathlib import Path

from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────────────────
# 1. 경로 및 환경 변수 설정
# ──────────────────────────────────────────────────────────────────────────
# 이 파일(main.py)이 있는 디렉토리를 프로젝트 루트로 간주합니다.
ROOT_DIR = Path(__file__).resolve().parent
ENV_PATH = ROOT_DIR / ".env"
SRC_DIR = ROOT_DIR / "src"

# .env 파일 로드 (로깅 설정 전이므로 print를 사용하여 상태 출력)
if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH, verbose=True)
    print(f"INFO: '.env' 파일 로드 완료: {ENV_PATH}", file=sys.stderr)
else:
    print(f"WARNING: '.env' 파일을 찾을 수 없습니다: {ENV_PATH}. 시스템 환경변수를 확인하세요.", file=sys.stderr)

# src 디렉토리를 Python 경로에 추가 (다른 모듈 import 전에 수행)
if SRC_DIR.exists() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
    print(f"INFO: Python 경로에 'src' 폴더 추가 완료: {SRC_DIR}", file=sys.stderr)


# ──────────────────────────────────────────────────────────────────────────
# 2. 로깅 설정
# ──────────────────────────────────────────────────────────────────────────
def setup_logging():
    """애플리케이션 전반의 로깅 시스템을 설정하고 초기화합니다."""
    
    LOG_DIR = ROOT_DIR / "logs"
    LOG_DIR.mkdir(exist_ok=True)
    LOG_FILE = LOG_DIR / "trading_bot.log"
    ERROR_LOG_FILE = LOG_DIR / "trading_bot_errors.log" # ✅ 오류 로그 파일 경로 정의

    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    if not isinstance(log_level, int):
        print(f"WARNING: 잘못된 LOG_LEVEL '{log_level_str}'. 기본값 INFO를 사용합니다.", file=sys.stderr)
        log_level = logging.INFO
    
    # ✅ 기본 포매터 정의
    log_formatter = logging.Formatter(
        '%(asctime)s - %(name)-25s - %(levelname)-8s - %(filename)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # ✅ 오류만 기록하는 핸들러 생성
    error_handler = RotatingFileHandler(
        ERROR_LOG_FILE,
        maxBytes=5 * 1024 * 1024, # 5 MB
        backupCount=3,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR) # ERROR 레벨 이상만 처리
    error_handler.setFormatter(log_formatter)

    # ✅ 모든 내용을 기록하는 핸들러 생성
    full_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding='utf-8'
    )
    full_handler.setFormatter(log_formatter)
    
    # 로깅 기본 설정
    logging.basicConfig(
        level=log_level,
        handlers=[
            logging.StreamHandler(sys.stdout), # 콘솔 출력
            full_handler,                      # 전체 내용 파일로 출력
            error_handler                      # 오류 내용만 별도 파일로 출력
        ]
    )

    # 외부 라이브러리 로그 레벨 조정
    logging.getLogger("gate_api").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    
    logger = logging.getLogger(__name__)
    logger.info(f"로깅 시스템 초기화 완료. 로그 레벨: {log_level_str}.")
    logger.info(f"전체 로그 파일: {LOG_FILE.resolve()}")
    logger.info(f"오류 로그 파일: {ERROR_LOG_FILE.resolve()}")
    return logger

# ──────────────────────────────────────────────────────────────────────────
# 3. 애플리케이션 실행
# ──────────────────────────────────────────────────────────────────────────
def run_cli_app(logger: logging.Logger):
    """모든 초기 설정 완료 후, CLI 애플리케이션을 실행합니다."""
    try:
        # src 경로가 추가된 후 import 수행
        from trading_bot.cli import main as cli_main_command
        
        logger.info("CLI 애플리케이션 시작...")
        cli_main_command()
        
    except ImportError:
        logger.critical("trading_bot.cli 모듈 임포트 실패. 'src' 디렉토리 및 의존성 설치를 확인하세요.", exc_info=True)
        sys.exit(1)
    except Exception as e:
        logger.critical(f"CLI 실행 중 예상치 못한 오류 발생: {e}", exc_info=True)
        sys.exit(1)

# ──────────────────────────────────────────────────────────────────────────
# 메인 실행 블록
# ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # 1. 로깅 시스템 설정 및 로거 인스턴스 가져오기
    main_logger = setup_logging()
    
    # 2. 메인 CLI 앱 실행
    main_logger.info(f"Trading_BOT 스크립트 실행 시작. CWD: {Path.cwd()}")
    run_cli_app(main_logger)
    main_logger.info("Trading_BOT 스크립트 실행 완료.")


    
    