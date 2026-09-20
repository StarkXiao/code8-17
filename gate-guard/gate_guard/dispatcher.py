"""指令下发关口：核验不通过（存在 ERROR 冲突）的指令一律阻断。

这是核验系统与实际执行机构（PLC / SCADA）之间的唯一通道。
被阻断的指令不会更新任何内部状态；通过的指令才被标记“已放行”，
其目标开度进入当前系统状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .engine import InstructionVerifier, initial_state
from .models import Instruction, Scenario, SystemState, Verdict


class BlockedInstructionError(Exception):
    """指令与约束冲突、被阻断下发。"""

    def __init__(self, verdict: Verdict) -> None:
        self.verdict = verdict
        codes = ", ".join(sorted({v.code for v in verdict.errors}))
        super().__init__(
            f"指令已阻断（闸门 {verdict.instruction.gate_id}，"
            f"{verdict.instruction.target_value_text}），冲突规则：{codes}"
        )


@dataclass
class DispatchResult:
    instruction: Instruction
    verdict: Verdict
    dispatched: bool  # True=已放行；False=已阻断

    @property
    def blocked(self) -> bool:
        return not self.dispatched


class GateDispatcher:
    """逐条核验并放行/阻断指令；按真实时钟维护系统状态。"""

    def __init__(self, scenario: Scenario, *, strict: bool = False) -> None:
        self.scenario = scenario
        self.verifier = InstructionVerifier(scenario, warnings_as_errors=strict)
        self.state: SystemState = initial_state(scenario)
        self.clock: datetime | None = scenario.start_time
        self.history: list[DispatchResult] = []

    def submit(self, instruction: Instruction, *, raise_on_block: bool = False) -> DispatchResult:
        """提交一条指令；有冲突直接阻断，不产生任何下发动作。"""
        if self.clock is not None and instruction.issued_at > self.clock:
            gap_h = (instruction.issued_at - self.clock).total_seconds() / 3600.0
            if gap_h > 0:
                traj = self.verifier._evolve_to(self.state, gap_h)  # noqa: SLF001
                self.state = traj
        self.clock = instruction.issued_at

        verdict = self.verifier.verify(instruction, self.state)
        result = DispatchResult(
            instruction=instruction, verdict=verdict, dispatched=verdict.accepted
        )
        self.history.append(result)

        if verdict.accepted:
            gate = self.scenario.gates[instruction.gate_id]
            target = (
                instruction.target_opening
                if instruction.target_opening is not None
                else (verdict.resolved_opening or 0.0)
            )
            # 已放行：目标开度进入系统状态（实际启闭过程由执行机构按 duration 完成）
            self.state.openings[gate.id] = target
        elif raise_on_block:
            raise BlockedInstructionError(verdict)
        return result

    def submit_batch(
        self, instructions: list[Instruction], *, stop_on_block: bool = False
    ) -> list[DispatchResult]:
        """批量提交（按时间排序）。默认继续核验后续指令，逐条独立放行/阻断。"""
        results: list[DispatchResult] = []
        ordered = sorted(instructions, key=lambda x: (x.issued_at, x.seq or 0))
        for ins in ordered:
            result = self.submit(ins)
            results.append(result)
            if result.blocked and stop_on_block:
                break
        return results
