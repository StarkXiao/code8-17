/**
 * @file HTTP 服务：闸门调度指令核验 API（零依赖，node:http）
 *
 * 路由：
 *   GET  /health                         健康检查
 *   GET  /api/state                      当前水情 + 闸门状态总览
 *   GET  /api/rules                      规则清单（含阈值来源）
 *   POST /api/commands/check             预核验（dry-run，不落状态）
 *   POST /api/commands/dispatch          核验并下发（阻断时 409，不改状态）
 *   POST /api/commands/dispatch-plan     批量顺序核验下发
 *   GET  /api/logs/accepted              已接受指令回执
 *   GET  /api/logs/blocked               被阻断指令审计
 */
import { createServer } from 'node:http';
import { createStore } from '../domain/store.js';
import { createDispatcher } from '../domain/dispatcher.js';
import { GATES, REACHES, LIMITS, BIG_FLOW_MIN_ROLE } from '../config/model.js';

const PORT = Number(process.env.PORT ?? 4100);

export function createApp() {
  const store = createStore();
  const dispatcher = createDispatcher(store);

  const RULES = [
    { id: 'R01', title: '指令结构合法性', severity: 'hard' },
    { id: 'R02', title: '遥测数据时效', severity: 'hard', threshold: `≤ ${LIMITS.maxTelemetryAgeMin} 分钟` },
    { id: 'R03', title: '开度物理范围', severity: 'hard', threshold: '0..各闸 maxOpening' },
    { id: 'R04', title: '单次操作变幅', severity: 'hard', threshold: '各闸 maxStep（全关豁免）' },
    { id: 'R05', title: '上游抗旱保水', severity: 'hard', threshold: `水位 < 警戒水位 − 2m 时禁止加大引水` },
    { id: 'R06', title: '上游防洪泄压', severity: 'hard', threshold: '水位 ≥ 保证水位时禁止减少下泄' },
    { id: 'R07', title: '下游河道稳态水位', severity: 'hard/soft', threshold: '平衡水位 > 保证=阻断，> 警戒=告警' },
    { id: 'R08', title: '渠化河段暂态水位', severity: 'hard/soft', threshold: '按指令时长水量平衡推演' },
    { id: 'R09', title: '反向水头保护', severity: 'hard', threshold: '下游水位 ≥ 上游时禁止增加开度' },
    { id: 'R10', title: '水头差防冲限制', severity: 'hard/soft', threshold: '按各闸配置（G3 为 soft）' },
    { id: 'R11', title: '出流能力', severity: 'hard', threshold: '额定能力 + 当前水位物理能力' },
    { id: 'R12', title: '大流量操作权限', severity: 'hard', threshold: `≥ 各闸 bigFlowThreshold 需 ${BIG_FLOW_MIN_ROLE}` },
  ];

  function json(res, status, body) {
    const payload = JSON.stringify(body, null, 2);
    res.writeHead(status, {
      'content-type': 'application/json; charset=utf-8',
      'content-length': Buffer.byteLength(payload),
    });
    res.end(payload);
  }

  async function readJson(req) {
    const chunks = [];
    let size = 0;
    for await (const chunk of req) {
      size += chunk.length;
      if (size > 1_000_000) {
        const err = new Error('payload too large');
        err.statusCode = 413;
        throw err;
      }
      chunks.push(chunk);
    }
    if (chunks.length === 0) return {};
    try {
      return JSON.parse(Buffer.concat(chunks).toString('utf8'));
    } catch {
      const err = new Error('invalid JSON body');
      err.statusCode = 400;
      throw err;
    }
  }

  const server = createServer(async (req, res) => {
    const url = new URL(req.url, `http://${req.headers.host ?? 'localhost'}`);
    const route = `${req.method} ${url.pathname}`;

    try {
      if (route === 'GET /health') return json(res, 200, { ok: true, service: 'gate-guard' });

      if (route === 'GET /api/state') {
        return json(res, 200, store.overview());
      }

      if (route === 'GET /api/rules') {
        return json(res, 200, {
          principle: '任一 HARD 规则命中即阻断下发；SOFT 仅告警。HARD 不可由任何人/标志位绕过。',
          gates: GATES,
          reaches: REACHES,
          rules: RULES,
        });
      }

      if (route === 'POST /api/commands/check') {
        const body = await readJson(req);
        const result = dispatcher.check(body);
        return json(res, 200, { decision: result.allowed ? 'ACCEPTED' : 'BLOCKED', ...result });
      }

      if (route === 'POST /api/commands/dispatch') {
        const body = await readJson(req);
        const out = dispatcher.dispatch(body);
        const status = out.decision === 'ACCEPTED' ? 200 : 409;
        return json(res, status, out);
      }

      if (route === 'POST /api/commands/dispatch-plan') {
        const body = await readJson(req);
        if (!Array.isArray(body.commands)) {
          return json(res, 400, { error: 'commands 必须是数组', code: 'BAD_REQUEST' });
        }
        const out = dispatcher.dispatchPlan(body.commands, {
          mode: body.mode === 'all' ? 'all' : 'stop-on-block',
        });
        return json(res, 200, out);
      }

      if (route === 'GET /api/logs/accepted') {
        return json(res, 200, { entries: store.getAcceptedLog() });
      }
      if (route === 'GET /api/logs/blocked') {
        return json(res, 200, { entries: store.getBlockedLog() });
      }

      return json(res, 404, { error: `not found: ${route}`, code: 'NOT_FOUND' });
    } catch (err) {
      const status = err.statusCode ?? 500;
      return json(res, status, {
        error: err.message ?? 'internal error',
        code: status === 400 ? 'BAD_REQUEST' : status === 413 ? 'PAYLOAD_TOO_LARGE' : 'INTERNAL',
      });
    }
  });

  return { server, store };
}

const isMain = import.meta.url === `file://${process.argv[1]}`;
if (isMain) {
  const { server } = createApp();
  server.listen(PORT, () => {
    console.log(`[gate-guard] 核验服务已启动: http://localhost:${PORT}`);
    console.log('  GET /api/state   GET /api/rules');
    console.log('  POST /api/commands/check | /dispatch | /dispatch-plan');
  });
}
