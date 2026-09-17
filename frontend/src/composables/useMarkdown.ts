/**
 * F-10.02 delivery.render_answer（FUNCTION-MAP §4 / FRONTEND-SPEC §4.4、§7.7）
 *
 * 处理逻辑：事件序号去重 → rAF 批次输出 → 禁原始 HTML 与危险协议 →
 * 文本代码高亮复制 → 引用定位。末批必须 flush。
 *
 * 安全（FRONTEND-SPEC §8 第 5 条）：markdown-it 关闭 html；输出再经 DOMPurify 净化；
 * 链接协议白名单 http/https/mailto，其余不渲染为可点击链接；代码块按文本渲染。
 */

import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js'
import type { CitationDetail, SseEvent } from '@/api/types'

const SAFE_LINK = /^(?:https?:|mailto:)/i

const md = new MarkdownIt({
  html: false, // 禁止不受控 HTML（DESIGN_REVISION §3）
  linkify: true,
  breaks: false,
  typographer: false,
})

md.renderer.rules.fence = (tokens, idx) => {
  const token = tokens[idx]!
  const info = token.info ? token.info.trim() : ''
  const lang = info.split(/\s+/)[0] || ''

  let highlighted: string
  try {
    highlighted =
      lang && hljs.getLanguage(lang)
        ? hljs.highlight(token.content, { language: lang, ignoreIllegals: true }).value
        : md.utils.escapeHtml(token.content)
  } catch {
    highlighted = md.utils.escapeHtml(token.content)
  }

  // 代码块按文本渲染 + 复制按钮；复制内容由 DOM textContent 取得，与原文一致
  const langLabel = lang ? `<span class="kb-code-lang">${md.utils.escapeHtml(lang)}</span>` : ''
  return (
    `<pre data-kb-code="1">${langLabel}` +
    `<button type="button" class="kb-code-copy" data-kb-copy="1">复制</button>` +
    `<code class="hljs">${highlighted}</code></pre>`
  )
}

md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  const token = tokens[idx]!
  token.attrSet('target', '_blank')
  token.attrSet('rel', 'noopener noreferrer')
  token.attrSet('class', 'kb-ext')
  return self.renderToken(tokens, idx, options)
}

/**
 * 链接协议白名单（DESIGN_REVISION §3）。
 * 不在白名单内的链接**整对开闭标签一起摘除**，只留纯文本，
 * 避免只去掉 link_open 而留下孤立的 </a> 形成畸形 HTML。
 */
md.core.ruler.push('kb_safe_links', (state) => {
  for (const token of state.tokens) {
    if (token.type !== 'inline' || !token.children) continue
    const kids = token.children
    const drop = new Set<number>()
    let openIdx = -1

    kids.forEach((child, j) => {
      if (child.type === 'link_open') {
        const hrefIndex = child.attrIndex('href')
        const href = hrefIndex >= 0 ? child.attrs?.[hrefIndex]?.[1] ?? '' : ''
        if (!SAFE_LINK.test(href.trim())) {
          drop.add(j)
          openIdx = j
        } else {
          openIdx = -1
        }
      } else if (child.type === 'link_close' && openIdx >= 0) {
        drop.add(j)
        openIdx = -1
      }
    })

    if (drop.size > 0) {
      token.children = kids.filter((_, idx) => !drop.has(idx))
    }
  }
  return true
})

// 表格外层包一个横向滚动容器（§7.7）
md.renderer.rules.table_open = () => '<div class="kb-table-wrap"><table>'
md.renderer.rules.table_close = () => '</table></div>'

/**
 * 净化配置（第二层防线）。
 *
 * **不要用 `ALLOWED_URI_REGEXP` 来做链接协议白名单。** DOMPurify 会对**每个**
 * 非惰性属性的值校验"必须匹配该正则"（`_isValidAttribute`），于是
 * `target="_blank"`、`rel="noopener noreferrer"` 这类与 URI 无关的合法值
 * 会被判成不安全而**整条属性被静默删除**，破坏 FRONTEND-SPEC §7.7 要求的外链属性。
 * 协议白名单改由下方 `uponSanitizeAttribute` 钩子**只作用于 href**。
 *
 * 同理不设 `USE_PROFILES`：它会重置允许标签/属性集合，与 `ADD_ATTR` 叠加时难以预期。
 */
const PURIFY_CONFIG = {
  ADD_ATTR: ['target', 'rel', 'data-kb-code', 'data-kb-copy'],
  FORBID_TAGS: ['style', 'iframe', 'object', 'embed', 'form', 'input', 'textarea', 'script'],
  FORBID_ATTR: ['style', 'onerror', 'onload', 'onclick', 'onmouseover'],
}

/**
 * 链接协议白名单（DESIGN_REVISION §3）在净化层的落点。
 * 只约束 `href` / `xlink:href`，不波及其他属性。
 */
DOMPurify.addHook('uponSanitizeAttribute', (_node, data) => {
  const name = data.attrName.toLowerCase()
  if (name !== 'href' && name !== 'xlink:href') return
  if (!SAFE_LINK.test(data.attrValue.trim())) data.keepAttr = false
})

/** 安全渲染：Markdown 文本 → 净化后的 HTML 片段 */
export function renderMarkdown(text: string): string {
  if (!text) return ''
  const raw = md.render(text)
  return DOMPurify.sanitize(raw, PURIFY_CONFIG)
}

export interface CitationRef {
  no: number
  unit_id: number
  version: number
  chunk_id: number
  title: string
}

export interface RenderAnswerState {
  text: string
  html: string
  last_seq: number
  terminal: boolean
  status: string | null
  resultType: string | null
  /** 部分无权时的固定提示（原文不可改写），未发生拒绝为 null */
  notice: string | null
  /** 全部无权终态：不调用生成模型，只显示固定提示 */
  refused: boolean
  citations: CitationRef[]
  usage: Record<string, unknown> | null
  durationMs: number | null
  firstTokenMs: number | null
  /** 协议异常（坏帧/缺序），调用方据此进入恢复或重新订阅 */
  problem: 'malformed' | 'gap' | null
}

/** 固定受限提示文案（FUNCTION-MAP §1，不得改写，不得含数量/ID） */
export const PARTIAL_RESTRICTED_NOTICE = '部分参考资料因权限受限无法展示'
export const ACCESS_RESTRICTED_NOTICE = '该问题超出当前权限范围，无法作答'

export function emptyRenderState(lastSeq = 0): RenderAnswerState {
  return {
    text: '',
    html: '',
    last_seq: lastSeq,
    terminal: false,
    status: null,
    resultType: null,
    notice: null,
    refused: false,
    citations: [],
    usage: null,
    durationMs: null,
    firstTokenMs: null,
    problem: null,
  }
}

/**
 * 纯处理函数：事件序号去重 → 按序应用文本/引用/状态。
 * **调用方必须在应用成功之后才提交 last_seq**（API-CONTRACTS §4）。
 */
export function renderAnswer(events: SseEvent[], lastSeq: number): RenderAnswerState {
  const state = emptyRenderState(lastSeq)

  const ordered = [...events].sort((a, b) => a.seq - b.seq)

  for (const evt of ordered) {
    if (evt.problem) {
      state.problem = evt.problem
      continue
    }
    if (evt.seq <= state.last_seq) continue // 重复 seq：幂等跳过
    state.last_seq = evt.seq

    switch (evt.event) {
      case 'meta':
        state.status = typeof evt.payload.status === 'string' ? evt.payload.status : state.status
        break
      case 'delta':
        if (typeof evt.payload.text === 'string') state.text += evt.payload.text
        break
      case 'citations': {
        const items = Array.isArray(evt.payload.items) ? evt.payload.items : []
        state.citations = items.map((raw) => {
          const it = raw as Record<string, unknown>
          return {
            no: Number(it.no ?? 0),
            unit_id: Number(it.unit_id ?? 0),
            version: Number(it.version ?? 0),
            chunk_id: Number(it.chunk_id ?? 0),
            title: typeof it.title === 'string' ? it.title : '受限知识',
          }
        })
        break
      }
      case 'denied':
        // 固定提示：只取 message，且服务端保证不含受限 ID/标题/数量
        state.notice =
          typeof evt.payload.message === 'string' && evt.payload.message
            ? evt.payload.message
            : ACCESS_RESTRICTED_NOTICE
        state.refused = true
        break
      case 'done':
        state.terminal = true
        state.status = typeof evt.payload.status === 'string' ? evt.payload.status : 'completed'
        state.resultType = typeof evt.payload.result_type === 'string' ? evt.payload.result_type : null
        state.usage = (evt.payload.usage as Record<string, unknown>) ?? null
        state.durationMs = typeof evt.payload.duration_ms === 'number' ? evt.payload.duration_ms : null
        state.firstTokenMs = typeof evt.payload.first_token_ms === 'number' ? evt.payload.first_token_ms : null
        if (state.resultType === 'access_restricted') {
          state.refused = true
          state.notice = state.notice ?? ACCESS_RESTRICTED_NOTICE
        } else if (state.resultType === 'answered' || state.resultType === 'faq_hit') {
          // 部分无权：追加固定提示（DESIGN_REVISION §2.2）
          if (evt.payload.partial_restricted === true) state.notice = PARTIAL_RESTRICTED_NOTICE
        }
        break
      case 'error':
        state.terminal = true
        if (typeof evt.payload.message === 'string') state.notice = evt.payload.message
        break
      default:
        break
    }
  }

  state.html = renderMarkdown(state.text)
  return state
}

/**
 * rAF 批次输出（§4.4 第 2 条）。`flush()` 保证末批不丢：
 * done/error 之前必须调用 flush，不能依赖下一帧。
 */
export interface RafFlusher {
  schedule(): void
  flush(): void
  cancel(): void
  readonly pending: boolean
}

export function createRafFlusher(apply: () => void): RafFlusher {
  let handle: number | null = null
  const raf = typeof requestAnimationFrame === 'function' ? requestAnimationFrame : (cb: () => void) => setTimeout(cb, 16) as unknown as number
  const caf = typeof cancelAnimationFrame === 'function' ? cancelAnimationFrame : clearTimeout

  return {
    schedule() {
      if (handle !== null) return
      handle = raf(() => {
        handle = null
        apply()
      })
    },
    flush() {
      if (handle !== null) {
        caf(handle as number)
        handle = null
      }
      apply()
    },
    cancel() {
      if (handle !== null) {
        caf(handle as number)
        handle = null
      }
    },
    get pending() {
      return handle !== null
    },
  }
}

/** 引用卡点击定位（H35）：失效显示不可用，不展示受限正文 */
export function citationTooltip(citation: CitationDetail | null): string {
  if (!citation) return '来源当前不可用'
  const where =
    citation.page_no !== null ? `第 ${citation.page_no} 页` : citation.offset >= 0 ? `偏移 ${citation.offset}` : '位置未知'
  return `${citation.title}（${where}）`
}
