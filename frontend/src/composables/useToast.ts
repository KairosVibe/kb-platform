/** 统一提示通道：只展示后端安全 message，不展示原始异常（FRONTEND-SPEC §5.2） */
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiError } from '@/api/client'

export function useToast() {
  function success(message: string): void {
    ElMessage.success(message)
  }

  function info(message: string): void {
    ElMessage.info(message)
  }

  function warn(message: string): void {
    ElMessage.warning(message)
  }

  /** 错误统一入口：ApiError 只取契约 message */
  function error(err: unknown, fallback = '操作失败，请稍后重试'): void {
    if (err instanceof ApiError) {
      ElMessage.error(err.message || fallback)
      return
    }
    if (err instanceof Error && err.message) {
      ElMessage.error(err.message)
      return
    }
    ElMessage.error(fallback)
  }

  /**
   * revision 冲突（409）：不自动覆盖，提示后由用户重新确认（PRD §1 第 5 条）。
   * @returns 用户是否选择"刷新并放弃本次修改"
   */
  async function confirmRevisionConflict(): Promise<boolean> {
    try {
      await ElMessageBox.confirm(
        '该数据已被其他人修改。请先刷新获取最新内容，再重新确认提交。',
        '版本冲突',
        { confirmButtonText: '刷新', cancelButtonText: '保留当前编辑', type: 'warning' },
      )
      return true
    } catch {
      return false
    }
  }

  async function confirm(message: string, title = '确认操作'): Promise<boolean> {
    try {
      await ElMessageBox.confirm(message, title, { type: 'warning' })
      return true
    } catch {
      return false
    }
  }

  return { success, info, warn, error, confirm, confirmRevisionConflict }
}
