"""按馆方账还原实际发生的整套阅读服务，并做归属统计。

这是 *事实层*：只陈述 fulfillments（实际到手）能证明的结果，不掺入任何
反事实假设。评估单位是整套请求；部分满足与未到手严格区分。
"""

from __future__ import annotations

from dataclasses import dataclass

from .data_model import Dataset
from .timeline import TimelineBundle
from .types import Assignment, Outcome


@dataclass(frozen=True)
class ObservedFulfillmentView:
    request_id: str
    outcome: Outcome
    assignments: tuple[Assignment, ...]
    satisfied_needs: int
    total_needs: int
    missing_needs: tuple[int, ...]
    uncertain: bool  # 含归还时刻不明的借阅
    neighbor_pickup: bool


def reconstruct_observed(
    dataset: Dataset, bundle: TimelineBundle
) -> dict[str, ObservedFulfillmentView]:
    """逐请求还原实际到手、缺失需要、替代与跨馆代取。"""
    views: dict[str, ObservedFulfillmentView] = {}
    work_of = {c.id: c.work for c in dataset.copies}

    for req in dataset.requests:
        rows = bundle.observed_by_request.get(req.id, ())
        assignments: list[Assignment] = []
        uncertain = False
        used_needs: set[int] = set()
        home = dataset.household(req.household).home

        for ful in rows:
            work = work_of[ful.copy_id]
            need_index = next(
                i for i, need in enumerate(req.needs) if need.accepts(work)
            )
            used_needs.add(need_index)
            need = req.needs[need_index]
            timeline = bundle.get(ful.copy_id)
            loan = next(
                (s for s in timeline.loans if s.fulfillment_id == ful.id), None
            )
            if loan is not None and loan.uncertain:
                uncertain = True
            assignments.append(
                Assignment(
                    request_id=req.id,
                    need_index=need_index,
                    copy_id=ful.copy_id,
                    work=work,
                    branch=ful.pickup,
                    is_substitute=work != need.preferred,
                    is_neighbor_pickup=ful.pickup != home,
                    day=ful.day,
                )
            )

        if len(used_needs) == req.size:
            outcome = Outcome.COMPLETE
        elif used_needs:
            outcome = Outcome.PARTIAL
        else:
            outcome = Outcome.NONE
        missing = tuple(i for i in range(req.size) if i not in used_needs)

        views[req.id] = ObservedFulfillmentView(
            request_id=req.id,
            outcome=outcome,
            assignments=tuple(sorted(assignments, key=lambda a: a.need_index)),
            satisfied_needs=len(used_needs),
            total_needs=req.size,
            missing_needs=missing,
            uncertain=uncertain,
            neighbor_pickup=any(a.is_neighbor_pickup for a in assignments),
        )
    return views


def observed_attribution(
    dataset: Dataset,
    views: dict[str, ObservedFulfillmentView],
) -> dict[str, dict[str, int]]:
    """两套归属视角。

    * ``服务发生馆``：按实际取书馆统计册数（服务在哪儿发生）。
    * ``机会所属馆``：按住户 home 统计，显示机会是否同一批家庭在转馆。
    """
    service_branch: dict[str, int] = {}
    home_branch: dict[str, int] = {}
    for req in dataset.requests:
        view = views[req.id]
        home = dataset.household(req.household).home
        for a in view.assignments:
            service_branch[a.branch] = service_branch.get(a.branch, 0) + 1
            home_branch[home] = home_branch.get(home, 0) + 1
    return {
        "服务发生馆": dict(sorted(service_branch.items())),
        "机会所属馆(住户home)": dict(sorted(home_branch.items())),
    }


def observed_counts(
    views: dict[str, ObservedFulfillmentView],
) -> dict[str, int]:
    counts = {Outcome.COMPLETE.value: 0, Outcome.PARTIAL.value: 0, Outcome.NONE.value: 0}
    for view in views.values():
        counts[view.outcome.value] += 1
    return counts
