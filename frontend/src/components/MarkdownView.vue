<script setup lang="ts">
/**
 * 安全 Markdown 视图（FRONTEND-SPEC §7.7）
 * - HTML 已由 F-10.02 处理为净化后的片段，本组件不做二次拼接；
 * - 代码复制取自 DOM textContent，与原文逐字符一致（AC-10.02-01）；
 * - 引用角标点击向上抛出，由页面定位到引用卡。
 */
import { ElMessage } from 'element-plus'

defineProps<{ html: string }>()

const emit = defineEmits<{ (e: 'cite', no: number): void }>()

async function onClick(event: MouseEvent): Promise<void> {
  const target = event.target as HTMLElement | null
  if (!target) return

  const copyBtn = target.closest('[data-kb-copy]')
  if (copyBtn) {
    const pre = copyBtn.closest('pre')
    const code = pre?.querySelector('code')
    if (!code) return
    const text = code.textContent ?? ''
    try {
      await navigator.clipboard.writeText(text)
      ElMessage.success('已复制代码')
    } catch {
      ElMessage.warning('浏览器拒绝了剪贴板访问')
    }
    return
  }

  const cite = target.closest('[data-kb-cite]') as HTMLElement | null
  if (cite) {
    const no = Number(cite.getAttribute('data-kb-cite'))
    if (Number.isFinite(no)) emit('cite', no)
  }
}

/** 只允许纯展示：不接受外部传入的原始 HTML 拼接 */
</script>

<template>
  <div class="kb-markdown" v-html="html" @click="onClick" />
</template>
