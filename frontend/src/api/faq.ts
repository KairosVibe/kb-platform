/** M06 FAQ 沉淀审核与缓存：F-06.01—F-06.05 + H34 + API-S07 */
import { apiRequest } from './client'
import type { FaqRow, FaqSaved, MiningRun, MiningRunState, PageResult } from './types'

/**
 * H34 GET /api/faqs —— 检查 faq:review 或 faq:publish 及**全部来源读权**。
 * 边界：不先分页再过滤造成总数泄露，故 total 与 items 均由服务端授权后给出。
 */
export function listFaqs(query: {
  status?: string | null
  q?: string
  page?: number
  size?: number
}): Promise<PageResult<FaqRow>> {
  return apiRequest<PageResult<FaqRow>>('/faqs', { query })
}

/** F-06.01 POST /api/mining/runs —— 薄异步适配器，client_action_id 幂等 */
export function runMining(clientActionId: string, pipelineVersion: string): Promise<MiningRun> {
  return apiRequest<MiningRun>('/mining/runs', {
    method: 'POST',
    body: { client_action_id: clientActionId, pipeline_version: pipelineVersion },
    idempotent: true,
  })
}

/** API-S07 GET /api/mining/runs/{id} —— 仅统计与安全错误 */
export function getMiningRun(runId: number): Promise<MiningRunState> {
  return apiRequest<MiningRunState>(`/mining/runs/${runId}`)
}

/** F-06.02 候选编辑 —— 问题 1—4000，答案 1—20000，来源去重后 ≤1000 */
export function editCandidate(
  faqId: number,
  payload: { question: string; answer: string; source_ids: number[]; expected_revision: number },
): Promise<FaqSaved> {
  return apiRequest<FaqSaved>(`/faqs/${faqId}/candidate`, { method: 'PATCH', body: payload })
}

/** F-06.03 发布 —— 必须显式走 publish，来源为空不得自动发布 */
export function publishFaq(faqId: number, expectedRevision: number): Promise<FaqSaved> {
  return apiRequest<FaqSaved>(`/faqs/${faqId}/publish`, {
    method: 'POST',
    body: { expected_revision: expectedRevision },
  })
}

/**
 * F-06.04 状态流转。
 * action=reject 仅 candidate（faq:review）；
 * action=offline 仅 published（faq:publish）；
 * action=resubmit 仅 rejected/offline/stale（faq:review，重新验证全部来源后转 candidate）。
 */
export function changeFaqStatus(
  faqId: number,
  payload: { action: 'reject' | 'offline' | 'resubmit' | string; reason: string; expected_revision: number },
): Promise<{ faq_id: number; status: string }> {
  return apiRequest<{ faq_id: number; status: string }>(`/faqs/${faqId}/status`, {
    method: 'POST',
    body: payload,
  })
}

/** F-06.05 缓存开关 —— 不改变审核状态 */
export function setCacheEnabled(
  faqId: number,
  enabled: boolean,
  expectedRevision: number,
): Promise<{ faq_id: number; enabled: boolean; status: string }> {
  return apiRequest(`/faqs/${faqId}/cache-enabled`, {
    method: 'PUT',
    body: { enabled, expected_revision: expectedRevision },
  })
}
