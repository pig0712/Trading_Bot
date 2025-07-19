import os
import sys
import json
from pathlib import Path

import click
from dotenv import load_dotenv

# 봇의 GateIOClient를 가져오기 위한 경로 설정
ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if SRC_DIR.exists() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# .env 파일에서 API 키 로드
ENV_PATH = ROOT_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
    print(f"✅ '.env' 파일 로드 완료: {ENV_PATH}")
else:
    print(f"⚠️ '.env' 파일을 찾을 수 없습니다. API 키가 환경 변수에 설정되어 있어야 합니다.")
    sys.exit(1)

try:
    from trading_bot.exchange_gateio import GateIOClient
except ImportError:
    print("\n❌ 'trading_bot' 모듈을 찾을 수 없습니다.")
    print("   이 스크립트가 프로젝트 최상위 폴더에 있는지, 'src' 폴더 구조가 올바른지 확인하세요.")
    sys.exit(1)


def pretty_print_json(data, title=""):
    """JSON(dict) 데이터를 예쁘게 출력하는 함수"""
    if title:
        click.secho(f"\n--- {title} ---", fg="yellow")
    
    if data is None:
        click.secho(" (데이터 없음 - None)", fg="red")
        return
        
    formatted_json = json.dumps(
        data, 
        indent=2, 
        ensure_ascii=False,
        default=str
    )
    click.echo(formatted_json)


@click.command()
@click.option(
    '--contract', '-c',
    default="BTC_USDT",
    show_default=True,
    help="조회할 선물 계약 심볼."
)
def main(contract: str):
    """Gate.io API의 주요 정보를 직접 조회하는 테스트 도구입니다."""
    click.secho("="*10 + f"  GATE.IO API 직접 조회 테스트 ({contract}) " + "="*10, bold=True)
    
    try:
        click.echo("\n1. Gate.io 클라이언트 초기화 시도...")
        gate_client = GateIOClient()
        click.secho("✅ 클라이언트 초기화 성공!", fg="green")
    except Exception as e:
        click.secho(f"❌ 클라이언트 초기화 실패: {e}", fg="red")
        sys.exit(1)

    # 2. 계좌 정보 조회
    try:
        account_info = gate_client.get_account_info()
        pretty_print_json(account_info, "계좌 정보 (FuturesAccount)")
    except Exception as e:
        click.secho(f"❌ 계좌 정보 조회 중 오류 발생: {e}", fg="red")
    
    # 3. 포지션 정보 조회 (일반 모드와 양방향 모드 모두 시도)
    try:
        position_info = gate_client.get_position(contract)
        pretty_print_json(position_info, f"{contract} 포지션 정보 (통합 조회 결과)")
    except Exception as e:
        click.secho(f"❌ {contract} 포지션 조회 중 오류 발생: {e}", fg="red")

    click.secho("\n" + "="*50, bold=True)
    click.secho("✅ 모든 테스트 완료.", fg="green")


if __name__ == "__main__":
    main()