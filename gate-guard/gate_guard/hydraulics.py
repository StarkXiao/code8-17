"""简化的闸孔水力学计算。

采用工程上常用的判别与公式（宽顶堰上的平板闸门）：

* 相对开度 ``e/H₁ < 0.65``：闸孔出流
    - 自由出流  ``Q = μ b e √(2g(H₁-e))``
    - 淹没出流  ``Q = μ b e √(2g(H₁-H₂))``  （下游水位超过闸孔后）
* 相对开度 ``e/H₁ ≥ 0.65``：宽顶堰流
    ``Q = m b √(2g) H₁^1.5 · C_s``
    淹没系数用 Villemonte 形式
    ``C_s = (1 - 0.85·(H₂/H₁)^1.5)^0.385``。
* 下游水头高于上游时为倒灌，流量取负值（核验中按严重冲突处理）。

该模型面向调度指令核验（判断趋势与越界），不替代设计阶段的水工模型试验；
流量系数 μ、堰流系数 m 均可在闸门配置中按实测率定结果覆盖。
"""

from __future__ import annotations

import math

from .models import Gate

G = 9.81
_SQRT_2G = math.sqrt(2.0 * G)


def heads(gate: Gate, level_up: float, level_down: float) -> tuple[float, float]:
    """返回堰顶以上上、下游水头 (H₁, H₂)，不为负。"""
    h1 = max(0.0, level_up - gate.sill_elevation)
    h2 = max(0.0, level_down - gate.sill_elevation)
    return h1, h2


def _weir_submergence_factor(h1: float, h2: float) -> float:
    # 下游水位低于堰顶（h2<=0）时为自由出流；下游淹没系数按 Villemonte 折减
    if h1 <= 0:
        return 0.0
    if h2 <= 0:
        return 1.0
    if h2 >= h1:
        return 0.0
    s = (h2 / h1) ** 1.5
    return max(0.0, min(1.0, (1.0 - 0.85 * s) ** 0.385))


def gate_discharge(gate: Gate, level_up: float, level_down: float, opening: float) -> float:
    """计算单闸下泄流量（m³/s），向下游为正，倒灌为负。"""
    e = max(0.0, min(opening, gate.max_opening))
    if e <= 0:
        return 0.0
    h1, h2 = heads(gate, level_up, level_down)

    # 倒灌：以下游为工作水头镜像计算
    if h2 > h1:
        if e >= gate.gate_to_weir_ratio * h2:
            return -gate.weir_coef * gate.width * _SQRT_2G * h2 ** 1.5
        return -gate.discharge_coef * gate.width * e * _SQRT_2G * math.sqrt(h2 - h1)

    if h1 <= 0:
        return 0.0

    # 堰流（含淹没）
    if e >= gate.gate_to_weir_ratio * h1:
        cs = _weir_submergence_factor(h1, h2)
        return gate.weir_coef * gate.width * _SQRT_2G * h1 ** 1.5 * cs

    # 闸孔出流：下游水位没过闸孔 → 淹没孔流
    if h2 > e:
        return gate.discharge_coef * gate.width * e * _SQRT_2G * math.sqrt(h1 - h2)
    return gate.discharge_coef * gate.width * e * _SQRT_2G * math.sqrt(h1 - e)


def opening_for_flow(
    gate: Gate, level_up: float, level_down: float, target_flow: float
) -> tuple[float, float, bool]:
    """把目标流量反推为闸门开度。

    流量关于开度单调不减（孔流段随开度增大，转入堰流段后保持不变），
    因此用二分法求“能达到目标流量的最小开度”。

    :returns: (建议开度, 该开度下实际流量, 是否可行)。
              目标超过该水头下最大泄流能力时不可行。
    """
    if target_flow <= 0:
        return 0.0, 0.0, True

    q_max = gate_discharge(gate, level_up, level_down, gate.max_opening)
    if target_flow > q_max + 1e-9:
        return gate.max_opening, q_max, False

    lo, hi = 0.0, gate.max_opening
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if gate_discharge(gate, level_up, level_down, mid) >= target_flow:
            hi = mid
        else:
            lo = mid
    opening = (lo + hi) / 2.0
    return opening, gate_discharge(gate, level_up, level_down, opening), True
