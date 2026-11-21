"""事实锚定的反事实模拟：在馆方账占用之上比较供给办法。

为什么不能从零模拟
==================
留架预订与候补排队没有完整留痕（多数部分满足请求在窗口内没有任何过程
事件）。若假定馆方按某个确定性规则排队，会把未知的分配过程伪装成事实。
因此本模块：

1. 先把 fulfillments 的每次实际到手回放成 *硬占用*——同一副本在实际借阅
   期内对任何反事实分派都不可用，杜绝把同一册提前算给两家。
2. 实际已整套完成的请求直接记为已证实完成，不参与重排。
3. 反事实引擎只对实际未完成（部分满足/未到手）的请求，在 *未被实际占用*
   的副本上尝试凑齐，结果标注为条件性增量，并给乐观/保守区间。

这样输出的是「在不改变已发生借阅的前提下，换一种供给办法 *至多/至少*
还能多凑齐哪些请求」，与可查明资料严格对齐，而不是虚构一套更优的馆方
排队。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .data_model import Dataset, RotationOption, monday_of
from .reconstruct import reconstruct_observed
from .timeline import TimelineBundle, location_of
from .types import Assignment, Outcome, RequestResult


@dataclass
class SimulationResult:
    method: str
    optimistic: bool
    results: dict[str, RequestResult]
    loans_by_branch: dict[str, int]
    neighbor_pickups: int
    neighbor_households: frozenset[str]
    uncertainty_requests: frozenset[str]
    blocked_by_commitment: tuple[str, ...]
    # 口径透明：已证实整套数 与 含反事实增量的整套数
    observed_complete: int
    counterfactual_completes: int
    # 归属拆分：馆方账事实的跨馆代取 vs 反事实新增的跨馆代取
    observed_neighbor_pickups: int = 0
    counterfactual_neighbor_pickups: int = 0

    def counts(self) -> dict[str, int]:
        c = {Outcome.COMPLETE: 0, Outcome.PARTIAL: 0, Outcome.NONE: 0}
        for r in self.results.values():
            c[r.outcome] += 1
        return {k.value: v for k, v in c.items()}


@dataclass
class MethodComparison:
    method: str
    optimistic: SimulationResult
    pessimistic: SimulationResult

    def complete_range(self) -> tuple[int, int]:
        lo = self.pessimistic.counts()[Outcome.COMPLETE.value]
        hi = self.optimistic.counts()[Outcome.COMPLETE.value]
        return lo, hi

    def partial_range(self) -> tuple[int, int]:
        lo = self.optimistic.counts()[Outcome.PARTIAL.value]
        hi = self.pessimistic.counts()[Outcome.PARTIAL.value]
        return lo, hi

    def gain_range(self) -> tuple[int, int]:
        """反事实相对已证实完成的条件性增量区间（保守, 乐观）。"""
        base = self.optimistic.observed_complete
        return (
            self.pessimistic.counterfactual_completes,
            self.optimistic.counterfactual_completes,
        )


# ---------------------------------------------------------------------------

def _build_moves(option: RotationOption) -> dict[str, list[tuple[date, str]]]:
    moves: dict[str, list[tuple[date, str]]] = {}
    for move in option.moves:
        moves.setdefault(move.copy_id, []).append((move.week, move.to_branch))
    return moves


def _branch_at(bundle: TimelineBundle, copy_id: str, day: date, moves) -> str:
    return location_of(bundle.get(copy_id), monday_of(day), moves)


def _pickup_days(opens: date, window_weeks: int) -> tuple[date, ...]:
    """一个阅读周期内的取件窗日期。

    馆方账显示取书发生在开放周的周二起（offset 1..window_weeks），
    开放周一仅登记/上架，不作为可成功取书日。
    """
    return tuple(opens + timedelta(days=i) for i in range(1, window_weeks + 1))


def _observed_occupancy(
    bundle: TimelineBundle, optimistic: bool
) -> dict[str, list[tuple[date, date]]]:
    """每个副本被馆方账实际占用的 [取书日, 再可用日) 区间。"""
    occ: dict[str, list[tuple[date, date]]] = {}
    for copy_id, timeline in bundle.timelines.items():
        spans: list[tuple[date, date]] = []
        for loan in timeline.loans:
            release = loan.earliest_release if optimistic else loan.latest_release
            if release == date.max:
                release = loan.pickup_day + timedelta(weeks=52)
            spans.append((loan.pickup_day, release))
        occ[copy_id] = spans
    return occ


def simulate(
    dataset: Dataset,
    bundle: TimelineBundle,
    *,
    option: RotationOption | None,
    optimistic: bool,
) -> SimulationResult:
    moves = _build_moves(option) if option is not None else {}
    method = option.name if option is not None else "分散留架（无调拨）"
    observed_views = reconstruct_observed(dataset, bundle)
    occupancy = _observed_occupancy(bundle, optimistic)

    committed: dict[str, tuple[str, date]] = {
        c.copy_id: (c.destination, c.due) for c in dataset.commitments
    }

    # 反事实分派自身产生的增量占用（只落在未被事实占用的副本/时段）
    cf_busy_until: dict[str, date] = {}
    loans_by_branch: dict[str, int] = {}
    neighbor_pickups = 0
    neighbor_households: set[str] = set()
    blocked: list[str] = []
    uncertainty_requests: set[str] = set()
    results: dict[str, RequestResult] = {}
    cf_completes = 0
    observed_complete = 0

    requests_by_week: dict[date, list] = {}
    for req in dataset.requests:
        requests_by_week.setdefault(req.opens, []).append(req)

    for opens in sorted(requests_by_week):
        window = _pickup_days(opens, dataset.window_weeks)
        for req in sorted(requests_by_week[opens], key=lambda r: r.id):
            view = observed_views[req.id]
            if view.outcome is Outcome.COMPLETE:
                # 已证实整套完成：原样保留，不重排
                observed_complete += 1
                results[req.id] = _lock_observed(dataset, req, view, bundle)
                continue

            result = _counterfactual_for_request(
                dataset=dataset,
                bundle=bundle,
                req=req,
                view=view,
                window=window,
                moves=moves,
                committed=committed,
                occupancy=occupancy,
                cf_busy_until=cf_busy_until,
                optimistic=optimistic,
                blocked=blocked,
                uncertainty_requests=uncertainty_requests,
            )
            if result.counterfactual_gain:
                cf_completes += 1
            results[req.id] = result

    # 馆方账事实归属（所有已到手记录都计入服务发生馆）
    observed_neighbor = 0
    counterfactual_neighbor = 0
    for req in dataset.requests:
        view = observed_views[req.id]
        home = dataset.household(req.household).home
        for a in view.assignments:
            loans_by_branch[a.branch] = loans_by_branch.get(a.branch, 0) + 1
            if a.is_neighbor_pickup:
                neighbor_pickups += 1
                observed_neighbor += 1
                neighbor_households.add(req.household)
        # 反事实增量的归属
        result = results[req.id]
        for a in result.assignments:
            if not a.counterfactual:
                continue
            loans_by_branch[a.branch] = loans_by_branch.get(a.branch, 0) + 1
            if a.is_neighbor_pickup:
                neighbor_pickups += 1
                counterfactual_neighbor += 1
                neighbor_households.add(req.household)
        _ = home

    return SimulationResult(
        method=method,
        optimistic=optimistic,
        results=results,
        loans_by_branch=dict(sorted(loans_by_branch.items())),
        neighbor_pickups=neighbor_pickups,
        neighbor_households=frozenset(neighbor_households),
        uncertainty_requests=frozenset(uncertainty_requests),
        blocked_by_commitment=tuple(blocked),
        observed_complete=observed_complete,
        counterfactual_completes=cf_completes,
        observed_neighbor_pickups=observed_neighbor,
        counterfactual_neighbor_pickups=counterfactual_neighbor,
    )


def _lock_observed(dataset, req, view, bundle) -> RequestResult:
    return RequestResult(
        request_id=req.id,
        outcome=Outcome.COMPLETE,
        assignments=view.assignments,
        satisfied_needs=view.satisfied_needs,
        total_needs=view.total_needs,
        missing_needs=(),
        uncertain=view.uncertain,
        observed_complete=True,
        counterfactual_gain=False,
    )


def _is_observed_busy(occupancy, copy_id: str, day: date) -> bool:
    return any(start <= day < end for start, end in occupancy[copy_id])


def _counterfactual_for_request(
    *,
    dataset,
    bundle,
    req,
    view,
    window,
    moves,
    committed,
    occupancy,
    cf_busy_until,
    optimistic,
    blocked,
    uncertainty_requests,
) -> RequestResult:
    # 以馆方账已到手的册为起点
    assignments: list[Assignment] = list(view.assignments)
    used_copies = {a.copy_id for a in assignments}
    satisfied = {a.need_index for a in assignments}
    uncertain = view.uncertain
    home = dataset.household(req.household).home
    gained = False

    for need_index, need in enumerate(req.needs):
        if need_index in satisfied:
            continue
        candidate = _pick_free_copy(
            dataset=dataset,
            bundle=bundle,
            req=req,
            need=need,
            window=window,
            moves=moves,
            committed=committed,
            occupancy=occupancy,
            cf_busy_until=cf_busy_until,
            used_copies=used_copies,
            optimistic=optimistic,
            blocked=blocked,
        )
        if candidate is None:
            continue
        copy_id, branch, work, is_sub, day, copy_uncertain = candidate
        uncertain = uncertain or copy_uncertain
        used_copies.add(copy_id)
        satisfied.add(need_index)
        cf_busy_until[copy_id] = max(cf_busy_until.get(copy_id, day), req.deadline)
        gained = True
        assignments.append(
            Assignment(
                request_id=req.id,
                need_index=need_index,
                copy_id=copy_id,
                work=work,
                branch=branch,
                is_substitute=is_sub,
                is_neighbor_pickup=branch != home,
                day=day,
                counterfactual=True,
            )
        )

    if len(satisfied) == req.size:
        outcome = Outcome.COMPLETE
        uncertainty_requests.add(req.id)
    elif satisfied:
        outcome = Outcome.PARTIAL
    else:
        outcome = Outcome.NONE

    missing = tuple(i for i in range(req.size) if i not in satisfied)
    return RequestResult(
        request_id=req.id,
        outcome=outcome,
        assignments=tuple(sorted(assignments, key=lambda a: a.need_index)),
        satisfied_needs=len(satisfied),
        total_needs=req.size,
        missing_needs=missing,
        uncertain=uncertain,
        observed_complete=False,
        counterfactual_gain=(outcome is Outcome.COMPLETE),
    )


def _pick_free_copy(
    *,
    dataset,
    bundle,
    req,
    need,
    window,
    moves,
    committed,
    occupancy,
    cf_busy_until,
    used_copies,
    optimistic,
    blocked,
):
    best = None
    for copy in dataset.copies:
        if copy.id in used_copies or not need.accepts(copy.work):
            continue
        if copy.restricted and any(
            w <= monday_of(window[0]) for w, _ in moves.get(copy.id, ())
        ):
            blocked.append(f"{copy.id}:restricted:{req.id}")
            continue
        timeline = bundle.get(copy.id)
        chosen_day = None
        chosen_branch = None
        for day in window:
            branch = _branch_at(bundle, copy.id, day, moves)
            if branch not in req.pickup:
                continue
            if copy.id in committed:
                dest, due = committed[copy.id]
                if day < due and dest != branch:
                    blocked.append(f"{copy.id}:{req.id}")
                    continue
            # 硬约束：馆方账实际占用优先，绝不双重分派
            if _is_observed_busy(occupancy, copy.id, day):
                continue
            if copy.id in cf_busy_until and cf_busy_until[copy.id] > day:
                continue
            if not timeline.available_at(day, optimistic=optimistic):
                continue
            chosen_day, chosen_branch = day, branch
            break
        if chosen_day is None:
            continue
        copy_uncertain = any(
            loan.uncertain and loan.pickup_day <= chosen_day
            for loan in timeline.loans
        )
        is_sub = copy.work != need.preferred
        rank = (
            chosen_day,
            1 if is_sub else 0,
            0 if chosen_branch == dataset.household(req.household).home else 1,
            copy.id,
        )
        if best is None or rank < best[0]:
            best = (rank, chosen_day, copy.id, chosen_branch, copy.work, is_sub, copy_uncertain)
    if best is None:
        return None
    _, day, copy_id, branch, work, is_sub, copy_uncertain = best
    return copy_id, branch, work, is_sub, day, copy_uncertain


def compare_methods(
    dataset: Dataset,
    bundle: TimelineBundle,
    options: list[RotationOption] | None = None,
) -> dict[str, MethodComparison]:
    options = options if options is not None else list(dataset.options)
    out: dict[str, MethodComparison] = {}
    do = simulate(dataset, bundle, option=None, optimistic=True)
    dp = simulate(dataset, bundle, option=None, optimistic=False)
    out["分散留架"] = MethodComparison("分散留架", do, dp)
    for option in options:
        if not option.moves:
            continue
        out[option.name] = MethodComparison(
            option.name,
            simulate(dataset, bundle, option=option, optimistic=True),
            simulate(dataset, bundle, option=option, optimistic=False),
        )
    return out


def sensitivity_report(
    dataset: Dataset,
    bundle: TimelineBundle,
    comparison: dict[str, MethodComparison],
) -> dict[str, dict[str, object]]:
    """解释乐观/保守区间为何收敛或发散。

    返回每个办法下：依赖归还窗的请求数、两口径结果不同的请求、落入不确定
    归还窗内的反事实分派数。区间收敛意味着结论对归还时刻不敏感（资料可
    支撑的上限稳健），而非忽略了不确定性。
    """
    report: dict[str, dict[str, object]] = {}
    for name, mc in comparison.items():
        opt, pes = mc.optimistic, mc.pessimistic
        divergent = sorted(
            rid
            for rid in opt.results
            if opt.results[rid].outcome != pes.results[rid].outcome
        )
        in_window_cf = 0
        for rr in opt.results.values():
            for a in rr.assignments:
                if not a.counterfactual:
                    continue
                timeline = bundle.get(a.copy_id)
                for loan in timeline.loans:
                    if (
                        loan.uncertain
                        and loan.earliest_release <= a.day <= loan.latest_release
                    ):
                        in_window_cf += 1
        report[name] = {
            "uncertain_requests": len(opt.uncertainty_requests),
            "divergent_requests": divergent,
            "cf_inside_uncertain_window": in_window_cf,
            "complete_range": mc.complete_range(),
            "robust": not divergent and in_window_cf == 0,
        }
    return report
