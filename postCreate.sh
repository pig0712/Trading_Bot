#!/usr/bin/env bash
set -euo pipefail

# 💡 프로젝트 루트 경로
REPO_ROOT="/workspaces/AI_Trading"
BACKUP_DIR="$REPO_ROOT/.github/codespaces/backup"
EXT_TXT="$BACKUP_DIR/extensions.txt"
SETTINGS_BAK="$BACKUP_DIR/settings.json"
WORKSPACE_SETTINGS="$REPO_ROOT/.vscode/settings.json"

# 🌟 로그 출력 함수
log() {
  echo -e "\n🟦 $1"
}

# 📦 확장 설치 함수
install_extensions() {
  local EXT_FILE="$1"
  if [[ -f "$EXT_FILE" ]]; then
    while IFS= read -r ext; do
      [[ -n "$ext" ]] && code --install-extension "$ext" --force
    done < "$EXT_FILE"
  fi
}

###############################################################################
# 0) VSCode 확장, 설정 복원
###############################################################################
log "[0] 확장 및 설정 복원 시작"
install_extensions "$EXT_TXT"

if [[ -f "$SETTINGS_BAK" ]]; then
  mkdir -p "$REPO_ROOT/.vscode"
  cp "$SETTINGS_BAK" "$WORKSPACE_SETTINGS"
fi

###############################################################################
# 1) uv 설치 (sudo 가능 여부에 따라)
###############################################################################
log "[1] uv 설치 시작"
if sudo -n true 2>/dev/null; then
  echo "🗝  sudo 사용 가능 → /usr/local/bin 설치"
  curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
else
  echo "🔒 sudo 불가 → \$HOME/.local/bin 설치"
  mkdir -p "$HOME/.local/bin"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$HOME/.local/bin" sh
  for RC in "$HOME/.bashrc" "$HOME/.zshrc"; do
    grep -qxF 'export PATH="$HOME/.local/bin:$PATH"' "$RC" 2>/dev/null \
      || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$RC"
  done
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "✅ uv 버전 확인:"; uv --version || { echo "❗ uv 인식 실패"; exit 1; }

###############################################################################
# 2) Python 3.12 설치
###############################################################################
log "[2] Python 3.12 설치 중"
uv python install 3.12 --default --preview

###############################################################################
# 3) VSCode 인터프리터 설정 (/BTC/.venv/bin/python 사용)
###############################################################################
log "[3] VSCode 인터프리터 기본 설정 (BTC/.venv 사용)"
mkdir -p "$(dirname "$WORKSPACE_SETTINGS")"

TARGET_PYTHON="$REPO_ROOT/BTC/.venv/bin/python"

if command -v jq &>/dev/null && [[ -f "$WORKSPACE_SETTINGS" ]]; then
  tmp=$(mktemp)
  jq --arg p "$TARGET_PYTHON" \
     '. + { "python.defaultInterpreterPath": $p }' \
     "$WORKSPACE_SETTINGS" > "$tmp" && mv "$tmp" "$WORKSPACE_SETTINGS"
else
  cat > "$WORKSPACE_SETTINGS" <<EOF
{
  "python.defaultInterpreterPath": "$TARGET_PYTHON"
}
EOF
fi


###############################################################################
# 3-1) 테마 설정 자동 반영 (.github/codespaces/settings.json → .vscode/settings.json)
###############################################################################
THEME_SETTINGS="$REPO_ROOT/.github/codespaces/settings.json"
if [[ -f "$THEME_SETTINGS" ]]; then
  echo "🎨 테마 설정 적용 중"
  if command -v jq &>/dev/null; then
    tmp=$(mktemp)
    jq -s '.[0] * .[1]' "$WORKSPACE_SETTINGS" "$THEME_SETTINGS" > "$tmp" && mv "$tmp" "$WORKSPACE_SETTINGS"
  else
    echo "⚠️ jq 명령어가 없어 테마 병합을 건너뜁니다 (수동 반영 필요)"
  fi
fi

###############################################################################
# 4) 확장 목록 파일 기반 재설치 (.devcontainer/extensions.txt 있을 경우)
###############################################################################
log "[4] .devcontainer/extensions.txt 기반 확장 설치"
DEV_EXT="$REPO_ROOT/.devcontainer/extensions.txt"
install_extensions "$DEV_EXT"

###############################################################################
# 5) 현 상태 백업 (확장, settings.json, 테마)
###############################################################################
log "[5] VSCode 상태 백업"
mkdir -p "$BACKUP_DIR"

# 확장 목록 저장
code --list-extensions > "$EXT_TXT"

# 설정 백업
cp "$WORKSPACE_SETTINGS" "$SETTINGS_BAK"

# 테마 정보만 따로 추출
if command -v jq &>/dev/null; then
  jq -r '.["workbench.colorTheme"], .["workbench.iconTheme"]' \
      "$WORKSPACE_SETTINGS" | grep -v '^null$' > "$BACKUP_DIR/themes.txt"
fi

# → .venv 재생성(이미 있으면 건너뜀) + numpy, pandas 재설치
if [[ ! -d "$REPO_ROOT/BTC/.venv" ]]; then
  log "🐍 .venv 생성 중…"
  uv venv /workspaces/AI_Trading/BTC/.venv

log "🎉 postCreate.sh 실행 완료!"