/** 用户管理纯函数（T47/T49）：用户名校验、授权勾选集合与提交载荷互转。 */

/** 用户名规则（与后端 UserCreate 一致）：3-32 位字母/数字/._-。 */
export function validateUsername(username: string): string {
  if (!/^[a-zA-Z0-9._-]{3,32}$/.test(username)) {
    return "用户名需为 3-32 位字母/数字/._-";
  }
  return "";
}

export interface GrantsSelection {
  pipelines: Set<number>;
  models: Set<number>;
  providers: Set<number>;
}

export function emptySelection(): GrantsSelection {
  return { pipelines: new Set(), models: new Set(), providers: new Set() };
}

/** 勾选集合 → 提交载荷（排序保证幂等可比）。 */
export function selectionToPayload(sel: GrantsSelection): {
  pipelines: number[];
  models: number[];
  providers: number[];
} {
  return {
    pipelines: [...sel.pipelines].sort((a, b) => a - b),
    models: [...sel.models].sort((a, b) => a - b),
    providers: [...sel.providers].sort((a, b) => a - b),
  };
}

/** 勾选集合是否与提交载荷一致（保存前短路无变化的 PUT）。 */
export function selectionEqualsPayload(
  sel: GrantsSelection,
  payload: { pipelines: number[]; models: number[]; providers: number[] },
): boolean {
  const current = selectionToPayload(sel);
  return (
    sameIds(current.pipelines, payload.pipelines) &&
    sameIds(current.models, payload.models) &&
    sameIds(current.providers, payload.providers)
  );
}

function sameIds(a: number[], b: number[]): boolean {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

/** Key 列表"长期未使用"提示（T51 口径：30 天未用或从未使用且创建超 30 天）。 */
export const KEY_STALE_DAYS = 30;

export function isKeyStale(lastUsedAt: string | null, createdAt: string): boolean {
  const reference = lastUsedAt ? new Date(lastUsedAt) : new Date(createdAt);
  if (Number.isNaN(reference.getTime())) return false;
  return Date.now() - reference.getTime() > KEY_STALE_DAYS * 24 * 3600 * 1000;
}

/** 统计/日志行的用户展示名：user_id=0 且无用户名视为多用户之前的历史数据。 */
export function userLabel(row: { user_id: number; user_username: string }): string {
  return row.user_id === 0 && !row.user_username ? "（历史）" : row.user_username || "（历史）";
}
