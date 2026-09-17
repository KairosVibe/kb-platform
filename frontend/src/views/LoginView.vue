<script setup lang="ts">
/**
 * 登录页（M01 / PRD §1.1.1，FRONTEND-SPEC §4.1）
 * - 令牌由 api/client 只存内存，本页不写 localStorage/sessionStorage；
 * - 失败只显示契约 message，不做"用户不存在/密码错误"区分（防枚举）；
 * - 非敏感草稿（用户名）保留在内存中，回登录时回填，密码永不保留。
 */
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { User, Lock } from '@element-plus/icons-vue'
import { ApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { defaultLanding } from '@/router/navigation'

/**
 * 非敏感草稿：只保留用户名，且**只在内存中**（不写任何浏览器持久存储）。
 * 满足 PRD §1.1.1「回登录不丢弃非敏感草稿」，同时不触碰令牌存储红线。
 */
const draft = { username: '' }
function readDraft(key: 'username'): string {
  return key === 'username' ? draft.username : ''
}
function writeDraft(key: 'username', value: string): void {
  if (key === 'username') draft.username = value
}

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const username = ref('')
const password = ref('')
const submitting = ref(false)
const errorText = ref('')
const retryAfter = ref(0)

onMounted(() => {
  username.value = readDraft('username')
})

async function submit(): Promise<void> {
  if (submitting.value) return
  errorText.value = ''

  if (!username.value.trim()) {
    errorText.value = '请输入用户名'
    return
  }
  if (!password.value) {
    errorText.value = '请输入密码'
    return
  }

  submitting.value = true
  try {
    await auth.signIn(username.value, password.value)
    writeDraft('username', username.value)
    password.value = ''
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : ''
    await router.replace(redirect || defaultLanding(auth.permissionCodes))
  } catch (err) {
    if (err instanceof ApiError && err.status === 429) {
      errorText.value = '登录尝试过于频繁，请稍后重试'
    } else if (err instanceof ApiError) {
      errorText.value = err.message
    } else {
      errorText.value = '登录失败，请稍后重试'
    }
    ElMessage.closeAll()
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="kb-login">
    <section class="kb-login__brand">
      <p class="kb-login__logo">KB</p>
      <h1 class="kb-login__title">知识库管理平台</h1>
      <p class="kb-login__subtitle">
        组织与功能权限 · 多格式知识维护 · 四维数据权限 · 授权问答与流式回答 · FAQ 沉淀与知识缺口闭环
      </p>
      <ul class="kb-login__points">
        <li>正文读取按四维权限判定，默认全空拒绝</li>
        <li>权限变更即时生效，历史答案按当前来源状态复核</li>
        <li>受限内容只返回固定提示，不泄露标题与数量</li>
      </ul>
    </section>

    <section class="kb-login__panel">
      <h2 class="kb-login__panel-title">登录</h2>
      <el-form label-position="top" @submit.prevent>
        <el-form-item label="用户名">
          <el-input
            v-model="username"
            size="large"
            autocomplete="username"
            placeholder="请输入用户名"
            :prefix-icon="User"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="password"
            type="password"
            size="large"
            show-password
            autocomplete="current-password"
            placeholder="请输入密码"
            :prefix-icon="Lock"
            @keyup.enter="submit"
          />
        </el-form-item>
      </el-form>

      <p v-if="errorText" class="kb-login__error" role="alert">{{ errorText }}</p>

      <el-button type="primary" size="large" class="kb-login__submit" :loading="submitting" @click="submit">
        登录
      </el-button>

      <p class="kb-login__note">
        出于安全考虑，令牌只保存在内存中；刷新页面后需要重新登录，且不会丢失已填写的用户名。
      </p>
    </section>
  </div>
</template>

<style scoped>
.kb-login {
  display: flex;
  min-height: 100%;
  background: var(--kb-bg);
}
.kb-login__brand {
  flex: 1 1 55%;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: var(--kb-sp-6);
  padding: var(--kb-sp-12);
  background: linear-gradient(135deg, #1d4ed8 0%, #0f172a 100%);
  color: #fff;
}
.kb-login__logo {
  width: 44px;
  height: 44px;
  margin: 0;
  line-height: 44px;
  text-align: center;
  font-weight: 600;
  border-radius: var(--kb-r-4);
  background: rgba(255, 255, 255, 0.16);
}
.kb-login__title {
  margin: 0;
  font-size: var(--kb-fs-32);
  font-weight: 600;
}
.kb-login__subtitle {
  margin: 0;
  max-width: 520px;
  font-size: var(--kb-fs-14);
  line-height: 1.7;
  color: rgba(255, 255, 255, 0.82);
}
.kb-login__points {
  margin: 0;
  padding-left: var(--kb-sp-6);
  font-size: var(--kb-fs-13);
  line-height: 2;
  color: rgba(255, 255, 255, 0.72);
}
.kb-login__panel {
  flex: 1 1 45%;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: var(--kb-sp-12);
  max-width: 460px;
  margin: 0 auto;
}
.kb-login__panel-title {
  margin: 0 0 var(--kb-sp-6);
  font-size: var(--kb-fs-20);
  font-weight: 600;
}
.kb-login__error {
  margin: 0 0 var(--kb-sp-3);
  padding: var(--kb-sp-2) var(--kb-sp-3);
  font-size: var(--kb-fs-13);
  color: var(--kb-danger);
  background: var(--kb-danger-soft);
  border-radius: var(--kb-r-2);
}
.kb-login__submit {
  width: 100%;
}
.kb-login__note {
  margin: var(--kb-sp-4) 0 0;
  font-size: var(--kb-fs-12);
  line-height: 1.7;
  color: var(--kb-text-3);
}

@media (max-width: 1024px) {
  .kb-login__brand {
    display: none;
  }
  .kb-login__panel {
    flex: 1 1 100%;
  }
}
</style>
