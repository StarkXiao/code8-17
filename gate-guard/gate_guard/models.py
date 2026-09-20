"""领域数据模型：水位约束、闸门、水池、调度指令与核验结论。

高程单位：米（m）；流量单位：立方米每秒（m³/s）；时间单位：小时。
所有水位均使用同一高程基准（如黄海高程），不做基准换算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


class Severity(str, Enum):
    """冲突严重级别。``ERROR`` 阻断指令下发，``WARNING`` 仅提示不阻断。"""

    ERROR = "ERROR"
    WARNING = "WARNING"


class GateStatus(str, Enum):
    """闸门设备状态。只有 ``NORMAL`` 状态的闸门允许操作。"""

    NORMAL = "NORMAL"          # 正常可用
    MAINTENANCE = "MAINTENANCE"  # 检修中
    FAULT = "FAULT"            # 故障


class GateAction(str, Enum):
    """指令动作类型。"""

    SET_OPENING = "SET_OPENING"  # 直接设定目标开度（m）
    SET_FLOW = "SET_FLOW"        # 设定目标流量（m³/s），由水力关系反推开度


@dataclass(frozen=True)
class LevelBounds:
    """水位允许区间 [min, max]，可选汛限等附加上限（取其严者）。"""

    min: float | None = None
    max: float | None = None

    def check(self, level: float) -> str | None:
        """返回越界描述；未越界返回 None。"""
        if self.max is not None and level > self.max + 1e-9:
            return f"水位 {level:.2f}m 超过上限 {self.max:.2f}m"
        if self.min is not None and level < self.min - 1e-9:
            return f"水位 {level:.2f}m 低于下限 {self.min:.2f}m"
        return None

    def distance(self, level: float) -> float | None:
        """水位到区间边界的最小余量（m），用于接近上限预警；无约束返回 None。"""
        margins: list[float] = []
        if self.max is not None:
            margins.append(self.max - level)
        if self.min is not None:
            margins.append(level - self.min)
        return min(margins) if margins else None


@dataclass
class Pool:
    """闸控水池（上游库 / 下游河道等）。

    水面面积以高程-面积折点表示，模拟时按线性插值取值，
    相比固定面积更贴近库区形态。
    """

    id: str
    name: str
    bounds: LevelBounds
    surface_area: float  # m²；未提供折点时的固定水面面积
    area_curve: list[tuple[float, float]] = field(default_factory=list)
    # [(高程 m, 面积 m²), ...] 按高程升序
    initial_level: float | None = None  # m，场景起始水位
    min_level_rate: float | None = None  # m/h，水位每小时最大允许下降幅度（正值）
    max_level_rate: float | None = None  # m/h，水位每小时最大允许上升幅度（正值）

    def area_at(self, level: float) -> float:
        """按高程插值水面面积。"""
        curve = self.area_curve
        if not curve:
            return self.surface_area
        if level <= curve[0][0]:
            return curve[0][1]
        if level >= curve[-1][0]:
            return curve[-1][1]
        for (z0, a0), (z1, a1) in zip(curve, curve[1:]):
            if z0 <= level <= z1:
                if z1 == z0:
                    return a1
                t = (level - z0) / (z1 - z0)
                return a0 + t * (a1 - a0)
        return self.surface_area


@dataclass(frozen=True)
class Gate:
    """闸门设备参数（宽顶堰上的平板闸门，自由出流 / 孔流 / 淹没孔流）。"""

    id: str
    name: str
    upstream_pool: str   # 上游水池 id
    downstream_pool: str  # 下游水池 id
    width: float                 # 闸孔净宽 b，m
    sill_elevation: float        # 堰顶（闸孔底板）高程，m
    max_opening: float           # 最大允许开度 e_max，m
    discharge_coef: float = 0.62  # 孔流流量系数 μ
    weir_coef: float = 0.385      # 宽顶堰流量系数 m
    # 闸孔出流→堰流的判别相对开度 e/H 阈值
    gate_to_weir_ratio: float = 0.65
    # 闸门启闭速率限制（m/h，正值）；None 表示不限制
    open_rate: float | None = None
    close_rate: float | None = None
    status: GateStatus = GateStatus.NORMAL
    initial_opening: float = 0.0  # 当前开度，m

    def headwater(self, upstream_level: float) -> float:
        """堰顶以上上游水头 H1。"""
        return max(0.0, upstream_level - self.sill_elevation)


@dataclass(frozen=True)
class SystemConstraints:
    """跨闸门的系统级约束。"""

    # 最小上下游水位差（保证过流/防倒灌），m；None 不限制
    min_head_diff: float | None = None
    # 最大上下游水位差（闸室结构 / 消能防冲安全），m；None 不限制
    max_head_diff: float | None = None
    # 全系统生态基流（所有过流断面合计），m³/s；None 不限制
    min_total_flow: float | None = None
    # 水位距约束边界小于该余量（m）时给出接近预警
    warning_margin: float = 0.10


@dataclass
class Scenario:
    """一次核验所处的完整场景：水池、闸门、系统约束、边界过程线。"""

    pools: dict[str, Pool]
    gates: dict[str, Gate]
    constraints: SystemConstraints
    # 水池边界过程线：pool_id -> [(相对小时, 流量 m³/s)]
    # 对未连任何闸门的源头水池表示入流；其余水池可表示区间汇入/取用水
    inflows: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    withdrawals: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    start_time: datetime | None = None
    # 模拟步长（小时）与默认前瞻时长（小时）
    step_hours: float = 0.25
    default_horizon_hours: float = 6.0


@dataclass(frozen=True)
class Instruction:
    """一条闸门调度指令。

    ``target_opening`` 与 ``target_flow`` 二选一：
    SET_OPENING 直接给开度；SET_FLOW 给目标流量，由水力关系反推可达开度。
    """

    gate_id: str
    action: GateAction
    issued_at: datetime
    target_opening: float | None = None
    target_flow: float | None = None
    # 从当前开度动作到目标开度允许使用的时间（h），影响启闭速率校核
    duration_hours: float | None = None
    # 指令目标开度的最短保持时间（h），决定前瞻模拟的有效时长
    hold_hours: float = 6.0
    note: str = ""
    seq: int | None = None  # 指令序号，便于结果回溯

    @property
    def target_value_text(self) -> str:
        if self.action is GateAction.SET_FLOW:
            return f"目标流量 {self.target_flow} m³/s"
        return f"目标开度 {self.target_opening} m"


@dataclass(frozen=True)
class Violation:
    """单条冲突/预警。"""

    code: str
    severity: Severity
    message: str
    pool_id: str | None = None
    gate_id: str | None = None
    # 冲突发生时刻（相对指令下发的小时数）；静态冲突为 None
    at_hours: float | None = None
    # 触发时的现场量测（水位、流量等），用于追溯
    observed: dict[str, float] = field(default_factory=dict)


@dataclass
class Verdict:
    """一条指令的核验结论。"""

    instruction: Instruction
    accepted: bool  # 无 ERROR 即通过（有 WARNING 仍可下发）
    violations: list[Violation] = field(default_factory=list)
    # 水力反推结果（SET_FLOW 时的可达开度与实际流量）
    resolved_opening: float | None = None
    resolved_flow: float | None = None
    # 前瞻模拟极值记录
    simulated_levels: dict[str, tuple[float, float]] = field(default_factory=dict)
    # pool_id -> (最低水位, 最高水位)
    checked_at: datetime | None = None
    engine_version: str = "1.0"

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is Severity.WARNING]


@dataclass
class SystemState:
    """某一时刻的系统状态：各池水位、各闸开度。"""

    levels: dict[str, float]
    openings: dict[str, float]

    def copy(self) -> "SystemState":
        return SystemState(dict(self.levels), dict(self.openings))
