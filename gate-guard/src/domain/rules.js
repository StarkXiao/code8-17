/**
 * @file 核验规则库：把一条闸门调度指令与当前（以及计划中已接受的）水情
 *       逐条对照。任何一条 HARD 规则命中 => 阻断下发；SOFT 仅告警。
 *
 * 规则总览：
 *   R01 指令结构合法性            HARD（在 validateCommand 中）
 *   R02 遥测时效（≤15 分钟）       HARD
 *   R03 开度物理范围 0..maxOpening HARD
 *   R04 单次开度变幅 ≤ maxStep     HARD（全关操作豁免）
 *   R05 上游抗旱保水（低于保供水位不得加大引水） HARD
 *   R06 上游防洪泄压（达保证水位不得减少下泄）   HARD
 *   R07 下游河道稳态平衡水位 ≤ 保证水位 HARD / 警戒 SOFT
 *   R08 渠化河段暂态水位（按指令时长推演）        HARD/SOFT
 *   R09 反向水头（下游水位高于上游不得开启）       HARD
 *   R10 上下游水头差 ≤ 允许值（防冲/气蚀）        按闸门配置
 *   R11 出流能力（额定 + 当前水位物理能力）        HARD
 *   R12 大流量操作权限（≥阈值需调度员权限）        HARD
 *
 * 关键原则：SOFT 告警永远不会阻断指令；HARD 命中后任何人、任何标志位
 * 都无法放行（安全底线不可绕过）。
 */
import {
  gateById, reachById, LIMITS, ROLE_LEVEL, BIG_FLOW_MIN_ROLE,
} from '../config/model.js';
import {
  gateFlow, openingForFlow, maxPhysicalFlow, gateFlowsByReach,
  canalLevelDelta, levelForOutflow,
} from './hydraulics.js';

const EPS = 1e-6;

const ACTIONS = new Set(['SET_OPENING', 'SET_FLOW', 'SET_FULLY_OPEN', 'CLOSE']);

/**
 * 结构校验（R01）
 * @returns {{ ok: true } | { ok: false, check: Check }}
 */
export function validateCommand(cmd) {
  const fail = (message, evidence) => ({
    ok: false,
    check: makeCheck('R01', '指令结构合法性', 'hard', '指令本身', message, evidence),
  });

  if (!cmd || typeof cmd !== 'object') return fail('指令不是合法对象');
  if (!cmd.gateId || !gateById.has(cmd.gateId)) {
    return fail(`未知闸门: ${cmd?.gateId}`, { gateId: cmd?.gateId });
  }
  if (!ACTIONS.has(cmd.action)) {
    return fail(`未知动作: ${cmd.action}（允许 ${[...ACTIONS].join(' / ')}）`, { action: cmd.action });
  }
  if ((cmd.action === 'SET_OPENING' || cmd.action === 'SET_FLOW')
      && (typeof cmd.value !== 'number' || !Number.isFinite(cmd.value) || cmd.value < 0)) {
    return fail(`${cmd.action} 需要非负数值参数 value`, { value: cmd.value });
  }
  const [dMin, dMax] = LIMITS.durationRangeMin;
  if (typeof cmd.durationMin !== 'number' || !Number.isFinite(cmd.durationMin)
      || cmd.durationMin < dMin || cmd.durationMin > dMax) {
    return fail(`作用时长 durationMin 必须在 [${dMin}, ${dMax}] 分钟内`, { durationMin: cmd.durationMin });
  }
  if (!Object.prototype.hasOwnProperty.call(ROLE_LEVEL, cmd.role ?? '')) {
    return fail(`未知操作角色: ${cmd.role}`, { role: cmd.role });
  }
  return { ok: true };
}

/**
 * @typedef {Object} Check
 * @property {string} ruleId
 * @property {string} title
 * @property {'hard'|'soft'} severity
 * @property {string} scope
 * @property {string} message
 * @property {Record<string, unknown>} [evidence]
 */

function makeCheck(ruleId, title, severity, scope, message, evidence) {
  return { ruleId, title, severity, scope, message, ...(evidence ? { evidence } : {}) };
}

/**
 * 把指令解析成「目标开度 + 目标流量」。
 * @returns {{ opening: number, flow: number, mode?: string } | { deriveError: Check }}
 */
function resolveTarget(cmd, gate, hUp, hDown, currentOpening) {
  switch (cmd.action) {
    case 'CLOSE':
      return { opening: 0, flow: 0 };
    case 'SET_FULLY_OPEN':
      return {
        opening: gate.maxOpening,
        flow: gateFlow(gate, hUp, hDown, gate.maxOpening),
        mode: 'full',
      };
    case 'SET_OPENING':
      return { opening: cmd.value, flow: gateFlow(gate, hUp, hDown, cmd.value) };
    case 'SET_FLOW': {
      if (cmd.value === 0) return { opening: 0, flow: 0 };
      const derived = openingForFlow(gate, hUp, hDown, cmd.value);
      if (!derived) {
        return {
          deriveError: makeCheck(
            'R11', '出流能力核验', 'hard', '目标流量',
            `当前水头下无法通过该闸输出 ${cmd.value} m³/s（反向水头或超过满开能力）`,
            { targetFlow: cmd.value, hUp, hDown, maxOpening: gate.maxOpening },
          ),
        };
      }
      return { opening: derived.opening, flow: cmd.value, mode: derived.mode };
    }
    default:
      throw new Error(`unreachable action: ${cmd.action}`);
  }
}

/**
 * 核验单条指令。
 *
 * @param {object} command
 * @param {object} ctx
 * @param {Record<string, { level: number, inflow: number, measuredAt?: number }>} ctx.snapshots
 *        各河段实测水情
 * @param {Record<string, number>} ctx.openings
 *        核验所依据的闸门开度（单条=当前开度；批量=已接受指令叠加后的计划开度）
 * @param {number} ctx.now 核验时刻 epoch ms
 *
 * @returns {{
 *   allowed: boolean,
 *   requested?: { opening: number, flow: number, mode?: string },
 *   violations: Check[],
 *   warnings: Check[],
 * }}
 */
export function evaluateCommand(command, ctx) {
  const { snapshots, openings, now } = ctx;

  const structural = validateCommand(command);
  if (!structural.ok) {
    return { allowed: false, violations: [structural.check], warnings: [] };
  }

  const gate = gateById.get(command.gateId);
  const upReach = reachById.get(gate.upstreamReach);
  const downReach = reachById.get(gate.downstreamReach);
  const upSnap = snapshots[gate.upstreamReach];
  const downSnap = snapshots[gate.downstreamReach];
  const currentOpening = openings[gate.id] ?? 0;

  /** @type {Check[]} */
  const violations = [];
  /** @type {Check[]} */
  const warnings = [];
  const push = (c) => (c.severity === 'hard' ? violations : warnings).push(c);

  const hUp = upSnap.level;
  const hDown = downSnap.level;
  const isClosing = command.action === 'CLOSE'
    || (command.action === 'SET_OPENING' && command.value === 0)
    || (command.action === 'SET_FLOW' && command.value === 0);

  // ---- 解析目标开度/流量（R11 派生失败直接阻断） ----
  const resolved = resolveTarget(command, gate, hUp, hDown, currentOpening);
  if ('deriveError' in resolved) {
    violations.push(resolved.deriveError);
    return { allowed: false, violations, warnings };
  }
  const target = resolved;
  const currentFlow = gateFlow(gate, hUp, hDown, currentOpening);
  const increasesFlow = target.flow > currentFlow + EPS;
  const reducesFlow = target.flow < currentFlow - EPS;

  // ---- R02 遥测时效 ----
  for (const [reachName, snap] of [[`上游「${upReach.name}」`, upSnap], [`下游「${downReach.name}」`, downSnap]]) {
    const ageMin = snap.measuredAt == null
      ? Infinity
      : (now - snap.measuredAt) / 60_000;
    if (ageMin > LIMITS.maxTelemetryAgeMin) {
      push(makeCheck('R02', '遥测数据时效', 'hard', reachName,
        `${reachName}遥测数据已过期 ${Number.isFinite(ageMin) ? ageMin.toFixed(1) : '∞'} 分钟`
        + `（阈值 ${LIMITS.maxTelemetryAgeMin} 分钟），不得依据失效水情调度`,
        { ageMin: Number.isFinite(ageMin) ? Number(ageMin.toFixed(1)) : null,
          measuredAt: snap.measuredAt ?? null, thresholdMin: LIMITS.maxTelemetryAgeMin }));
    }
  }

  // ---- R03 开度物理范围 ----
  if (target.opening < -EPS || target.opening > gate.maxOpening + EPS) {
    push(makeCheck('R03', '开度物理范围', 'hard', gate.name,
      `目标开度 ${target.opening.toFixed(3)}m 超出 [0, ${gate.maxOpening}m]`,
      { targetOpening: target.opening, maxOpening: gate.maxOpening }));
  }

  // ---- R04 单次变幅（全关豁免：防汛紧急落闸允许一步到位） ----
  if (!isClosing) {
    const step = Math.abs(target.opening - currentOpening);
    if (step > gate.maxStep + EPS) {
      push(makeCheck('R04', '单次操作变幅', 'hard', gate.name,
        `本次开度变幅 ${step.toFixed(3)}m 超过单步上限 ${gate.maxStep}m`
        + `（${currentOpening.toFixed(2)}m → ${target.opening.toFixed(2)}m），请分步操作`,
        { step: Number(step.toFixed(3)), maxStep: gate.maxStep,
          from: currentOpening, to: target.opening }));
    }
  }

  // ---- R05 上游抗旱保水 ----
  // 上游水位低于「警戒水位 − 2m」的保供水位时，禁止加大引水/泄放。
  const droughtLevel = upReach.warnLevel - 2;
  if (hUp < droughtLevel && increasesFlow) {
    push(makeCheck('R05', '上游抗旱保水', 'hard', upReach.name,
      `上游水位 ${hUp.toFixed(2)}m 已低于保供水位 ${droughtLevel.toFixed(2)}m，`
      + '禁止加大引水或泄放；如需关闸保水请下发 CLOSE 指令',
      { level: hUp, droughtLevel }));
  }

  // ---- R06 上游防洪泄压 ----
  // 上游达到保证水位时，任何减少下泄的动作（含关闭）一律阻断。
  if (hUp >= upReach.safetyLevel && reducesFlow) {
    push(makeCheck('R06', '上游防洪泄压', 'hard', upReach.name,
      `上游水位 ${hUp.toFixed(2)}m 已达到/超过保证水位 ${upReach.safetyLevel}m，`
      + `禁止减少下泄（当前 ${currentFlow.toFixed(1)} → ${target.flow.toFixed(1)} m³/s）`,
      { level: hUp, safetyLevel: upReach.safetyLevel,
        currentFlow: Number(currentFlow.toFixed(1)), targetFlow: Number(target.flow.toFixed(1)) }));
  }

  // ---- R09 反向水头 ----
  if (hDown >= hUp && target.opening > currentOpening + EPS) {
    push(makeCheck('R09', '反向水头保护', 'hard', `${upReach.name} ↔ ${downReach.name}`,
      `下游水位 ${hDown.toFixed(2)}m 不低于上游 ${hUp.toFixed(2)}m，`
      + '开闸将造成倒灌，禁止增加开度',
      { hUp, hDown }));
  }

  // ---- R10 水头差（防冲/气蚀） ----
  if (target.opening > EPS) {
    const diff = hUp - hDown;
    if (diff > gate.maxHeadDiff + EPS) {
      push(makeCheck('R10', '水头差防冲限制', gate.maxHeadDiffSeverity, gate.name,
        `上下游水头差 ${diff.toFixed(2)}m 超过允许值 ${gate.maxHeadDiff}m，`
        + (gate.maxHeadDiffSeverity === 'hard'
          ? '禁止小开度高速出流（冲刷/气蚀风险），应先调整上下游水位或采用大开度'
          : '存在冲刷/气蚀风险，请确认下游消能工状态并留痕'),
        { headDiff: Number(diff.toFixed(2)), maxHeadDiff: gate.maxHeadDiff }));
    }
  }

  // ---- R11 出流能力（额定 + 当前水位物理能力） ----
  const physical = maxPhysicalFlow(gate, hUp, hDown);
  if (target.flow > gate.ratedFlow + EPS) {
    push(makeCheck('R11', '出流能力核验', 'hard', gate.name,
      `目标流量 ${target.flow.toFixed(1)} m³/s 超过额定泄流能力 ${gate.ratedFlow} m³/s`,
      { targetFlow: Number(target.flow.toFixed(1)), ratedFlow: gate.ratedFlow }));
  } else if (target.flow > physical + 0.5) {
    push(makeCheck('R11', '出流能力核验', 'hard', gate.name,
      `当前水位组合下满开物理出流仅约 ${physical.toFixed(1)} m³/s，`
      + `无法达到目标 ${target.flow.toFixed(1)} m³/s`,
      { targetFlow: Number(target.flow.toFixed(1)),
        physicalMax: Number(physical.toFixed(1)), hUp, hDown }));
  }

  // ---- R12 大流量权限 ----
  if (gate.bigFlowThreshold != null && target.flow >= gate.bigFlowThreshold + EPS) {
    const required = ROLE_LEVEL[BIG_FLOW_MIN_ROLE];
    if ((ROLE_LEVEL[command.role] ?? -1) < required) {
      push(makeCheck('R12', '大流量操作权限', 'hard', gate.name,
        `${target.flow.toFixed(1)} m³/s 达到大流量阈值 ${gate.bigFlowThreshold} m³/s，`
        + `当前角色「${command.role}」无权操作，需要「${BIG_FLOW_MIN_ROLE}」及以上权限`,
        { targetFlow: Number(target.flow.toFixed(1)),
          threshold: gate.bigFlowThreshold, role: command.role,
          requiredRole: BIG_FLOW_MIN_ROLE }));
    }
  }

  // ---- 下游影响：R07（河道稳态）/ R08（渠道暂态）需要全闸水账 ----
  // 用「本指令已生效」的计划开度推演全部闸门出流。
  const projectedOpenings = { ...openings, [gate.id]: target.opening };
  const levels = Object.fromEntries(
    Object.entries(snapshots).map(([id, s]) => [id, { level: s.level }]),
  );
  const { into } = gateFlowsByReach(levels, projectedOpenings, [...gateById.values()]);

  if (downReach.kind === 'river' && downReach.rating) {
    // R07 河道：平衡时 入流（外来 + 经闸）= 河段出流能力(平衡水位)
    const totalInflow = (downSnap.inflow ?? 0) + (into[downReach.id] ?? 0);
    const equilibrium = levelForOutflow(downReach, totalInflow);
    if (equilibrium > downReach.safetyLevel + EPS) {
      push(makeCheck('R07', '下游河道稳态水位', 'hard', downReach.name,
        `本指令生效后下游平衡入流约 ${totalInflow.toFixed(1)} m³/s，`
        + `推演平衡水位 ${equilibrium.toFixed(2)}m 超过保证水位 ${downReach.safetyLevel}m，`
        + '存在漫顶风险，禁止加大下泄',
        { totalInflow: Number(totalInflow.toFixed(1)),
          equilibriumLevel: Number(equilibrium.toFixed(2)),
          safetyLevel: downReach.safetyLevel, warnLevel: downReach.warnLevel }));
    } else if (equilibrium > downReach.warnLevel + EPS) {
      push(makeCheck('R07', '下游河道稳态水位', 'soft', downReach.name,
        `推演平衡水位 ${equilibrium.toFixed(2)}m 超过警戒水位 ${downReach.warnLevel}m`
        + '（未超保证水位），请通知下游河道值守单位',
        { totalInflow: Number(totalInflow.toFixed(1)),
          equilibriumLevel: Number(equilibrium.toFixed(2)),
          warnLevel: downReach.warnLevel }));
    }
  }

  if (downReach.kind === 'canal') {
    // R08 渠道：入渠经闸来水 + 渠外入流 − 渠尾经闸出水，按指令时长做暂态水量平衡
    const incoming = (into[downReach.id] ?? 0) + (downSnap.inflow ?? 0);
    const outgoing = [...gateById.values()]
      .filter((g) => g.upstreamReach === downReach.id)
      .reduce((sum, g) => sum + gateFlow(g, hUpSafe(snapshots, g.upstreamReach),
        hUpSafe(snapshots, g.downstreamReach), projectedOpenings[g.id] ?? 0), 0);
    const net = incoming - outgoing;
    if (net > EPS) {
      const delta = canalLevelDelta(net, downReach.surfaceArea, command.durationMin);
      const peak = hDown + delta;
      const evidence = {
        incoming: Number(incoming.toFixed(1)), outgoing: Number(outgoing.toFixed(1)),
        netInflow: Number(net.toFixed(1)), durationMin: command.durationMin,
        riseM: Number(delta.toFixed(2)), peakLevel: Number(peak.toFixed(2)),
        warnLevel: downReach.warnLevel, safetyLevel: downReach.safetyLevel,
      };
      if (peak > downReach.safetyLevel + EPS) {
        push(makeCheck('R08', '干渠暂态水位', 'hard', downReach.name,
          `本指令持续 ${command.durationMin} 分钟，干渠净入流约 ${net.toFixed(1)} m³/s，`
          + `水位将抬升 ${delta.toFixed(2)}m 至 ${peak.toFixed(2)}m，超过保证水位 `
          + `${downReach.safetyLevel}m（堤顶 ${downReach.bank}m），有漫渠风险`,
          evidence));
      } else if (peak > downReach.warnLevel + EPS) {
        push(makeCheck('R08', '干渠暂态水位', 'soft', downReach.name,
          `干渠水位预计抬升 ${delta.toFixed(2)}m 至 ${peak.toFixed(2)}m，超过警戒水位 `
          + `${downReach.warnLevel}m，请提前加大渠尾泄量或缩短引水时长`,
          evidence));
      }
    }
  }

  const allowed = violations.length === 0;
  return {
    allowed,
    requested: { opening: Number(target.opening.toFixed(3)), flow: Number(target.flow.toFixed(1)) },
    violations,
    warnings,
  };
}

function hUpSafe(snapshots, reachId) {
  return snapshots[reachId]?.level ?? -Infinity;
}
