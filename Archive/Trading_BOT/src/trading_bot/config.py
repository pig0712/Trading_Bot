"""봇 설정 관리를 위한 Dataclass 및 JSON 직렬화/역직렬화."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict, field, fields
from pathlib import Path
from typing import List, Literal, Optional

_LOG = logging.getLogger(__name__)

@dataclass
class BotConfig:
    """
    트레이딩 봇의 모든 설정을 담는 데이터 클래스입니다.
    """
    # ───────── 필수 거래 설정 (기본값 없는 필드를 위로) ─────────
    direction: Literal["long", "short"]
    symbol: str
    leverage: int
    margin_mode: Literal["cross", "isolated"]
    entry_amount_pct_of_balance: float
    max_split_count: int
    
    # ───────── 선택 설정 (기본값 있는 필드는 아래로) ─────────
    split_trigger_percents: List[float] = field(default_factory=list)
    split_amounts_pct_of_balance: List[float] = field(default_factory=list)
    take_profit_pct: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    
    trailing_take_profit_trigger_pct: Optional[float] = None
    trailing_take_profit_offset_pct: Optional[float] = None
    
    order_type: Literal["market", "limit"] = "market"
    limit_order_slippage_pct: float = 0.05
    repeat_after_take_profit: bool = False
    stop_bot_after_stop_loss: bool = True
    enable_stop_loss: bool = True
    check_interval_seconds: int = 10
    order_id_prefix: str = "t-tradingbot-"
    
    auto_determine_direction: bool = False
    enable_pyramiding: bool = False
    pyramiding_max_count: int = 0
    pyramiding_trigger_percents: List[float] = field(default_factory=list)
    pyramiding_amounts_pct_of_balance: List[float] = field(default_factory=list)

    # ⬇️ --- 이 아래의 모든 함수들이 클래스에 포함되도록 들여쓰기합니다. --- ⬇️

    def __post_init__(self):
        """설정값 유효성 검사 로직."""
        errors = []
        if self.leverage <= 0:
            errors.append("레버리지(leverage)는 0보다 커야 합니다.")
        
        if not (0 < self.entry_amount_pct_of_balance <= 100):
            errors.append("첫 진입 금액 비율(entry_amount_pct_of_balance)은 0보다 크고 100 이하여야 합니다.")
        
        # --- 분할매수(물타기) 유효성 검사 ---
        if self.max_split_count < 0:
            errors.append("최대 분할매수 횟수(max_split_count)는 0 이상이어야 합니다.")
        elif self.max_split_count > 0:
            if len(self.split_trigger_percents) != self.max_split_count:
                errors.append(f"분할매수 트리거 퍼센트 리스트의 길이가 횟수({self.max_split_count})와 일치해야 합니다.")
            elif any(p >= 0 for p in self.split_trigger_percents):
                errors.append("분할매수 트리거 퍼센트는 모두 0보다 작은 음수여야 합니다 (예: -2.5).")

            if len(self.split_amounts_pct_of_balance) != self.max_split_count:
                errors.append(f"분할매수 금액 비율 리스트의 길이가 횟수({self.max_split_count})와 일치해야 합니다.")
            elif any(not (0 < pct <= 100) for pct in self.split_amounts_pct_of_balance):
                errors.append("분할매수 금액 비율은 모두 0보다 크고 100 이하여야 합니다.")
        
        # --- 피라미딩(불타기) 유효성 검사 ---
        if self.enable_pyramiding:
            if self.pyramiding_max_count <= 0:
                errors.append("피라미딩 횟수(pyramiding_max_count)는 0보다 커야 합니다.")
            
            if len(self.pyramiding_trigger_percents) != self.pyramiding_max_count:
                errors.append(f"피라미딩 트리거 퍼센트 리스트 길이가 횟수({self.pyramiding_max_count})와 일치해야 합니다.")
            elif any(p <= 0 for p in self.pyramiding_trigger_percents):
                errors.append("피라미딩 트리거 퍼센트는 모두 0보다 큰 양수여야 합니다 (예: 2.5).")
            
            if len(self.pyramiding_amounts_pct_of_balance) != self.pyramiding_max_count:
                errors.append(f"피라미딩 금액 비율 리스트 길이가 횟수({self.pyramiding_max_count})와 일치해야 합니다.")
            elif any(not (0 < pct <= 100) for pct in self.pyramiding_amounts_pct_of_balance):
                errors.append("피라미딩 금액 비율은 모두 0보다 크고 100 이하여야 합니다.")

        # --- 청산 전략 유효성 검사 ---
        if self.take_profit_pct is not None and self.take_profit_pct <= 0:
            errors.append("일반 익절 퍼센트는 0보다 커야 합니다.")
        if self.stop_loss_pct is not None and self.stop_loss_pct <= 0:
            errors.append("손절 퍼센트는 0보다 커야 합니다.")

        if self.trailing_take_profit_trigger_pct is not None and self.trailing_take_profit_trigger_pct <= 0:
            errors.append("추적 익절 트리거 수익률은 0보다 커야 합니다.")
        if self.trailing_take_profit_offset_pct is not None and self.trailing_take_profit_offset_pct <= 0:
            errors.append("추적 익절 하락분(offset)은 0보다 커야 합니다.")

        # --- 기타 설정 유효성 검사 ---
        if self.check_interval_seconds <= 0:
            errors.append("확인 간격은 0보다 커야 합니다.")

        if errors:
            error_message = "잘못된 설정 값:\n" + "\n".join([f"  - {err}" for err in errors])
            _LOG.error(error_message)
            raise ValueError(error_message)
        _LOG.debug("BotConfig validation successful.")

    def to_dict(self) -> dict:
        """데이터 클래스를 딕셔너리로 변환합니다."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BotConfig":
        """딕셔너리에서 데이터 클래스 객체를 생성합니다."""
        config_fields = {f.name for f in fields(cls)}
        filtered_data = {k: v for k, v in data.items() if k in config_fields}
        return cls(**filtered_data)

    def save(self, file_path: str | Path) -> None:
        """현재 설정을 JSON 파일로 저장합니다."""
        path_obj = Path(file_path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path_obj, 'w', encoding='utf-8') as f:
                json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
            _LOG.info(f"설정이 성공적으로 저장되었습니다: {path_obj.resolve()}")
        except Exception as e:
            _LOG.error(f"설정 파일 저장 실패 ('{path_obj}'): {e}", exc_info=True)
            raise

    @classmethod
    def load(cls, file_path: str | Path) -> "BotConfig":
        """JSON 파일에서 설정을 불러옵니다."""
        path_obj = Path(file_path)
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