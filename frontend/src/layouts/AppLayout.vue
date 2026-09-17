<script setup lang="ts">
/**
 * 全局布局：左侧主导航 + 顶栏 + 内容区（FRONTEND-SPEC §7.5）。
 * 菜单来源：F-10.01 resolve_navigation（由 auth store 的 menu 计算属性提供）。
 */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { Moon, Sunny, Expand, Fold, SwitchButton, User } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { groupNavigation, NAV_ROUTES } from '@/router/navigation'

const auth = useAuthStore()
const ui = useUiStore()
const route = useRoute()
const router = useRouter()

const groups = computed(() => groupNavigation(auth.menu))

function iconFor(path: string): string {
  return NAV_ROUTES.find((r) => r.path === path)?.icon ?? 'Document'
}

async function handleSignOut(): Promise<void> {
  try {
    await ElMessageBox.confirm('退出后需要重新登录，当前会话的两类令牌都会失效。', '确认退出', {
      type: 'warning',
    })
  } catch {
    return
  }
  await auth.signOut()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <div class="kb-shell" :class="{ 'is-collapsed': ui.sidebarCollapsed }">
    <aside class="kb-side">
      <div class="kb-brand">
        <span class="kb-brand__mark">KB</span>
        <span v-show="!ui.sidebarCollapsed" class="kb-brand__text">知识库管理平台</span>
      </div>

      <nav class="kb-nav" aria-label="主导航">
        <div v-for="g in groups" :key="g.group" class="kb-nav__group">
          <p v-show="!ui.sidebarCollapsed" class="kb-nav__label">{{ g.group }}</p>
          <router-link
            v-for="item in g.items"
            :key="item.path"
            :to="item.path"
            class="kb-nav__item"
            :class="{ 'is-active': route.path === item.path }"
            :title="item.label"
          >
            <el-icon class="kb-nav__icon"><component :is="iconFor(item.path)" /></el-icon>
            <span v-show="!ui.sidebarCollapsed">{{ item.label }}</span>
          </router-link>
        </div>
        <p v-if="groups.length === 0" class="kb-nav__label">当前账号没有可用菜单</p>
      </nav>
    </aside>

    <div class="kb-main">
      <header class="kb-top">
        <el-button text :icon="ui.sidebarCollapsed ? Expand : Fold" @click="ui.toggleSidebar()" aria-label="切换侧栏" />
        <h1 class="kb-top__title">{{ route.meta.title || '知识库管理平台' }}</h1>
        <div class="kb-top__right">
          <el-button text :icon="ui.theme === 'dark' ? Sunny : Moon" @click="ui.toggleTheme()" aria-label="切换主题" />
          <span class="kb-user">
            <el-icon><User /></el-icon>
            <span>{{ auth.identity?.username ?? '未登录' }}</span>
          </span>
          <el-button text :icon="SwitchButton" @click="handleSignOut">退出</el-button>
        </div>
      </header>

      <main class="kb-content">
        <router-view />
      </main>
    </div>
  </div>
</template>

<style scoped>
.kb-shell {
  display: flex;
  min-height: 100%;
  background: var(--kb-bg);
}
.kb-side {
  width: var(--kb-sidebar-w);
  flex: 0 0 var(--kb-sidebar-w);
  background: var(--kb-surface);
  border-right: 1px solid var(--kb-border);
  transition: width 0.18s ease, flex-basis 0.18s ease;
  overflow: hidden;
}
.kb-shell.is-collapsed .kb-side {
  width: var(--kb-sidebar-w-collapsed);
  flex-basis: var(--kb-sidebar-w-collapsed);
}
.kb-brand {
  display: flex;
  align-items: center;
  gap: var(--kb-sp-2);
  height: var(--kb-topbar-h);
  padding: 0 var(--kb-sp-4);
  border-bottom: 1px solid var(--kb-border);
}
.kb-brand__mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  flex: 0 0 28px;
  border-radius: var(--kb-r-3);
  background: var(--kb-primary);
  color: #fff;
  font-size: var(--kb-fs-12);
  font-weight: 600;
}
.kb-brand__text {
  font-weight: 600;
  white-space: nowrap;
}
.kb-nav {
  padding: var(--kb-sp-3) var(--kb-sp-2);
}
.kb-nav__group + .kb-nav__group {
  margin-top: var(--kb-sp-4);
}
.kb-nav__label {
  margin: 0 0 var(--kb-sp-2);
  padding: 0 var(--kb-sp-2);
  font-size: var(--kb-fs-12);
  color: var(--kb-text-3);
}
.kb-nav__item {
  display: flex;
  align-items: center;
  gap: var(--kb-sp-2);
  height: 36px;
  padding: 0 var(--kb-sp-2);
  border-radius: var(--kb-r-2);
  color: var(--kb-text-2);
  font-size: var(--kb-fs-14);
}
.kb-nav__item:hover {
  background: var(--kb-surface-2);
  text-decoration: none;
}
.kb-nav__item.is-active {
  background: var(--kb-primary-soft);
  color: var(--kb-primary);
  font-weight: 500;
}
.kb-nav__icon {
  flex: 0 0 16px;
}
.kb-main {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.kb-top {
  display: flex;
  align-items: center;
  gap: var(--kb-sp-3);
  height: var(--kb-topbar-h);
  padding: 0 var(--kb-sp-4);
  background: var(--kb-surface);
  border-bottom: 1px solid var(--kb-border);
}
.kb-top__title {
  flex: 1 1 auto;
  margin: 0;
  font-size: var(--kb-fs-16);
  font-weight: 600;
}
.kb-top__right {
  display: flex;
  align-items: center;
  gap: var(--kb-sp-2);
}
.kb-user {
  display: inline-flex;
  align-items: center;
  gap: var(--kb-sp-1);
  font-size: var(--kb-fs-13);
  color: var(--kb-text-2);
}
.kb-content {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
}
</style>
