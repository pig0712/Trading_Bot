import os
import sys
from pathlib import Path
import time

import click
import pandas as pd
from dotenv import load_dotenv

# 봇의 GateIOClient를 가져오기 위한 경로 설정
ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if SRC_DIR.exists() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# .env 파일에서 API 키 로드
ENV_PATH = ROOT_DIR / "Trading_BOT/.env"
if not ENV_PATH.exists():
    ENV_PATH = ROOT_DIR / ".env"

if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
    print(f"✅ '.env' 파일 로드 완료: {ENV_PATH}")
else:
    print(f"⚠️ '.env' 파일을 찾을 수 없습니다.")
    sys.exit(1)

try:
    from trading_bot.exchange_gateio import GateIOClient
except ImportError:
    print("\n❌ 'trading_bot' 모듈을 찾을 수 없습니다.")
    sys.exit(1)


@click.command()
@click.option('--symbol', default="BTC_USDT", help="거래 내역을 조회할 코인 심볼")
def export_history(symbol: str):
    """Gate.io에서 나의 선물 거래 체결 내역을 조회하여 CSV 파일로 저장합니다."""
    
    click.secho(f"'{symbol}' 선물 거래 내역 조회를 시작합니다...", fg="green")
    
    try:
        gate_client = GateIOClient()
    except Exception as e:
        click.secho(f"❌ 클라이언트 초기화 실패: {e}", fg="red")
        sys.exit(1)

    all_trades = []
    limit = 100
    offset = 0
    total_fetched = 0
    filename = f"my_{symbol}_trade_history.csv"
    is_header_written = False

    with open(filename, 'w', newline='', encoding='utf-8') as f:
        click.echo("API를 통해 모든 거래 내역을 순차적으로 조회 후 파일에 기록합니다...")
        
        while True:
            try:
                trades = gate_client.futures_api.list_futures_trades(
                    settle='usdt',
                    contract=symbol,
                    limit=limit,
                    offset=offset
                )
                
                if not trades:
                    break 
                
                df = pd.DataFrame([trade.to_dict() for trade in trades])

                # ✅ 'role' 컬럼을 사용하지 않도록 수정
                # 사용 가능한 컬럼: create_time, contract, size, price, id, order_id 등
                df = df[['create_time', 'contract', 'size', 'price']]
                df.rename(columns={
                    'create_time': 'time',
                    'contract': 'symbol',
                    'size': 'quantity',
                }, inplace=True)
                
                df['time'] = pd.to_datetime(df['time'], unit='s')
                df['direction'] = df['quantity'].apply(lambda x: 'long' if x > 0 else 'short')

                if not is_header_written:
                    df.to_csv(f, index=False, header=True)
                    is_header_written = True
                else:
                    df.to_csv(f, index=False, header=False)

                total_fetched += len(trades)
                offset += limit
                
                click.echo(f"   -> 총 {total_fetched}개 거래 기록 완료.", nl=False)
                click.echo("\r", nl=False)
                
                time.sleep(0.2)

            except Exception as e:
                click.secho(f"\n❌ API 요청 중 오류 발생: {e}", fg="red")
                time.sleep(5)
    
    click.echo()
    if total_fetched == 0:
        click.secho("❌ 조회된 거래 내역이 없습니다.", fg="red")
    else:
        click.secho(f"\n✅ 데이터 저장이 완료되었습니다! 총 {total_fetched}개의 체결 기록.", fg="green")
        click.secho(f"   -> 파일명: {filename}", fg="cyan")


if __name__ == "__main__":
    export_history()