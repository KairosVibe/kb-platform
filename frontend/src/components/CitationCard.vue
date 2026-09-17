<script setup lang="ts">
/**
 * 引用卡（H35 / F-05.08，FRONTEND-SPEC §4.4）
 * 点击后由服务端复核来源权限；失效显示"不可用"，**不展示受限正文**。
 */
import { ref } from 'vue'
import { Link } from '@element-plus/icons-vue'
import * as chatApi from '@/api/chat'
import type { CitationDetail } from '@/api/types'
import { ApiError } from '@/api/client'

const props = defineProps<{
  requestId: number
  no: number
  title: string
}>()

const detail = ref<CitationDetail | null>(null)
const loading = ref(false)
const unavailable = ref(false)

async function open(): Promise<void> {
  if (loading.value || unavailable.value || detail.value) return
  loading.value = true
  try {
    detail.value = await chatApi.readCitation(props.requestId, props.no)
  } catch (err) {
    // 来源被撤权/失效：只显示不可用，不透露原因细节
    unavailable.value = true
    if (err instanceof ApiError && err.status >= 500) unavailable.value = true
  } finally {
    loading.value = false
  }
}

function locate(): string {
  const d = detail.value
  if (!d) return ''
  if (d.page_no !== null) return `第 ${d.page_no} 页`
  if (d.offset >= 0) return `偏移 ${d.offset}`
  return '位置未知'
}
</script>

<template>
  <div class="kb-cite-card" :class="{ 'is-open': detail }">
    <button type="button" class="kb-cite-card__head" @click="open">
      <span class="kb-cite-card__no">{{ no }}</span>
      <span class="kb-cite-card__title kb-truncate">{{ title || '受限知识' }}</span>
      <el-icon class="kb-cite-card__icon"><Link /></el-icon>
    </button>

    <p v-if="loading" class="kb-cite-card__hint">加载中…</p>
    <p v-else-if="unavailable" class="kb-cite-card__hint">来源当前不可用</p>
    <template v-else-if="detail">
      <p class="kb-cite-card__meta kb-faint">{{ locate() }}</p>
      <p class="kb-cite-card__snippet">{{ detail.snippet }}</p>
    </template>
  </div>
</template>

<style scoped>
.kb-cite-card {
  border: 1px solid var(--kb-border);
  border-radius: var(--kb-r-3);
  background: var(--kb-surface);
  overflow: hidden;
}
.kb-cite-card__head {
  display: flex;
  align-items: center;
  gap: var(--kb-sp-2);
  width: 100%;
  padding: var(--kb-sp-2) var(--kb-sp-3);
  background: none;
  border: none;
  cursor: pointer;
  text-align: left;
  color: inherit;
  font: inherit;
}
.kb-cite-card__head:hover {
  background: var(--kb-surface-2);
}
.kb-cite-card__no {
  flex: 0 0 20px;
  height: 20px;
  line-height: 20px;
  text-align: center;
  font-size: var(--kb-fs-12);
  color: var(--kb-primary);
  background: var(--kb-primary-soft);
  border-radius: var(--kb-r-1);
}
.kb-cite-card__title {
  flex: 1 1 auto;
  font-size: var(--kb-fs-13);
}
.kb-cite-card__icon {
  flex: 0 0 auto;
  color: var(--kb-text-3);
}
.kb-cite-card__hint,
.kb-cite-card__meta,
.kb-cite-card__snippet {
  margin: 0;
  padding: 0 var(--kb-sp-3) var(--kb-sp-2);
  font-size: var(--kb-fs-13);
}
.kb-cite-card__snippet {
  padding-bottom: var(--kb-sp-3);
  color: var(--kb-text-2);
  white-space: pre-wrap;
}
</style>
