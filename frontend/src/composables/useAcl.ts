/**
 * H33 frontend.normalize_acl（FUNCTION-MAP §4 / FRONTEND-SPEC §4.3）
 *
 * 处理逻辑：实体去重 → 保留非 global 选择 → 全空提示默认拒绝。
 * 边界：前端提示不等于放行，提交仍由 F-04.08 后端复核。
 */

export interface AclDraft {
  global: boolean
  depts: number[]
  roles: number[]
  users: number[]
}

/** 四维全空提示（不得暗示"无人可读"以外的任何信息，也不含实体名单） */
export const ACL_EMPTY_WARNING = '四维全空时，任何人都无法读取该知识（含创建者与系统管理员）'

/** 关闭 global 不丢弃其他维度选择；去重且保持稳定顺序 */
export function normalizeAcl(draft: AclDraft): AclDraft {
  return {
    global: Boolean(draft.global),
    depts: uniq(draft.depts),
    roles: uniq(draft.roles),
    users: uniq(draft.users),
  }
}

function uniq(ids: number[]): number[] {
  const seen = new Set<number>()
  const out: number[] = []
  for (const id of ids ?? []) {
    const n = Number(id)
    if (!Number.isFinite(n) || seen.has(n)) continue
    seen.add(n)
    out.push(n)
  }
  return out
}

/** 是否全空（此时服务端会拒绝所有人，包括创建者与管理员） */
export function isAclEmpty(draft: AclDraft): boolean {
  const d = normalizeAcl(draft)
  return !d.global && d.depts.length === 0 && d.roles.length === 0 && d.users.length === 0
}

/** 只把发生变化的维度放进 PATCH 对比，用于"是否有改动"判断 */
export function aclEquals(a: AclDraft, b: AclDraft): boolean {
  const x = normalizeAcl(a)
  const y = normalizeAcl(b)
  return (
    x.global === y.global &&
    sameIds(x.depts, y.depts) &&
    sameIds(x.roles, y.roles) &&
    sameIds(x.users, y.users)
  )
}

function sameIds(a: number[], b: number[]): boolean {
  if (a.length !== b.length) return false
  const sa = [...a].sort((m, n) => m - n)
  const sb = [...b].sort((m, n) => m - n)
  return sa.every((v, i) => v === sb[i])
}
