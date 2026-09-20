"""核验引擎：构建初始状态、执行水量平衡前推模拟、逐条核验调度指令。

模拟采用显式欧拉水量平衡：

    dV = ΣQ·dt，  dz = dV / A(z)

其中 ΣQ 包含闸门过流（按 :mod:`gate_guard.hydraulics`）与边界入流/取用水。
模型步长取场景配置的 ``step_hours``（默认 15 分钟），前瞻时长取指令的
``hold_hours``（目标开度保持期）。
"""

from __future__ import annotations

from datetime import datetime

from .hydraulics import gate_discharge, opening_for_flow
from .models import (
    Gate,
    Instruction,
    Scenario,
    Severity,
    SystemState,
    Verdict,
    Violation,
)
from .rules import (
    SIMULATION_RULES,
    STATIC_RULES,
    CheckContext,
    SimulationContext,
    SimulationPoint,
)
from .series import Series


def initial_state(scenario: Scenario) -> SystemState:
    """从场景配置构建系统初始状态。"""
    levels = {pid: pool.initial_level for pid, pool in scenario.pools.items()}
    openings = {gid: gate.initial_opening for gid, gate in scenario.gates.items()}
    return SystemState(levels=levels, openings=openings)  # type: ignore[arg-type]


def _travel_time(gate: Gate, current: float, target: float, duration_hours: float | None) -> float:
    """开度从 current 到 target 实际需要的小时数（计入设备启闭限速）。"""
    delta = abs(target - current)
    if delta <= 1e-12:
        return 0.0
    rate = gate.open_rate if target > current else gate.close_rate
    t_device = delta / rate if rate and rate > 0 else 0.0
    if duration_hours and duration_hours > 0:
        return max(duration_hours, t_device)
    return t_device  # 0 表示一步到位


def simulate(
    scenario: Scenario,
    state: SystemState,
    horizon_hours: float,
    *,
    moving_gate: Gate | None = None,
    target_opening: float = 0.0,
    duration_hours: float | None = None,
) -> list[SimulationPoint]:
    """从给定状态前推 ``horizon_hours``，返回逐时刻轨迹（含 t=0）。

    ``moving_gate`` 为 None 时为自由演进（各闸维持当前开度）。
    """
    dt = scenario.step_hours
    n_steps = max(1, int(round(horizon_hours / dt)))

    inflow_series = {pid: Series(pts) for pid, pts in scenario.inflows.items()}
    withdraw_series = {pid: Series(pts) for pid, pts in scenario.withdrawals.items()}

    levels = dict(state.levels)
    openings = dict(state.openings)

    start_opening = openings.get(moving_gate.id, 0.0) if moving_gate else 0.0
    travel = (
        _travel_time(moving_gate, start_opening, target_opening, duration_hours)
        if moving_gate else 0.0
    )

    def gate_opening_at(t: float) -> float:
        if travel <= 0:
            return target_opening
        if t >= travel:
            return target_opening
        r = t / travel
        return start_opening + r * (target_opening - start_opening)

    def flows_at(op: dict[str, float], lv: dict[str, float]) -> dict[str, float]:
        return {
            gid: gate_discharge(g, lv[g.upstream_pool], lv[g.downstream_pool], op[gid])
            for gid, g in scenario.gates.items()
        }

    points: list[SimulationPoint] = [
        SimulationPoint(0.0, dict(levels), dict(openings),
                        flows_at(openings, levels))
    ]

    for step in range(1, n_steps + 1):
        t = step * dt
        if moving_gate is not None:
            openings[moving_gate.id] = gate_opening_at(t)

        flows = flows_at(openings, levels)
        net = {pid: 0.0 for pid in scenario.pools}
        for pid, series in inflow_series.items():
            net[pid] += series.value_at(t)
        for pid, series in withdraw_series.items():
            net[pid] -= series.value_at(t)
        for gid, gate in scenario.gates.items():
            q = flows[gid]
            net[gate.upstream_pool] -= q
            net[gate.downstream_pool] += q

        for pid, pool in scenario.pools.items():
            volume_change = net[pid] * dt * 3600.0  # m³
            area = pool.area_at(levels[pid])
            levels[pid] += volume_change / area

        points.append(SimulationPoint(t, dict(levels), dict(openings), dict(flows)))

    return points


class InstructionVerifier:
    """逐条核验调度指令与上下游水位约束是否冲突。"""

    def __init__(self, scenario: Scenario, *, warnings_as_errors: bool = False) -> None:
        self.scenario = scenario
        self.warnings_as_errors = warnings_as_errors

    def verify(
        self,
        instruction: Instruction,
        state: SystemState | None = None,
        horizon_hours: float | None = None,
    ) -> Verdict:
        state = state or initial_state(self.scenario)
        gate = self.scenario.gates.get(instruction.gate_id)

        if gate is None:
            return Verdict(
                instruction=instruction,
                accepted=False,
                violations=[Violation(
                    code="GATE_NOT_FOUND", severity=Severity.ERROR,
                    gate_id=instruction.gate_id,
                    message=f"指令目标闸门 '{instruction.gate_id}' 不存在，无法核验/下发",
                )],
                checked_at=instruction.issued_at,
            )

        # 目标流量 → 开度反推
        resolved_opening: float | None = None
        resolved_flow: float | None = None
        feasible = True
        if instruction.action.value == "SET_FLOW":
            resolved_opening, resolved_flow, feasible = opening_for_flow(
                gate,
                state.levels[gate.upstream_pool],
                state.levels[gate.downstream_pool],
                instruction.target_flow or 0.0,
            )

        ctx = CheckContext(
            scenario=self.scenario,
            state=state,
            instruction=instruction,
            resolved_opening=resolved_opening,
            resolved_flow=resolved_flow,
            flow_feasible=feasible,
        )
        violations: list[Violation] = []
        for rule in STATIC_RULES:
            violations.extend(rule.check(ctx))

        # 前瞻模拟：按目标开度（反推开度）动作并保持
        target_opening = (
            instruction.target_opening
            if instruction.target_opening is not None
            else (resolved_opening or 0.0)
        )
        horizon = horizon_hours if horizon_hours is not None else instruction.hold_hours
        trajectory = simulate(
            self.scenario, state, max(horizon, self.scenario.step_hours),
            moving_gate=gate,
            target_opening=target_opening,
            duration_hours=instruction.duration_hours,
        )
        sim_ctx = SimulationContext(
            scenario=self.scenario, instruction=instruction,
            trajectory=trajectory, target_opening=target_opening,
        )
        for rule in SIMULATION_RULES:
            violations.extend(rule.check(sim_ctx))

        if self.warnings_as_errors:
            violations = [
                Violation(
                    code=v.code, severity=Severity.ERROR, message=v.message,
                    pool_id=v.pool_id, gate_id=v.gate_id, at_hours=v.at_hours,
                    observed=v.observed,
                )
                if v.severity is Severity.WARNING else v
                for v in violations
            ]

        extrema: dict[str, tuple[float, float]] = {}
        for pid in self.scenario.pools:
            seq = [pt.levels[pid] for pt in trajectory]
            extrema[pid] = (min(seq), max(seq))

        return Verdict(
            instruction=instruction,
            accepted=not any(v.severity is Severity.ERROR for v in violations),
            violations=violations,
            resolved_opening=resolved_opening,
            resolved_flow=resolved_flow,
            simulated_levels=extrema,
            checked_at=instruction.issued_at,
        )

    def verify_batch(
        self,
        instructions: list[Instruction],
        *,
        cascade: bool = True,
    ) -> list[Verdict]:
        """按下发时间排序逐条核验。

        ``cascade=True`` 时，前序**通过**指令的目标开度会作用于后续指令的
        核验起点（级联生效）；被阻断指令不改变系统状态。指令间隔期间按
        现有开度自由演进水位。
        """
        ordered = sorted(instructions, key=lambda x: (x.issued_at, x.seq or 0))
        state = initial_state(self.scenario)
        clock: datetime | None = self.scenario.start_time
        verdicts: list[Verdict] = []

        for ins in ordered:
            if cascade and clock is not None and ins.issued_at > clock:
                gap_h = (ins.issued_at - clock).total_seconds() / 3600.0
                if gap_h > 0:
                    state = self._evolve_to(state, gap_h)
            clock = ins.issued_at

            verdict = self.verify(ins, state)
            verdicts.append(verdict)

            if cascade and verdict.accepted:
                gate = self.scenario.gates[ins.gate_id]
                target = (
                    ins.target_opening
                    if ins.target_opening is not None
                    else (verdict.resolved_opening or 0.0)
                )
                state.openings[gate.id] = target

        return verdicts

    def _evolve_to(self, state: SystemState, gap_hours: float) -> SystemState:
        """无新指令时按当前开度自由演进 ``gap_hours`` 后的系统状态。"""
        trajectory = simulate(self.scenario, state, gap_hours)
        last = trajectory[-1]
        return SystemState(levels=last.levels, openings=last.openings)
