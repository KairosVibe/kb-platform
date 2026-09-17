<script setup lang="ts">
/**
 * 状态徽标（FRONTEND-SPEC §5.3、§7.2）
 * 只使用颜色不足以表达状态：**必须同时带文字标签**，颜色为辅助。
 * 未知状态值以原文展示，避免新增状态静默变成"正常"。
 */
import { computed } from 'vue'

const props = defineProps<{ value: string | null | undefined }>()

const LABELS: Record<string, string> = {
  // 知识索引
  pending: '待索引',
  indexed: '已索引',
  stale: '已过期',
  // 索引任务
  queued: '排队中',
  running: '执行中',
  retry_wait: '等待重试',
  succeeded: '已完成',
  failed: '已失败',
  superseded: '已被新版本取代',
  // 任务阶段（不并入状态）
  parsing: '解析中',
  embedding: '向量化中',
  indexing: '建索引中',
  // 问答请求
  accepted: '已登记',
  completed: '已完成',
  rejected: '已驳回',
  cancelled: '已取消',
  // 业务结果
  answered: '回答',
  faq_hit: 'FAQ 命中',
  access_restricted: '权限受限',
  no_evidence: '无可用知识',
  low_confidence: '置信不足',
  service_error: '服务异常',
  // FAQ
  candidate: '候选',
  published: '已发布',
  offline: '已下线',
  // 缺口
  open: '待处理',
  processing: '处理中',
  closed: '已关闭',
}

const safe = computed(() => {
  const v = props.value
  if (v === null || v === undefined || v === '') return 'unknown'
  return String(v)
})

const label = computed(() => LABELS[safe.value] ?? safe.value)
</script>

<template>
  <span class="kb-badge" :class="`kb-state-${safe}`">
    <i class="kb-badge__dot" aria-hidden="true" />
    <span>{{ label }}</span>
  </span>
</template>
