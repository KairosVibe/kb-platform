<script setup lang="ts">
/**
 * 无权限页（FRONTEND-SPEC §3、§2）
 * 全部菜单均无权限时的落点，以及直接输入受控 URL 的拦截结果。
 * 后端仍会独立授权，本页只是可用性反馈。
 */
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { defaultLanding } from '@/router/navigation'

const auth = useAuthStore()
const router = useRouter()

function goHome(): void {
  void router.replace(defaultLanding(auth.permissionCodes))
}

function signOut(): void {
  auth.resetIdentity()
  void router.replace({ name: 'login' })
}
</script>

<template>
  <div class="kb-page">
    <div class="kb-panel">
      <div class="kb-panel__body">
        <h2 class="kb-page__title">当前账号没有该功能权限</h2>
        <p class="kb-page__desc">
          菜单与按钮的显隐只是界面可用性措施，不构成安全边界；受控操作在后端仍会独立校验。
          如需权限，请联系系统管理员在「组织配置」中分配对应功能码。
        </p>
        <div class="kb-toolbar" style="margin-top: var(--kb-sp-4)">
          <el-button type="primary" @click="goHome">返回可用页面</el-button>
          <el-button @click="signOut">退出登录</el-button>
        </div>
      </div>
    </div>
  </div>
</template>
