/**
 * 路由表与全局门禁（FRONTEND-SPEC §2、§3）
 * AC-10.01-01：直接 URL 访问受控，后端仍做授权；
 * AC-10.01-02：刷新身份时同步菜单，不以 UI 代替安全边界。
 */
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { canAccess, defaultLanding, routeByPath } from './navigation'
import { useAuthStore } from '@/stores/auth'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/',
    component: () => import('@/layouts/AppLayout.vue'),
    children: [
      {
        path: 'chat',
        name: 'chat',
        component: () => import('@/views/ChatView.vue'),
        meta: { title: '问答工作台', permissions: ['ai:ask'] },
      },
      {
        path: 'knowledge',
        name: 'knowledge',
        component: () => import('@/views/KnowledgeView.vue'),
        meta: { title: '知识中心', permissions: ['kb:view'] },
      },
      {
        path: 'precipitation',
        name: 'precipitation',
        component: () => import('@/views/PrecipitationView.vue'),
        meta: { title: '沉淀运营', permissions: ['faq:review', 'faq:publish', 'gap:handle'] },
      },
      {
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('@/views/DashboardView.vue'),
        meta: { title: '运营看板', permissions: ['dashboard:view'] },
      },
      {
        path: 'org',
        name: 'org',
        component: () => import('@/views/OrgView.vue'),
        meta: { title: '组织配置', permissions: ['sys:dept', 'sys:user', 'sys:role'] },
      },
      {
        path: 'model-config',
        name: 'model-config',
        component: () => import('@/views/ModelConfigView.vue'),
        meta: { title: '模型配置', permissions: ['sys:model'] },
      },
      {
        path: '403',
        name: 'forbidden',
        component: () => import('@/views/ForbiddenView.vue'),
        meta: { title: '无权限' },
      },
    ],
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { public: true, title: '页面不存在' },
  },
]

const router = createRouter({
  history: createWebHistory(import.meta.env.VITE_BASE || '/'),
  routes,
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  // 未登录：受保护路由一律回登录页，并保留回跳目标（非敏感草稿）
  if (!auth.authenticated) {
    if (to.meta.public) return true
    return { name: 'login', query: to.fullPath !== '/' ? { redirect: to.fullPath } : undefined }
  }

  // 已登录：身份缺失（如刷新后重新登录）先同步一次，菜单随之重建
  if (!auth.identity) {
    const me = await auth.loadIdentity()
    if (!me) {
      return { name: 'login', query: to.fullPath !== '/' ? { redirect: to.fullPath } : undefined }
    }
  }

  if (to.name === 'login') {
    return defaultLanding(auth.permissionCodes)
  }

  if (to.path === '/') {
    return defaultLanding(auth.permissionCodes)
  }

  // 路由级权限：直接输入 URL 也要在这里被拦下（后端仍独立授权）
  const nav = routeByPath(to.path)
  if (nav && !canAccess(auth.permissionCodes, nav)) {
    return { name: 'forbidden' }
  }

  return true
})

router.afterEach((to) => {
  const title = to.meta.title as string | undefined
  document.title = title ? `${title} · 知识库管理平台` : '知识库管理平台'
})

export default router
