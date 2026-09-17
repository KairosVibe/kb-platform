/** M09 模型配置与运行控制：F-09.01—F-09.04 */
import { apiRequest } from './client'
import type { ModelConfig, ProbeResult } from './types'

/** F-09.01 */
export function readModelConfig(): Promise<ModelConfig> {
  return apiRequest<ModelConfig>('/model-config')
}

/**
 * F-09.02 PATCH —— 只提交变更字段。
 * patch 白名单（FUNCTION-MAP §1）：llm_model、embedding_model、rerank_model、
 * faq_threshold、cluster_threshold、gap_threshold、min_frequency、top_k、mining_interval_seconds。
 * 白名单之外字段一律 422，故前端不提供额外可编辑项。
 * embedding_model 变更若返回 409 REINDEX_REQUIRED，必须显式提示需重建索引。
 */
export function updateModelConfig(patch: Record<string, unknown>, expectedRevision: number): Promise<{ revision: number }> {
  return apiRequest<{ revision: number }>('/model-config', {
    method: 'PATCH',
    body: { patch, expected_revision: expectedRevision },
  })
}

/** F-09.03 连通探测 —— 展示实测延迟，不缓存探测结论 */
export function probeProvider(provider: string): Promise<ProbeResult> {
  return apiRequest<ProbeResult>('/model-config/probe', { method: 'POST', body: { provider } })
}

/** F-09.04 就绪检查（不经 /api 前缀） */
export async function readReadiness(): Promise<{ ready: boolean; checks: Record<string, boolean>; version: string }> {
  const resp = await fetch('/ready')
  if (!resp.ok) throw new Error(`ready ${resp.status}`)
  return (await resp.json()) as { ready: boolean; checks: Record<string, boolean>; version: string }
}
