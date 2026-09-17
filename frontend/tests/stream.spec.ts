/**
 * H31 `frontend.parse_sse` 单元测试（FRONTEND-SPEC §9「可测性要求」、AC-10.02-01/02）
 *
 * 覆盖：分帧（LF/CRLF）、**跨 UTF-8 多字节分块**、多行 data 拼接、
 * heartbeat 注释帧、重复 `seq` 幂等、**缺序不跳过**、**坏帧不静默**、末批残帧上报。
 *
 * 这些行为都是"错了也不会报错、只会悄悄少一段正文"的类型，所以必须有断言锁死。
 */

import { describe, expect, it } from 'vitest'
import { parseSse } from '@/composables/useSse'
import type { SseEvent } from '@/api/types'

const encoder = new TextEncoder()

function readerOf(chunks: Uint8Array[]): ReadableStreamDefaultReader<Uint8Array> {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk)
      controller.close()
    },
  })
  return stream.getReader()
}

async function collect(chunks: Uint8Array[], lastSeq = 0): Promise<SseEvent[]> {
  const out: SseEvent[] = []
  for await (const evt of parseSse(readerOf(chunks), lastSeq)) out.push(evt)
  return out
}

function encode(...parts: string[]): Uint8Array[] {
  return parts.map((part) => encoder.encode(part))
}

/** 一个完整帧（以空行结束），字段顺序与后端发送顺序一致 */
function frame(id: number, event: string, payload: unknown): string {
  return `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(payload)}\n\n`
}

function indexOfBytes(haystack: Uint8Array, needle: Uint8Array): number {
  outer: for (let i = 0; i + needle.length <= haystack.length; i += 1) {
    for (let j = 0; j < needle.length; j += 1) {
      if (haystack[i + j] !== needle[j]) continue outer
    }
    return i
  }
  return -1
}

describe('H31 parseSse · 分帧', () => {
  it('连续两个完整帧各自产出事件', async () => {
    const body = frame(1, 'delta', { text: '你' }) + frame(2, 'delta', { text: '好' })
    const events = await collect(encode(body))

    expect(events.map((e) => e.seq)).toEqual([1, 2])
    expect(events.map((e) => e.payload.text)).toEqual(['你', '好'])
    expect(events.every((e) => e.problem === undefined)).toBe(true)
  })

  it('帧边界被切在两个 chunk 之间也能完整解帧', async () => {
    const whole = frame(1, 'delta', { text: 'a' }) + frame(2, 'delta', { text: 'b' })
    const cut = whole.indexOf('\n\n') + 1 // 落在第一帧的结尾空行中间

    const events = await collect(encode(whole.slice(0, cut), whole.slice(cut)))

    expect(events.map((e) => e.seq)).toEqual([1, 2])
  })

  it('CRLF 行尾同样可解帧', async () => {
    const body = 'id: 1\r\nevent: delta\r\ndata: {"text":"a"}\r\n\r\n'
    const events = await collect(encode(body))

    expect(events).toHaveLength(1)
    expect(events[0]!.payload.text).toBe('a')
  })

  it('多行 data 以 \\n 拼接后再解析（JSON 空白处断行仍然合法）', async () => {
    const body = 'id: 1\nevent: delta\ndata: {"text":\ndata: "ab"}\n\n'
    const events = await collect(encode(body))

    expect(events).toHaveLength(1)
    expect(events[0]!.payload.text).toBe('ab')
  })

  it('在 JSON 字符串字面量内部断行会解析失败并标记 malformed（不猜测内容）', async () => {
    // 拼接后是 {"text":"a\nb"} —— 字符串里出现裸换行，属非法 JSON
    const body = 'id: 1\nevent: delta\ndata: {"text":"a\ndata: b"}\n\n'
    const events = await collect(encode(body))

    expect(events).toHaveLength(1)
    expect(events[0]!.problem).toBe('malformed')
    expect(events[0]!.raw).toBe('{"text":"a\nb"}')
  })

  it('空流不产出任何事件', async () => {
    expect(await collect([])).toEqual([])
  })
})

describe('H31 parseSse · 跨 UTF-8 多字节分块（AC-10.02-01）', () => {
  it('多字节字符被切成两半时不被解码成替换字符', async () => {
    const bytes = encoder.encode(frame(1, 'delta', { text: '你好，世界' }))
    const marker = encoder.encode('你')
    const at = indexOfBytes(bytes, marker)
    expect(at).toBeGreaterThan(0)

    // 在 3 字节字符的第 1 个字节之后切开 —— 解码器必须靠 stream:true 缓存半字符
    const events = await collect([bytes.slice(0, at + 1), bytes.slice(at + 1)])

    expect(events).toHaveLength(1)
    expect(events[0]!.payload.text).toBe('你好，世界')
    expect(JSON.stringify(events[0]!.payload)).not.toContain('\uFFFD')
  })

  it('每个字节单独分块（最极端切分）仍能还原文本', async () => {
    const bytes = encoder.encode(frame(1, 'delta', { text: '中文与 emoji 🚀' }))
    const chunks = Array.from(bytes, (b) => Uint8Array.of(b))

    const events = await collect(chunks)

    expect(events).toHaveLength(1)
    expect(events[0]!.payload.text).toBe('中文与 emoji 🚀')
  })
})

describe('H31 parseSse · 游标与幂等', () => {
  it('heartbeat 注释帧不产出事件、不推进游标', async () => {
    const body =
      ': ping\n\n' +
      frame(1, 'delta', { text: 'x' }) +
      ': heartbeat\n\n' +
      frame(2, 'delta', { text: 'y' })

    const events = await collect(encode(body))

    expect(events.map((e) => e.seq)).toEqual([1, 2])
  })

  it('重复 seq 幂等跳过，不重复产出内容', async () => {
    const body = frame(1, 'delta', { text: 'a' }) + frame(1, 'delta', { text: 'a' })
    const events = await collect(encode(body))

    expect(events).toHaveLength(1)
    expect(events[0]!.seq).toBe(1)
  })

  it('传入 lastSeq 时只产出更新的事件（重订阅只影响发送范围）', async () => {
    const body =
      frame(1, 'delta', { text: 'a' }) + frame(2, 'delta', { text: 'b' }) + frame(3, 'delta', { text: 'c' })

    const events = await collect(encode(body), 2)

    expect(events.map((e) => e.seq)).toEqual([3])
  })
})

describe('H31 parseSse · 异常不静默（AC-10.02-02）', () => {
  it('缺序不跳过：标记 problem=gap 交给调用方重新订阅', async () => {
    const body = frame(1, 'delta', { text: 'a' }) + frame(3, 'delta', { text: 'c' })
    const events = await collect(encode(body))

    expect(events.map((e) => e.seq)).toEqual([1, 3])
    expect(events[1]!.problem).toBe('gap')
  })

  it('以非 0 基线重订阅时缺序同样被识别', async () => {
    const body = frame(4, 'delta', { text: 'd' })
    const events = await collect(encode(body), 2) // 期望下一个是 3

    expect(events).toHaveLength(1)
    expect(events[0]!.problem).toBe('gap')
  })

  it('data 不是合法 JSON 的坏帧标记 malformed 并保留原文', async () => {
    const body = 'id: 1\nevent: delta\ndata: {oops\n\n'
    const events = await collect(encode(body))

    expect(events).toHaveLength(1)
    expect(events[0]!.problem).toBe('malformed')
    expect(events[0]!.raw).toBe('{oops')
  })

  it('缺少合法 seq 的事件标记 malformed（fail-closed，不假装连续）', async () => {
    const body = 'event: delta\ndata: {"text":"a"}\n\n'
    const events = await collect(encode(body))

    expect(events).toHaveLength(1)
    expect(events[0]!.problem).toBe('malformed')
    expect(events[0]!.payload.code).toBe('BAD_FRAME')
  })

  it('白名单之外的事件类型标记 malformed', async () => {
    const events = await collect(encode(frame(1, 'progress', { text: 'a' })))

    expect(events).toHaveLength(1)
    expect(events[0]!.problem).toBe('malformed')
    expect(events[0]!.payload.code).toBe('UNKNOWN_EVENT')
  })

  it('末批没有结尾空行的残帧不丢弃，作为 TRUNCATED_FRAME 上报', async () => {
    const body = frame(1, 'delta', { text: 'a' }) + 'id: 2\nevent: delta\ndata: {"text":"b"}'
    const events = await collect(encode(body))

    expect(events).toHaveLength(2)
    expect(events[1]!.problem).toBe('malformed')
    expect(events[1]!.payload.code).toBe('TRUNCATED_FRAME')
  })
})
