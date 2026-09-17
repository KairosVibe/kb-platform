<script setup lang="ts">
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { defaultLanding } from '@/router/navigation'

const auth = useAuthStore()
const router = useRouter()
</script>

<template>
  <div class="kb-page">
    <div class="kb-panel">
      <div class="kb-panel__body">
        <h2 class="kb-page__title">页面不存在</h2>
        <p class="kb-page__desc">你访问的地址没有对应页面，或该页面已调整。</p>
        <el-button
          type="primary"
          style="margin-top: var(--kb-sp-4)"
          @click="router.replace(auth.authenticated ? defaultLanding(auth.permissionCodes) : { name: 'login' })"
        >
          返回首页
        </el-button>
      </div>
    </div>
  </div>
</template>
