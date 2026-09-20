/**
 * @file 河网拓扑与闸门物理参数（基线模型）
 *
 * 拓扑（自上游到下游）：
 *
 *   R_UP  上游河道（梯级水库/河道站）
 *    │
 *    ▼  G1  进水闸（水库 → 干渠）
 *   R_CANAL  输水干渠（渠化河段）
 *    │
 *    ▼  G2  节制闸（干渠 → 下游河道）
 *   R_DOWN  下游河道（防洪保护对象所在河段）
 *    ▲
 *    │  G3  泄洪闸（另一个库区直接泄入 R_DOWN，用于旁路泄洪场景）
 *   R_UP（同一上游站，两孔出口）
 *
 * 所有高程/水位单位：米（m，黄海高程）
 * 所有流量单位：立方米/秒（m³/s）
 */

/**
 * 河段定义
 * @typedef {Object} Reach
 * @property {string} id
 * @property {'river'|'canal'} kind            river=天然河道，canal=渠化河段
 * @property {string} name
 * @property {number} bed                        渠底/河底高程（m）
 * @property {number} [bank]                     堤顶高程（m，渠化河段用）
 * @property {number} warnLevel                  警戒水位（m）
 * @property {number} safetyLevel                保证（安全）水位（m）
 * @property {number} surfaceArea                水面面积（km²，用于水量平衡折算水位变化）
 * @property {Array<[number, number]>} [rating]  水位-出流能力曲线 [[水位m, 可安全下泄流量m³/s], ...]，升序
 */

/** @type {Reach[]} */
export const REACHES = [
  {
    id: 'R_UP',
    kind: 'river',
    name: '上游河道站',
    bed: 18,
    warnLevel: 38.0,
    safetyLevel: 40.0,
    surfaceArea: 12,
    rating: [
      [30, 200],
      [33, 320],
      [36, 460],
      [38, 560],
      [40, 680],
    ],
  },
  {
    id: 'R_CANAL',
    kind: 'canal',
    name: '北干渠',
    bed: 24,
    bank: 31.2,
    warnLevel: 30.6,
    safetyLevel: 31.0,
    surfaceArea: 0.16,
    // 干渠末端由 G2 控制，出流能力在闸门侧计算，此处不给 rating
  },
  {
    id: 'R_DOWN',
    kind: 'river',
    name: '下游河道（城关段）',
    bed: 16,
    warnLevel: 25.0,
    safetyLevel: 27.0,
    surfaceArea: 6,
    rating: [
      [20, 60],
      [22, 140],
      [24, 240],
      [25, 300],
      [26, 340],
      [27, 380],
    ],
  },
];

/**
 * 闸门定义
 *
 * 出流能力采用闸孔出流简化公式：
 *   自由出流（h_up - sill >= 2 * opening）：
 *     Q = Cd * b * opening * sqrt(g * h_head)
 *   淹没出流（h_down 高于堰顶，闸孔被淹没）：
 *     Q = Cd * b * opening * sqrt(2g * (h_up - h_down))
 *
 * @typedef {Object} Gate
 * @property {string} id
 * @property {string} name
 * @property {string} upstreamReach
 * @property {string} downstreamReach
 * @property {number} sill          闸底（堰顶）高程（m）
 * @property {number} width         闸孔总净宽（m）
 * @property {number} maxOpening    最大允许开度（m）
 * @property {number} maxStep       单次操作允许的最大开度变幅（m）
 * @property {number} ratedFlow     额定泄流能力（m³/s，铭牌值）
 * @property {number} dischargeCoef 流量系数 Cd（综合收缩/流速系数）
 * @property {number} maxHeadDiff   允许最大上下游水头差（m，防冲/防气蚀）
 * @property {number} [bigFlowThreshold]   大流量操作阈值（m³/s），超过需更高权限
 * @property {'hard'|'soft'} maxHeadDiffSeverity  超水头差时阻断还是仅告警
 */

/** @type {Gate[]} */
export const GATES = [
  {
    id: 'G1',
    name: '北干渠进水闸',
    upstreamReach: 'R_UP',
    downstreamReach: 'R_CANAL',
    sill: 26.0,
    width: 8,
    maxOpening: 2.0,
    maxStep: 0.6,
    ratedFlow: 120,
    dischargeCoef: 0.62,
    maxHeadDiff: 10.0,
    maxHeadDiffSeverity: 'hard',
    bigFlowThreshold: 90,
  },
  {
    id: 'G2',
    name: '干渠节制闸',
    upstreamReach: 'R_CANAL',
    downstreamReach: 'R_DOWN',
    sill: 24.5,
    width: 10,
    maxOpening: 2.5,
    maxStep: 0.8,
    ratedFlow: 150,
    dischargeCoef: 0.6,
    maxHeadDiff: 6.0,
    maxHeadDiffSeverity: 'hard',
    bigFlowThreshold: 120,
  },
  {
    id: 'G3',
    name: '旁路泄洪闸',
    upstreamReach: 'R_UP',
    downstreamReach: 'R_DOWN',
    sill: 30.0,
    width: 20,
    maxOpening: 4.5,
    maxStep: 2.0,
    ratedFlow: 420,
    dischargeCoef: 0.62,
    maxHeadDiff: 13.0,
    maxHeadDiffSeverity: 'soft', // 泄洪闸气蚀/冲刷风险按告警处理，不阻断防洪调度
    bigFlowThreshold: 250,
  },
];

/** 全局核验参数 */
export const LIMITS = {
  /** 遥测数据最大允许年龄（分钟）。超过即视为数据失效，拒绝据此调度 */
  maxTelemetryAgeMin: 15,
  /** 指令有效期：下发时刻距计划执行时刻最大偏差（分钟） */
  maxCommandSkewMin: 30,
  /** 暂态分析指令作用时长的合法范围（分钟） */
  durationRangeMin: [1, 1440],
};

/**
 * 角色权限等级（数字越大权限越高）
 * @type {Record<string, number>}
 */
export const ROLE_LEVEL = {
  viewer: 0,
  operator: 1,
  dispatcher: 2,
  admin: 3,
};

/** 大流量操作所需最低角色 */
export const BIG_FLOW_MIN_ROLE = 'dispatcher';

export const reachById = new Map(REACHES.map((r) => [r.id, r]));
export const gateById = new Map(GATES.map((g) => [g.id, g]));
