/**
 * @file 运行态存储（进程内存）：当前水情、当前闸门开度、已接受指令日志。
 *       无外部依赖；重启后由 seed 基线恢复。
 */
import { randomUUID } from 'node:crypto';
import { baseline } from '../config/seed.js';
import { gateById, reachById } from '../config/model.js';

export function createStore(overrides = {}) {
  const seed = baseline(overrides.now ?? Date.now());

  /** @type {Record<string, { level: number, inflow: number, measuredAt: number }>} */
  const snapshots = structuredClone({ ...seed.reachState, ...(overrides.snapshots ?? {}) });
  /** @type {Record<string, { opening: number }>} */
  const gateState = structuredClone({ ...seed.gateState, ...(overrides.gateState ?? {}) });
  /** @type {Array} 已接受（放行）的指令日志，最新在前 */
  const acceptedLog = [];
  /** @type {Array} 被阻断的指令审计记录，最新在前 */
  const blockedLog = [];

  const fixedNow = overrides.now ?? null;

  return {
    get now() {
      return fixedNow ?? Date.now();
    },

    getSnapshots() {
      return structuredClone(snapshots);
    },

    getGateState() {
      return structuredClone(gateState);
    },

    /** 更新某河段遥测（模拟 SCADA 上报） */
    updateSnapshot(reachId, patch) {
      if (!reachById.has(reachId)) throw new Error(`unknown reach: ${reachId}`);
      snapshots[reachId] = { ...snapshots[reachId], ...patch };
      return structuredClone(snapshots[reachId]);
    },

    /** 指令核验通过后，由服务层调用，落计划开度 + 写日志 */
    commitAccepted(command, result) {
      gateState[command.gateId].opening = result.requested.opening;
      const entry = {
        id: randomUUID(),
        acceptedAt: new Date().toISOString(),
        command,
        requested: result.requested,
        warnings: result.warnings,
      };
      acceptedLog.unshift(entry);
      return entry;
    },

    /** 核验未通过：仅记审计，不改任何状态 */
    commitBlocked(command, result) {
      const entry = {
        id: randomUUID(),
        blockedAt: new Date().toISOString(),
        command,
        violations: result.violations,
        warnings: result.warnings,
      };
      blockedLog.unshift(entry);
      return entry;
    },

    getAcceptedLog(limit = 50) {
      return acceptedLog.slice(0, limit);
    },

    getBlockedLog(limit = 50) {
      return blockedLog.slice(0, limit);
    },

    overview() {
      return {
        time: new Date().toISOString(),
        reaches: Object.fromEntries(
          Object.keys(snapshots).map((id) => [id, {
            name: reachById.get(id)?.name ?? id,
            ...snapshots[id],
            measuredAtIso: snapshots[id].measuredAt
              ? new Date(snapshots[id].measuredAt).toISOString() : null,
          }]),
        ),
        gates: Object.fromEntries(
          Object.keys(gateState).map((id) => [id, {
            name: gateById.get(id)?.name ?? id,
            ...gateState[id],
            maxOpening: gateById.get(id)?.maxOpening,
          }]),
        ),
      };
    },
  };
}
