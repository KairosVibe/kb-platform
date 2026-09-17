/** M04 知识生命周期与四维权限：F-04.01—F-04.09 + API-S04 */
import { apiRequest } from './client'
import type {
  AclEntity,
  AclSaved,
  AclState,
  ChunkRow,
  IndexTaskState,
  KnowledgeUnit,
  PageResult,
  RetryResult,
} from './types'

/** F-04.01 GET /api/knowledge-units —— 管理台账最小元数据属于 kb:view 能力 */
export function listUnits(query: {
  q?: string
  category?: string | null
  enabled?: boolean | null
  page?: number
  size?: number
}): Promise<PageResult<KnowledgeUnit>> {
  return apiRequest<PageResult<KnowledgeUnit>>('/knowledge-units', { query })
}

/** F-04.02 */
export function updateMetadata(
  unitId: number,
  patch: { title: string; category: string; expected_revision: number },
): Promise<{ unit_id: number; revision: number }> {
  return apiRequest(`/knowledge-units/${unitId}`, { method: 'PATCH', body: patch })
}

/** F-04.03 正文切片（正文另需数据读权，非 kb:view） */
export function readChunks(
  unitId: number,
  query: { version?: number | null; page?: number; size?: number },
): Promise<PageResult<ChunkRow>> {
  return apiRequest<PageResult<ChunkRow>>(`/knowledge-units/${unitId}/chunks`, { query })
}

/** F-04.05 切片编辑/拆分/删除 —— action 与 text/split_offset 互斥使用 */
export function mutateChunks(
  unitId: number,
  payload: {
    chunk_id: number
    action: 'edit' | 'split' | 'delete' | string
    text?: string | null
    split_offset?: number | null
    expected_revision: number
  },
): Promise<{ task_id: number; target_version: number }> {
  return apiRequest(`/knowledge-units/${unitId}/chunk-mutations`, { method: 'POST', body: payload })
}

/** F-04.06 */
export function setEnabled(
  unitId: number,
  enabled: boolean,
  expectedRevision: number,
): Promise<{ unit_id: number; enabled: boolean; revision: number }> {
  return apiRequest(`/knowledge-units/${unitId}/enabled`, {
    method: 'PUT',
    body: { enabled, expected_revision: expectedRevision },
  })
}

/** F-04.07 —— 返回 deletion_id / cleanup_status；物理清理可重试 */
export function deleteUnit(
  unitId: number,
  expectedRevision: number,
): Promise<{ deletion_id: number; cleanup_status: string }> {
  return apiRequest(`/knowledge-units/${unitId}`, {
    method: 'DELETE',
    body: { expected_revision: expectedRevision },
  })
}

/** API-S04 GET /api/knowledge-units/{id}/acl —— 权限弹窗回填，不扩正文读权 */
export function readAcl(unitId: number): Promise<AclState> {
  return apiRequest<AclState>(`/knowledge-units/${unitId}/acl`)
}

/** F-04.08 PUT /api/knowledge-units/{id}/acl —— 权限变更即时生效，已发送内容无法撤回 */
export function updateAcl(
  unitId: number,
  payload: AclState & { expected_revision: number },
): Promise<AclSaved> {
  const body = {
    global: payload.global,
    depts: payload.depts,
    roles: payload.roles,
    users: payload.users,
    expected_revision: payload.expected_revision,
  }
  return apiRequest<AclSaved>(`/knowledge-units/${unitId}/acl`, { method: 'PUT', body })
}

/** F-04.09 GET /api/acl-entities —— 权限弹窗的实体选择器（kb:perm） */
export function listAclEntities(query: {
  kind: 'department' | 'role' | 'user'
  q?: string
  page?: number
  size?: number
}): Promise<PageResult<AclEntity>> {
  return apiRequest<PageResult<AclEntity>>('/acl-entities', { query })
}

/** F-03.03 GET /api/index-tasks/{id} —— 解析/索引进度（与传输进度分开，FUNCTION-MAP §2.4） */
export function getIndexTask(taskId: number): Promise<IndexTaskState> {
  return apiRequest<IndexTaskState>(`/index-tasks/${taskId}`)
}

/** F-03.05 POST /api/index-tasks/{id}/retry */
export function retryIndexTask(taskId: number, expectedRevision: number): Promise<RetryResult> {
  return apiRequest<RetryResult>(`/index-tasks/${taskId}/retry`, {
    method: 'POST',
    body: { expected_revision: expectedRevision },
  })
}
