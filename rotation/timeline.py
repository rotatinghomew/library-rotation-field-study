"""把馆方账与过程资料还原成每个副本的逐周可用性。

事实优先级
==========
1. ``fulfillments`` 是实际到手的馆方账，是借阅事实的最高来源。
2. ``logs`` 是在架、预订、调拨、退出等过程资料，用于解释机会与候补经过。
3. ``supply`` 只登记续期占用与归还待核窗，不新增借阅、不改写馆方账。

不确定归还的处理
================
一份借阅没有确认归还日（``returned is None``）时，只能查明归还窗
``[earliest, latest]``。时间线同时给出两种口径：

- 保守口径（pessimistic）：副本在 *最迟* 归还时刻之前都不可再派；
- 乐观口径（optimistic）：副本在 *最早* 归还时刻起就可再派。

任何统计都不得把同一个副本在窗口内同时算给两家，因此模拟分派在两种
口径下分别运行，结果区间即可辨认的影响范围，而不是凭空点估计。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .data_model import (
    Dataset,
    Fulfillment,
    monday_of,
)

# 过程日志的事件类型
EVENT_SHELF = "到架"
EVENT_DELAY = "调拨延后"
EVENT_HOLD = "预订"
EVENT_NEIGHBOR_PICKUP = "馆际代取"
EVENT_WITHDRAW = "退出"


@dataclass(frozen=True)
class LoanSpan:
    """一次可查明的占用：从取书日到可再上架的边界。"""

    fulfillment_id: str
    request_id: str
    household: str
    pickup_day: date
    pickup_week: date
    confirmed_return: date | None
    earliest_release: date   # 可查明的最早再可用日（含）
    latest_release: date     # 可查明的最迟再可用日（含）
    pickup_branch: str
    uncertain: bool


@dataclass
class CopyTimeline:
    copy_id: str
    work: str
    home_branch: str
    restricted: bool
    loans: tuple[LoanSpan, ...] = ()
    # 续期登记：[start, until] 内副本被原借用人占用，不能排进下一期推荐
    renewals: tuple[tuple[date, date], ...] = ()
    # 归还待核登记的窗口（与借阅窗互相印证，不重复计数）
    pending_windows: tuple[tuple[date, date, date], ...] = ()
    # 到架 / 调拨事件：week -> (branch, 实际到架周)
    movements: tuple[tuple[date, str, date], ...] = ()

    def release_for(self, loan: LoanSpan, optimistic: bool) -> date:
        return loan.earliest_release if optimistic else loan.latest_release

    def is_busy(self, on_day: date, *, optimistic: bool) -> bool:
        """该日副本是否仍被占用（取走且未到再可用时刻）。

        判定细化到取件窗的具体日期：同一周周一先被借走的副本，周二对
        其他家庭即不可再派，避免在同一窗口把同一副本算给两家。
        """
        for loan in self.loans:
            release = self.release_for(loan, optimistic)
            if loan.pickup_day <= on_day < release:
                return True
        for start, until in self.renewals:
            if start <= monday_of(on_day) <= until:
                return True
        return False

    def available_at(self, on_day: date, *, optimistic: bool) -> bool:
        return not self.is_busy(on_day, optimistic=optimistic)


@dataclass
class TimelineBundle:
    timelines: dict[str, CopyTimeline]
    # 资料不完整的馆-周：该处的在架判断只能给范围，不能给确定结论
    incomplete_cells: frozenset[tuple[str, date]]
    # 每个请求的实际到手副本（按需要顺序对应）
    observed_by_request: dict[str, tuple[Fulfillment, ...]]

    def get(self, copy_id: str) -> CopyTimeline:
        return self.timelines[copy_id]


def build_timelines(dataset: Dataset) -> TimelineBundle:
    observed_by_request: dict[str, list[Fulfillment]] = {}
    loan_index: dict[str, list[LoanSpan]] = {}

    household_of_request = {r.id: r.household for r in dataset.requests}

    for ful in dataset.fulfillments:
        observed_by_request.setdefault(ful.request_id, []).append(ful)
        if ful.returned is not None:
            earliest = latest = ful.returned
            uncertain = False
        elif ful.return_window is not None:
            earliest, latest = ful.return_window
            uncertain = True
        else:
            # 无任何归还资料：只能判定仍占用，不臆造归还日
            earliest = latest = date.max
            uncertain = True
        span = LoanSpan(
            fulfillment_id=ful.id,
            request_id=ful.request_id,
            household=household_of_request[ful.request_id],
            pickup_day=ful.day,
            pickup_week=monday_of(ful.day),
            confirmed_return=ful.returned,
            earliest_release=earliest,
            latest_release=latest,
            pickup_branch=ful.pickup,
            uncertain=uncertain,
        )
        loan_index.setdefault(ful.copy_id, []).append(span)

    renewals: dict[str, list[tuple[date, date]]] = {}
    pending: dict[str, list[tuple[date, date, date]]] = {}
    for note in dataset.supply:
        if note.kind == "续期":
            renewals.setdefault(note.copy_id, []).append((note.start, note.until))
        elif note.kind == "归还待核":
            pending.setdefault(note.copy_id, []).append(
                (note.earliest, note.latest, note.received)
            )

    movements: dict[str, list[tuple[date, str, date]]] = {}
    for event in dataset.logs:
        if event.kind in (EVENT_SHELF, EVENT_DELAY):
            movements.setdefault(event.copy_id, []).append(
                (event.week, event.branch, event.receipt_week)
            )

    timelines: dict[str, CopyTimeline] = {}
    for copy in dataset.copies:
        loans = tuple(sorted(loan_index.get(copy.id, ()), key=lambda s: s.pickup_day))
        timelines[copy.id] = CopyTimeline(
            copy_id=copy.id,
            work=copy.work,
            home_branch=copy.branch,
            restricted=copy.restricted,
            loans=loans,
            renewals=tuple(renewals.get(copy.id, ())),
            pending_windows=tuple(pending.get(copy.id, ())),
            movements=tuple(movements.get(copy.id, ())),
        )

    incomplete = frozenset(
        (branch, week)
        for (branch, week), complete in dataset.coverage.items()
        if not complete
    )

    ordered_observed = {
        rid: tuple(sorted(rows, key=lambda f: f.day))
        for rid, rows in observed_by_request.items()
    }
    return TimelineBundle(
        timelines=timelines,
        incomplete_cells=incomplete,
        observed_by_request=ordered_observed,
    )


def location_of(
    timeline: CopyTimeline,
    week: date,
    moves: dict[str, list[tuple[date, str]]] | None = None,
) -> str:
    """副本在某周的所在馆：以到架/调拨事件和轮转 moves 为准，否则取原馆。

    moves 形如 ``{copy_id: [(week, to_branch), ...]}``，按周一生效。
    """
    branch = timeline.home_branch
    for event_week, event_branch, receipt_week in sorted(timeline.movements):
        if receipt_week <= week:
            branch = event_branch
    if moves:
        for move_week, to_branch in sorted(moves.get(timeline.copy_id, ())):
            if move_week <= week:
                branch = to_branch
    return branch
