"""边界过程线（入流 / 取用水）的分段线性插值。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Series:
    """以相对小时为横轴的流量过程线，端点之外保持首尾值。"""

    points: list[tuple[float, float]]

    def __post_init__(self) -> None:
        if not self.points:
            raise ValueError("过程线不能为空")
        if any(b[0] < a[0] for a, b in zip(self.points, self.points[1:])):
            raise ValueError("过程线时间点必须升序")

    def value_at(self, hours: float) -> float:
        pts = self.points
        if hours <= pts[0][0]:
            return pts[0][1]
        if hours >= pts[-1][0]:
            return pts[-1][1]
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            if t0 <= hours <= t1:
                if t1 == t0:
                    return v1
                r = (hours - t0) / (t1 - t0)
                return v0 + r * (v1 - v0)
        return pts[-1][1]
