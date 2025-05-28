# src/trading_bot/config.py
"""봇 설정 관리를 위한 Dataclass 및 JSON 직렬화/역직렬화."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict, field # field 임포트 추가
from pathlib import Path
from typing import List, Literal, Optional # Literal 임포트 추가

_LOG = logging.getLogger(__name__)

@dataclass
class BotConfig:
    """
    트레이딩 봇의 모든 설정을 담는 데이터 클래스입니다.
    JSON 파일로 저장하거나 불러올 수 있으며, 생성 시 유효성 검사를 수행합니다.
    """
    # ───────── 필수 거래 설정 ─────────
    direction: Literal["long", "short"]
    symbol: str  # 예: "BTC_USDT"
    leverage: int  # 예: 5, 10, 20 (양수여야 함)
    margin_mode: Literal["cross", "isolated"]

    # ───────── 자금 및 분할매수 설정 ─────────
    entry_amount_usd: float  # 첫 진입 시 사용할 금액 (USD 기준, 양수여야 함)
    max_split_count: int     # 최대 분할매수 횟수 (0 이상이어야 함)
    
    # split_trigger_percents: 각 분할매수 트리거 가격 변동률 (평균 단가 대비 %)
    # - 롱 포지션: 음수 값 (예: -1.0, -2.0) -> 가격 하락 시 분할매수
    # - 숏 포지션: 양수 값 (예: 1.0, 2.0) -> 가격 상승 시 분할매수
    # 리스트의 길이는 max_split_count와 일치해야 함.
    split_trigger_percents: List[float] = field(default_factory=list)
    
    # split_amounts_usd: 각 분할매수 시 추가 진입할 금액 (USD 기준, 양수여야 함)
    # 리스트의 길이는 max_split_count와 일치해야 함.
    split_amounts_usd: List[float] = field(default_factory=list)

    # ───────── 익절 및 손절 설정 ─────────
    # take_profit_pct: 익절 실행할 수익률 (평균 진입가 대비 %, 양수여야 함). None이면 익절 안 함.
    take_profit_pct: Optional[float] = None
    # stop_loss_pct: 손절 실행할 손실률 (평균 진입가 대비 %, 양수여야 함). None이면 손절 안 함 (단, enable_stop_loss도 확인).
    stop_loss_pct: Optional[float] = None

    # ───────── 주문 관련 설정 ─────────
    order_type: Literal["market", "limit"] = "market" # 주문 유형
    # limit_order_slippage_pct: 지정가 주문 시, 계산된 트리거 가격 대비 슬리피지 (%).
    # 예: 0.05% -> 롱 진입 시 트리거 가격보다 0.05% 낮은 가격에, 숏 진입 시 0.05% 높은 가격에 지정가 주문. (0 이상이어야 함)
    limit_order_slippage_pct: float = 0.05

    # ───────── 봇 운영 관련 선택 설정 ─────────
    repeat_after_take_profit: bool = False # 익절 후 동일 설정으로 자동 반복 실행 여부
    stop_bot_after_stop_loss: bool = True  # 손절 발생 시 봇 자동 중지 여부
    enable_stop_loss: bool = True          # 손절 기능 전체 활성화/비활성화
    
    check_interval_seconds: int = 60       # 가격 및 전략 확인 주기 (초 단위, 양수여야 함)
    order_id_prefix: str = "t-tradingbot-" # 사용자 정의 주문 ID 접두사 (Gate.io는 "t-" 시작 권장)

    def __post_init__(self):
        """
        객체 생성 후 설정값에 대한 유효성 검사를 수행합니다.
        잘못된 설정값이 있으면 ValueError를 발생시킵니다.
        """
        errors = []
        if not isinstance(self.leverage, int) or self.leverage <= 0:
            errors.append("레버리지(leverage)는 0보다 큰 정수여야 합니다.")
        if not isinstance(self.entry_amount_usd, (int, float)) or self.entry_amount_usd <= 0:
            errors.append("첫 진입 금액(entry_amount_usd)은 0보다 큰 숫자여야 합니다.")
        if not isinstance(self.max_split_count, int) or self.max_split_count < 0:
            errors.append("최대 분할매수 횟수(max_split_count)는 0 이상의 정수여야 합니다.")
        
        if self.max_split_count > 0:
            if not isinstance(self.split_trigger_percents, list) or len(self.split_trigger_percents) != self.max_split_count:
                errors.append(f"분할매수 트리거 퍼센트(split_trigger_percents) 리스트의 길이는 "
                              f"최대 분할매수 횟수({self.max_split_count})와 일치해야 합니다.")
            else:
                for i, p_val in enumerate(self.split_trigger_percents):
                    if not isinstance(p_val, (int, float)):
                        errors.append(f"분할매수 트리거 퍼센트 {i+1}번째 값 '{p_val}'은 숫자여야 합니다.")
                    elif self.direction == "long" and p_val >= 0:
                        errors.append(f"롱 포지션의 분할매수 트리거 퍼센트 {i+1}번째 값({p_val})은 음수여야 합니다 (예: -1.0).")
                    elif self.direction == "short" and p_val <= 0:
                        errors.append(f"숏 포지션의 분할매수 트리거 퍼센트 {i+1}번째 값({p_val})은 양수여야 합니다 (예: 1.0).")

            if not isinstance(self.split_amounts_usd, list) or len(self.split_amounts_usd) != self.max_split_count:
                errors.append(f"분할매수 금액(split_amounts_usd) 리스트의 길이는 "
                              f"최대 분할매수 횟수({self.max_split_count})와 일치해야 합니다.")
            elif any(not isinstance(amt, (int, float)) or amt <= 0 for amt in self.split_amounts_usd):
                errors.append("분할매수 금액(split_amounts_usd)은 모두 0보다 큰 숫자여야 합니다.")
        
        if self.take_profit_pct is not None and (not isinstance(self.take_profit_pct, (int, float)) or self.take_profit_pct <= 0):
            errors.append("익절 퍼센트(take_profit_pct)는 0보다 큰 숫자여야 합니다 (설정 시).")
        if self.stop_loss_pct is not None and (not isinstance(self.stop_loss_pct, (int, float)) or self.stop_loss_pct <= 0):
            errors.append("손절 퍼센트(stop_loss_pct)는 0보다 큰 숫자여야 합니다 (설정 시).")
        
        if not isinstance(self.check_interval_seconds, int) or self.check_interval_seconds <= 0:
            errors.append("확인 간격(check_interval_seconds)은 0보다 큰 정수여야 합니다.")
        
        if not isinstance(self.order_id_prefix, str) or not self.order_id_prefix.startswith("t-"):
            # Gate.io API는 사용자 정의 ID (text 필드)에 "t-" 접두사를 요구합니다.
            _LOG.warning(f"주문 ID 접두사(order_id_prefix='{self.order_id_prefix}')가 't-'로 시작하지 않습니다. "
                         "Gate.io API 요구사항에 맞지 않을 수 있습니다. 자동으로 't-'를 추가합니다.")
            self.order_id_prefix = "t-" + self.order_id_prefix.lstrip("t-")


        if not isinstance(self.limit_order_slippage_pct, (int, float)) or self.limit_order_slippage_pct < 0:
            errors.append("지정가 슬리피지 퍼센트(limit_order_slippage_pct)는 0 이상이어야 합니다.")

        if errors:
            error_message = "잘못된 설정 값으로 BotConfig 생성 실패:\n" + "\n".join([f"  - {err}" for err in errors])
            _LOG.error(error_message)
            raise ValueError(error_message)
        
        _LOG.debug("BotConfig 객체 유효성 검사 성공.")

    @classmethod
    def from_dict(cls, data: dict) -> BotConfig:
        """딕셔너리에서 BotConfig 객체를 생성합니다."""
        _LOG.debug(f"딕셔너리로부터 BotConfig 객체 생성 시도: {data}")
        try:
            # 누락된 Optional 필드에 대해 기본값을 사용하도록 처리 (dataclass가 이미 잘 처리함)
            # 예를 들어, JSON 파일에 take_profit_pct가 없으면 None으로 설정됨.
            return cls(**data)
        except TypeError as e:
            # 필드 누락 또는 타입 불일치 시 발생
            _LOG.error(f"설정 데이터로부터 객체 생성 중 TypeError 발생: {data}. "
                         f"필요한 필드가 없거나 타입이 다를 수 있습니다. 상세 오류: {e}", exc_info=True)
            raise ValueError(f"설정 데이터 구조 또는 타입 오류: {e}")
        except ValueError as e: # __post_init__에서 발생한 유효성 검사 오류
            _LOG.error(f"딕셔너리 데이터 유효성 검사 실패: {e}", exc_info=True)
            raise # 원본 예외를 다시 발생

    def to_dict(self) -> dict:
        """BotConfig 객체를 딕셔너리로 변환합니다."""
        return asdict(self)

    def save(self, file_path: str | Path) -> None:
        """BotConfig 객체를 JSON 파일로 저장합니다."""
        path_obj = Path(file_path)
        # 파일 저장 전 상위 디렉토리 생성 (없으면)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        _LOG.info(f"BotConfig를 다음 경로에 저장 시도: {path_obj.resolve()}")
        try:
            with open(path_obj, 'w', encoding='utf-8') as f:
                json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
            _LOG.info(f"설정이 성공적으로 저장되었습니다: {path_obj.resolve()}")
        except IOError as e:
            _LOG.error(f"설정 파일 저장 중 IO 오류 발생 ('{path_obj}'): {e}", exc_info=True)
            raise # 호출한 쪽에서 처리하도록 예외를 다시 발생
        except Exception as e: # 기타 예외 (예: json 직렬화 실패 등)
            _LOG.error(f"알 수 없는 오류로 설정 파일 저장 실패 ('{path_obj}'): {e}", exc_info=True)
            raise

    @classmethod
    def load(cls, file_path: str | Path) -> BotConfig:
        """JSON 파일에서 BotConfig 객체를 불러옵니다."""
        path_obj = Path(file_path)
        _LOG.info(f"다음 경로에서 BotConfig 로드 시도: {path_obj.resolve()}")
        if not path_obj.exists():
            _LOG.error(f"설정 파일을 찾을 수 없습니다: {path_obj.resolve()}")
            raise FileNotFoundError(f"설정 파일 없음: {path_obj.resolve()}")
        try:
            with open(path_obj, 'r', encoding='utf-8') as f:
                data = json.load(f)
            _LOG.info(f"설정을 성공적으로 불러왔습니다: {path_obj.resolve()}")
            return cls.from_dict(data) # from_dict를 통해 유효성 검사 포함 객체 생성
        except json.JSONDecodeError as e:
            _LOG.error(f"설정 파일 JSON 파싱 오류 ('{path_obj}'): {e}", exc_info=True)
            raise ValueError(f"잘못된 JSON 형식의 설정 파일 '{path_obj}': {e}")
        except IOError as e:
            _LOG.error(f"설정 파일 읽기 중 IO 오류 발생 ('{path_obj}'): {e}", exc_info=True)
            raise
        except ValueError as e: # from_dict 또는 __post_init__에서 발생한 ValueError
            _LOG.error(f"설정 파일 내용 유효성 검사 실패 ('{path_obj}'): {e}", exc_info=True)
            raise # 원본 예외를 다시 발생시켜 호출자가 상세 내용을 알 수 있도록 함
        except Exception as e: # 기타 예상치 못한 예외
            _LOG.error(f"알 수 없는 오류로 설정 파일 불러오기 실패 ('{path_obj}'): {e}", exc_info=True)
            raise
