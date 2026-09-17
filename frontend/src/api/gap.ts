/** M07 知识缺口闭环：F-07.02—F-07.04 + API-S05/S06 */
import { apiRequest } from './client'
import type { GapConverted, GapRow, GapVerified, PageResult, SupplementBound, SupplementTask } from './types'

/** F-07.02 GET /api/knowledge-gaps */
export function listGaps(query: {
  status?: string | null
  dept_id?: number | null
  page?: number
  size?: number
}): Promise<PageResult<GapRow>> {
  return apiRequest<PageResult<GapRow>>('/knowledge-gaps', { query })
}

/** F-07.03 转建补充任务 —— client_action_id 幂等，重复点击只建一个任务 */
export function convertGap(gapId: number, clientActionId: string): Promise<GapConverted> {
  return apiRequest<GapConverted>(`/knowledge-gaps/${gapId}/convert`, {
    method: 'POST',
    body: { client_action_id: clientActionId },
    idempotent: true,
  })
}

/** F-07.04 回放验证 —— 回放失败保持 processing；仅上传完成不自动关闭 */
export function verifyGap(gapId: number): Promise<GapVerified> {
  return apiRequest<GapVerified>(`/knowledge-gaps/${gapId}/verify`, { method: 'POST' })
}

/** API-S05 GET /api/supplement-tasks */
export function listSupplementTasks(query: {
  gap_id?: number | null
  page?: number
  size?: number
}): Promise<PageResult<SupplementTask>> {
  return apiRequest<PageResult<SupplementTask>>('/supplement-tasks', { query })
}

/** API-S06 绑定补充文档 —— 禁止绑定不存在/已删除单元；target_version 由服务端从 DB 读取 */
export function bindSupplementSource(
  taskId: number,
  unitId: number,
  expectedRevision: number,
): Promise<SupplementBound> {
  return apiRequest<SupplementBound>(`/supplement-tasks/${taskId}/source`, {
    method: 'PUT',
    body: { unit_id: unitId, expected_revision: expectedRevision },
  })
}
