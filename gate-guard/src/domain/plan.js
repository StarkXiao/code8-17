/**
 * @file 指令计划核验：按顺序逐条核验「一批」指令。
 *
 * 语义：
 * - 每一条指令都基于「原始实测水情 + 此前已被接受的指令叠加后的计划开度」核验；
 *   被阻断的指令不生效，不影响后续指令的核验条件；
 * - mode='stop-on-block'：第一条 HARD 阻断后停止（默认，匹配现场逐条下发）；
 * - mode='all'：继续核验剩余指令，便于一次拿到整批问题清单；
 * - 任何一条被阻断，整批计划整体判定为 rejected（阻断下发）。
 */
import { gateById } from '../config/model.js';
import { evaluateCommand } from './rules.js';

/**
 * @param {object[]} commands
 * @param {object} base
 * @param {Record<string, { level: number, inflow: number, measuredAt?: number }>} base.snapshots
 * @param {Record<string, { opening: number }>} base.gateState
 * @param {number} base.now
 * @param {{ mode?: 'stop-on-block'|'all' }} [options]
 */
export function evaluatePlan(commands, base, options = {}) {
  const mode = options.mode ?? 'stop-on-block';
  const currentOpenings = Object.fromEntries(
    Object.entries(base.gateState).map(([id, s]) => [id, s.opening]),
  );

  const results = [];
  let blockedCount = 0;
  let warnedCount = 0;
  let stopped = false;

  commands.forEach((command, index) => {
    if (stopped) {
      results.push({ index, command, status: 'skipped',
        result: { allowed: false, violations: [], warnings: [] } });
      return;
    }

    const result = evaluateCommand(command, {
      snapshots: base.snapshots,
      openings: currentOpenings,
      now: base.now,
    });

    const entry = {
      index,
      command,
      status: result.allowed ? 'accepted' : 'blocked',
      result,
    };
    results.push(entry);

    if (result.allowed) {
      // 接受：把计划开度推进到目标开度
      const gate = gateById.get(command.gateId);
      if (gate && result.requested) {
        currentOpenings[command.gateId] = result.requested.opening;
      }
      if (result.warnings.length > 0) warnedCount += 1;
    } else {
      blockedCount += 1;
      if (mode === 'stop-on-block') stopped = true;
    }
  });

  return {
    overall: blockedCount === 0 ? 'accepted' : 'rejected',
    mode,
    total: commands.length,
    blockedCount,
    warnedCount,
    results,
  };
}
