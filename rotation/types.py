"""模拟与事实还原共用的结果类型与结果枚举。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class Outcome(str, Enum):
    COMPLETE = "整套完成"
    PARTIAL = "部分满足"
    NONE = "未到手"


@dataclass(frozen=True)
class Assignment:
    request_id: str
    need_index: int
    copy_id: str
    work: str
    branch: str
    is_substitute: bool
    is_neighbor_pickup: bool
    day: date
    counterfactual: bool = False  # True=反事实增量，False=馆方账事实


@dataclass
class RequestResult:
    request_id: str
    outcome: Outcome
    assignments: tuple[Assignment, ...]
    satisfied_needs: int
    total_needs: int
    missing_needs: tuple[int, ...]
    uncertain: bool
    observed_complete: bool       # 馆方账是否已证实整套完成
    counterfactual_gain: bool     # 是否靠反事实分派才凑齐

    @property
    def served_at_all(self) -> bool:
        return self.outcome is not Outcome.NONE
