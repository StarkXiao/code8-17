/**
 * @file 12 个典型调度情景的可执行演示：`npm run verify`
 *       每个情景使用独立水情，互不污染；只做「核验+下发」的完整动作。
 */
import { createStore } from '../domain/store.js';
import { createDispatcher } from '../domain/dispatcher.js';
import { GATES, REACHES } from '../config/model.js';

const NOW = new Date('2026-09-20T08:00:00+08:00').getTime();
const MIN = 60_000;

const gateName = (id) => GATES.find((g) => g.id === id)?.name ?? id;
const reachName = (id) => REACHES.find((r) => r.id === id)?.name ?? id;

/**
 * @typedef {Object} Scenario
 * @property {string} title
 * @property {string} expect  ACCEPTED / BLOCKED
 * @property {object} [snapshots]
 * @property {object} [gateState]
 * @property {object|object[]} command  单条指令或一组指令
 * @property {'stop-on-block'|'all'} [mode]
 */

/** @type {Scenario[]} */
const SCENARIOS = [
  {
    title: '1. 正常引水（小幅加大进水闸，下游无压力）',
    expect: 'ACCEPTED',
    command: { gateId: 'G1', action: 'SET_OPENING', value: 0.9, durationMin: 60, role: 'operator' },
  },
  {
    title: '2. 越权大流量泄洪（操作员开 G3 至 2m，须调度员权限）',
    expect: 'BLOCKED',
    command: { gateId: 'G3', action: 'SET_OPENING', value: 2.0, durationMin: 60, role: 'operator' },
  },
  {
    title: '3. 同上操作由调度员执行（放行；水头差触发防冲 SOFT 告警）',
    expect: 'ACCEPTED',
    snapshots: {
      R_UP: { level: 37.0, inflow: 260, measuredAt: NOW - 2 * MIN },
      R_DOWN: { level: 23.0, inflow: 20, measuredAt: NOW - 1 * MIN },
    },
    command: { gateId: 'G3', action: 'SET_OPENING', value: 2.0, durationMin: 60, role: 'dispatcher' },
  },
  {
    title: '4. 干渠暂态超载（尾闸关闭时长渠大流量引水 120 分钟 → 漫渠）',
    expect: 'BLOCKED',
    snapshots: { R_CANAL: { level: 27.0, inflow: 0, measuredAt: NOW - 2 * MIN } },
    gateState: { G1: { opening: 1.2 }, G2: { opening: 0 } },
    command: { gateId: 'G1', action: 'SET_OPENING', value: 1.6, durationMin: 120, role: 'dispatcher' },
  },
  {
    title: '5. 单次变幅超限（G1 从 0.5m 一步到 2.0m，上限 0.6m）',
    expect: 'BLOCKED',
    command: { gateId: 'G1', action: 'SET_OPENING', value: 2.0, durationMin: 30, role: 'dispatcher' },
  },
  {
    title: '6. 抗旱保水（上游 34.5m 低于保供水位 36m，仍要加大引水）',
    expect: 'BLOCKED',
    snapshots: { R_UP: { level: 34.5, inflow: 80, measuredAt: NOW - 2 * MIN } },
    command: { gateId: 'G1', action: 'SET_OPENING', value: 1.0, durationMin: 60, role: 'dispatcher' },
  },
  {
    title: '7. 低水位时关闸保水（R05 只拦加大引水，关闸应放行）',
    expect: 'ACCEPTED',
    snapshots: { R_UP: { level: 34.5, inflow: 80, measuredAt: NOW - 2 * MIN } },
    command: { gateId: 'G1', action: 'CLOSE', durationMin: 30, role: 'operator' },
  },
  {
    title: '8. 上游达保证水位仍试图关闭泄洪闸（违反防洪泄压底线）',
    expect: 'BLOCKED',
    snapshots: { R_UP: { level: 40.2, inflow: 600, measuredAt: NOW - 1 * MIN } },
    gateState: { G3: { opening: 0.5 } },
    command: { gateId: 'G3', action: 'CLOSE', durationMin: 30, role: 'dispatcher' },
  },
  {
    title: '9. 遥测失效（下游水位 20 分钟未上报，拒绝据此调度）',
    expect: 'BLOCKED',
    snapshots: { R_DOWN: { level: 23.0, inflow: 120, measuredAt: NOW - 20 * MIN } },
    command: { gateId: 'G2', action: 'SET_OPENING', value: 1.2, durationMin: 30, role: 'operator' },
  },
  {
    title: '10. 反向水头（干渠水位高于上游，开 G1 将造成倒灌）',
    expect: 'BLOCKED',
    snapshots: {
      R_UP: { level: 28.0, inflow: 120, measuredAt: NOW - 2 * MIN },
      R_CANAL: { level: 29.5, inflow: 0, measuredAt: NOW - 2 * MIN },
    },
    gateState: { G1: { opening: 0 } },
    command: { gateId: 'G1', action: 'SET_OPENING', value: 0.5, durationMin: 30, role: 'dispatcher' },
  },
  {
    title: '11. 下游漫顶（G3 大流量泄洪，平衡水位超保证水位 27m）',
    expect: 'BLOCKED',
    gateState: { G3: { opening: 2.0 } },
    command: { gateId: 'G3', action: 'SET_OPENING', value: 3.5, durationMin: 120, role: 'dispatcher' },
  },
  {
    title: '12. 联合调度（先开尾闸 G2 再加大进水闸 G1：单条会拦，联合放行）',
    expect: 'ACCEPTED',
    snapshots: { R_CANAL: { level: 29.0, inflow: 0, measuredAt: NOW - 2 * MIN } },
    gateState: { G1: { opening: 0 }, G2: { opening: 0 } },
    mode: 'stop-on-block',
    command: [
      { gateId: 'G2', action: 'SET_OPENING', value: 0.6, durationMin: 180, role: 'dispatcher' },
      { gateId: 'G1', action: 'SET_OPENING', value: 0.5, durationMin: 180, role: 'dispatcher' },
    ],
  },
];

function fmtChecks(list, indent = '      ') {
  return list.map((c) => {
    const tag = c.severity === 'hard' ? '阻断' : '告警';
    const lines = [
      `${indent}[${tag} ${c.ruleId}] ${c.scope} — ${c.message}`,
    ];
    if (c.evidence) {
      const ev = JSON.stringify(c.evidence);
      lines.push(`${indent}        证据: ${ev}`);
    }
    return lines.join('\n');
  }).join('\n');
}

function runSingle(s) {
  const store = createStore({
    now: NOW,
    snapshots: s.snapshots,
    gateState: s.gateState,
  });
  const dispatcher = createDispatcher(store);
  return dispatcher.dispatch(s.command);
}

function runPlan(s) {
  const store = createStore({
    now: NOW,
    snapshots: s.snapshots,
    gateState: s.gateState,
  });
  const dispatcher = createDispatcher(store);
  return dispatcher.dispatchPlan(s.command, { mode: s.mode ?? 'stop-on-block' });
}

let passCount = 0;
let failCount = 0;

for (const s of SCENARIOS) {
  console.log('—'.repeat(86));
  console.log(`▸ ${s.title}`);
  console.log(`  期望：${s.expect}`);

  if (Array.isArray(s.command)) {
    const plan = runPlan(s);
    console.log(`  核验（顺序逐条，模式 ${plan.mode}）：`);
    for (const r of plan.results) {
      const g = gateName(r.command.gateId);
      const detail = `${r.command.action}${r.command.value !== undefined ? '=' + r.command.value : ''}`;
      console.log(`    #${r.index + 1} ${g} ${detail}`);
      if (r.status === 'accepted') {
        console.log(`      → 放行（目标开度 ${r.result.requested.opening}m，流量 ${r.result.requested.flow} m³/s）`);
      } else if (r.status === 'blocked') {
        console.log('      → 阻断，指令未下发');
      } else {
        console.log('      → 跳过（前序已阻断）');
      }
      if (r.result?.warnings?.length) console.log(fmtChecks(r.result.warnings));
      if (r.result?.violations?.length) console.log(fmtChecks(r.result.violations));
    }
    const decision = plan.overall === 'accepted' ? 'ACCEPTED' : 'BLOCKED';
    console.log(`  整批结论：${decision}（接受 ${plan.total - plan.blockedCount}/${plan.total}）`);
    if (decision === s.expect) { passCount += 1; console.log('  ✅ 与期望一致'); }
    else { failCount += 1; console.log('  ❌ 与期望不一致'); }
  } else {
    const out = runSingle(s);
    const r = out.result;
    console.log(`  目标：${gateName(s.command.gateId)}（当前水情 ${
      s.snapshots ? '已替换为情景水情' : '基线水情'}）`);
    if (out.decision === 'ACCEPTED') {
      console.log(`  → 放行：目标开度 ${r.requested.opening}m，预估流量 ${r.requested.flow} m³/s` +
        `，回执号 ${out.receipt.id.slice(0, 8)}`);
    } else {
      console.log('  → 阻断：指令未下发，已写入阻断审计');
    }
    if (r.warnings.length) console.log(fmtChecks(r.warnings));
    if (r.violations.length) console.log(fmtChecks(r.violations));
    if (out.decision === s.expect) { passCount += 1; console.log('  ✅ 与期望一致'); }
    else { failCount += 1; console.log('  ❌ 与期望不一致'); }
  }
}

console.log('—'.repeat(86));
console.log(`情景汇总：${passCount} 通过，${failCount} 失败（共 ${SCENARIOS.length}）`);
process.exitCode = failCount === 0 ? 0 : 1;
