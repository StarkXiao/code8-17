"""核验报告渲染：控制台表格（中文）与机器可读 JSON。"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Iterable

from .dispatcher import DispatchResult
from .models import GateAction, Severity, Verdict

_STATUS_PASS = "通过 可下发"
_STATUS_WARN = "通过 有预警"
_STATUS_BLOCK = "阻断 不下发"


def _status_text(v: Verdict) -> str:
    if not v.accepted:
        return _STATUS_BLOCK
    if v.warnings:
        return _STATUS_WARN
    return _STATUS_PASS


def render_text(verdicts: Iterable[Verdict | DispatchResult]) -> str:
    """生成人类可读的逐条核验报告。"""
    lines: list[str] = []
    n_pass = n_warn = n_block = 0

    for item in verdicts:
        verdict = item.verdict if isinstance(item, DispatchResult) else item
        ins = verdict.instruction
        gate_name = ins.gate_id
        status = _status_text(verdict)
        if not verdict.accepted:
            n_block += 1
        elif verdict.warnings:
            n_warn += 1
        else:
            n_pass += 1

        seq = f"#{ins.seq} " if ins.seq is not None else ""
        lines.append("=" * 72)
        lines.append(
            f"{seq}闸门 {gate_name} | {ins.action.value} | {ins.target_value_text}"
            f" | 下发时刻 {ins.issued_at.isoformat()}  ==> 【{status}】"
        )
        if ins.note:
            lines.append(f"    备注：{ins.note}")
        if ins.action is GateAction.SET_FLOW and verdict.resolved_opening is not None:
            infeasible = any(v.code == "FLOW_CAPACITY_EXCEEDED" for v in verdict.violations)
            feas = "不可达（已超限）" if infeasible else "可达"
            lines.append(
                f"    水力反推：{feas}，建议开度 {verdict.resolved_opening:.2f}m，"
                f"对应流量 {verdict.resolved_flow:.2f} m³/s"
            )
        if verdict.simulated_levels:
            lines.append("    前瞻模拟水位极值（最低 ~ 最高，m）：")
            for pid, (lo, hi) in verdict.simulated_levels.items():
                lines.append(f"      - {pid}: {lo:.2f} ~ {hi:.2f}")
        if verdict.violations:
            for vio in verdict.violations:
                tag = "阻断" if vio.severity is Severity.ERROR else "预警"
                when = f" @t={vio.at_hours:.2f}h" if vio.at_hours is not None else ""
                lines.append(f"    [{tag}] {vio.code}{when}：{vio.message}")
        else:
            lines.append("    全部约束校核通过，无上下游水位冲突。")

    total = n_pass + n_warn + n_block
    if total:
        lines.append("=" * 72)
        lines.append(
            f"汇总：共 {total} 条 —— 通过 {n_pass}，通过但有预警 {n_warn}，"
            f"阻断 {n_block}。"
        )
        if n_block:
            lines.append("注：被阻断指令不得下发；预警不阻断，但建议人工复核。")
    return "\n".join(lines)


def verdict_to_dict(v: Verdict) -> dict:
    return {
        "gate_id": v.instruction.gate_id,
        "seq": v.instruction.seq,
        "action": v.instruction.action.value,
        "target_opening": v.instruction.target_opening,
        "target_flow": v.instruction.target_flow,
        "issued_at": v.instruction.issued_at.isoformat(),
        "accepted": v.accepted,
        "status": _status_text(v),
        "resolved_opening": v.resolved_opening,
        "resolved_flow": v.resolved_flow,
        "simulated_levels": {pid: {"min": lo, "max": hi}
                             for pid, (lo, hi) in v.simulated_levels.items()},
        "violations": [asdict(x) for x in v.violations],
    }


def render_json(verdicts: Iterable[Verdict | DispatchResult], *, indent: int = 2) -> str:
    rows = []
    for item in verdicts:
        v = item.verdict if isinstance(item, DispatchResult) else item
        d = verdict_to_dict(v)
        if isinstance(item, DispatchResult):
            d["dispatched"] = item.dispatched
        rows.append(d)
    return json.dumps(
        {"count": len(rows),
         "blocked": sum(1 for r in rows if not r["accepted"]),
         "verdicts": rows},
        ensure_ascii=False, indent=indent,
    )
