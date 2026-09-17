/**
 * H33 `frontend.normalize_acl` 单元测试（FRONTEND-SPEC §4.3、§9 可测性要求）
 *
 * 安全前提（DESIGN_REVISION §2.1）：四维权限**全空即拒绝所有人**——包括创建者与系统管理员。
 * 因此"全空"必须是可见的显式提示，而不是静默提交。
 * 前端提示不等于放行：提交仍由后端 F-04.08 复核。
 */

import { describe, expect, it } from 'vitest'
import {
  ACL_EMPTY_WARNING,
  aclEquals,
  isAclEmpty,
  normalizeAcl,
  type AclDraft,
} from '@/composables/useAcl'

const empty: AclDraft = { global: false, depts: [], roles: [], users: [] }

describe('H33 normalizeAcl', () => {
  it('去重且保持首次出现顺序', () => {
    const out = normalizeAcl({ global: false, depts: [3, 1, 3, 1, 2], roles: [7, 7], users: [] })

    expect(out.depts).toEqual([3, 1, 2])
    expect(out.roles).toEqual([7])
  })

  it('global 强制为布尔值', () => {
    expect(normalizeAcl({ global: 1 as unknown as boolean, depts: [], roles: [], users: [] }).global).toBe(true)
    expect(normalizeAcl({ global: 0 as unknown as boolean, depts: [], roles: [], users: [] }).global).toBe(false)
  })

  it('非有限数值被剔除（不把 NaN / Infinity 当作有效实体 ID）', () => {
    const out = normalizeAcl({
      global: false,
      depts: [Number.NaN, 2, Number.POSITIVE_INFINITY],
      roles: [],
      users: [5],
    })

    expect(out.depts).toEqual([2])
    expect(out.users).toEqual([5])
  })

  it('维度为 undefined 时不抛错（服务端可能不带该字段）', () => {
    const out = normalizeAcl({ global: false, depts: undefined as unknown as number[], roles: [], users: [] })

    expect(out.depts).toEqual([])
  })

  it('关闭 global 不丢弃其他维度选择', () => {
    const out = normalizeAcl({ global: false, depts: [1], roles: [2], users: [3] })

    expect(out).toEqual({ global: false, depts: [1], roles: [2], users: [3] })
  })

  it('不修改入参对象', () => {
    const draft: AclDraft = { global: true, depts: [1, 1], roles: [], users: [] }

    normalizeAcl(draft)

    expect(draft.depts).toEqual([1, 1])
  })
})

describe('H33 isAclEmpty · 全空即拒绝所有人', () => {
  it('四维全空时为 true', () => {
    expect(isAclEmpty(empty)).toBe(true)
    expect(isAclEmpty({ global: false, depts: [], roles: [], users: [] })).toBe(true)
  })

  it('global 为真时不是全空', () => {
    expect(isAclEmpty({ ...empty, global: true })).toBe(false)
  })

  it('任一维度有实体即不是全空', () => {
    expect(isAclEmpty({ ...empty, depts: [1] })).toBe(false)
    expect(isAclEmpty({ ...empty, roles: [1] })).toBe(false)
    expect(isAclEmpty({ ...empty, users: [1] })).toBe(false)
  })

  it('去重后才判定：重复 ID 不算多个维度', () => {
    expect(isAclEmpty({ ...empty, users: [9, 9, 9] })).toBe(false)
    expect(isAclEmpty({ ...empty, users: [] })).toBe(true)
  })
})

describe('H33 固定提示文案', () => {
  it('文案逐字固定，且不含任何数量或实体名单', () => {
    expect(ACL_EMPTY_WARNING).toBe('四维全空时，任何人都无法读取该知识（含创建者与系统管理员）')
    expect(/\d/.test(ACL_EMPTY_WARNING)).toBe(false)
  })

  it('必须明示"含创建者与系统管理员"（不得暗示存在读权旁路）', () => {
    expect(ACL_EMPTY_WARNING).toContain('创建者')
    expect(ACL_EMPTY_WARNING).toContain('系统管理员')
  })
})

describe('H33 aclEquals', () => {
  it('顺序与重复不影响相等判定', () => {
    const a: AclDraft = { global: false, depts: [1, 2], roles: [], users: [] }
    const b: AclDraft = { global: false, depts: [2, 1, 1], roles: [], users: [] }

    expect(aclEquals(a, b)).toBe(true)
  })

  it('任一维度变化即判定为已改动', () => {
    const base: AclDraft = { global: false, depts: [1], roles: [], users: [] }

    expect(aclEquals(base, { ...base, global: true })).toBe(false)
    expect(aclEquals(base, { ...base, depts: [1, 2] })).toBe(false)
    expect(aclEquals(base, { ...base, roles: [3] })).toBe(false)
    expect(aclEquals(base, { ...base, users: [4] })).toBe(false)
  })

  it('全空与 global=true 不相等（提示必须出现）', () => {
    expect(aclEquals(empty, { ...empty, global: true })).toBe(false)
  })
})
