/**
 * H30 frontend.api_request（FUNCTION-MAP §4 / FRONTEND-SPEC §5.1）
 *
 * 契约要点：
 * 1. 附 Authorization Bearer（API-CONTRACTS §2）；
 * 2. 401 时**共用一次** refresh（并发 401 只触发一次），禁止无限刷新；
 * 3. 错误映射到业务 code；两层错误码（HTTP 层 / 任务层）不互相冒充；
 * 4. **只有带幂等键的写请求**才自动重试。
 *
 * 安全红线（FRONTEND-SPEC §8 第 1 条）：access/refresh token 只存内存，
 * 不写 localStorage / sessionStorage / URL，也不进日志。
 */

import type { ApiEnvelope } from './types'

export type HttpMethod = 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'

export interface RequestOptions {
  method?: HttpMethod
  query?: Record<string, unknown>
  body?: unknown
  /**
   * 该写请求的 body 中是否携带幂等键（client_request_id / client_upload_id /
   * client_batch_id / client_action_id）。仅当为 true 时网络类失败会自动重试。
   */
  idempotent?: boolean
  /** 跳过 Authorization（login / refresh 用） */
  skipAuth?: boolean
  /** 不参与 401 → refresh 流程（refresh 自身用，避免递归） */
  noRefresh?: boolean
  /** 返回原始 Response（SSE 用） */
  raw?: boolean
  signal?: AbortSignal
}

export class ApiError extends Error {
  readonly code: string
  readonly status: number
  readonly requestId: string
  readonly isHttpLayer: boolean

  constructor(code: string, message: string, status: number, requestId = '') {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.requestId = requestId
    // HTTP 层与任务层是两个通道（API-CONTRACTS §1）：有状态码的是 HTTP 层错误
    this.isHttpLayer = status > 0
  }

  /** 依据契约状态码判断是否值得重试（仅幂等请求） */
  get retryable(): boolean {
    return this.status === 0 || this.status === 429 || this.status >= 500
  }
}

/* ---------- 令牌持有（仅内存） ---------- */

interface TokenState {
  accessToken: string | null
  refreshToken: string | null
  /** 令牌到期时间戳（ms），仅用于被动续期提示，不作为授权依据 */
  expiresAt: number
}

const tokens: TokenState = {
  accessToken: null,
  refreshToken: null,
  expiresAt: 0,
}

type Listener = (authenticated: boolean) => void

const authListeners = new Set<Listener>()

export function onAuthStateChange(fn: Listener): () => void {
  authListeners.add(fn)
  return () => authListeners.delete(fn)
}

function emitAuthState(): void {
  const authed = Boolean(tokens.accessToken)
  for (const fn of authListeners) fn(authed)
}

export function setTokens(pair: { access_token: string; refresh_token: string; expires_in?: number }): void {
  tokens.accessToken = pair.access_token
  tokens.refreshToken = pair.refresh_token
  tokens.expiresAt = pair.expires_in ? Date.now() + pair.expires_in * 1000 : 0
  emitAuthState()
}

export function clearTokens(): void {
  tokens.accessToken = null
  tokens.refreshToken = null
  tokens.expiresAt = 0
  emitAuthState()
}

export function hasAccessToken(): boolean {
  return Boolean(tokens.accessToken)
}

/** 仅供上传（XMLHttpRequest 通道）与调试取用；不落任何持久存储 */
export function currentAccessToken(): string | null {
  return tokens.accessToken
}

export function currentRefreshToken(): string | null {
  return tokens.refreshToken
}

export function accessTokenExpiresAt(): number {
  return tokens.expiresAt
}

/* ---------- URL 组装 ---------- */

const API_PREFIX = '/api'

function buildQuery(query?: Record<string, unknown>): string {
  if (!query) return ''
  const usp = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    usp.append(key, String(value))
  }
  const s = usp.toString()
  return s ? `?${s}` : ''
}

export function apiUrl(path: string, query?: Record<string, unknown>): string {
  const p = path.startsWith('/') ? path : `/${path}`
  return `${API_PREFIX}${p}${buildQuery(query)}`
}

/* ---------- 401 → 单飞 refresh ---------- */

let refreshInFlight: Promise<boolean> | null = null

async function performRefresh(): Promise<boolean> {
  const rt = tokens.refreshToken
  if (!rt) return false

  try {
    const resp = await fetch(apiUrl('/auth/refresh'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt }),
    })
    if (!resp.ok) {
      clearTokens()
      return false
    }
    const env = (await resp.json()) as ApiEnvelope<{ access_token: string; refresh_token: string; expires_in: number }>
    if (env.code !== 'OK' || !env.data) {
      clearTokens()
      return false
    }
    setTokens(env.data)
    return true
  } catch {
    clearTokens()
    return false
  }
}

/** 共用一次 refresh：并发的 401 只触发一个刷新请求 */
function refreshOnce(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = performRefresh().finally(() => {
      refreshInFlight = null
    })
  }
  return refreshInFlight
}

/* ---------- 核心请求 ---------- */

function buildHeaders(skipAuth: boolean, body: unknown): Headers {
  const headers = new Headers()
  if (body !== undefined && !(body instanceof FormData)) {
    headers.set('Content-Type', 'application/json')
  }
  if (!skipAuth && tokens.accessToken) {
    headers.set('Authorization', `Bearer ${tokens.accessToken}`)
  }
  return headers
}

function serializeBody(body: unknown): BodyInit | undefined {
  if (body === undefined || body === null) return undefined
  if (body instanceof FormData) return body
  return JSON.stringify(body)
}

async function readEnvelope<T>(resp: Response): Promise<T> {
  let env: ApiEnvelope<T> | null = null
  try {
    env = (await resp.json()) as ApiEnvelope<T>
  } catch {
    env = null
  }

  if (resp.ok && env && env.code === 'OK') {
    return env.data
  }

  const code = env?.code || `HTTP_${resp.status}`
  const message = env?.message || defaultMessage(resp.status)
  throw new ApiError(code, message, resp.status, env?.request_id || '')
}

/** 契约状态码语义（API-CONTRACTS §1）——只显示安全文案，不展示原始异常 */
export function defaultMessage(status: number): string {
  switch (status) {
    case 0:
      return '网络不可用，请检查连接后重试'
    case 401:
      return '身份无效或已过期，请重新登录'
    case 403:
      return '当前账号没有该功能权限'
    case 404:
      return '对象不存在或无权访问'
    case 409:
      return '数据已被修改或存在冲突，请刷新后重试'
    case 410:
      return '事件游标已过期'
    case 413:
      return '请求内容超出大小限制'
    case 415:
      return '不支持的文件格式'
    case 422:
      return '请求参数不合法'
    case 429:
      return '操作过于频繁，请稍后重试'
    case 503:
      return '依赖服务或授权事实不可用'
    default:
      return '请求失败，请稍后重试'
  }
}

const MAX_RETRY = 1

/**
 * 统一的 API 请求入口。返回 data 内结构（不是外层响应包）。
 */
export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', query, body, idempotent = false, skipAuth = false, noRefresh = false, raw = false } = options

  const url = apiUrl(path, query)
  const init: RequestInit = {
    method,
    headers: buildHeaders(skipAuth, body),
    body: serializeBody(body),
    signal: options.signal,
  }

  let attempt = 0
  // 只有"带幂等键的写请求"才允许自动重试；GET 天然幂等，也可重试
  const canRetry = method === 'GET' || idempotent

  for (;;) {
    attempt += 1
    let resp: Response
    try {
      resp = await fetch(url, init)
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') throw err
      const netErr = new ApiError('NETWORK_UNAVAILABLE', defaultMessage(0), 0)
      if (canRetry && attempt <= MAX_RETRY) continue
      throw netErr
    }

    if (resp.status === 401 && !noRefresh && !skipAuth) {
      const refreshed = await refreshOnce()
      if (refreshed) {
        // 刷新成功后用新令牌重放一次原请求
        ;(init.headers as Headers).set('Authorization', `Bearer ${tokens.accessToken}`)
        if (attempt <= MAX_RETRY + 1) continue
      }
      clearTokens()
      throw new ApiError('TOKEN_INVALID', defaultMessage(401), 401)
    }

    if (raw) {
      if (!resp.ok) await readEnvelope<never>(resp)
      return resp as unknown as T
    }

    if (resp.status === 429 || resp.status >= 500) {
      if (canRetry && attempt <= MAX_RETRY) {
        // 服务端未给退避时长时的最小退避，避免立即打回
        await new Promise((r) => setTimeout(r, 300 * attempt))
        continue
      }
    }

    return await readEnvelope<T>(resp)
  }
}

/** SSE 专用：返回可读流，不解析响应包（SSE 不套 JSON 外壳，API-CONTRACTS §1） */
export async function openEventStream(
  path: string,
  query?: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<Response> {
  const url = apiUrl(path, query)
  const headers = buildHeaders(false, undefined)
  const resp = await fetch(url, { method: 'GET', headers, signal })

  if (resp.status === 401) {
    const refreshed = await refreshOnce()
    if (refreshed) {
      const retryHeaders = buildHeaders(false, undefined)
      const retry = await fetch(url, { method: 'GET', headers: retryHeaders, signal })
      if (!retry.ok || !retry.body) {
        throw new ApiError('STREAM_UNAVAILABLE', defaultMessage(retry.status), retry.status)
      }
      return retry
    }
    clearTokens()
    throw new ApiError('TOKEN_INVALID', defaultMessage(401), 401)
  }

  if (!resp.ok) {
    // 410 由调用方处理为"取安全快照"（FRONTEND-SPEC §4.4）
    await readEnvelope<never>(resp)
  }
  if (!resp.body) {
    throw new ApiError('STREAM_UNAVAILABLE', '服务未返回事件流', resp.status)
  }
  return resp
}
