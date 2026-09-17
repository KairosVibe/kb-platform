/**
 * v-permission 按钮门禁（FRONTEND-SPEC §3）
 * 用法：v-permission="'kb:edit'" 或 v-permission="['faq:review','faq:publish']"
 *
 * 注意：显隐**不是安全边界**，所有受控操作后端必须复核（AC-10.01-01/02、U01）。
 */
import type { App, Directive } from 'vue'
import { useAuthStore } from '@/stores/auth'

type Value = string | string[] | undefined

const directive: Directive<HTMLElement, Value> = {
  mounted(el, binding) {
    apply(el, binding.value)
  },
  updated(el, binding) {
    apply(el, binding.value)
  },
}

function apply(el: HTMLElement, value: Value): void {
  if (!value) return
  const auth = useAuthStore()
  const codes = Array.isArray(value) ? value : [value]
  const allowed = codes.length === 0 ? true : auth.canAny(codes)
  el.style.display = allowed ? '' : 'none'
  el.setAttribute('aria-hidden', allowed ? 'false' : 'true')
}

export function registerPermissionDirective(app: App): void {
  app.directive('permission', directive)
}
