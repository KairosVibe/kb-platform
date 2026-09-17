/**
 * 身份状态（FRONTEND-SPEC §3）
 *
 * 安全红线：
 * - 令牌只存内存（由 api/client 持有），本 store 不写 localStorage；
 * - 页面刷新后内存清空 → 重新登录是首版行为（API-CONTRACTS §2）；
 * - 权限码只来自 GET /api/auth/me，不在前端拼装。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as authApi from '@/api/auth'
import {
  clearTokens,
  currentRefreshToken,
  onAuthStateChange,
  setTokens,
} from '@/api/client'
import type { MeResponse } from '@/api/types'
import { canAccess, resolveNavigation, type NavItem } from '@/router/navigation'

export const useAuthStore = defineStore('auth', () => {
  const identity = ref<MeResponse | null>(null)
  /** 是否有可用 access token（内存态） */
  const authenticated = ref(false)
  const identityLoading = ref(false)

  onAuthStateChange((authed) => {
    authenticated.value = authed
    if (!authed) identity.value = null
  })

  const permissionCodes = computed<string[]>(() => identity.value?.permission_codes ?? [])

  const menu = computed<NavItem[]>(() => {
    if (!identity.value) return []
    return resolveNavigation(permissionCodes.value)
  })

  function can(code: string): boolean {
    return permissionCodes.value.includes(code)
  }

  function canAny(codes: string[]): boolean {
    return codes.some((c) => can(c))
  }

  async function loadIdentity(): Promise<MeResponse | null> {
    identityLoading.value = true
    try {
      const me = await authApi.getMe()
      identity.value = me
      return me
    } catch {
      // 401 已由 client 清令牌；其余错误保持未登录态，由调用方提示
      identity.value = null
      return null
    } finally {
      identityLoading.value = false
    }
  }

  async function signIn(username: string, password: string): Promise<void> {
    const pair = await authApi.login({ username, password })
    setTokens(pair)
    await loadIdentity()
  }

  async function signOut(): Promise<void> {
    const rt = currentRefreshToken()
    try {
      if (rt) await authApi.logout(rt)
    } catch {
      // 注销失败也要清本地身份，避免"看起来还登录着"
    } finally {
      clearTokens()
      identity.value = null
    }
  }

  /** 清空身份（401 后使用），不调用后端 */
  function resetIdentity(): void {
    clearTokens()
    identity.value = null
  }

  return {
    identity,
    authenticated,
    identityLoading,
    permissionCodes,
    menu,
    can,
    canAny,
    loadIdentity,
    signIn,
    signOut,
    resetIdentity,
  }
})
