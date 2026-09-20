"""核验规则。

分两类：

* **静态规则**：依据下发瞬间的系统状态与指令目标态做即时校核
  （设备状态、开度范围、启闭速率、水位差、当前水位、生态基流、泄流能力）。
* **模拟规则**：对指令执行后的水位演进轨迹逐时刻校核
  （水位上下限、接近限值预警、水位变幅、水位差/倒灌、生态基流）。

每条规则返回若干 :class:`Violation`；``ERROR`` 阻断下发，``WARNING`` 仅提示。
规则之间相互独立，新增规则只需实现 ``check`` 并注册到对应规则表。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .hydraulics import gate_discharge
from .models import (
    Gate,
    Instruction,
    Scenario,
    Severity,
    SystemState,
    Violation,
)


# ---------------------------------------------------------------------------
# 校核上下文与轨迹点
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CheckContext:
    """单条指令的静态校核上下文。"""

    scenario: Scenario
    state: SystemState
    instruction: Instruction
    # SET_FLOW 经水力反推得到的目标开度；SET_OPENING 时为 None
    resolved_opening: float | None = None
    resolved_flow: float | None = None
    flow_feasible: bool = True

    @property
    def gate(self) -> Gate:
        return self.scenario.gates[self.instruction.gate_id]


@dataclass(frozen=True)
class SimulationPoint:
    """前推模拟中的一个时刻。"""

    t_hours: float
    levels: dict[str, float]
    openings: dict[str, float]
    flows: dict[str, float]  # gate_id -> 下泄流量（含正负）


@dataclass
class SimulationContext:
    scenario: Scenario
    instruction: Instruction
    trajectory: list[SimulationPoint]
    target_opening: float


# ---------------------------------------------------------------------------
# 规则协议与基类
# ---------------------------------------------------------------------------

class StaticRule(Protocol):
    code: str

    def check(self, ctx: CheckContext) -> list[Violation]: ...


class SimulationRule(Protocol):
    code: str

    def check(self, ctx: SimulationContext) -> list[Violation]: ...


def _target_opening_of(ctx: CheckContext) -> float:
    ins = ctx.instruction
    if ins.action.value == "SET_OPENING":
        return ins.target_opening if ins.target_opening is not None else 0.0
    return ctx.resolved_opening if ctx.resolved_opening is not None else 0.0


# ---------------------------------------------------------------------------
# 静态规则
# ---------------------------------------------------------------------------

class GateAvailableRule:
    """检修/故障闸门不得操作。"""

    code = "GATE_UNAVAILABLE"

    def check(self, ctx: CheckContext) -> list[Violation]:
        gate = ctx.gate
        if gate.status.value != "NORMAL":
            label = {"MAINTENANCE": "检修中", "FAULT": "故障"}.get(gate.status.value, gate.status.value)
            return [Violation(
                code=self.code, severity=Severity.ERROR, gate_id=gate.id,
                message=f"闸门 {gate.name}（{gate.id}）当前{label}，禁止下达操作指令",
            )]
        return []


class OpeningRangeRule:
    """目标开度必须在 [0, 最大开度] 内。"""

    code = "OPENING_OUT_OF_RANGE"

    def check(self, ctx: CheckContext) -> list[Violation]:
        gate = ctx.gate
        ins = ctx.instruction
        if ins.action.value != "SET_OPENING" or ins.target_opening is None:
            return []
        target = ins.target_opening
        if target > gate.max_opening + 1e-9:
            return [Violation(
                code=self.code, severity=Severity.ERROR, gate_id=gate.id,
                message=(f"目标开度 {target:.2f}m 超过闸门 {gate.name} 最大允许开度 "
                         f"{gate.max_opening:.2f}m"),
                observed={"target_opening": target, "max_opening": gate.max_opening},
            )]
        return []


class FlowCapacityRule:
    """目标流量必须为正且在当前水头下的泄流能力以内。"""

    code = "FLOW_CAPACITY_EXCEEDED"

    def check(self, ctx: CheckContext) -> list[Violation]:
        ins = ctx.instruction
        if ins.action.value != "SET_FLOW" or ins.target_flow is None:
            return []
        gate = ctx.gate
        if ins.target_flow < 0:
            return [Violation(
                code=self.code, severity=Severity.ERROR, gate_id=gate.id,
                message=f"目标流量 {ins.target_flow:.2f} m³/s 为负值，禁止下达反向（倒灌）泄放指令",
                observed={"target_flow": ins.target_flow},
            )]
        if not ctx.flow_feasible:
            qmax = ctx.resolved_flow
            return [Violation(
                code=self.code, severity=Severity.ERROR, gate_id=gate.id,
                message=(f"当前水位条件下闸门 {gate.name} 最大泄流能力约 "
                         f"{qmax:.2f} m³/s，达不到目标流量 {ins.target_flow:.2f} m³/s；"
                         f"强行操作将使上下游水位失控"),
                observed={"target_flow": ins.target_flow, "capacity": qmax or 0.0},
            )]
        return []


class GateMotionRateRule:
    """启闭动作所需速率不得超过设备启闭能力。"""

    code = "GATE_MOTION_RATE_EXCEEDED"

    def check(self, ctx: CheckContext) -> list[Violation]:
        gate = ctx.gate
        ins = ctx.instruction
        # 目标开度已超闸门上限时，由 OPENING_OUT_OF_RANGE 负责，不再叠加速率冲突
        if (
            ins.action.value == "SET_OPENING"
            and ins.target_opening is not None
            and ins.target_opening > gate.max_opening + 1e-9
        ):
            return []
        duration = ins.duration_hours
        if duration is None or duration <= 0:
            return []  # 未给动作时限：由设备自身限速，模拟中按设备速率动作
        current = ctx.state.openings.get(gate.id, gate.initial_opening)
        # SET_FLOW 反推开度不可达时，启闭速率校核无意义（已由泄流能力规则阻断）
        if ins.action.value == "SET_FLOW" and not ctx.flow_feasible:
            return []
        target = _target_opening_of(ctx)
        delta = target - current
        if abs(delta) <= 1e-9:
            return []
        required = abs(delta) / duration
        if delta > 0 and gate.open_rate is not None and required > gate.open_rate + 1e-9:
            return [Violation(
                code=self.code, severity=Severity.ERROR, gate_id=gate.id,
                message=(f"要求 {duration:.2f}h 内开启 {delta:.2f}m（速率 {required:.2f}m/h），"
                         f"超过闸门 {gate.name} 最大开启速率 {gate.open_rate:.2f}m/h"),
                observed={"required_rate": required, "max_rate": gate.open_rate},
                at_hours=0.0,
            )]
        if delta < 0 and gate.close_rate is not None and required > gate.close_rate + 1e-9:
            return [Violation(
                code=self.code, severity=Severity.ERROR, gate_id=gate.id,
                message=(f"要求 {duration:.2f}h 内关闭 {abs(delta):.2f}m（速率 {required:.2f}m/h），"
                         f"超过闸门 {gate.name} 最大关闭速率 {gate.close_rate:.2f}m/h"),
                observed={"required_rate": required, "max_rate": gate.close_rate},
                at_hours=0.0,
            )]
        return []


class CurrentLevelRule:
    """下发瞬间各水池水位必须在允许区间内。"""

    code = "CURRENT_LEVEL_OUT_OF_BOUNDS"

    def check(self, ctx: CheckContext) -> list[Violation]:
        out: list[Violation] = []
        for pid, level in ctx.state.levels.items():
            pool = ctx.scenario.pools[pid]
            desc = pool.bounds.check(level)
            if desc:
                out.append(Violation(
                    code=self.code, severity=Severity.ERROR, pool_id=pid,
                    message=f"水池 {pool.name} 当前{desc}，禁止再叠加调度动作，应先处置水位风险",
                    observed={"level": level}, at_hours=0.0,
                ))
        return out


class ImmediateHeadRule:
    """指令目标态下的瞬时水位差校核（结构上限阻断；下限不足预警；倒灌阻断）。"""

    code = "HEAD_DIFF"

    def check(self, ctx: CheckContext) -> list[Violation]:
        return _head_violations(
            scenario=ctx.scenario,
            gate=ctx.gate,
            level_up=ctx.state.levels[ctx.gate.upstream_pool],
            level_down=ctx.state.levels[ctx.gate.downstream_pool],
            opening=_target_opening_of(ctx),
            at_hours=0.0,
        )


class ImmediateEcoFlowRule:
    """指令目标态下全系统下泄流量不得低于生态基流。"""

    code = "ECO_FLOW_SHORTAGE"

    def check(self, ctx: CheckContext) -> list[Violation]:
        minimum = ctx.scenario.constraints.min_total_flow
        if minimum is None:
            return []
        total = _total_discharge(ctx.scenario, ctx.state, override_gate=ctx.gate.id,
                                 override_opening=_target_opening_of(ctx))
        if total < minimum - 1e-9:
            return [Violation(
                code=self.code, severity=Severity.ERROR,
                gate_id=ctx.gate.id,
                message=(f"按指令执行后全系统下泄流量仅 {total:.2f} m³/s，"
                         f"低于生态基流 {minimum:.2f} m³/s"),
                observed={"total_flow": total, "min_total_flow": minimum}, at_hours=0.0,
            )]
        return []


STATIC_RULES: list[StaticRule] = [
    GateAvailableRule(),
    OpeningRangeRule(),
    FlowCapacityRule(),
    GateMotionRateRule(),
    CurrentLevelRule(),
    ImmediateHeadRule(),
    ImmediateEcoFlowRule(),
]


# ---------------------------------------------------------------------------
# 模拟轨迹规则
# ---------------------------------------------------------------------------

class TrajectoryLevelRule:
    """演进过程中各水池水位不得越限；接近限值给出预警。"""

    code = "LEVEL_OUT_OF_BOUNDS"

    def check(self, ctx: SimulationContext) -> list[Violation]:
        out: list[Violation] = []
        margin = ctx.scenario.constraints.warning_margin
        reported: dict[tuple[str, str], bool] = {}
        for pt in ctx.trajectory:
            if pt.t_hours <= 0:
                continue  # t=0 由静态规则负责
            for pid, level in pt.levels.items():
                pool = ctx.scenario.pools[pid]
                desc = pool.bounds.check(level)
                key = (pid, "OOB")
                if desc:
                    if key not in reported:  # 同一水池的越限只报首次
                        reported[key] = True
                        out.append(Violation(
                            code=self.code, severity=Severity.ERROR, pool_id=pid,
                            message=f"水池 {pool.name} 在指令下发后 {pt.t_hours:.2f}h {desc}",
                            observed={"level": level}, at_hours=pt.t_hours,
                        ))
                    continue
                dist = pool.bounds.distance(level)
                near_key = (pid, "NEAR")
                if dist is not None and dist < margin and near_key not in reported:
                    reported[near_key] = True
                    limit = pool.bounds.max if abs(level - (pool.bounds.max or -1e18)) < \
                        abs(level - (pool.bounds.min or 1e18)) else pool.bounds.min
                    out.append(Violation(
                        code="LEVEL_NEAR_LIMIT", severity=Severity.WARNING, pool_id=pid,
                        message=(f"水池 {pool.name} 在 {pt.t_hours:.2f}h 水位 {level:.2f}m，"
                                 f"距限值 {limit:.2f}m 仅 {dist:.2f}m（预警余量 {margin:.2f}m）"),
                        observed={"level": level, "distance": dist}, at_hours=pt.t_hours,
                    ))
        return out


class TrajectoryLevelRateRule:
    """演进过程中水位每小时变幅不得超过水池允许的涨/落速率。"""

    code = "LEVEL_RATE_EXCEEDED"

    def check(self, ctx: SimulationContext) -> list[Violation]:
        out: list[Violation] = []
        traj = ctx.trajectory
        reported: set[tuple[str, str]] = set()  # (pool_id, 涨/落) 只报首次
        for prev, cur in zip(traj, traj[1:]):
            dt = cur.t_hours - prev.t_hours
            if dt <= 0:
                continue
            for pid in cur.levels:
                pool = ctx.scenario.pools[pid]
                rate = (cur.levels[pid] - prev.levels[pid]) / dt
                if pool.max_level_rate is not None and rate > pool.max_level_rate + 1e-9:
                    key = (pid, "rise")
                    if key in reported:
                        continue
                    reported.add(key)
                    out.append(Violation(
                        code=self.code, severity=Severity.ERROR, pool_id=pid,
                        message=(f"水池 {pool.name} 在 {cur.t_hours:.2f}h 水位上涨速率 "
                                 f"{rate:.2f}m/h，超过限值 {pool.max_level_rate:.2f}m/h"),
                        observed={"rate": rate, "limit": pool.max_level_rate},
                        at_hours=cur.t_hours,
                    ))
                if pool.min_level_rate is not None and -rate > pool.min_level_rate + 1e-9:
                    key = (pid, "fall")
                    if key in reported:
                        continue
                    reported.add(key)
                    out.append(Violation(
                        code=self.code, severity=Severity.ERROR, pool_id=pid,
                        message=(f"水池 {pool.name} 在 {cur.t_hours:.2f}h 水位下降速率 "
                                 f"{-rate:.2f}m/h，超过限值 {pool.min_level_rate:.2f}m/h"),
                        observed={"rate": -rate, "limit": pool.min_level_rate},
                        at_hours=cur.t_hours,
                    ))
        return out


class TrajectoryHeadRule:
    """演进过程中逐时刻校核全部闸门的水位差与倒灌。"""

    code = "HEAD_DIFF"

    def check(self, ctx: SimulationContext) -> list[Violation]:
        out: list[Violation] = []
        seen: set[tuple[str, str]] = set()  # (gate_id, code)：只报首次
        for pt in ctx.trajectory:
            if pt.t_hours <= 0:
                continue
            for gate in ctx.scenario.gates.values():
                opening = pt.openings[gate.id]
                violations = _head_violations(
                    scenario=ctx.scenario, gate=gate,
                    level_up=pt.levels[gate.upstream_pool],
                    level_down=pt.levels[gate.downstream_pool],
                    opening=opening, at_hours=pt.t_hours,
                )
                for v in violations:
                    key = (gate.id, v.code)
                    if key not in seen:
                        seen.add(key)
                        out.append(v)
        return out


class TrajectoryEcoFlowRule:
    """演进过程中全系统下泄流量不得持续低于生态基流。"""

    code = "ECO_FLOW_SHORTAGE"

    def check(self, ctx: SimulationContext) -> list[Violation]:
        minimum = ctx.scenario.constraints.min_total_flow
        if minimum is None:
            return []
        out: list[Violation] = []
        reported = False
        for pt in ctx.trajectory:
            if pt.t_hours <= 0:
                continue
            total = sum(pt.flows.values())
            if total < minimum - 1e-9 and not reported:
                out.append(Violation(
                    code=self.code, severity=Severity.ERROR,
                    gate_id=ctx.instruction.gate_id,
                    message=(f"指令下发后 {pt.t_hours:.2f}h 全系统下泄流量降至 "
                             f"{total:.2f} m³/s，低于生态基流 {minimum:.2f} m³/s"),
                    observed={"total_flow": total, "min_total_flow": minimum},
                    at_hours=pt.t_hours,
                ))
                reported = True  # 报首次即可，轨迹后续不重复刷
        return out


SIMULATION_RULES: list[SimulationRule] = [
    TrajectoryLevelRule(),
    TrajectoryLevelRateRule(),
    TrajectoryHeadRule(),
    TrajectoryEcoFlowRule(),
]


# ---------------------------------------------------------------------------
# 共用辅助
# ---------------------------------------------------------------------------

def _head_violations(
    *, scenario: Scenario, gate: Gate, level_up: float, level_down: float,
    opening: float, at_hours: float | None,
) -> list[Violation]:
    """水位差三件套：倒灌（阻断）、上限（阻断）、下限（预警，仅对开启的闸门）。"""
    out: list[Violation] = []
    diff = level_up - level_down
    obs = {"level_up": level_up, "level_down": level_down, "head_diff": diff}

    if opening > 1e-9 and diff < -1e-9:
        out.append(Violation(
            code="REVERSE_FLOW", severity=Severity.ERROR, gate_id=gate.id,
            message=(f"闸门 {gate.name} 下游水位（{level_down:.2f}m）高于上游"
                     f"（{level_up:.2f}m），开启将发生倒灌 {abs(diff):.2f}m"),
            observed=obs, at_hours=at_hours,
        ))
        return out

    c = scenario.constraints
    if c.max_head_diff is not None and diff > c.max_head_diff + 1e-9:
        out.append(Violation(
            code="HEAD_DIFF_EXCEEDED", severity=Severity.ERROR, gate_id=gate.id,
            message=(f"闸门 {gate.name} 上下游水位差 {diff:.2f}m 超过结构安全上限 "
                     f"{c.max_head_diff:.2f}m，开启泄流可能危及消能防冲安全"),
            observed=obs, at_hours=at_hours,
        ))
    if c.min_head_diff is not None and opening > 1e-9 and diff < c.min_head_diff - 1e-9:
        out.append(Violation(
            code="HEAD_TOO_LOW", severity=Severity.WARNING, gate_id=gate.id,
            message=(f"闸门 {gate.name} 上下游水位差仅 {diff:.2f}m，低于要求的最小水头 "
                     f"{c.min_head_diff:.2f}m，过流能力不足且存在反向风险"),
            observed=obs, at_hours=at_hours,
        ))
    return out


def _total_discharge(
    scenario: Scenario, state: SystemState, *,
    override_gate: str | None = None, override_opening: float | None = None,
) -> float:
    total = 0.0
    for gate in scenario.gates.values():
        opening = state.openings.get(gate.id, gate.initial_opening)
        if gate.id == override_gate and override_opening is not None:
            opening = override_opening
        total += gate_discharge(
            gate,
            state.levels[gate.upstream_pool],
            state.levels[gate.downstream_pool],
            opening,
        )
    return total
