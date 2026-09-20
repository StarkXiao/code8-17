/**
 * @file 核验服务层：唯一的「下发入口」。
 *       任何调用方（HTTP API / CLI / 未来的 SCADA 适配器）想下发指令，
 *       都必须经过这里——核验不通过直接阻断，绝不改变闸门状态。
 */
import { evaluateCommand } from './rules.js';
import { evaluatePlan } from './plan.js';

export function createDispatcher(store) {
  /**
   * 核验（不执行）单条指令：dry-run，供前端预校验。
   */
  function check(command) {
    return evaluateCommand(command, {
      snapshots: store.getSnapshots(),
      openings: Object.fromEntries(
        Object.entries(store.getGateState()).map(([id, s]) => [id, s.opening]),
      ),
      now: store.now,
    });
  }

  /**
   * 下发单条指令。
   * @returns {{ decision: 'ACCEPTED'|'BLOCKED', command: object, result: object, receipt?: object }}
   */
  function dispatch(command) {
    const result = check(command);
    if (result.allowed) {
      const receipt = store.commitAccepted(command, result);
      return { decision: 'ACCEPTED', command, result, receipt };
    }
    const audit = store.commitBlocked(command, result);
    return { decision: 'BLOCKED', command, result, audit };
  }

  /**
   * 下发一批指令（顺序核验，逐条接受/阻断）。
   * 注意：只有被 ACCEPTED 的指令会真正落到 store；整体被拒时部分指令仍已接受，
   * 这与现场「逐条下发、哪条被拦哪条不动」一致。
   */
  function dispatchPlan(commands, options = {}) {
    // 直接复用 plan 评估（基于 store 快照做纯推演），再把 accepted 的逐条提交。
    const plan = evaluatePlan(commands, {
      snapshots: store.getSnapshots(),
      gateState: store.getGateState(),
      now: store.now,
    }, options);

    const receipts = [];
    for (const r of plan.results) {
      if (r.status === 'accepted') {
        receipts.push(store.commitAccepted(r.command, r.result));
      } else if (r.status === 'blocked') {
        store.commitBlocked(r.command, r.result);
      }
    }
    return { ...plan, receipts };
  }

  return { check, dispatch, dispatchPlan };
}
