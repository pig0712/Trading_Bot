#!/usr/bin/env bash
# Codespace 생성 후 개발 환경을 자동으로 설정하는 스크립트입니다.
set -euo pipefail

# 💡 프로젝트 경로 변수 설정
# REPO_ROOT는 Codespace의 최상위 작업 디렉토리입니다.
REPO_ROOT="/workspaces/Trading_Bot"
# PROJECT_DIR은 실제 Python 프로젝트 파일들이 있는 디렉토리입니다.
PROJECT_DIR="$REPO_ROOT/Trading_BOT"

# 백업 및 설정 파일 경로
BACKUP_DIR="$REPO_ROOT/.github/codespaces/backup" # 백업 경로 예시
EXT_TXT="$BACKUP_DIR/extensions.txt"
SETTINGS_BAK="$BACKUP_DIR/settings.json"
WORKSPACE_SETTINGS="$REPO_ROOT/.vscode/settings.json"

# 🌟 로그 출력 함수
log() {
  # 파란색 배경과 흰색 글씨로 로그를 출력합니다.
  echo -e "\n\033[44m\033[1;37m >> $1 \033[0m"
}

# 📦 확장 프로그램 설치 함수
install_extensions() {
  local EXT_FILE="$1"
  if [[ -f "$EXT_FILE" ]]; then
    log "📦 '$EXT_FILE' 파일 기반 확장 프로그램 설치 중..."
    while IFS= read -r ext; do
      # 빈 줄이나 #으로 시작하는 주석은 건너뜁니다.
      if [[ -n "$ext" && ! "$ext" =~ ^\s*# ]]; then
        echo "   - 설치 중: $ext"
        code --install-extension "$ext" --force
      fi
    done < "$EXT_FILE"
    echo "✅ 확장 프로그램 설치 완료."
  fi
}

###############################################################################
# 0) VSCode 확장 및 설정 복원 (백업이 있는 경우)
###############################################################################
log "[0] VSCode 확장 및 설정 복원 시도"
install_extensions "$EXT_TXT"

if [[ -f "$SETTINGS_BAK" ]]; then
  mkdir -p "$(dirname "$WORKSPACE_SETTINGS")"
  echo "   -> settings.json 복원 중..."
  cp "$SETTINGS_BAK" "$WORKSPACE_SETTINGS"
fi

###############################################################################
# 1) uv (빠른 Python 패키지 관리자) 설치
###############################################################################
log "[1] uv 설치 시작"
# sudo 권한 여부에 따라 설치 위치를 다르게 합니다.
if sudo -n true 2>/dev/null; then
  echo "🗝️  sudo 사용 가능 → /usr/local/bin 에 설치합니다."
  curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
else
  echo "🔒 sudo 사용 불가 → \$HOME/.local/bin 에 설치합니다."
  mkdir -p "$HOME/.local/bin"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$HOME/.local/bin" sh
  # 현재 셸 및 향후 셸을 위해 PATH 설정
  if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
    export PATH="$HOME/.local/bin:$PATH"
    for RC in "$HOME/.bashrc" "$HOME/.zshrc"; do
      if [ -f "$RC" ]; then
        grep -qxF 'export PATH="$HOME/.local/bin:$PATH"' "$RC" 2>/dev/null \
          || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$RC"
      fi
    done
  fi
fi
echo "✅ uv 버전 확인:"; uv --version || { echo "❗ uv 인식 실패. PATH를 확인해주세요."; exit 1; }

###############################################################################
# 2) Python 3.12 설치 및 가상 환경 생성
###############################################################################
VENV_DIR="$PROJECT_DIR/.venv"
log "[2] Python 3.12 설치 및 가상 환경 설정: $VENV_DIR"

# 가상 환경이 없는 경우에만 새로 생성합니다.
if [[ ! -d "$VENV_DIR" ]]; then
  log "   -> Python 3.12 설치 중..."
  # uv가 시스템에 Python 3.12를 설치하도록 합니다.
  uv python install 3.12 --preview

  log "   -> '$VENV_DIR' 가상 환경 생성 중…"
  # uv venv 명령어는 현재 디렉토리를 기준으로 .venv를 생성하므로,
  # cd를 사용하여 프로젝트 디렉토리로 이동한 후 실행합니다.
  cd "$PROJECT_DIR"
  # 특정 Python 버전을 사용하여 가상 환경 생성
  uv venv -p 3.12
  cd "$REPO_ROOT" # 원래 디렉토리로 복귀
  log "   -> 가상 환경 생성 완료."

  # requirements.txt 파일이 있으면 라이브러리를 설치합니다.
  if [[ -f "$PROJECT_DIR/requirements.txt" ]]; then
    log "   -> requirements.txt 의존성 라이브러리 설치 중..."
    "$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"
    log "   -> 의존성 설치 완료."
  else
    log "   -> 'requirements.txt' 파일이 없어 라이브러리를 설치하지 않았습니다."
  fi
else
    log "   -> 이미 가상 환경이 존재하여 생성 단계를 건너뜁니다."
    # 이미 존재하는 경우, 라이브러리 업데이트를 원하면 아래 주석 해제
    # log "   -> 기존 가상 환경의 라이브러리를 업데이트합니다..."
    # "$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"
fi

###############################################################################
# 3) VSCode 인터프리터 설정
###############################################################################
log "[3] VSCode Python 인터프리터 기본 경로 설정"
mkdir -p "$(dirname "$WORKSPACE_SETTINGS")"

# --- 여기가 수정된 부분입니다 ---
TARGET_PYTHON="$PROJECT_DIR/.venv/bin/python"
echo "   -> 목표 파이썬 인터프리터 경로: $TARGET_PYTHON"

# .vscode/settings.json 파일이 없으면 빈 JSON 객체로 생성합니다.
if [[ ! -f "$WORKSPACE_SETTINGS" ]]; then
    echo "{}" > "$WORKSPACE_SETTINGS"
fi

# jq가 설치되어 있는지 확인 후, settings.json 파일에 인터프리터 경로를 추가/수정합니다.
if command -v jq &>/dev/null; then
  tmp_settings=$(mktemp)
  jq --arg p "$TARGET_PYTHON" \
     '. + { "python.defaultInterpreterPath": $p }' \
     "$WORKSPACE_SETTINGS" > "$tmp_settings" && mv "$tmp_settings" "$WORKSPACE_SETTINGS"
  echo "   -> jq를 사용하여 인터프리터 경로 설정 완료."
else
  # jq가 없을 경우 경고 메시지를 표시합니다. Codespace에는 보통 jq가 기본 설치되어 있습니다.
  echo "   -> ⚠️ 경고: jq 명령어가 없어 VSCode 설정을 자동으로 업데이트하지 못했습니다."
  echo "      수동으로 .vscode/settings.json 파일에 다음을 추가해주세요:"
  echo '      "python.defaultInterpreterPath": "'$TARGET_PYTHON'"'
fi

###############################################################################
# 4) 기타 설정 및 백업
###############################################################################
# .devcontainer/extensions.txt 파일이 있으면 해당 확장 프로그램을 설치합니다.
DEV_EXT="$REPO_ROOT/.devcontainer/extensions.txt"
install_extensions "$DEV_EXT"

log "[5] VSCode 현재 상태 백업"
mkdir -p "$BACKUP_DIR"

# 현재 설치된 확장 프로그램 목록을 저장합니다.
log "   -> 현재 확장 프로그램 목록을 '$EXT_TXT'에 저장합니다."
code --list-extensions > "$EXT_TXT"

# 현재 VSCode 설정(.vscode/settings.json)을 백업합니다.
if [[ -f "$WORKSPACE_SETTINGS" ]]; then
    log "   -> 현재 설정 파일을 '$SETTINGS_BAK'에 백업합니다."
    cp "$WORKSPACE_SETTINGS" "$SETTINGS_BAK"
fi

# 최종 완료 메시지
log "🎉 모든 환경 설정 스크립트(postCreate.sh) 실행이 완료되었습니다!"

