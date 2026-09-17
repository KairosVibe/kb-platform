/**
 * F-10.02 `delivery.render_answer` 单元测试（FRONTEND-SPEC §4.4、§7.7、§8 第 5 条）
 *
 * 两部分：
 * 1. `renderAnswer` —— 事件去重、终态、固定受限文案、`last_seq` 语义（AC-10.02-02）；
 * 2. `renderMarkdown` —— Markdown 安全渲染与末批 flush。
 *
 * 安全断言用真实 DOM 检查（`querySelector`），不用字符串匹配：
 * 转义后的文本里仍会出现 `onerror=` 这样的**字面量**，字符串断言会给出假阳性。
 */

import { describe, expect, it, vi } from 'vitest'
import {
  ACCESS_RESTRICTED_NOTICE,
  PARTIAL_RESTRICTED_NOTICE,
  citationTooltip,
  createRafFlusher,
  renderAnswer,
  renderMarkdown,
} from '@/composables/useMarkdown'
import type { CitationDetail, SseEvent, SseEventName } from '@/api/types'

function evt(
  seq: number,
  event: SseEventName,
  payload: Record<string, unknown> = {},
  problem?: 'malformed' | 'gap',
): SseEvent {
  return { seq, event, request_id: 1, payload, ...(problem ? { problem } : {}) }
}

function toDom(html: string): HTMLDivElement {
  const host = document.createElement('div')
  host.innerHTML = html
  return host
}

describe('F-10.02 renderAnswer · 正文与去重', () => {
  it('按 seq 顺序拼接 delta 文本', () => {
    const state = renderAnswer([evt(1, 'delta', { text: '你' }), evt(2, 'delta', { text: '好' })], 0)

    expect(state.text).toBe('你好')
    expect(state.last_seq).toBe(2)
  })

  it('乱序到达的事件按 seq 重排后再应用', () => {
    const state = renderAnswer([evt(2, 'delta', { text: 'b' }), evt(1, 'delta', { text: 'a' })], 0)

    expect(state.text).toBe('ab')
  })

  it('重复 seq 幂等：不重复追加同一段内容', () => {
    const state = renderAnswer([evt(1, 'delta', { text: 'a' }), evt(1, 'delta', { text: 'a' })], 0)

    expect(state.text).toBe('a')
  })

  it('last_seq 基线决定重放范围：基线 0 得到全文，基线靠后只剩新内容', () => {
    const events = [evt(1, 'delta', { text: 'a' }), evt(2, 'delta', { text: 'b' }), evt(3, 'delta', { text: 'c' })]

    // 渲染基线必须固定为 0（累计事件不因重订阅清空），否则已渲染正文会被重建掉
    expect(renderAnswer(events, 0).text).toBe('abc')
    expect(renderAnswer(events, 2).text).toBe('c')
  })

  it('meta 提供状态；citations 解析为引用卡数据', () => {
    const state = renderAnswer(
      [
        evt(1, 'meta', { status: 'running' }),
        evt(2, 'citations', {
          items: [{ no: 1, unit_id: 10, version: 2, chunk_id: 3, title: '制度' }],
        }),
      ],
      0,
    )

    expect(state.status).toBe('running')
    expect(state.citations).toEqual([{ no: 1, unit_id: 10, version: 2, chunk_id: 3, title: '制度' }])
  })

  it('引用项缺 title 时回落到「受限知识」，不泄露也不留空', () => {
    const state = renderAnswer([evt(1, 'citations', { items: [{ no: 1, unit_id: 10, version: 2, chunk_id: 3 }] })], 0)

    expect(state.citations[0]!.title).toBe('受限知识')
  })

  it('协议异常事件只置 problem，不把异常事件当成已应用内容', () => {
    const state = renderAnswer([evt(3, 'error', {}, 'gap')], 1)

    expect(state.problem).toBe('gap')
    expect(state.text).toBe('')
    expect(state.html).toBe('')
    expect(state.last_seq).toBe(1) // 游标不因异常事件推进
  })
})

describe('F-10.02 renderAnswer · 终态与固定受限文案', () => {
  it('done 事件给出终态、结果类型与用量', () => {
    const state = renderAnswer(
      [
        evt(1, 'done', {
          status: 'completed',
          result_type: 'answered',
          usage: { prompt_tokens: 10 },
          duration_ms: 1200,
          first_token_ms: 300,
        }),
      ],
      0,
    )

    expect(state.terminal).toBe(true)
    expect(state.status).toBe('completed')
    expect(state.resultType).toBe('answered')
    expect(state.usage).toEqual({ prompt_tokens: 10 })
    expect(state.durationMs).toBe(1200)
    expect(state.firstTokenMs).toBe(300)
    expect(state.notice).toBeNull()
  })

  it('部分无权：answered + partial_restricted 追加固定提示（逐字，不含数量）', () => {
    const state = renderAnswer(
      [evt(1, 'delta', { text: '答案' }), evt(2, 'done', { result_type: 'answered', partial_restricted: true })],
      0,
    )

    expect(state.notice).toBe(PARTIAL_RESTRICTED_NOTICE)
    expect(state.refused).toBe(false)
    expect(/\d/.test(state.notice!)).toBe(false)
  })

  it('全部无权终态：拒绝作答并只显示固定提示', () => {
    const state = renderAnswer([evt(1, 'done', { result_type: 'access_restricted' })], 0)

    expect(state.refused).toBe(true)
    expect(state.notice).toBe(ACCESS_RESTRICTED_NOTICE)
    expect(/\d/.test(state.notice!)).toBe(false)
  })

  it('denied 事件取服务端固定 message，缺省时回落固定文案', () => {
    const withMessage = renderAnswer([evt(1, 'denied', { message: PARTIAL_RESTRICTED_NOTICE })], 0)
    const withoutMessage = renderAnswer([evt(1, 'denied', {})], 0)

    expect(withMessage.refused).toBe(true)
    expect(withMessage.notice).toBe(PARTIAL_RESTRICTED_NOTICE)
    expect(withoutMessage.notice).toBe(ACCESS_RESTRICTED_NOTICE)
  })

  it('error 事件进入终态并展示安全 message', () => {
    const state = renderAnswer([evt(1, 'error', { message: '模型服务暂不可用' })], 0)

    expect(state.terminal).toBe(true)
    expect(state.notice).toBe('模型服务暂不可用')
  })

  it('固定文案与契约逐字一致，不得改写', () => {
    expect(PARTIAL_RESTRICTED_NOTICE).toBe('部分参考资料因权限受限无法展示')
    expect(ACCESS_RESTRICTED_NOTICE).toBe('该问题超出当前权限范围，无法作答')
  })
})

describe('F-10.02 renderMarkdown · 安全渲染（§8 第 5 条）', () => {
  it('空输入返回空串', () => {
    expect(renderMarkdown('')).toBe('')
  })

  it('原始 HTML 不被解析为元素', () => {
    const host = toDom(renderMarkdown('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>'))

    expect(host.querySelector('script')).toBeNull()
    expect(host.querySelector('img')).toBeNull()
    expect(host.textContent).toContain('<script>alert(1)</script>')
  })

  it('安全链接带 target/rel 与外链标识', () => {
    const html = renderMarkdown('[示例](https://example.com/a)')
    const anchor = toDom(html).querySelector('a')

    expect(anchor).not.toBeNull()
    expect(anchor!.getAttribute('href')).toBe('https://example.com/a')
    expect(anchor!.getAttribute('target')).toBe('_blank')
    expect(anchor!.getAttribute('rel')).toBe('noopener noreferrer')
    expect(anchor!.className).toContain('kb-ext')
  })

  it('危险协议与白名单外协议不渲染为可点击链接', () => {
    for (const target of ['javascript:alert(1)', 'vbscript:msgbox(1)', 'data:text/html;base64,PHNjcmlwdD4=']) {
      const host = toDom(renderMarkdown(`[点我](${target})`))
      expect(host.querySelector('a')).toBeNull()
    }
  })

  it('开闭标签成对摘除：白名单外协议只留纯文本，不产生畸形 </a>', () => {
    const host = toDom(renderMarkdown('见 [附件](ftp://example.com/a) 说明'))

    expect(host.querySelector('a')).toBeNull()
    expect(host.textContent).toContain('附件')
    expect(host.innerHTML).not.toContain('</a>')
  })

  it('代码块按文本渲染，并带复制按钮与语言标签', () => {
    const html = renderMarkdown('```html\n<script>alert(1)</script>\n```')
    const host = toDom(html)

    expect(host.querySelector('script')).toBeNull()
    expect(host.querySelector('pre[data-kb-code]')).not.toBeNull()
    expect(host.querySelector('.kb-code-copy')?.textContent).toBe('复制')
    expect(host.querySelector('.kb-code-lang')?.textContent).toBe('html')
    expect(host.querySelector('code.hljs')?.textContent).toContain('<script>alert(1)</script>')
  })

  it('未标注语言的代码块也被转义', () => {
    const host = toDom(renderMarkdown('```\n<b>x</b>\n```'))

    expect(host.querySelector('b')).toBeNull()
    expect(host.querySelector('code')?.textContent).toContain('<b>x</b>')
  })

  it('表格被包进横向滚动容器', () => {
    const host = toDom(renderMarkdown('| a | b |\n|---|---|\n| 1 | 2 |'))

    expect(host.querySelector('.kb-table-wrap > table')).not.toBeNull()
  })
})

describe('F-10.02 末批 flush（§4.4 第 2 条）', () => {
  function installFakeRaf() {
    let queue: Array<{ id: number; cb: () => void }> = []
    let nextId = 1
    vi.stubGlobal('requestAnimationFrame', (cb: () => void) => {
      const id = nextId++
      queue.push({ id, cb })
      return id
    })
    vi.stubGlobal('cancelAnimationFrame', (id: number) => {
      queue = queue.filter((entry) => entry.id !== id)
    })
    return {
      runNext() {
        const next = queue.shift()
        next?.cb()
        return this.pending()
      },
      pending: () => queue.length,
    }
  }

  it('同一帧内的多次 schedule 只应用一次', () => {
    const raf = installFakeRaf()
    const apply = vi.fn()
    const flusher = createRafFlusher(apply)

    flusher.schedule()
    flusher.schedule()

    expect(apply).not.toHaveBeenCalled()
    expect(flusher.pending).toBe(true)
    expect(raf.pending()).toBe(1)

    raf.runNext()

    expect(apply).toHaveBeenCalledTimes(1)
    expect(flusher.pending).toBe(false)
  })

  it('flush() 在无待处理帧时立即应用（末批不丢）', () => {
    installFakeRaf()
    const apply = vi.fn()
    const flusher = createRafFlusher(apply)

    flusher.flush()

    expect(apply).toHaveBeenCalledTimes(1)
  })

  it('flush() 会取消已排队的帧，避免重复应用', () => {
    installFakeRaf()
    const apply = vi.fn()
    const flusher = createRafFlusher(apply)

    flusher.schedule()
    flusher.flush()

    expect(apply).toHaveBeenCalledTimes(1)
    expect(flusher.pending).toBe(false)
  })

  it('cancel() 后既不应用也不再挂起', () => {
    const raf = installFakeRaf()
    const apply = vi.fn()
    const flusher = createRafFlusher(apply)

    flusher.schedule()
    flusher.cancel()

    expect(flusher.pending).toBe(false)
    expect(raf.pending()).toBe(0)
    expect(apply).not.toHaveBeenCalled()
  })
})

describe('F-10.02 H35 citationTooltip', () => {
  const citation: CitationDetail = {
    no: 1,
    unit_id: 10,
    version: 1,
    chunk_id: 2,
    title: '报销制度',
    snippet: '…',
    page_no: 3,
    offset: 120,
  }

  it('来源失效时显示不可用，不展示受限正文', () => {
    expect(citationTooltip(null)).toBe('来源当前不可用')
  })

  it('优先展示页码，其次偏移，最后位置未知', () => {
    expect(citationTooltip(citation)).toBe('报销制度（第 3 页）')
    expect(citationTooltip({ ...citation, page_no: null })).toBe('报销制度（偏移 120）')
    expect(citationTooltip({ ...citation, page_no: null, offset: -1 })).toBe('报销制度（位置未知）')
  })
})
