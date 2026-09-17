/**
 * 传输层 DTO 类型，逐字段对照 docs/API-CONTRACTS.md §8 功能接口登记表与 §5 补充入口。
 * 约定：外层 request_id 是链路追踪 UUID；data.request_id 是问答业务整数 ID，两者不可混用。
 */

/** 统一响应包（API-CONTRACTS §1） */
export interface ApiEnvelope<T> {
  code: string
  message: string
  data: T
  /** 链路追踪 UUID，与业务 ID 同名但不同物 */
  request_id: string
}

export interface PageResult<T> {
  items: T[]
  total: number
}

/* ---------- M01 身份认证 ---------- */

export interface LoginRequest {
  username: string
  password: string
}

export interface TokenPair {
  access_token: string
  refresh_token: string
  expires_in: number
}

export interface LogoutResult {
  revoked: boolean
}

/** F-01.04 GET /api/auth/me —— 权限码的唯一来源 */
export interface MeResponse {
  user_id: number
  username: string
  dept_id: number | null
  role_ids: number[]
  permission_codes: string[]
  revision: number
}

/* ---------- M02 组织用户与功能权限 ---------- */

export interface Department {
  id: number
  parent_id: number | null
  name: string
  revision: number
}

export interface UserRow {
  id: number
  username: string
  dept_id: number | null
  role_ids: number[]
  enabled: boolean
  revision: number
}

export interface RoleRow {
  id: number
  name: string
  codes: string[]
  revision: number
}

/** API-S03 GET /api/permission-codes —— 由后端返回、前端组树 */
export interface PermissionCode {
  code: string
  label: string
  module: string
}

export interface DirectoryItem {
  id: number
  label: string
  enabled: boolean | null
}

/* ---------- M03 文档导入解析与任务 ---------- */

export interface UploadAccepted {
  unit_id: number
  task_id: number
  status: string
}

export interface BatchItemResult {
  client_file_id: string
  relative_path: string
  unit_id: number | null
  task_id: number | null
  error_code: string | null
}

export interface BatchAccepted {
  batch_id: number
  items: BatchItemResult[]
}

/** index_task.status / stage 是两层信息，不可合并（PRD §1.2 第 2 条） */
export interface IndexTaskState {
  status: 'queued' | 'running' | 'retry_wait' | 'succeeded' | 'failed' | 'superseded' | string
  stage: 'parsing' | 'embedding' | 'indexing' | string
  progress: number | null
  error_code: string | null
  attempts: number
  revision: number
}

export interface RetryResult {
  task_id: number
  status: string
}

/* ---------- M04 知识生命周期与四维权限 ---------- */

export interface KnowledgeUnit {
  id: number
  code: string
  title: string
  format: string
  category: string
  acl_tags: string[]
  updated_at: string
  enabled: boolean
  index_status: 'pending' | 'indexed' | 'stale' | string
  revision: number
}

export interface ChunkRow {
  chunk_id: number
  version: number
  seq: number
  text: string
  page_no: number | null
  offset: number
}

export interface AclState {
  global: boolean
  depts: number[]
  roles: number[]
  users: number[]
  revision: number
}

export interface AclSaved {
  acl_version: number
  revision: number
}

export interface AclEntity {
  id: number
  label: string
  parent_id: number | null
}

/* ---------- M05 会话检索与流式问答 ---------- */

export interface SessionRow {
  id: number
  title: string
  updated_at: string
}

export interface SessionCreated {
  session_id: number
  title: string
}

export interface SessionMutated {
  session_id: number
  deleted: boolean
}

export interface HistoryMessage {
  id: number
  role: string
  text: string | null
  restricted: boolean
}

export interface ChatAccepted {
  request_id: number
  status: string
}

/** H27 安全快照 */
export interface RequestSnapshot {
  status: string
  last_seq: number
  answer: string | null
  restricted: boolean
}

export interface CancelResult {
  request_id: number
  status: string
}

export interface CitationDetail {
  no: number
  unit_id: number
  version: number
  chunk_id: number
  title: string
  snippet: string
  page_no: number | null
  offset: number
}

/* ---------- M06 FAQ ---------- */

export interface SourceRef {
  unit_id: number
  version: number
  chunk_id: number | null
}

export type FaqStatus = 'candidate' | 'published' | 'rejected' | 'offline' | 'stale'

export interface FaqRow {
  id: number
  question: string
  answer: string
  status: FaqStatus | string
  frequency: number
  confidence: number
  source_refs: SourceRef[]
  hit_count: number
}

export interface FaqSaved {
  faq_id: number
  revision: number
  status: string
}

export interface MiningRun {
  run_id: number
  status: string
}

export interface MiningRunState {
  status: string
  consumed: number
  candidates: number
  failed: number
  error_code: string | null
}

/* ---------- M07 知识缺口 ---------- */

export interface GapRow {
  id: number
  question: string
  dept_id: number | null
  recent_frequency: number
  max_similarity: number | null
  suggested_category: string
  last_seen_at: string
  status: 'open' | 'processing' | 'closed' | string
}

export interface GapConverted {
  task_id: number
  gap_id: number
  status: string
}

export interface GapVerified {
  passed: boolean
  state: string
  reason: string | null
}

export interface SupplementTask {
  id: number
  gap_id: number
  status: string
  unit_id: number | null
  target_version: number | null
  revision: number
}

export interface SupplementBound {
  task_id: number
  unit_id: number
  target_version: number
  revision: number
}

/* ---------- M08 审计与看板 ---------- */

export interface DashboardSummary {
  pv: number
  uv: number
  faq_hit_rate: number
  coverage: number
  knowledge_count: number
  error_rate: number
  unknown_usage_count: number
}

/** get_charts 字段见 FUNCTION-MAP §1；服务端已按 Asia/Shanghai 分桶 */
export interface DashboardCharts {
  traffic: Array<{ date: string; pv: number; uv: number }>
  questions: Array<{ key: string; label: string; count: number }>
  knowledge_heat: Array<{ key: string; label: string; count: number }>
  usage: Array<{
    date: string
    prompt_tokens?: number
    completion_tokens?: number
    embedding_tokens?: number
    rerank_units?: number
    unknown_count?: number
    [k: string]: unknown
  }>
  latency: Array<{ result_type: string; lower_ms: number; upper_ms: number | null; count: number }>
  knowledge_counts: { total?: number; enabled?: number; indexed?: number; [k: string]: unknown }
}

export interface AuditRow {
  id: number
  actor_id: number
  action: string
  resource_id: number | null
  at: string
  status: string
  request_id: number | null
  before: unknown
  after: unknown
}

/* ---------- M09 模型配置 ---------- */

export interface ModelConfig {
  revision: number
  models: Record<string, unknown>
  thresholds: Record<string, number>
  limits: Record<string, number>
  key_configured: boolean
}

export interface ProbeResult {
  ok: boolean
  latency_ms: number
  error_code: string | null
}

export interface Readiness {
  ready: boolean
  checks: Record<string, boolean>
  version: string
}

/* ---------- SSE（API-CONTRACTS §4） ---------- */

export type SseEventName = 'meta' | 'delta' | 'citations' | 'denied' | 'done' | 'error'

export interface SseEvent {
  /** 持久事件单调序号；heartbeat 为注释帧，无 seq，不推进游标 */
  seq: number
  event: SseEventName
  request_id: number
  payload: Record<string, unknown>
  /**
   * 由前端标记的协议异常，**必须进入恢复流程而不是静默跳过**（AC-10.02-02）：
   * - `malformed`：坏帧（JSON 不可解析 / 缺字段）；
   * - `gap`：序号跳跃（缺序），应重新订阅而不是跳过。
   */
  problem?: 'malformed' | 'gap'
  /** 坏帧原文，仅用于本地诊断，不渲染 */
  raw?: string
}
