"""水利闸门调度指令核验系统（gate-guard）。

对外主要接口：

- :class:`~gate_guard.engine.InstructionVerifier`：逐条核验调度指令；
- :class:`~gate_guard.dispatcher.GateDispatcher`：核验通过的指令才下发，冲突直接阻断；
- :func:`~gate_guard.loader.load_scenario` / :func:`~gate_guard.loader.load_instructions`：装载场景与指令。
"""

from .models import (
    Gate,
    GateStatus,
    Instruction,
    LevelBounds,
    Pool,
    Scenario,
    Severity,
    SystemConstraints,
    SystemState,
    Verdict,
    Violation,
)
from .engine import InstructionVerifier
from .dispatcher import BlockedInstructionError, GateDispatcher
from .loader import LoaderError, load_instructions, load_scenario

__all__ = [
    "Gate",
    "GateStatus",
    "Instruction",
    "LevelBounds",
    "Pool",
    "Scenario",
    "Severity",
    "SystemConstraints",
    "SystemState",
    "InstructionVerifier",
    "BlockedInstructionError",
    "GateDispatcher",
    "LoaderError",
    "load_instructions",
    "load_scenario",
    "Verdict",
    "Violation",
]
