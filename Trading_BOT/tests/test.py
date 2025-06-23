# test_debug.py (이 이름으로 저장하여 실행해보세요)
import os
import gate_api
import json # JSON 출력을 위해 추가
from dotenv import load_dotenv
from gate_api.exceptions import ApiException

def debug_futures_account_object():
    """
    Gate.io 선물 계좌 정보를 가져와, 객체가 가진 모든 데이터를 출력하여 구조를 확인합니다.
    """

    # 1. .env 파일에서 환경 변수 로드
    load_dotenv()

    # 2. API 키 및 시크릿 로드
    api_key = os.getenv("GATE_API_KEY")
    api_secret = os.getenv("GATE_API_SECRET")

    if not api_key or not api_secret:
        print("🚨 오류: .env 파일에 GATE_API_KEY와 GATE_API_SECRET을 설정해주세요.")
        return

    # 3. Gate.io API 클라이언트 설정
    configuration = gate_api.Configuration(
        host="https://api.gateio.ws/api/v4",
        key=api_key,
        secret=api_secret
    )
    api_client = gate_api.ApiClient(configuration)
    futures_api = gate_api.FuturesApi(api_client)

    print("🔄 Gate.io USDT 무기한 선물 계좌 객체 구조를 확인합니다...")

    try:
        # 4. 선물 계좌 정보 조회
        settle_currency = 'usdt'
        futures_account = futures_api.list_futures_accounts(settle=settle_currency)

        # 5. 객체를 딕셔너리로 변환하여 모든 데이터 확인
        # to_dict() 메소드는 객체의 모든 속성을 key-value 형태로 변환해줍니다.
        account_data = futures_account.to_dict()

        print("\n" + "="*15 + " 🔍 API 응답 객체 상세 정보 " + "="*15)
        # json.dumps를 사용하면 딕셔너리를 보기 좋게 출력할 수 있습니다.
        print(json.dumps(account_data, indent=2, ensure_ascii=False))
        print("="*55 + "\n")
        
        print("✅ 위에 출력된 내용을 확인하여 'user' 또는 'user_id'에 해당하는 정확한 키(key) 이름을 찾아보세요.")
        print("   예를 들어, 'uid' 또는 'user'와 같이 다른 이름일 수 있습니다.")


    except ApiException as e:
        print(f"❌ API 오류 발생: Status {e.status}, Reason: {e.reason}")
        print(f"Body: {e.body}")

    except Exception as e:
        print(f"❌ 알 수 없는 오류 발생: {e}")

if __name__ == "__main__":
    debug_futures_account_object()
