<script setup lang="ts">
/**
 * 问答工作台（M05 / PRD §1.1.4，FRONTEND-SPEC §4.4）
 *
 * 关键契约点：
 * - 发送前生成 client_request_id 并在重试中复用（幂等；同键异载荷 409）；
 * - SSE 用 fetch 携带 Authorization（不把 token 写查询参数）；
 * - **完整解帧并校验 seq 之后**才应用内容并提交 last_seq（不得先存游标）；
 * - 坏帧进恢复、缺序重新订阅，都不静默跳过；
 * - 410 取安全快照（H27），**不重新 POST 同问题**；
 * - 停止调用 F-05.07；已完成态不改为取消；断网 ≠ 取消；
 * - 组件销毁中止订阅但**不自动取消服务端请求**；
 * - 恢复状态与生成状态分开显示。
 */
import { computed, onBeforeUnmount, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Delete, Edit, Plus, Promotion, VideoPause } from '@element-plus/icons-vue'
import * as chatApi from '@/api/chat'
import { ApiError } from '@/api/client'
import type { HistoryMessage, SseEvent, SessionRow } from '@/api/types'
import { createRafFlusher, renderAnswer, PARTIAL_RESTRICTED_NOTICE, type CitationRef } from '@/composables/useMarkdown'
import { parseSse } from '@/composables/useSse'
import EmptyState from '@/components/EmptyState.vue'
import StatusTag from '@/components/StatusTag.vue'
import MarkdownView from '@/components/MarkdownView.vue'
import CitationCard from '@/components/CitationCard.vue'

interface UiMessage {
  key: string
  role: 'user' | 'assistant'
  text: string
  html: string
  citations: CitationRef[]
  /** 固定受限提示（部分或全部无权），不携带任何受限元数据 */
  notice: string | null
  refused: boolean
  status: string | null
  resultType: string | null
  durationMs: number | null
  firstTokenMs: number | null
  terminal: boolean
  requestId: number | null
  /** 恢复态与生成态分开（PRD §1.1.4） */
  restoring: boolean
  generating: boolean
  problem: 'malformed' | 'gap' | null
}

const MAX_RESUBSCRIBE = 5

const sessions = ref<SessionRow[]>([])
const sessionsLoading = ref(false)
const sessionsError = ref(false)
const activeSessionId = ref<number | null>(null)

const messages = ref<UiMessage[]>([])
const historyLoading = ref(false)

const question = ref('')
const sending = ref(false)
const suggestions = ref<string[]>([])
const activeRequestId = ref<number | null>(null)

const citationPanelOpen = ref(false)
const activeCitations = ref<{ requestId: number; items: CitationRef[] }>({ requestId: 0, items: [] })

let subscription: AbortController | null = null
let questionTimer: ReturnType<typeof setTimeout> | null = null

/** 当前请求累计的持久事件（重订阅时**不清空**，否则已渲染的正文会丢） */
let accumulatedEvents: SseEvent[] = []
/** 渲染基线：始终从 0 开始重放，才能得到完整正文 */
const renderBase = 0
let pendingMessage: UiMessage | null = null
let resubscribeCount = 0

const canSend = computed(() => question.value.trim().length > 0 && !sending.value)
const streaming = computed(() => activeRequestId.value !== null)

function newRequestId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `req-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

const flusher = createRafFlusher(() => applyPending())

function applyPending(): void {
  if (!pendingMessage || accumulatedEvents.length === 0) return
  const state = renderAnswer(accumulatedEvents, renderBase)
  const target = pendingMessage
  target.text = state.text
  target.html = state.html
  target.citations = state.citations
  target.notice = state.notice
  target.refused = state.refused
  target.status = state.status ?? target.status
  target.resultType = state.resultType
  target.durationMs = state.durationMs
  target.firstTokenMs = state.firstTokenMs
  target.terminal = state.terminal
  target.problem = state.problem
  if (state.terminal) {
    target.generating = false
    target.restoring = false
  }
  if (state.citations.length > 0 && target.requestId) {
    activeCitations.value = { requestId: target.requestId, items: state.citations }
  }
}

/* ---------- 会话 ---------- */

async function loadSessions(): Promise<void> {
  sessionsLoading.value = true
  sessionsError.value = false
  try {
    const page = await chatApi.listSessions({ page: 1, size: 50 })
    sessions.value = page.items
  } catch {
    sessionsError.value = true
  } finally {
    sessionsLoading.value = false
  }
}

function startNewSession(): void {
  abortSubscription()
  activeSessionId.value = null
  messages.value = []
}

async function ensureSession(): Promise<number> {
  if (activeSessionId.value !== null) return activeSessionId.value
  const created = await chatApi.createSession(null)
  activeSessionId.value = created.session_id
  sessions.value = [
    { id: created.session_id, title: created.title, updated_at: new Date().toISOString() },
    ...sessions.value,
  ]
  return created.session_id
}

async function selectSession(id: number): Promise<void> {
  if (activeSessionId.value === id) return
  abortSubscription()
  activeSessionId.value = id
  messages.value = []
  historyLoading.value = true
  try {
    const page = await chatApi.readHistory(id, { page: 1, size: 50 })
    messages.value = page.items.map(toUiMessage)
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '历史加载失败')
  } finally {
    historyLoading.value = false
  }
}

function toUiMessage(m: HistoryMessage): UiMessage {
  // restricted 的历史答案只能显示受限占位，禁止回填正文（DESIGN_REVISION §2.3）
  return {
    key: `h-${m.id}`,
    role: m.role === 'user' ? 'user' : 'assistant',
    text: m.restricted ? '' : m.text ?? '',
    html: '',
    citations: [],
    notice: m.restricted ? PARTIAL_RESTRICTED_NOTICE : null,
    refused: Boolean(m.restricted),
    status: null,
    resultType: null,
    durationMs: null,
    firstTokenMs: null,
    terminal: false,
    requestId: null,
    restoring: false,
    generating: false,
    problem: null,
  }
}

async function renameSession(item: SessionRow): Promise<void> {
  const next = window.prompt('会话名称', item.title)
  if (next === null) return
  try {
    await chatApi.mutateSession(item.id, { action: 'rename', title: next })
    item.title = next
    ElMessage.success('已重命名')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '重命名失败')
  }
}

async function removeSession(item: SessionRow): Promise<void> {
  if (!window.confirm(`删除会话「${item.title}」？该操作不可撤销。`)) return
  try {
    await chatApi.mutateSession(item.id, { action: 'delete' })
    sessions.value = sessions.value.filter((s) => s.id !== item.id)
    if (activeSessionId.value === item.id) {
      activeSessionId.value = null
      messages.value = []
    }
    ElMessage.success('已删除')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '删除失败')
  }
}

/* ---------- 发送与订阅 ---------- */

async function send(): Promise<void> {
  const text = question.value.trim()
  if (!text || sending.value) return

  sending.value = true
  try {
    const sessionId = await ensureSession()
    const clientRequestId = newRequestId()

    const accepted = await chatApi.acceptQuestion({
      session_id: sessionId,
      client_request_id: clientRequestId,
      question: text,
    })

    messages.value.push({
      key: `u-${Date.now()}`,
      role: 'user',
      text,
      html: '',
      citations: [],
      notice: null,
      refused: false,
      status: null,
      resultType: null,
      durationMs: null,
      firstTokenMs: null,
      terminal: false,
      requestId: null,
      restoring: false,
      generating: false,
      problem: null,
    })

    const assistant: UiMessage = {
      key: `a-${accepted.request_id}`,
      role: 'assistant',
      text: '',
      html: '',
      citations: [],
      notice: null,
      refused: false,
      status: accepted.status,
      resultType: null,
      durationMs: null,
      firstTokenMs: null,
      terminal: false,
      requestId: accepted.request_id,
      restoring: false,
      generating: true,
      problem: null,
    }
    messages.value.push(assistant)

    question.value = ''
    suggestions.value = []

    // 新请求：重置累计与重订阅预算
    accumulatedEvents = []
    resubscribeCount = 0
    await subscribe(accepted.request_id, assistant, 0)
  } catch (err) {
    if (err instanceof ApiError && err.code === 'SESSION_BUSY') {
      ElMessage.warning('本会话已有进行中的提问，请先等待结束或停止')
    } else {
      ElMessage.error(err instanceof ApiError ? err.message : '发送失败，请重试')
    }
  } finally {
    sending.value = false
  }
}

function abortSubscription(): void {
  subscription?.abort()
  subscription = null
  flusher.cancel()
  accumulatedEvents = []
  pendingMessage = null
  activeRequestId.value = null
  resubscribeCount = 0
}

async function subscribe(requestId: number, target: UiMessage, afterSeq: number): Promise<void> {
  subscription?.abort()
  const controller = new AbortController()
  subscription = controller
  activeRequestId.value = requestId
  pendingMessage = target

  try {
    const resp = await chatApi.openEvents(requestId, afterSeq, controller.signal)
    const reader = resp.body!.getReader()

    for await (const evt of parseSse(reader, afterSeq)) {
      if (controller.signal.aborted) return

      if (evt.problem === 'gap' || evt.problem === 'malformed') {
        target.problem = evt.problem
        target.restoring = true
        await reader.cancel().catch(() => undefined)

        if (resubscribeCount >= MAX_RESUBSCRIBE) {
          target.generating = false
          target.restoring = false
          ElMessage.warning('事件恢复次数已达上限，请重新打开该会话')
          return
        }
        resubscribeCount += 1

        if (evt.problem === 'gap') {
          // 缺序：以已应用游标重新订阅，不跳过
          await subscribe(requestId, target, currentCursor())
        } else {
          // 坏帧：取安全快照恢复，不静默丢弃
          await recoverFromSnapshot(requestId, target)
        }
        return
      }

      accumulatedEvents.push(evt)
      target.restoring = false
      flusher.schedule()

      if (evt.event === 'done' || evt.event === 'error') {
        // 末批必须 flush（AC-10.02-02）
        flusher.flush()
        target.generating = false
        target.terminal = true
        await reader.cancel().catch(() => undefined)
        return
      }
    }

    flusher.flush()
    target.generating = false
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') return

    if (err instanceof ApiError && err.status === 410) {
      // 游标过期：取安全快照，绝不重新 POST 同问题
      await recoverFromSnapshot(requestId, target)
      return
    }
    target.generating = false
    target.restoring = false
    ElMessage.error(err instanceof ApiError ? err.message : '事件流中断，可重新打开该请求')
  } finally {
    if (subscription === controller) {
      subscription = null
      activeRequestId.value = null
    }
  }
}

/** 已应用游标 = 当前累计事件中的最大 seq */
function currentCursor(): number {
  let max = 0
  for (const e of accumulatedEvents) if (e.seq > max) max = e.seq
  return max
}

async function recoverFromSnapshot(requestId: number, target: UiMessage): Promise<void> {
  try {
    const snapshot = await chatApi.getRequestSnapshot(requestId)
    target.status = snapshot.status
    target.refused = snapshot.restricted
    target.text = snapshot.restricted ? '' : snapshot.answer ?? ''
    target.html = ''
    target.notice = snapshot.restricted ? PARTIAL_RESTRICTED_NOTICE : null
    target.generating = false
    target.restoring = false
    target.terminal = true
  } catch {
    target.generating = false
    target.restoring = false
    ElMessage.warning('恢复失败，请稍后重新打开该会话')
  }
}

async function stop(): Promise<void> {
  const id = activeRequestId.value
  if (id === null) return
  try {
    await chatApi.cancelRequest(id)
    flusher.flush()
    const target = messages.value.find((m) => m.requestId === id)
    if (target) target.generating = false
    ElMessage.info('已请求停止')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '停止失败')
  }
}

/* ---------- 智能联想 ---------- */

function onInput(): void {
  if (questionTimer) clearTimeout(questionTimer)
  const prefix = question.value.trim()
  if (prefix.length < 2) {
    suggestions.value = []
    return
  }
  questionTimer = setTimeout(async () => {
    try {
      const res = await chatApi.suggest(prefix, 8)
      suggestions.value = res.items
    } catch {
      suggestions.value = []
    }
  }, 300)
}

function pickSuggestion(item: string): void {
  question.value = item
  suggestions.value = []
}

/* ---------- 引用 ---------- */

function openCitations(message: UiMessage): void {
  if (!message.requestId) return
  activeCitations.value = { requestId: message.requestId, items: message.citations }
  citationPanelOpen.value = true
}

function onCite(message: UiMessage, no: number): void {
  openCitations(message)
  void no
}

onBeforeUnmount(() => {
  // 中止订阅，但**不取消服务端请求**（FUNCTION-MAP §2.4）
  subscription?.abort()
  subscription = null
  flusher.cancel()
  if (questionTimer) clearTimeout(questionTimer)
})

void loadSessions()
</script>

<template>
  <div class="kb-chat">
    <!-- 会话侧栏 -->
    <aside class="kb-chat__side">
      <div class="kb-chat__side-head">
        <span class="kb-panel__title">我的会话</span>
        <el-button text size="small" :icon="Plus" @click="startNewSession">新建</el-button>
      </div>

      <el-skeleton v-if="sessionsLoading" :rows="4" animated style="padding: var(--kb-sp-3)" />
      <EmptyState v-else-if="sessionsError" variant="error" title="会话加载失败" @retry="loadSessions" />
      <EmptyState v-else-if="sessions.length === 0" title="还没有会话" description="发送第一个问题即自动创建。" />
      <ul v-else class="kb-chat__sessions">
        <li
          v-for="item in sessions"
          :key="item.id"
          class="kb-chat__session"
          :class="{ 'is-active': item.id === activeSessionId }"
          @click="selectSession(item.id)"
        >
          <span class="kb-truncate">{{ item.title || `会话 #${item.id}` }}</span>
          <span class="kb-chat__session-ops">
            <el-button text size="small" :icon="Edit" aria-label="重命名" @click.stop="renameSession(item)" />
            <el-button text size="small" :icon="Delete" aria-label="删除" @click.stop="removeSession(item)" />
          </span>
        </li>
      </ul>
    </aside>

    <!-- 对话区 -->
    <section class="kb-chat__main">
      <div class="kb-chat__stream">
        <el-skeleton v-if="historyLoading" :rows="6" animated style="padding: var(--kb-sp-6)" />
        <EmptyState
          v-else-if="messages.length === 0"
          title="开始一次授权问答"
          description="回答仅使用你有权读取的证据；若资料受权限限制，只会得到固定提示，不会透露受限内容。"
        />

        <article v-for="m in messages" :key="m.key" class="kb-chat__msg" :class="`is-${m.role}`">
          <div class="kb-chat__bubble">
            <template v-if="m.role === 'user'">
              <p class="kb-chat__user-text">{{ m.text }}</p>
            </template>

            <template v-else>
              <div class="kb-chat__meta">
                <StatusTag v-if="m.status" :value="m.status" />
                <StatusTag v-if="m.resultType" :value="m.resultType" />
                <!-- 恢复态与生成态分开显示（PRD §1.1.4） -->
                <span v-if="m.restoring" class="kb-chat__hint">正在恢复…</span>
                <span v-else-if="m.generating" class="kb-chat__hint">生成中…</span>
                <span v-if="m.problem === 'gap'" class="kb-chat__warn">事件序号不连续，已重新订阅</span>
                <span v-else-if="m.problem === 'malformed'" class="kb-chat__warn">收到异常帧，已进入恢复</span>
              </div>

              <MarkdownView v-if="m.html" :html="m.html" @cite="(no: number) => onCite(m, no)" />
              <p v-else-if="!m.notice && m.generating" class="kb-chat__hint">正在检索并生成…</p>

              <!-- 受限提示：固定文案，不含任何数量与 ID -->
              <div v-if="m.notice" class="kb-restricted" role="status">
                <span>{{ m.notice }}</span>
              </div>

              <div v-if="m.citations.length" class="kb-chat__cites">
                <el-button text size="small" @click="openCitations(m)">引用 {{ m.citations.length }} 条</el-button>
              </div>

              <p v-if="m.terminal && (m.durationMs !== null || m.firstTokenMs !== null)" class="kb-chat__stat kb-faint">
                <span v-if="m.durationMs !== null">耗时 {{ m.durationMs }} ms</span>
                <span v-if="m.firstTokenMs !== null"> · 首字 {{ m.firstTokenMs }} ms</span>
              </p>
            </template>
          </div>
        </article>
      </div>

      <footer class="kb-chat__composer">
        <ul v-if="suggestions.length" class="kb-chat__suggest">
          <li v-for="s in suggestions" :key="s" @click="pickSuggestion(s)">{{ s }}</li>
        </ul>

        <el-input
          v-model="question"
          type="textarea"
          :rows="3"
          maxlength="4000"
          show-word-limit
          resize="none"
          placeholder="输入问题（1—4000 字符）。回答只使用你有权读取的证据。"
          @input="onInput"
          @keydown.enter.exact.prevent="send"
        />

        <div class="kb-chat__actions">
          <el-button v-if="streaming" :icon="VideoPause" @click="stop">停止</el-button>
          <el-button type="primary" :icon="Promotion" :disabled="!canSend" :loading="sending" @click="send">
            发送
          </el-button>
        </div>
      </footer>
    </section>

    <!-- 来源与引用面板 -->
    <aside class="kb-chat__cited">
      <div class="kb-panel__head">
        <span class="kb-panel__title">来源与引用</span>
      </div>
      <div class="kb-panel__body">
        <EmptyState
          v-if="activeCitations.items.length === 0"
          title="暂无引用"
          description="回答产生引用后会显示在这里；点击引用卡会按当前权限重新校验来源。"
        />
        <div v-else class="kb-chat__cite-list">
          <CitationCard
            v-for="c in activeCitations.items"
            :key="c.no"
            :request-id="activeCitations.requestId"
            :no="c.no"
            :title="c.title"
          />
        </div>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.kb-chat {
  display: grid;
  grid-template-columns: 240px 1fr 320px;
  gap: var(--kb-sp-4);
  padding: var(--kb-sp-4);
  height: calc(100vh - var(--kb-topbar-h));
  box-sizing: border-box;
}
.kb-chat__side,
.kb-chat__cited {
  background: var(--kb-surface);
  border: 1px solid var(--kb-border);
  border-radius: var(--kb-r-3);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.kb-chat__side-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--kb-sp-3);
  border-bottom: 1px solid var(--kb-border);
}
.kb-chat__sessions {
  list-style: none;
  margin: 0;
  padding: var(--kb-sp-2);
  overflow: auto;
}
.kb-chat__session {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--kb-sp-1);
  height: 34px;
  padding: 0 var(--kb-sp-2);
  border-radius: var(--kb-r-2);
  font-size: var(--kb-fs-13);
  cursor: pointer;
}
.kb-chat__session:hover {
  background: var(--kb-surface-2);
}
.kb-chat__session.is-active {
  background: var(--kb-primary-soft);
  color: var(--kb-primary);
}
.kb-chat__session-ops {
  display: none;
}
.kb-chat__session:hover .kb-chat__session-ops {
  display: inline-flex;
}
.kb-chat__main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--kb-surface);
  border: 1px solid var(--kb-border);
  border-radius: var(--kb-r-3);
  overflow: hidden;
}
.kb-chat__stream {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: var(--kb-sp-4);
}
.kb-chat__msg {
  display: flex;
  margin-bottom: var(--kb-sp-4);
}
.kb-chat__msg.is-user {
  justify-content: flex-end;
}
.kb-chat__bubble {
  max-width: 92%;
  padding: var(--kb-sp-3) var(--kb-sp-4);
  border-radius: var(--kb-r-3);
}
.kb-chat__msg.is-user .kb-chat__bubble {
  background: var(--kb-primary-soft);
  max-width: 76%;
}
.kb-chat__msg.is-assistant .kb-chat__bubble {
  background: var(--kb-surface-2);
  border: 1px solid var(--kb-border);
  width: 100%;
}
.kb-chat__user-text {
  margin: 0;
  white-space: pre-wrap;
}
.kb-chat__meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--kb-sp-2);
  margin-bottom: var(--kb-sp-2);
}
.kb-chat__hint {
  font-size: var(--kb-fs-12);
  color: var(--kb-text-3);
}
.kb-chat__warn {
  font-size: var(--kb-fs-12);
  color: var(--kb-warning);
}
.kb-chat__stat {
  margin: var(--kb-sp-2) 0 0;
  font-size: var(--kb-fs-12);
}
.kb-chat__cites {
  margin-top: var(--kb-sp-2);
}
.kb-chat__cite-list {
  display: flex;
  flex-direction: column;
  gap: var(--kb-sp-2);
}
.kb-chat__composer {
  border-top: 1px solid var(--kb-border);
  padding: var(--kb-sp-3) var(--kb-sp-4);
  background: var(--kb-surface);
}
.kb-chat__actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--kb-sp-2);
  margin-top: var(--kb-sp-2);
}
.kb-chat__suggest {
  list-style: none;
  margin: 0 0 var(--kb-sp-2);
  padding: 0;
  max-height: 160px;
  overflow: auto;
  border: 1px solid var(--kb-border);
  border-radius: var(--kb-r-2);
}
.kb-chat__suggest li {
  padding: var(--kb-sp-2) var(--kb-sp-3);
  font-size: var(--kb-fs-13);
  cursor: pointer;
}
.kb-chat__suggest li:hover {
  background: var(--kb-surface-2);
}

/* §7.5 断点 */
@media (max-width: 1440px) {
  .kb-chat {
    grid-template-columns: 220px 1fr;
  }
  .kb-chat__cited {
    display: none;
  }
}
@media (max-width: 1024px) {
  .kb-chat {
    grid-template-columns: 1fr;
  }
  .kb-chat__side {
    display: none;
  }
}
</style>
