/**
 * @file 运行态基线：各河段实测水情 + 各闸门当前开度。
 *       CLI 演示与测试可以在其基础上构造不同情景；服务端启动时加载到内存。
 */

/**
 * @typedef {Object} ReachSnapshot
 * @property {number} level          实测水位（m，高程）
 * @property {number} inflow         河段外来入流（m³/s，与本系统闸门无关的来水）
 * @property {number} [measuredAt]   遥测时间戳（epoch ms）。缺省取当前时刻。
 *
 * @typedef {Object} GateState
 * @property {number} opening        当前开度（m）
 */

/**
 * 生成一份基线水情。
 * @param {number} [now] 时间锚点（epoch ms），默认 Date.now()
 */
export function baseline(now = Date.now()) {
  /** @type {Record<string, ReachSnapshot>} */
  const reachState = {
    R_UP:    { level: 37.0, inflow: 260, measuredAt: now - 2 * 60_000 },
    R_CANAL: { level: 27.2, inflow: 0,   measuredAt: now - 3 * 60_000 },
    R_DOWN:  { level: 23.0, inflow: 120, measuredAt: now - 1 * 60_000 },
  };

  /** @type {Record<string, GateState>} */
  const gateState = {
    G1: { opening: 0.5 },
    G2: { opening: 0.8 },
    G3: { opening: 0.0 },
  };

  return { reachState, gateState, anchoredAt: now };
}
