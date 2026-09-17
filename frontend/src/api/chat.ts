/** M05 会话检索与流式问答：F-05.01—F-05.09 + H27/H28/H35 */
import { apiRequest, openEventStream } from './client'
import type {
  CancelResult,
  ChatAccepted,
  CitationDetail,
  HistoryMessage,
  PageResult,
  RequestSnapshot,
  SessionCreated,
  SessionMutated,
  SessionRow,
} from './types'

/** H28 GET /api/sessions —— owner 过滤后分页，不展示他人会话标题 */
export function listSessions(query: { page?: number; size?: number }): Promise<PageResult<SessionRow>> {
  return apiRequest<PageResult<SessionRow>>('/sessions', { query })
}

/** F-05.01 */
export function createSession(title: string | null): Promise<SessionCreated> {
  return apiRequest<SessionCreated>('/sessions', { method: 'POST', body: { title } })
}

/** F-05.02 历史消息（restricted=true 的条目只能显示受限占位） */
export function readHistory(
  sessionId: number,
  query: { page?: number; size?: number },
): Promise<PageResult<HistoryMessage>> {
  return apiRequest<PageResult<HistoryMessage>>(`/sessions/${sessionId}/messages`, { query })
}

/** F-05.03 action=rename|delete */
export function mutateSession(
  sessionId: number,
  payload: { action: 'rename' | 'delete' | string; title?: string | null },
): Promise<SessionMutated> {
  return apiRequest<SessionMutated>(`/sessions/${sessionId}`, { method: 'PATCH', body: payload })
}

/**
 * F-05.04 POST /api/chat/requests
 * client_request_id 由调用方生成并在重试中**复用同一值**，这是幂等的唯一依据：
 * 同键同载荷返回 200 与原 ID，同键异载荷 409。
 */
export function acceptQuestion(payload: {
  session_id: number
  client_request_id: string
  question: string
}): Promise<ChatAccepted> {
  return apiRequest<ChatAccepted>('/chat/requests', {
    method: 'POST',
    body: payload,
    idempotent: true,
  })
}

/** F-05.06 —— 返回原始 Response，由 H31 解帧；不套 JSON 外壳 */
export function openEvents(requestId: number, afterSeq: number, signal?: AbortSignal): Promise<Response> {
  return openEventStream(`/chat/requests/${requestId}/events`, { after_seq: afterSeq }, signal)
}

/** H27 GET /api/chat/requests/{id} —— 410 时的安全快照入口，不得重新 POST 同问题 */
export function getRequestSnapshot(requestId: number): Promise<RequestSnapshot> {
  return apiRequest<RequestSnapshot>(`/chat/requests/${requestId}`)
}

/** F-05.07 —— 停止按钮；已完成态不改为取消，断网不等于取消 */
export function cancelRequest(requestId: number): Promise<CancelResult> {
  return apiRequest<CancelResult>(`/chat/requests/${requestId}/cancel`, { method: 'POST' })
}

/** H35 / F-05.08 引用详情定位 —— 来源由服务端复核，失效时显示不可用 */
export function readCitation(requestId: number, no: number): Promise<CitationDetail> {
  return apiRequest<CitationDetail>(`/chat/requests/${requestId}/citations/${no}`)
}

/** F-05.09 智能联想 */
export function suggest(prefix: string, limit = 8): Promise<{ items: string[] }> {
  return apiRequest<{ items: string[] }>('/chat/suggestions', { query: { prefix, limit } })
}
