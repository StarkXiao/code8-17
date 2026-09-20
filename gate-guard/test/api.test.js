/**
 * @file 端到端测试：经过 HTTP API 完成「预核验 → 下发 → 被阻断 → 审计留痕」。
 *       零依赖：直接向 server 发原生 http 请求。
 */
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { createApp } from '../src/server/index.js';

const app = createApp();
const server = app.server;
server.listen(0);
await once(server, 'listening');
const port = server.address().port;

after(() => server.close());

async function call(method, path, body) {
  const res = await fetch(`http://localhost:${port}${path}`, {
    method,
    headers: { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const json = await res.json();
  return { status: res.status, json };
}

test('GET /health 与 GET /api/state 正常', async () => {
  const h = await call('GET', '/health');
  assert.equal(h.status, 200);
  assert.equal(h.json.ok, true);

  const s = await call('GET', '/api/state');
  assert.equal(s.status, 200);
  assert.ok(s.json.reaches.R_UP);
  assert.ok(s.json.gates.G1);
});

test('合法指令：check 与 dispatch 都放行，状态推进且有回执', async () => {
  const before = (await call('GET', '/api/state')).json.gates.G1.opening;
  const command = { gateId: 'G1', action: 'SET_OPENING', value: 0.9, durationMin: 60, role: 'operator' };

  const dry = await call('POST', '/api/commands/check', command);
  assert.equal(dry.status, 200);
  assert.equal(dry.json.decision, 'ACCEPTED');

  // dry-run 不应改变状态
  assert.equal((await call('GET', '/api/state')).json.gates.G1.opening, before);

  const out = await call('POST', '/api/commands/dispatch', command);
  assert.equal(out.status, 200);
  assert.equal(out.json.decision, 'ACCEPTED');
  assert.ok(out.json.receipt.id);
  assert.equal(out.json.result.requested.opening, 0.9);

  assert.equal((await call('GET', '/api/state')).json.gates.G1.opening, 0.9);

  const log = await call('GET', '/api/logs/accepted');
  assert.equal(log.json.entries[0].command.gateId, 'G1');
});

test('冲突指令：返回 409、闸门状态不变、阻断审计可查', async () => {
  const before = (await call('GET', '/api/state')).json.gates.G1.opening;
  const bad = { gateId: 'G1', action: 'SET_OPENING', value: 2.0, durationMin: 30, role: 'dispatcher' };

  const out = await call('POST', '/api/commands/dispatch', bad);
  assert.equal(out.status, 409);
  assert.equal(out.json.decision, 'BLOCKED');
  assert.ok(out.json.result.violations.length > 0);
  assert.ok(out.json.result.violations.every((v) => v.severity === 'hard'));

  assert.equal((await call('GET', '/api/state')).json.gates.G1.opening, before);

  const blocked = await call('GET', '/api/logs/blocked');
  assert.equal(blocked.json.entries[0].command.action, 'SET_OPENING');
  assert.ok(blocked.json.entries[0].violations.some((v) => v.ruleId === 'R04' || v.ruleId === 'R11'));
});

test('结构非法指令：dry-run 也返回 BLOCKED（R01），不落任何日志', async () => {
  const out = await call('POST', '/api/commands/check', { gateId: 'GHOST', action: 'X', durationMin: 60, role: 'operator' });
  assert.equal(out.json.decision, 'BLOCKED');
  assert.equal(out.json.violations[0].ruleId, 'R01');
});

test('dispatch-plan：顺序核验，整体 rejected 时返回每条结论', async () => {
  const plan = await call('POST', '/api/commands/dispatch-plan', {
    mode: 'all',
    commands: [
      { gateId: 'G3', action: 'SET_OPENING', value: 3.5, durationMin: 60, role: 'operator' },
      { gateId: 'G1', action: 'CLOSE', durationMin: 60, role: 'operator' },
    ],
  });
  assert.equal(plan.status, 200);
  assert.equal(plan.json.overall, 'rejected');
  assert.equal(plan.json.results[0].status, 'blocked');
  assert.equal(plan.json.results[1].status, 'accepted');
});

test('非法 JSON / 错误路由返回规范错误体', async () => {
  const res = await fetch(`http://localhost:${port}/api/commands/dispatch`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: '{bad',
  });
  assert.equal(res.status, 400);
  assert.equal((await res.json()).code, 'BAD_REQUEST');

  const nf = await call('GET', '/nope');
  assert.equal(nf.status, 404);
});
