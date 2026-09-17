/** M01 身份认证：F-01.01—F-01.04 */
import { apiRequest } from './client'
import type { LoginRequest, LogoutResult, MeResponse, TokenPair } from './types'

/** F-01.01 POST /api/auth/login —— 不前置 H02（API-CONTRACTS §2） */
export function login(payload: LoginRequest): Promise<TokenPair> {
  return apiRequest<TokenPair>('/auth/login', {
    method: 'POST',
    body: payload,
    skipAuth: true,
    noRefresh: true,
  })
}

/** F-01.03 POST /api/auth/logout —— 会话级撤销，注销后 access token 立即失效（依赖服务端 sid 校验） */
export function logout(refreshToken: string): Promise<LogoutResult> {
  return apiRequest<LogoutResult>('/auth/logout', {
    method: 'POST',
    body: { refresh_token: refreshToken },
  })
}

/** F-01.04 GET /api/auth/me —— 权限码唯一来源；401 清身份（FRONTEND-SPEC §3） */
export function getMe(): Promise<MeResponse> {
  return apiRequest<MeResponse>('/auth/me')
}
