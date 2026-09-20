/**
 * @file 核验规则测试：每条 HARD 规则各有一个阻断用例和一个边界放行用例。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { evaluateCommand, validateCommand } from '../src/domain/rules.js';
import { createStore } from '../src/domain/store.js';
import { evaluatePlan } from '../src/domain/plan.js';

const NOW = new Date('2026-09-20T08:00:00+08:00').getTime();
const MIN = 60_000;

function ctx(overrides = {}) {
  const store = createStore({ now: NOW, ...overrides });
  return {
    snapshots: store.getSnapshots(),
    openings: Object.fromEntries(Object.entries(store.getGateState()).map(([id, s]) => [id, s.opening])),
    now: NOW,
  };
}

const cmd = (patch) => ({
  gateId: 'G1', action: 'SET_OPENING', value: 0.9, durationMin: 60, role: 'operator', ...patch,
});

function ids(result) {
  return { hard: result.violations.map((v) => v.ruleId), soft: result.warnings.map((v) => v.ruleId) };
}

// ---------- R01 结构 ----------
test('R01: 未知闸门/动作/非法参数被结构拦截', () => {
  assert.equal(validateCommand(cmd({ gateId: 'NOPE' })).ok, false);
  assert.equal(validateCommand(cmd({ action: 'BLAST' })).ok, false);
  assert.equal(validateCommand(cmd({ value: -1 })).ok, false);
  assert.equal(validateCommand(cmd({ durationMin: 0 })).ok, false);
  assert.equal(validateCommand(cmd({ durationMin: 99999 })).ok, false);
  assert.equal(validateCommand(cmd({ role: 'root' })).ok, false);
  assert.equal(validateCommand(cmd({ value: undefined })).ok, false);
  assert.equal(validateCommand(cmd()).ok, true);
});

// ---------- R02 遥测时效 ----------
test('R02: 遥测过期阻断；新鲜数据放行', () => {
  const stale = evaluateCommand(cmd(), ctx({
    snapshots: { R_CANAL: { level: 27.2, inflow: 0, measuredAt: NOW - 20 * MIN } },
  }));
  assert.deepEqual(ids(stale).hard, ['R02']);
  assert.equal(stale.allowed, false);

  const fresh = evaluateCommand(cmd(), ctx());
  assert.ok(!ids(fresh).hard.includes('R02'));
});

// ---------- R03 开度范围 ----------
test('R03: 开度超出物理范围被拦', () => {
  const r = evaluateCommand(cmd({ value: 99, gateId: 'G3' }), ctx());
  assert.ok(ids(r).hard.includes('R03'));
});

// ---------- R04 单次变幅 ----------
test('R04: 单步变幅超限阻断；分步/全关豁免', () => {
  const big = evaluateCommand(cmd({ value: 2.0 }), ctx()); // 0.5 -> 2.0
  assert.ok(ids(big).hard.includes('R04'));

  const step = evaluateCommand(cmd({ value: 0.9 }), ctx()); // 0.5 -> 0.9
  assert.ok(!ids(step).hard.includes('R04'));

  const emergencyClose = evaluateCommand(cmd({ action: 'CLOSE', value: undefined }), ctx());
  assert.ok(!emergencyClose.violations.some((v) => v.ruleId === 'R04'));
});

// ---------- R05 抗旱 ----------
test('R05: 低于保供水位加大引水被拦；关闸保水放行', () => {
  const snap = { R_UP: { level: 34.5, inflow: 80, measuredAt: NOW - 2 * MIN } };
  const open = evaluateCommand(cmd({ value: 1.0 }), ctx({ snapshots: snap }));
  assert.ok(ids(open).hard.includes('R05'));

  const close = evaluateCommand(cmd({ action: 'CLOSE', value: undefined }), ctx({ snapshots: snap }));
  assert.ok(!ids(close).hard.includes('R05'));
  assert.equal(close.allowed, true);
});

// ---------- R06 防洪泄压 ----------
test('R06: 上游达保证水位时关闭泄洪闸被拦', () => {
  const r = evaluateCommand(cmd({ gateId: 'G3', action: 'CLOSE', value: undefined }), ctx({
    snapshots: { R_UP: { level: 40.2, inflow: 600, measuredAt: NOW - 1 * MIN } },
    gateState: { G3: { opening: 0.5 } },
  }));
  assert.ok(ids(r).hard.includes('R06'));
});

// ---------- R07 下游河道稳态 ----------
test('R07: 下游平衡水位超保证水位阻断；正常工况放行', () => {
  const flood = evaluateCommand(cmd({ gateId: 'G3', value: 3.5, role: 'dispatcher' }),
    ctx({ gateState: { G3: { opening: 2.0 } } }));
  assert.ok(ids(flood).hard.includes('R07'));

  const normal = evaluateCommand(cmd(), ctx());
  assert.ok(!ids(normal).hard.includes('R07'));
});

// ---------- R08 干渠暂态 ----------
test('R08: 尾闸关闭大流量长时引水漫渠被拦；先开尾闸后可接受', () => {
  const over = evaluateCommand(
    cmd({ gateId: 'G1', value: 1.6, durationMin: 120 }),
    ctx({
      snapshots: { R_CANAL: { level: 27.0, inflow: 0, measuredAt: NOW - 2 * MIN } },
      gateState: { G1: { opening: 1.2 }, G2: { opening: 0 } },
    }),
  );
  assert.ok(ids(over).hard.includes('R08'));

  // 联合调度：尾闸先开，同样的引水就不应有 R08
  const plan = evaluatePlan([
    { gateId: 'G2', action: 'SET_OPENING', value: 0.6, durationMin: 180, role: 'dispatcher' },
    { gateId: 'G1', action: 'SET_OPENING', value: 0.5, durationMin: 180, role: 'dispatcher' },
  ], {
    snapshots: ctx({
      snapshots: { R_CANAL: { level: 29.0, inflow: 0, measuredAt: NOW - 2 * MIN } },
      gateState: { G1: { opening: 0 }, G2: { opening: 0 } },
    }).snapshots,
    gateState: { G1: { opening: 0 }, G2: { opening: 0 } },
    now: NOW,
  });
  assert.equal(plan.overall, 'accepted');
  assert.deepEqual(plan.results.map((r) => r.status), ['accepted', 'accepted']);
});

// ---------- R09 反向水头 ----------
test('R09: 下游水位高于上游时禁止增加开度', () => {
  const r = evaluateCommand(cmd({ gateId: 'G1', value: 0.5 }), ctx({
    snapshots: {
      R_UP: { level: 28, inflow: 120, measuredAt: NOW - 2 * MIN },
      R_CANAL: { level: 29.5, inflow: 0, measuredAt: NOW - 2 * MIN },
    },
    gateState: { G1: { opening: 0 } },
  }));
  assert.ok(ids(r).hard.includes('R09'));
});

// ---------- R10 水头差 ----------
test('R10: 水头差超限时按闸门配置决定 hard/soft', () => {
  // G1 hard（10m），G3 soft（13m）
  const g1Case = evaluateCommand(cmd({ gateId: 'G1', value: 0.6 }), ctx({
    snapshots: {
      R_UP: { level: 39, inflow: 200, measuredAt: NOW - 1 * MIN },
      R_CANAL: { level: 27.2, inflow: 0, measuredAt: NOW - 2 * MIN },
    },
  }));
  assert.ok(ids(g1Case).hard.includes('R10'));

  const g3Case = evaluateCommand(cmd({ gateId: 'G3', value: 2.0, role: 'dispatcher' }), ctx());
  assert.ok(ids(g3Case).soft.includes('R10'));
});

// ---------- R11 出流能力 ----------
test('R11: SET_FLOW 超过额定/物理能力被拦；可实现的流量放行', () => {
  const tooMuch = evaluateCommand(cmd({ action: 'SET_FLOW', value: 999 }), ctx());
  assert.ok(ids(tooMuch).hard.includes('R11'));

  const feasible = evaluateCommand(
    cmd({ action: 'SET_FLOW', value: 40 }),
    ctx(),
  );
  assert.ok(!ids(feasible).hard.includes('R11'));
  assert.equal(feasible.allowed, true);
  assert.ok(feasible.requested.opening > 0);
});

// ---------- R12 权限 ----------
test('R12: 大流量操作需要调度员权限，viewer/operator 一律被拦', () => {
  for (const role of ['viewer', 'operator']) {
    const r = evaluateCommand(cmd({ gateId: 'G3', value: 2.0, role }), ctx());
    assert.ok(ids(r).hard.includes('R12'), `${role} 应被 R12 拦截`);
  }
  const dispatcher = evaluateCommand(
    cmd({ gateId: 'G3', value: 2.0, role: 'dispatcher' }),
    ctx({ snapshots: { R_DOWN: { level: 23, inflow: 20, measuredAt: NOW - 1 * MIN } } }),
  );
  assert.ok(!ids(dispatcher).hard.includes('R12'));
});

// ---------- 批量计划语义 ----------
test('批量: 被阻断的指令不推进计划开度，后续指令仍以原开度核验', () => {
  const plan = evaluatePlan([
    { gateId: 'G3', action: 'SET_OPENING', value: 3.5, durationMin: 60, role: 'operator' }, // 被拦（权限+漫顶）
    { gateId: 'G1', action: 'CLOSE', durationMin: 60, role: 'operator' },                  // 仍可执行
  ], {
    snapshots: ctx().snapshots,
    gateState: { G1: { opening: 0.5 }, G2: { opening: 0.8 }, G3: { opening: 0 } },
    now: NOW,
  }, { mode: 'all' });

  assert.equal(plan.overall, 'rejected');
  assert.equal(plan.results[0].status, 'blocked');
  assert.equal(plan.results[1].status, 'accepted');
  // 被拦的 G3 不应出现在第二句的推演开度中（第二句 CLOSE 不触发 R07 漫顶）
  assert.ok(!plan.results[1].result.violations.some((v) => v.ruleId === 'R07'));
});

test('批量 stop-on-block: 第一条阻断后后续标记 skipped', () => {
  const plan = evaluatePlan([
    { gateId: 'G3', action: 'SET_OPENING', value: 3.5, durationMin: 60, role: 'operator' },
    { gateId: 'G1', action: 'CLOSE', durationMin: 60, role: 'operator' },
  ], {
    snapshots: ctx().snapshots,
    gateState: { G1: { opening: 0.5 }, G2: { opening: 0.8 }, G3: { opening: 0 } },
    now: NOW,
  });
  assert.equal(plan.results[0].status, 'blocked');
  assert.equal(plan.results[1].status, 'skipped');
});

test('阻断结果必须带证据字段，便于审计追溯', () => {
  const r = evaluateCommand(cmd({ value: 2.0 }), ctx());
  for (const v of r.violations) {
    assert.ok(v.evidence, `${v.ruleId} 缺少证据`);
    assert.equal(typeof v.message, 'string');
  }
});
