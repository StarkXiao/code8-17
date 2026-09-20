/**
 * @file 水力学纯函数测试
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { gateFlow, openingForFlow, interp, levelForOutflow, canalLevelDelta } from '../src/domain/hydraulics.js';
import { GATES, reachById } from '../src/config/model.js';

const g1 = GATES.find((g) => g.id === 'G1');
const g3 = GATES.find((g) => g.id === 'G3');

test('gateFlow: 零开度/反向水头不出流', () => {
  assert.equal(gateFlow(g1, 37, 27, 0), 0);
  assert.equal(gateFlow(g1, 27, 37, 1), 0);       // 反向
  assert.equal(gateFlow(g1, g1.sill - 1, 20, 1), 0); // 上游水位在堰顶以下
});

test('gateFlow: 自由出流随开度线性增长、恒为正', () => {
  const q1 = gateFlow(g3, 37, 23, 1);
  const q2 = gateFlow(g3, 37, 23, 2);
  assert.ok(q1 > 0 && q2 > 0);
  assert.ok(Math.abs(q2 - 2 * q1) < 1e-6);
});

test('gateFlow: 淹没出流小于同开度自由出流', () => {
  const free = gateFlow(g1, 37, 25, 1);      // 下游低于 sill 26
  const submerged = gateFlow(g1, 37, 26.5, 1);
  assert.ok(submerged < free);
  assert.ok(submerged > 0);
});

test('openingForFlow: 可反算且回代误差 < 0.5 m³/s', () => {
  const target = 50;
  const d = openingForFlow(g1, 37, 27, target);
  assert.ok(d);
  const back = gateFlow(g1, 37, 27, d.opening);
  assert.ok(Math.abs(back - target) < 0.5, `回代流量 ${back}`);
});

test('openingForFlow: 超过满开能力或反向水头返回 null', () => {
  assert.equal(openingForFlow(g1, 37, 27, 10_000), null);
  assert.equal(openingForFlow(g1, 27, 37, 10), null);
});

test('rating 曲线插值与反查互为逆运算', () => {
  const rDown = reachById.get('R_DOWN');
  for (const [level, q] of rDown.rating) {
    assert.ok(Math.abs(levelForOutflow(rDown, q) - level) < 1e-9);
  }
  assert.ok(interp(rDown.rating, 24.5) > 240 && interp(rDown.rating, 24.5) < 300);
});

test('levelForOutflow: 超过最大安全泄量时外推出高于保证水位', () => {
  const rDown = reachById.get('R_DOWN');
  const lvl = levelForOutflow(rDown, 500);
  assert.ok(lvl > rDown.safetyLevel);
});

test('canalLevelDelta: 水量平衡折算与手算一致', () => {
  // 净入流 100 m³/s，面积 0.16 km²，60 分钟
  const delta = canalLevelDelta(100, 0.16, 60);
  const expected = (100 * 3600) / 160_000;
  assert.ok(Math.abs(delta - expected) < 1e-9);
  assert.ok(Math.abs(delta - 2.25) < 1e-9);
});
