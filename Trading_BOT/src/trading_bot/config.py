"""설정 ✨ dataclass + JSON 직렬화/역직렬화."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List


@dataclass
class BotConfig:
    # ───────── 필수 설정 ─────────
    direction: str               # "long" | "short"
    symbol: str                  # 예) "BTC_USDT"
    leverage: int
    margin_mode: str             # "cross" | "isolated"
    entry_amount: float
    split_trigger_percents: List[float]
    split_amounts: List[float]
    take_profit_pct: float
    stop_loss_pct: float
    order_type: str              # "market" | "limit"
    max_split_count: int
    # ───────── 선택 설정 ─────────
    repeat_after_take_profit: bool = False
    stop_after_loss: bool = True
    enable_stop_loss: bool = True

    # ───── 직렬화 / 역직렬화 ─────
    @classmethod
    def from_dict(cls, data: dict) -> "BotConfig":
        return cls(**data)

    def to_dict(self) -> dict:
        # 내부에 다른 dataclass가 와도 안전하도록 asdict 사용
        return asdict(self)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False))

    @classmethod
    def load(cls, path: str | Path) -> "BotConfig":
        return cls.from_dict(json.loads(Path(path).read_text()))
