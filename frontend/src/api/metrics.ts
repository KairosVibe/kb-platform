/** M08 审计与数据看板：F-08.02—F-08.04 */
import { apiRequest } from './client'
import type { AuditRow, DashboardCharts, DashboardSummary, PageResult } from './types'

export type DashRange = 'day' | 'week'

/**
 * F-08.02 指标摘要。
 * 零分母返回 0 并显示"无样本"；空样本为 null 显示"—"（PRD §1.3）。
 */
export function getSummary(query: { range: DashRange; anchor_date: string }): Promise<DashboardSummary> {
  return apiRequest<DashboardSummary>('/dashboard/summary', { query })
}

/** F-08.03 六类图表（top_n 1—50）；服务端已按 Asia/Shanghai 分桶，前端不二次聚合 */
export function getCharts(query: {
  range: DashRange
  anchor_date: string
  top_n?: number
}): Promise<DashboardCharts> {
  return apiRequest<DashboardCharts>('/dashboard/charts', { query })
}

/** F-08.04 审计查询（按分项权限脱敏） */
export function searchAudit(query: {
  request_id?: number | null
  action?: string | null
  page?: number
  size?: number
}): Promise<PageResult<AuditRow>> {
  return apiRequest<PageResult<AuditRow>>('/audit', { query })
}
