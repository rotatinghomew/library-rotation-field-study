"""原始资料的读取、引用校验与领域对象。

时间一律用 ISO 周一日（``YYYY-MM-DD``）表示；资料里的 day 是具体取书日，
week 是周一。所有跨表引用在这里集中校验，后续模块只面对校验过的对象。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def parse_day(value: str) -> date:
    """解析 ISO 日期。"""
    return date.fromisoformat(value)


def monday_of(value: date | str) -> date:
    """把任意日期归一到所属周的周一。"""
    if isinstance(value, str):
        value = parse_day(value)
    return value - timedelta(days=value.weekday())


def add_weeks(value: date, weeks: int) -> date:
    return value + timedelta(weeks=weeks)


def iso(value: date) -> str:
    return value.isoformat()


# ---------------------------------------------------------------------------
# 领域对象
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorkNeed:
    """一份作品需要：preferred 是首选作品，accepted 是明确可替的作品集合。"""

    preferred: str
    accepted: frozenset[str]

    def accepts(self, work: str) -> bool:
        return work in self.accepted


@dataclass(frozen=True)
class Copy:
    id: str
    work: str
    branch: str
    restricted: bool


@dataclass(frozen=True)
class Household:
    id: str
    home: str
    fixed_on: date


@dataclass(frozen=True)
class Request:
    """一份完整阅读请求。works 必须同时满足，缺任何一册都不算整套完成。"""

    id: str
    household: str
    opens: date
    deadline: date
    needs: tuple[WorkNeed, ...]
    pickup: frozenset[str]
    min_complete_floor: int

    @property
    def size(self) -> int:
        return len(self.needs)


@dataclass(frozen=True)
class Fulfillment:
    """馆方账上确认的实际到手记录，是借阅事实的最高优先级来源。"""

    id: str
    request_id: str
    copy_id: str
    day: date
    returned: date | None
    return_window: tuple[date, date] | None
    pickup: str
    received: date


@dataclass(frozen=True)
class LogEvent:
    week: date
    kind: str
    copy_id: str
    branch: str
    household: str | None
    receipt_week: date


@dataclass(frozen=True)
class SupplyNote:
    """续期 / 归还待核登记。只表达供给侧的占用或不确定窗，不改写馆方账。"""

    copy_id: str
    kind: str
    start: date | None
    until: date | None
    earliest: date | None
    latest: date | None
    received: date
    handler: str


@dataclass(frozen=True)
class Commitment:
    """已确认的承诺：读者预订或已签收轮转，轮转挪书不得违背。"""

    copy_id: str
    due: date
    kind: str
    destination: str
    household: str | None


@dataclass(frozen=True)
class Move:
    copy_id: str
    to_branch: str
    week: date


@dataclass(frozen=True)
class RotationOption:
    name: str
    moves: tuple[Move, ...]


@dataclass(frozen=True)
class Group:
    id: str
    branches: tuple[str, ...]
    assigned_week: date
    method: str
    eligible_neighbors: tuple[str, ...]


@dataclass
class Dataset:
    groups: tuple[Group, ...]
    households: tuple[Household, ...]
    copies: tuple[Copy, ...]
    logs: tuple[LogEvent, ...]
    requests: tuple[Request, ...]
    fulfillments: tuple[Fulfillment, ...]
    supply: tuple[SupplyNote, ...]
    commitments: tuple[Commitment, ...]
    floor: dict[str, int]
    coverage: dict[tuple[str, date], bool]
    options: tuple[RotationOption, ...]
    window_weeks: int
    weeks: tuple[date, ...] = field(default_factory=tuple)

    def copy(self, copy_id: str) -> Copy:
        return self._copies_by_id[copy_id]

    def request(self, request_id: str) -> Request:
        return self._requests_by_id[request_id]

    def household(self, household_id: str) -> Household:
        return self._households_by_id[household_id]

    def __post_init__(self) -> None:
        self._copies_by_id = {c.id: c for c in self.copies}
        self._requests_by_id = {r.id: r for r in self.requests}
        self._households_by_id = {h.id: h for h in self.households}


# ---------------------------------------------------------------------------
# 读取与校验
# ---------------------------------------------------------------------------

class DataValidationError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DataValidationError(message)


def load_dataset(folder: str | Path) -> Dataset:
    folder = Path(folder)
    raw: dict[str, Any] = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(folder.glob("*.json"))
    }

    copies = tuple(
        Copy(
            id=row["id"],
            work=row["work"],
            branch=row["branch"],
            restricted=bool(row["restricted"]),
        )
        for row in raw["copies"]
    )
    copy_ids = {c.id for c in copies}
    works = {c.work for c in copies}
    branches = {c.branch for c in copies}

    households = tuple(
        Household(
            id=row["id"],
            home=row["home"],
            fixed_on=parse_day(row["fixed_on"]),
        )
        for row in raw["households"]
    )
    household_ids = {h.id for h in households}
    _require(
        {h.home for h in households} <= branches,
        "住户所属馆超出副本所在馆集合",
    )

    requests = tuple(
        Request(
            id=row["id"],
            household=row["household"],
            opens=parse_day(row["opens_week"]),
            deadline=parse_day(row["deadline_week"]),
            needs=tuple(
                WorkNeed(
                    preferred=item["preferred"],
                    accepted=frozenset(item["accepted"]),
                )
                for item in row["works"]
            ),
            pickup=frozenset(row["pickup"]),
            min_complete_floor=int(row["min_complete_floor"]),
        )
        for row in raw["requests"]
    )
    request_ids = {r.id for r in requests}

    fulfillments = tuple(
        Fulfillment(
            id=row["id"],
            request_id=row["request"],
            copy_id=row["copy"],
            day=parse_day(row["day"]),
            returned=parse_day(row["returned"]) if row["returned"] else None,
            return_window=(
                (parse_day(row["return_window"][0]), parse_day(row["return_window"][1]))
                if row["return_window"]
                else None
            ),
            pickup=row["pickup"],
            received=parse_day(row["received"]),
        )
        for row in raw["fulfillments"]
    )

    logs = tuple(
        LogEvent(
            week=parse_day(row["week"]),
            kind=row["kind"],
            copy_id=row["copy"],
            branch=row["branch"],
            household=row.get("household"),
            receipt_week=parse_day(row["receipt_week"]),
        )
        for row in raw["logs"]
    )

    supply = tuple(
        SupplyNote(
            copy_id=row["copy"],
            kind=row["kind"],
            start=parse_day(row["start_week"]) if row.get("start_week") else None,
            until=parse_day(row["until_week"]) if row.get("until_week") else None,
            earliest=(
                parse_day(row["earliest_week"]) if row.get("earliest_week") else None
            ),
            latest=parse_day(row["latest_week"]) if row.get("latest_week") else None,
            received=parse_day(row["received_week"]),
            handler=row["handler"],
        )
        for row in raw["supply"]
    )

    commitments = tuple(
        Commitment(
            copy_id=row["copy"],
            due=parse_day(row["due"]),
            kind=row["kind"],
            destination=row["destination"],
            household=row.get("household"),
        )
        for row in raw["commitments"]
    )

    options = tuple(
        RotationOption(
            name=row["name"],
            moves=tuple(
                Move(
                    copy_id=m["copy"],
                    to_branch=m["to"],
                    week=parse_day(m["week"]),
                )
                for m in row["moves"]
            ),
        )
        for row in raw["options"]
    )

    groups = tuple(
        Group(
            id=row["id"],
            branches=tuple(row["branches"]),
            assigned_week=parse_day(row["assigned_week"]),
            method=row["method"],
            eligible_neighbors=tuple(row["eligible_neighbors"]),
        )
        for row in raw["groups"]
    )

    floor = dict(raw["floor"])
    coverage = {
        (row["branch"], parse_day(row["week"])): bool(row["complete"])
        for row in raw["coverage"]
    }
    window_weeks = int(raw["window_weeks"])

    _validate_references(
        copies=copy_ids,
        works=works,
        branches=branches,
        households=household_ids,
        requests=requests,
        request_ids=request_ids,
        fulfillments=fulfillments,
        logs=logs,
        supply=supply,
        commitments=commitments,
        options=options,
        floor=floor,
        coverage=coverage,
    )

    weeks = tuple(sorted({r.opens for r in requests}))
    return Dataset(
        groups=groups,
        households=households,
        copies=copies,
        logs=logs,
        requests=requests,
        fulfillments=fulfillments,
        supply=supply,
        commitments=commitments,
        floor=floor,
        coverage=coverage,
        options=options,
        window_weeks=window_weeks,
        weeks=weeks,
    )


def _validate_references(
    *,
    copies: set[str],
    works: set[str],
    branches: set[str],
    households: set[str],
    requests: tuple[Request, ...],
    request_ids: set[str],
    fulfillments: tuple[Fulfillment, ...],
    logs: tuple[LogEvent, ...],
    supply: tuple[SupplyNote, ...],
    commitments: tuple[Commitment, ...],
    options: tuple[RotationOption, ...],
    floor: dict[str, int],
    coverage: dict[tuple[str, date], bool],
) -> None:
    # 请求内部规则
    for req in requests:
        _require(req.opens < req.deadline, f"请求 {req.id} 开放日不早于截止日")
        _require(req.household in households, f"请求 {req.id} 引用未知住户")
        _require(len(req.pickup) >= 1, f"请求 {req.id} 没有取书范围")
        _require(req.pickup <= branches, f"请求 {req.id} 取书馆越界")
        for need in req.needs:
            _require(
                need.preferred in need.accepted,
                f"请求 {req.id} 首选作品不在可接受集合",
            )
            _require(need.accepted <= works, f"请求 {req.id} 接受了不存在的作品")

    # 馆方账引用与窗口
    for ful in fulfillments:
        _require(ful.request_id in request_ids, f"到手记录引用未知请求 {ful.request_id}")
        _require(ful.copy_id in copies, f"到手记录引用未知副本 {ful.copy_id}")
        req = next(r for r in requests if r.id == ful.request_id)
        _require(
            req.opens <= monday_of(ful.day) < req.deadline,
            f"到手记录 {ful.id} 不在请求周期内",
        )
        _require(ful.pickup in req.pickup, f"到手记录 {ful.id} 取书馆超出登记范围")
        if ful.return_window is not None:
            _require(
                ful.returned is None,
                f"到手记录 {ful.id} 同时有确认归还与待核窗",
            )
            _require(
                ful.return_window[0] <= ful.return_window[1],
                f"到手记录 {ful.id} 归还窗上下界颠倒",
            )

    # 过程日志引用
    for event in logs:
        _require(event.copy_id in copies, f"日志引用未知副本 {event.copy_id}")
        _require(event.branch in branches, f"日志引用未知馆 {event.branch}")
        if event.household is not None:
            _require(
                event.household in households,
                f"日志引用未知住户 {event.household}",
            )

    for note in supply:
        _require(note.copy_id in copies, f"供给登记引用未知副本 {note.copy_id}")
        if note.kind == "续期":
            _require(note.start is not None and note.until is not None, note.copy_id)
        elif note.kind == "归还待核":
            _require(
                note.earliest is not None and note.latest is not None, note.copy_id
            )
            _require(note.earliest <= note.latest, note.copy_id)

    for commitment in commitments:
        _require(
            commitment.copy_id in copies,
            f"承诺引用未知副本 {commitment.copy_id}",
        )
        _require(commitment.destination in branches, "承诺目的地馆越界")

    for option in options:
        for move in option.moves:
            _require(move.copy_id in copies, f"轮转组合 {option.name} 引用未知副本")
            _require(move.to_branch in branches, f"轮转组合 {option.name} 目的地越界")

    _require(set(floor) == branches, "floor 未覆盖全部馆")
    _require(
        {branch for branch, _ in coverage} == branches,
        "coverage 未覆盖全部馆",
    )
