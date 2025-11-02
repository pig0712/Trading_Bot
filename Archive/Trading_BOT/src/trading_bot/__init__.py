# src/trading_bot/__init__.py
"""
Trading_BOT 패키지 초기화 파일입니다.
이 파일은 'trading_bot' 디렉토리를 Python 패키지로 인식하도록 합니다.
필요에 따라 패키지 레벨의 초기화 코드나 __all__ 변수를 정의할 수 있습니다.
"""
import logging

# 패키지 로드 시 기본 로깅 핸들러 설정 (애플리케이션에서 덮어쓰지 않은 경우 대비)
# 라이브러리로 사용될 경우, NullHandler를 추가하여 로깅 메시지가 버려지도록 하는 것이 일반적.
# 애플리케이션 (main.py)에서 이미 로깅 설정을 하므로, 여기서는 특별한 설정 불필요.
# logging.getLogger(__name__).addHandler(logging.NullHandler())

# 패키지에서 공개할 모듈이나 클래스를 __all__ 리스트에 정의할 수 있습니다.
# 예: from trading_bot import BotConfig (이렇게 사용하려면 아래 __all__ 설정 필요)
# __all__ = [
#     "config",       # 모듈 자체
#     "BotConfig",    # config 모듈 내의 클래스 (이렇게 하려면 config.__init__에서 import 필요)
#     "prices",
#     "liquidation",
#     "cli",
#     "exchange_gateio",
#     "GateIOClient"  # exchange_gateio 모듈 내의 클래스
# ]

_LOG = logging.getLogger(__name__)
_LOG.debug("Trading_BOT package initialized.")

# 자주 사용되는 클래스들을 패키지 레벨로 끌어올려 사용 편의성 증진 (선택 사항)
# from .config import BotConfig
# from .exchange_gateio import GateIOClient
# from .cli import main as run_bot_cli # 예시: run_bot() 함수로 노출

