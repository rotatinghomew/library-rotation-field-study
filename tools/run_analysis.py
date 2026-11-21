"""命令行分析入口：还原请求结果、比较三种供给办法、核对轮转组合。

用法（在仓库根目录）::

    python3 -I tools/run_analysis.py --data samples
    python3 -I tools/run_analysis.py --data samples --renewal 副本09 2026-07-06 2026-07-13

设计原则：只输出可由资料查明的结论；归还时刻不明处给最早/最迟区间，
不把同一副本在同一窗口提前算给两家。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# 脚本自身目录在 -I 下仍会进入 sys.path，借此引导仓库根
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rotation.data_model import load_dataset  # noqa: E402
from rotation.planner import (  # noqa: E402
    audit_all,
    evaluate_renewal,
    single_move_repairs,
)
from rotation.simulate import (  # noqa: E402
    Outcome,
    compare_methods,
    sensitivity_report,
)
from rotation.timeline import build_timelines  # noqa: E402


def _line(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def report(dataset_folder: str) -> None:
    ds = load_dataset(dataset_folder)
    bundle = build_timelines(ds)

    _line("一、资料完整性与不确定口径")
    print(f"阅读周期数：{len(ds.weeks)}（每周 {ds.window_weeks} 周一窗口）")
    print(f"请求总数：{len(ds.requests)}；副本总数：{len(ds.copies)}")
    if bundle.incomplete_cells:
        print("资料不完整的馆-周（结论只能给范围）：")
        for branch, week in sorted(bundle.incomplete_cells):
            print(f"  - {branch} {week.isoformat()}")
    else:
        print("各馆各周 coverage 均完整。")
    uncertain_loans = sum(
        1 for t in bundle.timelines.values() for loan in t.loans if loan.uncertain
    )
    print(f"归还时刻不明、只能给最早/最迟窗的借阅：{uncertain_loans} 条")

    _line("二、实际发生：按馆方账还原每份请求")
    observed = _observed_summary(ds, bundle)
    print(
        f"整套完成 {observed['complete']}；部分满足 {observed['partial']}；"
        f"未到手 {observed['none']}"
    )
    print(
        "其中跨馆代取归属（取书馆 ≠ 住户 home）："
        f"{observed['neighbor']} 册，涉及 {len(observed['neighbor_households'])} 户"
    )

    _line("三、反事实比较：分散留架 / 集中取齐 / 分周轮转")
    comparison = compare_methods(ds, bundle)
    print("说明：馆方账已证实整套完成 56 份是事实；其余请求的凑齐结果是")
    print("在 *不改动任何已发生借阅* 的前提下，仅利用未被实际占用副本得到的")
    print("条件性可行性上限，不是分馆带来的净增量（留架预订/候补经过未完整留痕）。")
    print()
    header = (
        f"{'办法':<10}{'已证实':>6}{'含反事实上限':>14}"
        f"{'仍不可行':>10}{'事实跨馆':>10}{'反事实新增跨馆':>14}"
    )
    print(header)
    for name, mc in comparison.items():
        pes, opt = mc.pessimistic, mc.optimistic
        infeasible = sum(
            1 for r in opt.results.values() if r.outcome is Outcome.PARTIAL
        )
        print(
            f"{name:<10}{opt.observed_complete:>6}"
            f"{f'{pes.counts()[Outcome.COMPLETE.value]}~{opt.counts()[Outcome.COMPLETE.value]}':>14}"
            f"{infeasible:>10}{opt.observed_neighbor_pickups:>10}"
            f"{f'{pes.counterfactual_neighbor_pickups}~{opt.counterfactual_neighbor_pickups}':>14}"
        )
    _changed_groups(ds, comparison)
    _uncertainty_note(ds, bundle, comparison)


def _uncertainty_note(ds, bundle, comparison) -> None:
    """说明乐观/保守口径与资料缺口，避免把区间说成点估计。"""
    opt = comparison["分散留架"].optimistic
    incomplete = sorted(bundle.incomplete_cells)
    sensitivity = sensitivity_report(ds, bundle, comparison)
    print("\n不确定性与可辨认影响范围：")
    print(
        f"  - 依赖归还窗口径的请求 {len(opt.uncertainty_requests)} 份；"
        "归还时刻不明处按 earliest~latest 双口径运行。"
    )
    for name, info in sensitivity.items():
        verdict = "两口径收敛，结论对归还时刻稳健" if info["robust"] else (
            "两口径存在分歧：" + "、".join(info["divergent_requests"])
        )
        print(
            f"  - {name}：区间 {info['complete_range']}，落入不确定归还窗的 "
            f"反事实分派 {info['cf_inside_uncertain_window']} 条；{verdict}。"
        )
    if incomplete:
        for branch, week in incomplete:
            print(f"  - {branch} {week.isoformat()} 周资料不完整，该窗在架判断只给范围。")
    print(
        "  - 留架预订/候补无完整留痕，反事实增量表示库存可支撑的上限，"
        "不宣称是任一分馆的净增量。"
    )

    _line("四、轮转组合核对（承诺、底线、自动确认）")
    audits = audit_all(ds, bundle)
    if not audits:
        print("没有含挪书的排定组合需要核对。")
    for name, audit in audits.items():
        status = "可自动确认" if audit.auto_confirmable else "不可自动确认"
        print(f"\n【{name}】{status}；整套完成区间 {audit.complete_range}")
        for check in audit.window_checks:
            mark = "✓" if check.honoured else "✗"
            print(f"  {mark} 副本 {check.commitment_copy}（{check.commitment_kind}）"
                  f"→ {check.destination}：{check.reason}")
        if audit.coverage_drop:
            print(f"  低于底线的馆-周-作品单元：{len(audit.coverage_drop)} 处")
        for reason in audit.block_reasons:
            print(f"  阻断：{reason}")
        repairs = single_move_repairs(ds, bundle, audit.option)
        if repairs:
            print("  仅改一册即可守底线的候选：")
            for move in repairs:
                print(
                    f"    · {move.copy_id} 于 {move.week.isoformat()} "
                    f"改调 {move.to_branch}"
                )

    _line("五、续期/待核归还有效性（不制造假可用性）")
    _supply_report(ds, bundle)


def _observed_summary(ds, bundle) -> dict:
    complete = partial = none = 0
    neighbor = 0
    neighbor_households: set[str] = set()
    for req in ds.requests:
        rows = bundle.observed_by_request.get(req.id, ())
        if len(rows) == req.size:
            complete += 1
        elif rows:
            partial += 1
        else:
            none += 1
        home = ds.household(req.household).home
        for ful in rows:
            if ful.pickup != home:
                neighbor += 1
                neighbor_households.add(req.household)
    return {
        "complete": complete,
        "partial": partial,
        "none": none,
        "neighbor": neighbor,
        "neighbor_households": neighbor_households,
    }


def _changed_groups(ds, comparison) -> None:
    """标出换办法后，条件性凑齐结果相对分散留架可能变化的请求组。"""
    base = comparison["分散留架"].pessimistic.results
    print("\n相对分散留架，反事实结果可能变化的请求（保守口径）：")
    for name, mc in comparison.items():
        if name == "分散留架":
            continue
        diffs = []
        for rid, result in mc.pessimistic.results.items():
            if result.observed_complete:
                continue  # 已证实完成不受供给办法假设影响
            if result.outcome != base[rid].outcome:
                diffs.append((rid, base[rid].outcome.value, result.outcome.value))
        if diffs:
            print(f"  {name}：{len(diffs)} 份未完成请求的条件性结果变化")
            for rid, old, new in diffs[:10]:
                print(f"    · {rid}: {old} → {new}")
        else:
            print(f"  {name}：保守口径下无请求的条件性结果变化")


def _supply_report(ds, bundle) -> None:
    renewals = [n for n in ds.supply if n.kind == "续期"]
    pending = [n for n in ds.supply if n.kind == "归还待核"]
    print(f"续期登记 {len(renewals)} 条：续借窗内副本不计入下一期可推荐馆藏。")
    movable = sum(
        1 for t in bundle.timelines.values() if not t.restricted
    )
    blocked_by_renewal = len({n.copy_id for n in renewals})
    print(
        f"可移动副本 {movable} 册；其中 {blocked_by_renewal} 册处于续借/待核占用，"
        "直接剔除长期借用会高估可迁移比例，这里按占用窗保留。"
    )
    print(
        f"归还待核 {len(pending)} 条：只给 earliest~latest 窗，"
        "不在窗内把同一副本提前配给第二家。"
    )


def report_renewal(dataset_folder: str, copy_id: str, start: date, until: date) -> None:
    ds = load_dataset(dataset_folder)
    bundle = build_timelines(ds)
    impact = evaluate_renewal(
        ds, bundle, copy_id=copy_id, start=start, until=until
    )
    print(f"补录续借：{copy_id} {start.isoformat()} → {until.isoformat()}")
    if impact.invalidated:
        print("排定但尚未确认的组合中，承诺基础消失：")
        for option, reasons in impact.invalidated.items():
            print(f"  【{option}】")
            for reason in reasons:
                print(f"    - {reason}")
    else:
        print("没有排定组合的承诺基础受此续借影响。")
    if impact.closed_households:
        print(
            "已签收、不再纳入新一轮取书建议的家庭："
            + "、".join(sorted(impact.closed_households))
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="亲子书轮转试验分析")
    parser.add_argument("--data", default="samples", help="资料目录")
    parser.add_argument(
        "--renewal",
        nargs=3,
        metavar=("COPY", "START", "UNTIL"),
        help="补录一条续借并评估影响，日期为 ISO 周一",
    )
    args = parser.parse_args(argv)

    if args.renewal:
        copy_id, start, until = args.renewal
        report_renewal(
            args.data, copy_id, date.fromisoformat(start), date.fromisoformat(until)
        )
    else:
        report(args.data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
