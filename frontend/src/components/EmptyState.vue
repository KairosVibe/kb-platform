<script setup lang="ts">
/** 空态 / 失败可重试（FRONTEND-SPEC §5.2：每个页面必须实现五种状态） */
import { Refresh } from '@element-plus/icons-vue'

withDefaults(
  defineProps<{
    title?: string
    description?: string
    /** 'empty' 空态；'error' 失败可重试 */
    variant?: 'empty' | 'error'
    retryText?: string
  }>(),
  { title: '暂无数据', description: '', variant: 'empty', retryText: '重试' },
)

defineEmits<{ (e: 'retry'): void }>()
</script>

<template>
  <div class="kb-empty">
    <p class="kb-empty__title">{{ title }}</p>
    <p v-if="description" class="kb-empty__desc">{{ description }}</p>
    <el-button v-if="variant === 'error'" :icon="Refresh" @click="$emit('retry')">{{ retryText }}</el-button>
    <slot />
  </div>
</template>

<style scoped>
.kb-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--kb-sp-2);
  padding: var(--kb-sp-8) var(--kb-sp-4);
  color: var(--kb-text-2);
  text-align: center;
}
.kb-empty__title {
  margin: 0;
  font-size: var(--kb-fs-14);
  color: var(--kb-text);
}
.kb-empty__desc {
  margin: 0;
  font-size: var(--kb-fs-13);
  max-width: 520px;
}
</style>
