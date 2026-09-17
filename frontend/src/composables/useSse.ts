/**
 * H31 frontend.parse_sse（FUNCTION-MAP §4 / FRONTEND-SPEC §4.4）
 *
 * 契约逻辑：流式 UTF-8 缓存 → LF/CRLF 帧解析 → 多行 data 拼接 → seq 去重。
 * 硬性要求：
 * - **完整解帧后才产出事件**，调用方在"应用内容成功"之后才提交 last_seq（不得先存游标）；
 * - **坏帧不静默跳过**，标记 `problem='malformed'` 交给恢复流程（AC-10.02-02）；
 * - **缺序不跳过**，标记 `problem='gap'`，由调用方重新订阅；
 * - heartbeat 是注释帧，不持久化、不推进游标。
 */

import type { SseEvent, SseEventName } from '@/api/types'

const PERSISTENT_EVENTS: ReadonlySet<string> = new Set([
  'meta',
  'delta',
  'citations',
  'denied',
  'done',
  'error',
])

/** 找到最早的"空行"分帧位置（兼容 CRLF / LF / CR 三种行尾） */
function findFrameEnd(buffer: string): { index: number; length: number } | null {
  const candidates: Array<{ index: number; length: number }> = []
  const crlf = buffer.indexOf('\r\n\r\n')
  if (crlf >= 0) candidates.push({ index: crlf, length: 4 })
  const lf = buffer.indexOf('\n\n')
  if (lf >= 0) candidates.push({ index: lf, length: 2 })
  const cr = buffer.indexOf('\r\r')
  if (cr >= 0) candidates.push({ index: cr, length: 2 })
  if (candidates.length === 0) return null
  candidates.sort((a, b) => a.index - b.index)
  return candidates[0]!
}

interface RawFrame {
  id: string | null
  event: string | null
  data: string[] | null
}

function parseFrame(raw: string): RawFrame {
  const frame: RawFrame = { id: null, event: null, data: null }
  for (const line of raw.split(/\r\n|\n|\r/)) {
    if (line === '') continue
    // 注释帧（heartbeat）：不持久化、不推进游标
    if (line.startsWith(':')) continue
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)

    if (field === 'id') frame.id = value
    else if (field === 'event') frame.event = value
    else if (field === 'data') {
      if (frame.data === null) frame.data = []
      frame.data.push(value)
    }
    // retry 等字段本契约不使用
  }
  return frame
}

/**
 * 把一个 SSE 帧转换成 SseEvent；协议不合法时返回 `problem='malformed'` 的事件。
 * 返回 null 表示该帧不含持久事件（例如纯注释帧）。
 */
function toEvent(frame: RawFrame, lastSeq: number): SseEvent | null {
  const hasData = frame.data !== null && frame.data.length > 0
  if (frame.id === null && frame.event === null && !hasData) return null

  if (frame.id === null || !/^\d+$/.test(frame.id)) {
    return {
      seq: lastSeq,
      event: 'error',
      request_id: 0,
      payload: { code: 'BAD_FRAME', message: '事件缺少合法 seq' },
      problem: 'malformed',
      raw: JSON.stringify(frame),
    }
  }

  const seq = Number(frame.id)
  const name = frame.event || 'message'

  if (!PERSISTENT_EVENTS.has(name)) {
    return {
      seq,
      event: 'error',
      request_id: 0,
      payload: { code: 'UNKNOWN_EVENT', message: `未知事件类型 ${name}` },
      problem: 'malformed',
    }
  }

  // 多行 data 以 \n 拼接（SSE 规范）
  const rawData = (frame.data ?? []).join('\n')
  let payload: Record<string, unknown>
  try {
    payload = rawData ? (JSON.parse(rawData) as Record<string, unknown>) : {}
  } catch {
    return {
      seq,
      event: name as SseEventName,
      request_id: 0,
      payload: {},
      problem: 'malformed',
      raw: rawData,
    }
  }

  const requestId = typeof payload.request_id === 'number' ? payload.request_id : 0
  return { seq, event: name as SseEventName, request_id: requestId, payload }
}

/**
 * 把字节流解析为持久事件序列。
 * @param reader 浏览器 ReadableStreamDefaultReader<Uint8Array>
 * @param lastSeq 已应用游标（调用方持有）；只有 seq > lastSeq 的事件才是新内容
 */
export async function* parseSse(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  lastSeq: number,
): AsyncGenerator<SseEvent, void, void> {
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let cursor = lastSeq

  const drain = (): SseEvent[] => {
    const out: SseEvent[] = []
    for (;;) {
      const end = findFrameEnd(buffer)
      if (!end) break
      const raw = buffer.slice(0, end.index)
      buffer = buffer.slice(end.index + end.length)
      const evt = toEvent(parseFrame(raw), cursor)
      if (!evt) continue
      if (evt.seq <= cursor && !evt.problem) continue // 重复 seq：幂等跳过
      if (evt.problem === 'gap') {
        out.push(evt)
        continue
      }
      if (evt.seq > cursor + 1) {
        // 缺序：不跳过坏帧，也不假装连续 —— 交给调用方重新订阅
        out.push({ ...evt, problem: 'gap' })
        continue
      }
      cursor = evt.seq
      out.push(evt)
    }
    return out
  }

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    // stream:true 保证跨 chunk 的多字节字符不被截断（AC-10.02-01）
    buffer += decoder.decode(value, { stream: true })
    for (const evt of drain()) yield evt
  }

  buffer += decoder.decode()
  for (const evt of drain()) yield evt

  // 残帧（无结尾空行）不丢弃，作为坏帧上报
  if (buffer.trim() !== '') {
    yield {
      seq: cursor,
      event: 'error',
      request_id: 0,
      payload: { code: 'TRUNCATED_FRAME', message: '事件流在帧中途结束' },
      problem: 'malformed',
      raw: buffer,
    }
  }
}
