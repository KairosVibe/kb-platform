/** M02 组织用户与功能权限：F-02.01—F-02.09 + API-S01/S02/S03 */
import { apiRequest } from './client'
import type {
  Department,
  DirectoryItem,
  PageResult,
  PermissionCode,
  RoleRow,
  UserRow,
} from './types'

/** F-02.01 GET /api/departments —— 部门树（API-CONTRACTS §1 分页规则的明确例外） */
export function listDepartments(): Promise<{ items: Department[] }> {
  return apiRequest<{ items: Department[] }>('/departments')
}

/** F-02.02 */
export function createDepartment(parentId: number | null, name: string): Promise<Department> {
  return apiRequest<Department>('/departments', { method: 'POST', body: { parent_id: parentId, name } })
}

/** F-02.03 —— 部门不能移动到自身后代（DESIGN_REVISION §7），前端预检 + 后端强制 */
export function updateDepartment(
  id: number,
  patch: { parent_id: number | null; name: string; expected_revision: number },
): Promise<{ id: number; revision: number }> {
  return apiRequest<{ id: number; revision: number }>(`/departments/${id}`, { method: 'PATCH', body: patch })
}

/** F-02.04 —— 引用存在时 409，提示先迁移成员或撤销引用 */
export function deleteDepartment(id: number, expectedRevision: number): Promise<{ deleted: boolean }> {
  return apiRequest<{ deleted: boolean }>(`/departments/${id}`, {
    method: 'DELETE',
    body: { expected_revision: expectedRevision },
  })
}

/** API-S01 GET /api/users（不返回密码哈希） */
export function listUsers(query: {
  q?: string
  page?: number
  size?: number
  enabled?: boolean
}): Promise<PageResult<UserRow>> {
  return apiRequest<PageResult<UserRow>>('/users', { query })
}

/** F-02.05 —— 密码创建时 12—72 UTF-8 字节（API-CONTRACTS §1） */
export function createUser(payload: {
  username: string
  password: string
  dept_id: number | null
  role_ids: number[]
}): Promise<{ id: number; username: string; revision: number }> {
  return apiRequest('/users', { method: 'POST', body: payload })
}

/** F-02.06 —— 停用账号对所有人不可检索，前端需显式提示该后果 */
export function updateUser(
  id: number,
  patch: { dept_id: number | null; role_ids: number[]; enabled: boolean; expected_revision: number },
): Promise<{ id: number; enabled: boolean; revision: number }> {
  return apiRequest(`/users/${id}`, { method: 'PATCH', body: patch })
}

/** API-S02 GET /api/roles */
export function listRoles(query: { q?: string; page?: number; size?: number }): Promise<PageResult<RoleRow>> {
  return apiRequest<PageResult<RoleRow>>('/roles', { query })
}

/** API-S03 GET /api/permission-codes —— 后端固定 14 码，前端按 module 组树 */
export function listPermissionCodes(): Promise<{ items: PermissionCode[] }> {
  return apiRequest<{ items: PermissionCode[] }>('/permission-codes')
}

/** F-02.07 PUT /api/roles —— id=null 为新建 */
export function saveRole(payload: {
  id: number | null
  name: string
  codes: string[]
  expected_revision: number | null
}): Promise<{ id: number; codes: string[]; revision: number }> {
  return apiRequest('/roles', { method: 'PUT', body: payload })
}

/** F-02.08 */
export function deleteRole(id: number, expectedRevision: number): Promise<{ deleted: boolean }> {
  return apiRequest<{ deleted: boolean }>(`/roles/${id}`, {
    method: 'DELETE',
    body: { expected_revision: expectedRevision },
  })
}

/** F-02.09 GET /api/directory —— 最小选择器数据 */
export function listDirectory(query: {
  kind: 'user' | 'role' | 'department'
  q?: string
  page?: number
  size?: number
}): Promise<PageResult<DirectoryItem>> {
  return apiRequest<PageResult<DirectoryItem>>('/directory', { query })
}
