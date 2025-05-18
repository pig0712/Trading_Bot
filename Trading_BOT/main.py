#!/usr/bin/env python
"""엔트리포인트.

패키지를 설치하지 않고도 바로 실행할 수 있도록 src 경로를 임시로 추가하지만,
배포·테스트 단계에서는 `python -m trading_bot.cli` 방식이 더 권장됩니다.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))      # <─ 임시 PYTHONPATH 추가

from trading_bot.cli import main as cli_main  # noqa: E402

if __name__ == "__main__":
    cli_main()
