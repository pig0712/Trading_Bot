# src/trading_bot/config.py
"""봇 설정 관리를 위한 Dataclass 및 JSON 직렬화/역직렬화."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List, Literal, Optional

_LOG = logging.getLogger(__name__)

@dataclass
class BotConfig:
    """
    트레이딩 봇의 모든 설정을 담는 데이터 클래스입니다.
    JSON 파일로 저장하거나 불러올 수 있으며, 생성 시 유효성 검사를 수행합니다.
    """
    # ───────── 필수 거래 설정 ─────────
    direction: Literal["long", "short"]
    symbol: str
    leverage: int
    margin_mode: Literal["cross", "isolated"]

    # --- 여기가 수정된 부분입니다: 고정 금액 -> 비율(%)로 변경 ---
    # ───────── 자금 및 분할매수 설정 (비율 기반) ─────────
    entry_amount_pct_of_balance: float  # 첫 진입 시 사용할 자산 비율 (%)
    max_split_count: int
    
    # 각 분할매수 트리거 가격 변동률 (평균 단가 대비 %)
    split_trigger_percents: List[float] = field(default_factory=list)
    
    # 각 분할매수 시 추가 진입할 자산 비율 (%)
    split_amounts_pct_of_balance: List[float] = field(default_factory=list)

    # ───────── 익절 및 손절 설정 ─────────
    take_profit_pct: Optional[float] = None
    stop_loss_pct: Optional[float] = None

    # ───────── 주문 관련 설정 ─────────
    order_type: Literal["market", "limit"] = "market"
    limit_order_slippage_pct: float = 0.05

    # ───────── 봇 운영 관련 선택 설정 ─────────
    repeat_after_take_profit: bool = False
    stop_bot_after_stop_loss: bool = True
    enable_stop_loss: bool = True
    check_interval_seconds: int = 60
    order_id_prefix: str = "t-tradingbot-"

    def __post_init__(self):
        """설정값 유효성 검사 로직."""
        errors = []
        if self.leverage <= 0:
            errors.append("레버리지(leverage)는 0보다 커야 합니다.")
        
        # --- 여기가 수정된 부분입니다: 비율에 대한 유효성 검사 ---
        if not (0 < self.entry_amount_pct_of_balance <= 100):
            errors.append("첫 진입 금액 비율(entry_amount_pct_of_balance)은 0보다 크고 100 이하여야 합니다.")
        
        if self.max_split_count < 0:
            errors.append("최대 분할매수 횟수(max_split_count)는 0 이상이어야 합니다.")
        
        if self.max_split_count > 0:
            if len(self.split_trigger_percents) != self.max_split_count:
                errors.append(f"분할매수 트리거 퍼센트 리스트의 길이가 횟수({self.max_split_count})와 일치해야 합니다.")
            else:
                if self.direction == "long" and any(p >= 0 for p in self.split_trigger_percents):
                    errors.append("롱 포지션의 분할매수 트리거 퍼센트는 모두 음수여야 합니다.")
                if self.direction == "short" and any(p <= 0 for p in self.split_trigger_percents):
                    errors.append("숏 포지션의 분할매수 트리거 퍼센트는 모두 양수여야 합니다.")

            if len(self.split_amounts_pct_of_balance) != self.max_split_count:
                errors.append(f"분할매수 금액 비율 리스트의 길이가 횟수({self.max_split_count})와 일치해야 합니다.")
            elif any(not (0 < pct <= 100) for pct in self.split_amounts_pct_of_balance):
                errors.append("분할매수 금액 비율은 모두 0보다 크고 100 이하여야 합니다.")
        
        if self.take_profit_pct is not None and self.take_profit_pct <= 0:
            errors.append("익절 퍼센트는 0보다 커야 합니다.")
        if self.stop_loss_pct is not None and self.stop_loss_pct <= 0:
            errors.append("손절 퍼센트는 0보다 커야 합니다.")
        
        if self.check_interval_seconds <= 0:
            errors.append("확인 간격은 0보다 커야 합니다.")
        if not self.order_id_prefix.startswith("t-"):
            self.order_id_prefix = "t-" + self.order_id_prefix.lstrip("t-")

        if self.limit_order_slippage_pct < 0:
            errors.append("지정가 슬리피지 퍼센트는 0 이상이어야 합니다.")

        if errors:
            error_message = "잘못된 설정 값:\n" + "\n".join([f"  - {err}" for err in errors])
            _LOG.error(error_message)
            raise ValueError(error_message)
        _LOG.debug("BotConfig validation successful.")

    @classmethod
    def from_dict(cls, data: dict) -> BotConfig:
        return cls(**data)

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, file_path: str | Path) -> None:
        path_obj = Path(file_path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        _LOG.info(f"BotConfig를 다음 경로에 저장 시도: {path_obj.resolve()}")
        try:
            with open(path_obj, 'w', encoding='utf-8') as f:
                json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
            _LOG.info(f"설정이 성공적으로 저장되었습니다: {path_obj.resolve()}")
        except Exception as e:
            _LOG.error(f"설정 파일 저장 실패 ('{path_obj}'): {e}", exc_info=True)
            raise

    @classmethod
    def load(cls, file_path: str | Path) -> BotConfig:
        path_obj = Path(file_path)
        _LOG.info(f"다음 경로에서 BotConfig 로드 시도: {path_obj.resolve()}")
        if not path_obj.exists():
            raise FileNotFoundError(f"설정 파일 없음: {path_obj.resolve()}")
        try:
            with open(path_obj, 'r', encoding='utf-8') as f:
                data = json.load(f)
            _LOG.info(f"설정을 성공적으로 불러왔습니다: {path_obj.resolve()}")
            return cls.from_dict(data)
        except Exception as e:
            _LOG.error(f"설정 파일 불러오기 실패 ('{path_obj}'): {e}", exc_info=True)
            raise
