/**
 * F-10.01 delivery.resolve_navigation（FUNCTION-MAP §4 / FRONTEND-SPEC §3）
 *
 * 纯函数：按 identity.permission_codes 过滤静态路由表，输出 `{path,label}`。
 * 不依赖任何组件实例或网络，便于直接单测 AC-10.01-01/02。
 *
 * 不可放宽：菜单过滤只是可用性措施，**不以 UI 代替安全边界**（DESIGN_REVISION §2.1）。
 */

export interface NavRoute {
  path: string
  label: string
  /** 所属菜单分组 */
  group: string
  icon: string
  /** 任一命中即可见 */
  permissions?: string[]
  /** 全部命中才可见 */
  permissionsAll?: string[]
  /** 落地优先级，越小越靠前 */
  order: number
}

/** 路由表（path 字符串由 FRONTEND-SPEC §2 定义，上游未规定） */
export const NAV_ROUTES: NavRoute[] = [
  { path: '/chat', label: '问答工作台', group: '问答', icon: 'ChatDotRound', permissions: ['ai:ask'], order: 10 },
  { path: '/knowledge', label: '知识中心', group: '知识', icon: 'Collection', permissions: ['kb:view'], order: 20 },
  {
    path: '/precipitation',
    label: '沉淀运营',
    group: '沉淀',
    icon: 'Refresh',
    permissions: ['faq:review', 'faq:publish', 'gap:handle'],
    order: 30,
  },
  { path: '/dashboard', label: '运营看板', group: '系统', icon: 'DataLine', permissions: ['dashboard:view'], order: 40 },
  {
    path: '/org',
    label: '组织配置',
    group: '系统',
    icon: 'OfficeBuilding',
    permissions: ['sys:dept', 'sys:user', 'sys:role'],
    order: 50,
  },
  { path: '/model-config', label: '模型配置', group: '系统', icon: 'Setting', permissions: ['sys:model'], order: 60 },
]

export interface NavItem {
  path: string
  label: string
}

function hasAll(codes: Set<string>, required: string[]): boolean {
  return required.every((code) => codes.has(code))
}

function hasAny(codes: Set<string>, required: string[]): boolean {
  return required.some((code) => codes.has(code))
}

/** 单条路由是否对当前权限码可见 */
export function canAccess(permissionCodes: readonly string[], route: NavRoute): boolean {
  const codes = new Set(permissionCodes)
  if (route.permissionsAll && route.permissionsAll.length > 0 && !hasAll(codes, route.permissionsAll)) {
    return false
  }
  if (route.permissions && route.permissions.length > 0 && !hasAny(codes, route.permissions)) {
    return false
  }
  return true
}

/** 过滤菜单；输出顺序按 order 稳定排序 */
export function resolveNavigation(
  permissionCodes: readonly string[],
  routes: readonly NavRoute[] = NAV_ROUTES,
): NavItem[] {
  return routes
    .filter((route) => canAccess(permissionCodes, route))
    .sort((a, b) => a.order - b.order)
    .map((route) => ({ path: route.path, label: route.label }))
}

/** 已登录后的默认落点：第一个可见菜单；全部不可见时落到 /403 */
export function defaultLanding(permissionCodes: readonly string[]): string {
  const items = resolveNavigation(permissionCodes)
  return items.length > 0 ? items[0]!.path : '/403'
}

/** 按分组组织菜单，供侧边栏渲染 */
export function groupNavigation(items: NavItem[]): Array<{ group: string; items: NavItem[] }> {
  const order = ['问答', '知识', '沉淀', '系统']
  const map = new Map<string, NavItem[]>()
  for (const item of items) {
    const route = NAV_ROUTES.find((r) => r.path === item.path)
    const group = route?.group ?? '其他'
    if (!map.has(group)) map.set(group, [])
    map.get(group)!.push(item)
  }
  return [...map.entries()]
    .sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]))
    .map(([group, groupItems]) => ({ group, items: groupItems }))
}

export function routeByPath(path: string): NavRoute | undefined {
  return NAV_ROUTES.find((r) => r.path === path)
}
