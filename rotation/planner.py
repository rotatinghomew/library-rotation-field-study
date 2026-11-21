"""下一轮轮转组合的可兑现性、覆盖下滑与承诺基础核对。

职责
====
1. 逐个取件窗核对组合是否仍能兑现已确认承诺（预订/已签收轮转）。
2. 计算每个作品组在各馆/各取件窗的覆盖，标出相对底线的下滑。
3. 单册周转好但整套覆盖变差时给出不可自动确认的信号。
4. 补录一条续借后，找出排定但尚未确认的组合中哪些承诺基础消失。
5. 已签收家庭不再进入新一轮取书建议（其请求已闭环）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .data_model import (
    Dataset,
    Move,
    RotationOption,
)
from .simulate import Outcome, simulate
from .timeline import TimelineBundle, location_of


@dataclass(frozen=True)
class WindowCheck:
    option_name: str
    week: date
    commitment_copy: str
    commitment_kind: str
    destination: str
    honoured: bool
    reason: str


@dataclass(frozen=True)
class CoverageCell:
    work: str
    branch: str
    week: date
    copies_available: int
    meets_floor: bool


@dataclass
class OptionAudit:
    option: RotationOption
    window_checks: tuple[WindowCheck, ...]
    coverage_drop: tuple[CoverageCell, ...]
    complete_range: tuple[int, int]
    partial_range: tuple[int, int]
    auto_confirmable: bool
    block_reasons: tuple[str, ...]
    # 涉及前一轮已确认预订的挪书
    touches_commitment: frozenset[str]


@dataclass
class RenewalImpact:
    copy_id: str
    start: date
    until: date
    # 承诺基础因此消失的（尚未确认的）组合与原因
    invalidated: dict[str, tuple[str, ...]]
    # 已签收、不应再进新一轮建议的家庭
    closed_households: frozenset[str]


def _moves_map(option: RotationOption) -> dict[str, list[tuple[date, str]]]:
    out: dict[str, list[tuple[date, str]]] = {}
    for move in option.moves:
        out.setdefault(move.copy_id, []).append((move.week, move.to_branch))
    return out


def audit_option(
    dataset: Dataset,
    bundle: TimelineBundle,
    option: RotationOption,
) -> OptionAudit:
    moves = _moves_map(option)
    window_checks: list[WindowCheck] = []
    touches: set[str] = set()
    block_reasons: list[str] = []

    commitment_by_copy = {c.copy_id: c for c in dataset.commitments}

    # --- 1. 逐取件窗核对承诺 ---------------------------------------------
    for move in option.moves:
        if move.copy_id not in commitment_by_copy:
            continue
        touches.add(move.copy_id)
        commitment = commitment_by_copy[move.copy_id]
        # 承诺 due 之前副本必须能在 destination 取到
        branch_at_due = location_of(
            bundle.get(move.copy_id), commitment.due, moves
        )
        timeline = bundle.get(move.copy_id)
        busy = not timeline.available_at(
            commitment.due, optimistic=False
        )
        honoured = (
            branch_at_due == commitment.destination and not busy
        )
        if move.week <= commitment.due and move.to_branch != commitment.destination:
            honoured = False
        reasons = []
        if branch_at_due != commitment.destination:
            reasons.append(
                f"承诺要求 {commitment.destination}，{commitment.due.isoformat()} "
                f"实际所在为 {branch_at_due}"
            )
        if busy:
            reasons.append("承诺到期周副本仍被占用（保守口径不可用）")
        window_checks.append(
            WindowCheck(
                option_name=option.name,
                week=move.week,
                commitment_copy=move.copy_id,
                commitment_kind=commitment.kind,
                destination=commitment.destination,
                honoured=honoured,
                reason="；".join(reasons) if reasons else "承诺可兑现",
            )
        )
        if not honoured:
            block_reasons.append(
                f"{move.copy_id} 的{commitment.kind}承诺无法在 "
                f"{commitment.due.isoformat()} 于 {commitment.destination} 兑现"
            )

    # --- 2. 作品组覆盖 vs 分散留架基线（守护线是相对值） ------------------
    # 每馆每作品常备只有 1 册，floor 不可能是绝对在架数；它表达的是
    # “轮转后不得让任一馆-周-作品的可查明在架低于基线应保水平”。
    baseline_stock = _stock_grid(dataset, bundle, {})
    option_stock = _stock_grid(dataset, bundle, moves)
    coverage_drop = _coverage_drop(
        dataset, baseline_stock, option_stock
    )
    for cell in coverage_drop:
        block_reasons.append(
            f"{cell.branch} {cell.week.isoformat()} 周 {cell.work} 覆盖由 "
            f"{baseline_stock[(cell.branch, cell.work, cell.week)]} 册降为 "
            f"{cell.copies_available} 册，跌破守护线 "
            f"{dataset.floor[cell.branch]}"
        )

    # --- 3. 反事实整套结果区间 -------------------------------------------
    opt = simulate(dataset, bundle, option=option, optimistic=True)
    pes = simulate(dataset, bundle, option=option, optimistic=False)
    complete_hi = opt.counts()[Outcome.COMPLETE.value]
    complete_lo = pes.counts()[Outcome.COMPLETE.value]

    baseline_pes = simulate(dataset, bundle, option=None, optimistic=False)
    baseline_complete = baseline_pes.counts()[Outcome.COMPLETE.value]
    # 单册周转好而整套覆盖变差：保守口径整套数低于分散留架底线
    if complete_lo < baseline_complete:
        block_reasons.append(
            f"保守口径整套完成 {complete_lo} 低于分散留架基线 "
            f"{baseline_complete}：单册周转不能替代整套覆盖"
        )

    auto_confirmable = not block_reasons
    return OptionAudit(
        option=option,
        window_checks=tuple(window_checks),
        coverage_drop=tuple(coverage_drop),
        complete_range=(complete_lo, complete_hi),
        partial_range=(
            opt.counts()[Outcome.PARTIAL.value],
            pes.counts()[Outcome.PARTIAL.value],
        ),
        auto_confirmable=auto_confirmable,
        block_reasons=tuple(block_reasons),
        touches_commitment=frozenset(touches),
    )


def _stock_grid(
    dataset: Dataset,
    bundle: TimelineBundle,
    moves: dict[str, list[tuple[date, str]]],
) -> dict[tuple[str, str, date], int]:
    """保守口径下 (馆, 作品, 周) 的可查明在架册数。"""
    stock: dict[tuple[str, str, date], int] = {}
    for week in dataset.weeks:
        for copy in dataset.copies:
            branch = location_of(bundle.get(copy.id), week, moves)
            timeline = bundle.get(copy.id)
            if not timeline.available_at(week, optimistic=False):
                continue
            key = (branch, copy.work, week)
            stock[key] = stock.get(key, 0) + 1
    return stock


def _coverage_drop(
    dataset: Dataset,
    baseline: dict[tuple[str, str, date], int],
    option_stock: dict[tuple[str, str, date], int],
) -> list[CoverageCell]:
    """挑出相对分散留架基线出现覆盖下滑、且跌破守护线的单元。"""
    drops: list[CoverageCell] = []
    works = sorted({c.work for c in dataset.copies})
    for week in dataset.weeks:
        for branch in dataset.floor:
            for work in works:
                key = (branch, work, week)
                before = baseline.get(key, 0)
                after = option_stock.get(key, 0)
                # 守护线：低于 floor 即视为该馆该作品组失守；同时必须是
                # 相对基线真实下滑，避免把物理上不可能的绝对库存当失败。
                if after < before and after < dataset.floor[branch]:
                    drops.append(
                        CoverageCell(
                            work=work,
                            branch=branch,
                            week=week,
                            copies_available=after,
                            meets_floor=False,
                        )
                    )
    return drops


def audit_all(
    dataset: Dataset, bundle: TimelineBundle
) -> dict[str, OptionAudit]:
    """只审计真正含挪书的排定组合；空 moves 的登记项等同分散留架基线。"""
    return {
        option.name: audit_option(dataset, bundle, option)
        for option in dataset.options
        if option.moves
    }


# ---------------------------------------------------------------------------
# 补录续借的影响
# ---------------------------------------------------------------------------

def evaluate_renewal(
    dataset: Dataset,
    bundle: TimelineBundle,
    *,
    copy_id: str,
    start: date,
    until: date,
) -> RenewalImpact:
    """补录一条续借后，核对各 *尚未确认* 组合的承诺基础。

    已确认组合（ commitments 中 kind=已签收轮转 ）不参与新一轮建议；
    其家庭视为已闭环。
    """
    timeline = bundle.get(copy_id)
    invalidated: dict[str, tuple[str, ...]] = {}

    # 只评估真正含挪书、尚未确认的排定组合；分散留架基线不在此列。
    pending_options = [o for o in dataset.options if o.moves]
    for option in pending_options:
        reasons: list[str] = []
        for move in option.moves:
            if move.copy_id != copy_id:
                continue
            # 续借窗与挪书周重叠：副本无法按时到新馆
            if start <= move.week <= until:
                reasons.append(
                    f"续借占用 {start.isoformat()}–{until.isoformat()} 覆盖挪书周 "
                    f"{move.week.isoformat()}，{move.to_branch} 的承诺基础消失"
                )
        # 该副本若承担任何承诺，续借同样动摇其可兑现性
        for commitment in dataset.commitments:
            if commitment.copy_id != copy_id:
                continue
            if start <= commitment.due <= until:
                reasons.append(
                    f"{commitment.kind}承诺到期 {commitment.due.isoformat()} "
                    "落在续借窗内，无法在目的地兑现"
                )
        if reasons:
            invalidated[option.name] = tuple(reasons)

    closed_households = frozenset(
        c.household
        for c in dataset.commitments
        if c.kind == "已签收轮转" and c.household
    )
    return RenewalImpact(
        copy_id=copy_id,
        start=start,
        until=until,
        invalidated=invalidated,
        closed_households=closed_households,
    )


def single_move_repairs(
    dataset: Dataset,
    bundle: TimelineBundle,
    option: RotationOption,
) -> list[Move]:
    """找出仅改一册去向即可让组合守住底线的候选挪书。

    对每个低于底线的 (馆, 周, 作品)，在可移动（非 restricted、未被承诺
    锁定）的同作品副本里，寻找改派后能补足该单元且不破坏承诺的单一 move。
    """
    audit = audit_option(dataset, bundle, option)
    if audit.auto_confirmable:
        return []

    moves = _moves_map(option)
    repairs: list[Move] = []
    committed_locked = {
        c.copy_id for c in dataset.commitments
    }

    for cell in audit.coverage_drop:
        for copy in dataset.copies:
            if copy.work != cell.work or copy.restricted:
                continue
            if copy.id in committed_locked:
                continue
            current = location_of(bundle.get(copy.id), cell.week, moves)
            if current == cell.branch:
                continue
            candidate = Move(
                copy_id=copy.id,
                to_branch=cell.branch,
                week=cell.week,
            )
            if _move_keeps_commitments(dataset, bundle, option, candidate):
                repairs.append(candidate)
                break  # 每个缺口只给第一个可守底线的单册改法
    return repairs


def _move_keeps_commitments(
    dataset: Dataset,
    bundle: TimelineBundle,
    option: RotationOption,
    candidate: Move,
) -> bool:
    merged: dict[str, list[tuple[date, str]]] = _moves_map(option)
    merged.setdefault(candidate.copy_id, []).append(
        (candidate.week, candidate.to_branch)
    )
    for commitment in dataset.commitments:
        if commitment.copy_id != candidate.copy_id:
            continue
        branch = location_of(
            bundle.get(commitment.copy_id), commitment.due, merged
        )
        if branch != commitment.destination:
            return False
    return True
