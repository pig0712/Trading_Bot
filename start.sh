#!/usr/bin/env bash
# start.sh — uv + pyproject.toml 기반 부트스트랩 (실행 X, 환경/프로젝트 구성만)
# - 로컬 패키지: src/trading_bot
# - 빌드 에러 방지: hatch wheel target에 packages 명시
# - 인터프리터 고정: ./.venv/bin/python (./python 래퍼 제공)

set -euo pipefail

ROOT="$(pwd)"

echo "==> (0) Install uv (if missing)"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "    uv: $(uv --version)"

echo "==> (1) Create project layout"
mkdir -p "$ROOT/src/ingest" "$ROOT/src/indicators" "$ROOT/src/strategies" "$ROOT/src/backtest" "$ROOT/src/live"
mkdir -p "$ROOT/src/data/raw" "$ROOT/src/data/processed"
mkdir -p "$ROOT/configs/strategy" "$ROOT/configs/experiment"
mkdir -p "$ROOT/reports/backtests" "$ROOT/logs/backtest" "$ROOT/logs/live"
mkdir -p "$ROOT/src/trading_bot"
[ -f "$ROOT/.env.example" ] || touch "$ROOT/.env.example"
[ -f "$ROOT/src/trading_bot/__init__.py" ] || echo '__all__ = []' > "$ROOT/src/trading_bot/__init__.py"

echo "==> (2) .gitignore"
cat > "$ROOT/.gitignore" <<'EOF'
__pycache__/
*.pyc
.env
.env.*
src/data/processed/
reports/
logs/
.venv/
uv.lock
EOF

echo "==> (3) pyproject.toml (uv-managed)"
cat > "$ROOT/pyproject.toml" <<'EOF'
[project]
name = "trading-bot"
version = "0.1.0"
description = "BTCUSDT 1m ingest + backtest skeleton (uv + pyproject)"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
  "ccxt>=4.3.98",
  "pandas>=2.2.2",
  "pyarrow>=17.0.0",
  "python-dateutil>=2.9.0",
  "tqdm>=4.66.4",
  "PyYAML>=6.0.2",
]

[tool.uv]
# uv 기본 설정(필요시 확장)

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
# 로컬 패키지 경로를 명시해 hatch 빌드 에러 차단
packages = ["src/trading_bot"]
EOF

echo "==> (4) README.md"
cat > "$ROOT/README.md" <<'EOF'
# Trading_Bot (uv + pyproject)

## Setup
  uv sync          # .venv 생성 및 의존성 설치
  ./python -V      # 고정 인터프리터 확인

## Run (examples)
  ./python src/ingest/fetch_1m.py
  ./python src/backtest/run_backtest.py

## Notes
- 로컬 패키지는 src/trading_bot 로 잡혀 있습니다.
- 의존성은 pyproject.toml 에서 관리하세요.
EOF

echo "==> (5) Makefile (uses ./.venv/bin/python)"
cat > "$ROOT/Makefile" <<'EOF'
PY := ./.venv/bin/python

.PHONY: sync run
sync:
	uv sync

# ex) make run SCRIPT=src/backtest/run_backtest.py
run:
	$(PY) $(SCRIPT)
EOF

echo "==> (6) Create venv & install deps via uv sync"
uv sync

echo "==> (7) Pin interpreter & create ./python wrapper"
# 기본 인터프리터 고정
export PY="$ROOT/.venv/bin/python"
export PATH="$ROOT/.venv/bin:$PATH"
# 래퍼
cat > "$ROOT/python" <<'EOF'
#!/usr/bin/env bash
exec ".venv/bin/python" "$@"
EOF
chmod +x "$ROOT/python"

echo "==> Done. Project is ready (uv + pyproject)."
echo "Use:"
echo "  uv sync"
echo "  ./python your_script.py"
echo "  make run SCRIPT=src/backtest/run_backtest.py"
