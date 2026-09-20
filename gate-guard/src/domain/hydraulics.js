/**
 * @file 水力学计算：闸孔出流、水位-流量曲线插值、开度反算、水量平衡。
 *       全部为确定性纯函数，无外部状态。
 */
import { reachById } from '../config/model.js';

const G = 9.81;

/**
 * 闸孔出流能力（标准闸孔公式，在自由/淹没衔接点连续）。
 *
 *   Q = Cd · b · a · √(2g · he)
 *
 * - 自由出流（下游水位不高于堰顶）：he = 上游水位 − 堰顶（堰顶水头）
 * - 淹没出流（下游水位高于堰顶）：  he = 上游水位 − 下游水位（作用水头差）
 * - he ≤ 0 或开度为 0：Q = 0（反向时绝不出流）
 *
 * 当 hDown = sill 时两种取法相等，公式连续，不会出现"刚淹没流量反而跳大"。
 *
 * @param {import('../config/model.js').Gate} gate
 * @param {number} hUp    上游水位（m）
 * @param {number} hDown  下游水位（m）
 * @param {number} opening 开度（m）
 * @returns {number} 流量 m³/s（非负）
 */
export function gateFlow(gate, hUp, hDown, opening) {
  if (opening <= 0) return 0;
  const headUp = hUp - gate.sill;
  if (headUp <= 0) return 0;
  const he = hDown > gate.sill
    ? Math.min(hUp - hDown, headUp) // 淹没出流
    : headUp;                        // 自由出流
  if (he <= 0) return 0;
  return gate.dischargeCoef * gate.width * opening * Math.sqrt(2 * G * he);
}

/**
 * 指定目标流量反算所需开度；给不出（超过物理能力/反向水头）返回 null。
 *
 * @param {import('../config/model.js').Gate} gate
 * @param {number} hUp
 * @param {number} hDown
 * @param {number} targetQ 目标流量（必须 > 0；关闸目标 0 调用方自行处理）
 * @returns {{ opening: number, mode: 'free'|'submerged' } | null}
 */
export function openingForFlow(gate, hUp, hDown, targetQ) {
  if (!(targetQ > 0)) return null;
  const headUp = hUp - gate.sill;
  if (headUp <= 0) return null;
  const submerged = hDown > gate.sill;
  const he = submerged ? Math.min(hUp - hDown, headUp) : headUp;
  if (he <= 0) return null;

  const opening = targetQ / (gate.dischargeCoef * gate.width * Math.sqrt(2 * G * he));
  if (opening > gate.maxOpening + 1e-9) return null; // 物理上过不去
  return { opening: Math.min(opening, gate.maxOpening), mode: submerged ? 'submerged' : 'free' };
}

/** 闸门当前水位组合下的物理最大出流（满开） */
export function maxPhysicalFlow(gate, hUp, hDown) {
  return gateFlow(gate, hUp, hDown, gate.maxOpening);
}

/** 线性插值（超出端点按端点外推，曲线通常只在邻近区间使用） */
export function interp(points, x) {
  if (x <= points[0][0]) return points[0][1];
  const last = points[points.length - 1];
  if (x >= last[0]) return last[1];
  for (let i = 0; i < points.length - 1; i++) {
    const [x0, y0] = points[i];
    const [x1, y1] = points[i + 1];
    if (x >= x0 && x <= x1) {
      return y0 + ((y1 - y0) * (x - x0)) / (x1 - x0);
    }
  }
  return last[1];
}

/** 在水位-流量曲线上，由目标出流反查水位（二分 + 线性分段） */
export function levelForOutflow(reach, targetQ) {
  const pts = /** @type {Array<[number, number]>} */ (reach.rating);
  if (!pts || pts.length === 0) return null;
  if (targetQ <= pts[0][1]) return pts[0][0];
  const last = pts[pts.length - 1];
  if (targetQ > last[1]) {
    // 超过河段最大安全下泄能力：按最后一段斜率外推，供 R7 判断"漫顶"
    const prev = pts[pts.length - 2];
    const slope = (last[0] - prev[0]) / (last[1] - prev[1]);
    return last[0] + (targetQ - last[1]) * slope;
  }
  for (let i = 0; i < pts.length - 1; i++) {
    const [x0, y0] = pts[i];
    const [x1, y1] = pts[i + 1];
    if (targetQ >= y0 && targetQ <= y1) {
      return x0 + ((x1 - x0) * (targetQ - y0)) / (y1 - y0);
    }
  }
  return last[0];
}

/**
 * 渠化河段（无自由出流曲线）在恒定净入流持续 durationMin 后的水位变化。
 *
 *   ΔV = (Q净入流) · 60 · durationMin     （m³）
 *   Δh = ΔV / (面积 m²)
 *
 * @param {number} netInflow   净入流（入 − 出，m³/s；负为排水）
 * @param {number} surfaceKm2  水面面积（km²）
 * @param {number} durationMin 持续时长（分钟）
 * @returns {number} 水位变幅（m，正为抬升）
 */
export function canalLevelDelta(netInflow, surfaceKm2, durationMin) {
  const areaM2 = surfaceKm2 * 1_000_000;
  return (netInflow * 60 * durationMin) / areaM2;
}

/**
 * 计算一组闸门出流之后，每个河段收到的「经闸来水」总量。
 *
 * @param {Record<string, { level: number }>} levels
 * @param {Record<string, number>} openings  gateId -> 开度
 * @param {import('../config/model.js').Gate[]} gates
 * @returns {{ into: Record<string, number>, flows: Record<string, number> }}
 *          into[reachId] = 该河段从上游闸门收到的总入流；flows[gateId] = 各闸出流
 */
export function gateFlowsByReach(levels, openings, gates) {
  /** @type {Record<string, number>} */
  const into = {};
  /** @type {Record<string, number>} */
  const flows = {};
  for (const g of gates) {
    const q = gateFlow(g, levels[g.upstreamReach]?.level ?? -Infinity,
      levels[g.downstreamReach]?.level ?? Infinity, openings[g.id] ?? 0);
    flows[g.id] = q;
    into[g.downstreamReach] = (into[g.downstreamReach] ?? 0) + q;
  }
  return { into, flows };
}

/** 便捷读取河段定义 */
export function reach(id) {
  const r = reachById.get(id);
  if (!r) throw new Error(`unknown reach: ${id}`);
  return r;
}
