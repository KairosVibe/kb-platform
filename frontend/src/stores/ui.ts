/** UI 偏好：仅主题与折叠状态，**不存任何身份或令牌**（FRONTEND-SPEC §7.6） */
import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

const THEME_KEY = 'kb.theme'
const SIDEBAR_KEY = 'kb.sidebar.collapsed'

export type ThemeMode = 'light' | 'dark'

export const useUiStore = defineStore('ui', () => {
  const theme = ref<ThemeMode>(readTheme())
  const sidebarCollapsed = ref(readBool(SIDEBAR_KEY, false))

  function applyTheme(mode: ThemeMode): void {
    const root = document.documentElement
    root.classList.toggle('dark', mode === 'dark')
  }

  applyTheme(theme.value)

  watch(theme, (mode) => {
    applyTheme(mode)
    try {
      localStorage.setItem(THEME_KEY, mode)
    } catch {
      /* 隐私模式忽略 */
    }
  })

  watch(sidebarCollapsed, (collapsed) => {
    try {
      localStorage.setItem(SIDEBAR_KEY, String(collapsed))
    } catch {
      /* 隐私模式忽略 */
    }
  })

  function toggleTheme(): void {
    theme.value = theme.value === 'dark' ? 'light' : 'dark'
  }

  function toggleSidebar(): void {
    sidebarCollapsed.value = !sidebarCollapsed.value
  }

  return { theme, sidebarCollapsed, toggleTheme, toggleSidebar }
})

function readTheme(): ThemeMode {
  try {
    return localStorage.getItem(THEME_KEY) === 'dark' ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

function readBool(key: string, fallback: boolean): boolean {
  try {
    const raw = localStorage.getItem(key)
    return raw === null ? fallback : raw === 'true'
  } catch {
    return fallback
  }
}
