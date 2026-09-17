/**
 * F-10.01 `delivery.resolve_navigation` 单元测试（FRONTEND-SPEC §3、AC-10.01-01/02）
 *
 * 两条不可放宽的语义：
 * 1. **未知权限码不放行**（不是"不认识就显示"，也不是"不认识就全隐藏"的默认值陷阱，
 *    而是"只有显式命中才可见"）；
 * 2. **菜单过滤只是可用性措施**，因此本文件只断言"显示/不显示"，
 *    绝不把它当作安全边界（后端复核见 AC-10.01-01/02、U01）。
 */

import { describe, expect, it } from 'vitest'
import {
  NAV_ROUTES,
  canAccess,
  defaultLanding,
  groupNavigation,
  resolveNavigation,
  routeByPath,
  type NavRoute,
} from '@/router/navigation'

const ALL_CODES = [
  'ai:ask',
  'kb:view',
  'faq:review',
  'faq:publish',
  'gap:handle',
  'dashboard:view',
  'sys:dept',
  'sys:user',
  'sys:role',
  'sys:model',
]

const ALL_PATHS = ['/chat', '/knowledge', '/precipitation', '/dashboard', '/org', '/model-config']

describe('F-10.01 resolveNavigation · 权限码组合', () => {
  it('拥有全部权限码时按 order 输出六个菜单', () => {
    const items = resolveNavigation(ALL_CODES)

    expect(items.map((i) => i.path)).toEqual(ALL_PATHS)
    expect(items.every((i) => typeof i.label === 'string' && i.label.length > 0)).toBe(true)
  })

  it('权限码全空时输出空菜单，默认落点为 /403', () => {
    expect(resolveNavigation([])).toEqual([])
    expect(defaultLanding([])).toBe('/403')
  })

  it('未知权限码不放行任何菜单（不进也不出）', () => {
    expect(resolveNavigation(['kb:delete', 'sys:audit', 'root'])).toEqual([])
  })

  it('单个权限码只放出对应菜单', () => {
    expect(resolveNavigation(['kb:view']).map((i) => i.path)).toEqual(['/knowledge'])
    expect(resolveNavigation(['ai:ask']).map((i) => i.path)).toEqual(['/chat'])
  })

  it('"任一命中即可见"的菜单在任一子权限下都可见', () => {
    for (const code of ['faq:review', 'faq:publish', 'gap:handle']) {
      expect(resolveNavigation([code]).map((i) => i.path)).toEqual(['/precipitation'])
    }
    for (const code of ['sys:dept', 'sys:user', 'sys:role']) {
      expect(resolveNavigation([code]).map((i) => i.path)).toEqual(['/org'])
    }
  })

  it('组合权限码按 order 稳定排序', () => {
    expect(resolveNavigation(['sys:model', 'kb:view', 'ai:ask']).map((i) => i.path)).toEqual([
      '/chat',
      '/knowledge',
      '/model-config',
    ])
  })

  it('默认落点取第一个可见菜单', () => {
    expect(defaultLanding(['sys:model', 'ai:ask'])).toBe('/chat')
    expect(defaultLanding(['sys:model'])).toBe('/model-config')
  })
})

describe('F-10.01 canAccess · permissions 与 permissionsAll', () => {
  const anyRoute: NavRoute = { path: '/x', label: 'X', group: '系统', icon: 'Setting', permissions: ['a', 'b'], order: 1 }
  const allRoute: NavRoute = { path: '/y', label: 'Y', group: '系统', icon: 'Setting', permissionsAll: ['a', 'b'], order: 2 }
  const bothRoute: NavRoute = {
    path: '/z',
    label: 'Z',
    group: '系统',
    icon: 'Setting',
    permissions: ['a', 'b'],
    permissionsAll: ['c'],
    order: 3,
  }

  it('permissions 是"任一命中"', () => {
    expect(canAccess(['a'], anyRoute)).toBe(true)
    expect(canAccess(['c'], anyRoute)).toBe(false)
  })

  it('permissionsAll 是"全部命中"', () => {
    expect(canAccess(['a'], allRoute)).toBe(false)
    expect(canAccess(['a', 'b'], allRoute)).toBe(true)
  })

  it('两个字段同时存在时必须都满足', () => {
    expect(canAccess(['a', 'c'], bothRoute)).toBe(true)
    expect(canAccess(['a', 'b'], bothRoute)).toBe(false)
    expect(canAccess(['c'], bothRoute)).toBe(false)
  })

  it('两个字段都缺省表示公共菜单', () => {
    expect(canAccess([], { path: '/open', label: '公共', group: '系统', icon: 'Setting', order: 9 })).toBe(true)
  })
})

describe('F-10.01 纯函数与路由表稳定性', () => {
  it('resolveNavigation 不改动入参数组，也不重排 NAV_ROUTES', () => {
    const snapshot = NAV_ROUTES.map((r) => r.path)
    const input = ['kb:view']

    resolveNavigation(input)

    expect(input).toEqual(['kb:view'])
    expect(NAV_ROUTES.map((r) => r.path)).toEqual(snapshot)
  })

  it('分组顺序固定为 问答 → 知识 → 沉淀 → 系统，且不丢项', () => {
    const groups = groupNavigation(resolveNavigation(ALL_CODES))

    expect(groups.map((g) => g.group)).toEqual(['问答', '知识', '沉淀', '系统'])
    expect(groups.flatMap((g) => g.items).map((i) => i.path)).toEqual(ALL_PATHS)
  })

  it('routeByPath 可回查路由定义（守卫据此取 meta）', () => {
    expect(routeByPath('/chat')?.permissions).toEqual(['ai:ask'])
    expect(routeByPath('/nope')).toBeUndefined()
  })
})
