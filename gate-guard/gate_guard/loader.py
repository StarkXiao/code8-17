"""从 JSON 装载核验场景与调度指令，并做配置完整性校验。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import (
    Gate,
    GateAction,
    GateStatus,
    Instruction,
    LevelBounds,
    Pool,
    Scenario,
    SystemConstraints,
)


class LoaderError(ValueError):
    """场景/指令配置不合法。"""


def _read_json(source: str | Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(source, dict):
        return source
    path = Path(source)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LoaderError(f"文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise LoaderError(f"JSON 解析失败（{path}:{exc.lineno}）：{exc.msg}") from exc


def _require(obj: dict[str, Any], key: str, ctx: str) -> Any:
    if key not in obj or obj[key] is None:
        raise LoaderError(f"{ctx}缺少必填字段 '{key}'")
    return obj[key]


def _as_float(value: Any, ctx: str, *, nonneg: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LoaderError(f"{ctx}必须是数值，实际为 {value!r}")
    f = float(value)
    if nonneg and f < 0:
        raise LoaderError(f"{ctx}不能为负，实际为 {f}")
    return f


def _parse_bounds(raw: dict[str, Any] | None, ctx: str) -> LevelBounds:
    if raw is None:
        return LevelBounds()
    lo = raw.get("min")
    hi = raw.get("max")
    lo = _as_float(lo, f"{ctx}.min") if lo is not None else None
    hi = _as_float(hi, f"{ctx}.max") if hi is not None else None
    if lo is not None and hi is not None and lo > hi:
        raise LoaderError(f"{ctx} 的 min({lo}) 大于 max({hi})")
    return LevelBounds(lo, hi)


def _parse_series(raw: Any, ctx: str) -> list[tuple[float, float]]:
    if not isinstance(raw, list) or not raw:
        raise LoaderError(f"{ctx}必须是非空数组 [[小时, 流量], ...]")
    pts: list[tuple[float, float]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise LoaderError(f"{ctx}[{i}] 必须是 [小时, 流量] 两元素数组")
        t = _as_float(item[0], f"{ctx}[{i}].小时")
        q = _as_float(item[1], f"{ctx}[{i}].流量")
        pts.append((t, q))
    if any(b[0] < a[0] for a, b in zip(pts, pts[1:])):
        raise LoaderError(f"{ctx}时间点必须升序")
    return pts


def load_scenario(source: str | Path | dict[str, Any]) -> Scenario:
    """把 JSON（文件路径或 dict）装载为 :class:`Scenario`。"""
    data = _read_json(source)

    # ---- 水池 ----
    pools: dict[str, Pool] = {}
    for i, raw in enumerate(data.get("pools", [])):
        ctx = f"pools[{i}]"
        pid = str(_require(raw, "id", ctx))
        area = _as_float(_require(raw, "surface_area", ctx), f"{ctx}.surface_area", nonneg=True)
        curve_raw = raw.get("area_curve", [])
        curve = [
            (_as_float(z, f"{ctx}.area_curve[{j}].elevation"),
             _as_float(a, f"{ctx}.area_curve[{j}].area", nonneg=True))
            for j, (z, a) in enumerate(curve_raw)
        ]
        if any(b[0] < a[0] for a, b in zip(curve, curve[1:])):
            raise LoaderError(f"{ctx}.area_curve 高程必须升序")
        pool = Pool(
            id=pid,
            name=str(raw.get("name", pid)),
            bounds=_parse_bounds(raw.get("bounds"), f"{ctx}.bounds"),
            surface_area=area,
            area_curve=curve,
            initial_level=raw.get("initial_level"),
            min_level_rate=(
                _as_float(raw["min_level_rate"], f"{ctx}.min_level_rate", nonneg=True)
                if raw.get("min_level_rate") is not None else None
            ),
            max_level_rate=(
                _as_float(raw["max_level_rate"], f"{ctx}.max_level_rate", nonneg=True)
                if raw.get("max_level_rate") is not None else None
            ),
        )
        if pid in pools:
            raise LoaderError(f"水池 id 重复：{pid}")
        pools[pid] = pool

    if not pools:
        raise LoaderError("场景至少需要一个水池")

    # ---- 闸门 ----
    gates: dict[str, Gate] = {}
    for i, raw in enumerate(data.get("gates", [])):
        ctx = f"gates[{i}]"
        gid = str(_require(raw, "id", ctx))
        up = str(_require(raw, "upstream_pool", ctx))
        down = str(_require(raw, "downstream_pool", ctx))
        for pid in (up, down):
            if pid not in pools:
                raise LoaderError(f"{ctx} 引用了不存在的水池 '{pid}'")
        if up == down:
            raise LoaderError(f"{ctx} 上下游水池不能相同")
        gate = Gate(
            id=gid,
            name=str(raw.get("name", gid)),
            upstream_pool=up,
            downstream_pool=down,
            width=_as_float(_require(raw, "width", ctx), f"{ctx}.width", nonneg=True),
            sill_elevation=_as_float(_require(raw, "sill_elevation", ctx), f"{ctx}.sill_elevation"),
            max_opening=_as_float(_require(raw, "max_opening", ctx), f"{ctx}.max_opening", nonneg=True),
            discharge_coef=_as_float(raw.get("discharge_coef", 0.62), f"{ctx}.discharge_coef", nonneg=True),
            weir_coef=_as_float(raw.get("weir_coef", 0.385), f"{ctx}.weir_coef", nonneg=True),
            gate_to_weir_ratio=_as_float(raw.get("gate_to_weir_ratio", 0.65), f"{ctx}.gate_to_weir_ratio", nonneg=True),
            open_rate=(
                _as_float(raw["open_rate"], f"{ctx}.open_rate", nonneg=True)
                if raw.get("open_rate") is not None else None
            ),
            close_rate=(
                _as_float(raw["close_rate"], f"{ctx}.close_rate", nonneg=True)
                if raw.get("close_rate") is not None else None
            ),
            status=GateStatus(str(raw.get("status", "NORMAL")).upper()),
            initial_opening=_as_float(raw.get("initial_opening", 0.0), f"{ctx}.initial_opening", nonneg=True),
        )
        if gate.initial_opening > gate.max_opening + 1e-9:
            raise LoaderError(f"{ctx}.initial_opening 超过最大开度")
        if gid in gates:
            raise LoaderError(f"闸门 id 重复：{gid}")
        gates[gid] = gate

    if not gates:
        raise LoaderError("场景至少需要一个闸门")

    # ---- 系统约束 ----
    raw_c = data.get("constraints", {})
    constraints = SystemConstraints(
        min_head_diff=(
            _as_float(raw_c["min_head_diff"], "constraints.min_head_diff", nonneg=True)
            if raw_c.get("min_head_diff") is not None else None
        ),
        max_head_diff=(
            _as_float(raw_c["max_head_diff"], "constraints.max_head_diff", nonneg=True)
            if raw_c.get("max_head_diff") is not None else None
        ),
        min_total_flow=(
            _as_float(raw_c["min_total_flow"], "constraints.min_total_flow", nonneg=True)
            if raw_c.get("min_total_flow") is not None else None
        ),
        warning_margin=_as_float(raw_c.get("warning_margin", 0.10), "constraints.warning_margin", nonneg=True),
    )
    if (
        constraints.min_head_diff is not None
        and constraints.max_head_diff is not None
        and constraints.min_head_diff > constraints.max_head_diff
    ):
        raise LoaderError("constraints.min_head_diff 大于 max_head_diff")

    # ---- 边界过程线 ----
    inflows = {
        pid: _parse_series(pts, f"inflows['{pid}']")
        for pid, pts in data.get("inflows", {}).items()
    }
    withdrawals = {
        pid: _parse_series(pts, f"withdrawals['{pid}']")
        for pid, pts in data.get("withdrawals", {}).items()
    }
    for group, series_map in (("inflows", inflows), ("withdrawals", withdrawals)):
        for pid in series_map:
            if pid not in pools:
                raise LoaderError(f"{group} 引用了不存在的水池 '{pid}'")

    start_time = None
    if data.get("start_time"):
        start_time = _parse_time(str(data["start_time"]), "start_time", base=None)

    scenario = Scenario(
        pools=pools,
        gates=gates,
        constraints=constraints,
        inflows=inflows,
        withdrawals=withdrawals,
        start_time=start_time,
        step_hours=_as_float(data.get("step_hours", 0.25), "step_hours", nonneg=True),
        default_horizon_hours=_as_float(
            data.get("default_horizon_hours", 6.0), "default_horizon_hours", nonneg=True
        ),
    )
    if scenario.step_hours <= 0:
        raise LoaderError("step_hours 必须大于 0")

    # 起始水位完整性
    for pid, pool in pools.items():
        if pool.initial_level is None:
            raise LoaderError(f"水池 '{pid}' 缺少 initial_level")
        if pool.bounds.check(pool.initial_level) is not None:
            raise LoaderError(
                f"水池 '{pid}' 起始水位 {pool.initial_level}m 已超出自身允许区间，无法核验"
            )
    return scenario


def _parse_time(text: str, ctx: str, base: datetime | None) -> datetime:
    """支持绝对 ISO8601，或以 +2h / +30m 表示相对基准时间。"""
    text = text.strip()
    if text.startswith("+"):
        if base is None:
            raise LoaderError(f"{ctx} 使用了相对时间 '{text}'，但场景没有 start_time 作为基准")
        token = text[1:]
        try:
            if token.endswith("h"):
                delta = float(token[:-1])
            elif token.endswith("m"):
                delta = float(token[:-1]) / 60.0
            else:
                raise ValueError
        except ValueError as exc:
            raise LoaderError(f"{ctx} 相对时间格式错误：'{text}'（应为 +2h / +30m）") from exc
        return base + timedelta(hours=delta)
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise LoaderError(f"{ctx} 时间格式错误：'{text}'（ISO8601 或 +2h）") from exc


def load_instructions(
    source: str | Path | list[dict[str, Any]],
    scenario: Scenario | None = None,
) -> list[Instruction]:
    """装载指令列表。时间可用 ISO8601；场景提供 start_time 时也可用 '+2h'。"""
    if isinstance(source, (list, dict)):
        data = source
    else:
        data = _read_json(source)
    raw_list = data.get("instructions", data) if isinstance(data, dict) else data
    if not isinstance(raw_list, list):
        raise LoaderError("指令文件应为数组，或含 'instructions' 数组的对象")

    base = scenario.start_time if scenario else None
    instructions: list[Instruction] = []
    for i, raw in enumerate(raw_list):
        ctx = f"instructions[{i}]"
        action = GateAction(str(_require(raw, "action", ctx)).upper())
        target_opening = target_flow = None
        if action is GateAction.SET_OPENING:
            target_opening = _as_float(
                _require(raw, "target_opening", ctx), f"{ctx}.target_opening", nonneg=True
            )
        else:
            target_flow = _as_float(
                _require(raw, "target_flow", ctx), f"{ctx}.target_flow"
            )
        issued = _parse_time(str(_require(raw, "issued_at", ctx)), f"{ctx}.issued_at", base)
        instructions.append(
            Instruction(
                gate_id=str(_require(raw, "gate_id", ctx)),
                action=action,
                issued_at=issued,
                target_opening=target_opening,
                target_flow=target_flow,
                duration_hours=(
                    _as_float(raw["duration_hours"], f"{ctx}.duration_hours", nonneg=True)
                    if raw.get("duration_hours") is not None else None
                ),
                hold_hours=_as_float(raw.get("hold_hours", 6.0), f"{ctx}.hold_hours", nonneg=True),
                note=str(raw.get("note", "")),
                seq=raw.get("seq", i + 1),
            )
        )
    return instructions
