<script setup lang="ts">
/**
 * 沉淀运营页（M06 FAQ + M07 缺口，FRONTEND-SPEC §4.5）
 *
 * FAQ：
 * - 多来源必须**逐个列出**：多来源 FAQ 需每一个来源当前都可读（来源之间 AND）；
 * - 状态机 candidate→published|rejected；published→offline；来源失效→stale；
 *   offline/stale 重新审核前转 candidate；编辑已发布答案必须先下线；
 * - 缓存开关不改变审核状态。
 * 缺口：
 * - open→processing→closed；关联补充任务是独立实体；
 * - 转建幂等（client_action_id）；回放失败保持 processing；仅上传完成不自动关闭。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, Search } from '@element-plus/icons-vue'
import * as faqApi from '@/api/faq'
import * as gapApi from '@/api/gap'
import { ApiError } from '@/api/client'
import type { FaqRow, GapRow, SupplementTask } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useToast } from '@/composables/useToast'
import { newIdempotencyKey } from '@/composables/useUpload'
import EmptyState from '@/components/EmptyState.vue'
import StatusTag from '@/components/StatusTag.vue'

const auth = useAuthStore()
const toast = useToast()

const tab = ref<'faq' | 'gap'>('faq')
const canReview = computed(() => auth.can('faq:review'))
const canPublish = computed(() => auth.can('faq:publish'))
const canGap = computed(() => auth.can('gap:handle'))

/* ---------- FAQ ---------- */
const faqStatus = ref<'candidate' | 'published'>('candidate')
const faqs = ref<FaqRow[]>([])
const faqTotal = ref(0)
const faqLoading = ref(false)
const faqError = ref(false)
const faqQuery = ref('')
const faqPage = ref(1)

const miningRunning = ref(false)
const miningState = ref<string>('')

/* ---------- 缺口 ---------- */
const gaps = ref<GapRow[]>([])
const gapTotal = ref(0)
const gapLoading = ref(false)
const gapError = ref(false)
const gapStatus = ref<string>('open')
const gapPage = ref(1)

const tasks = ref<SupplementTask[]>([])
const taskLoading = ref(false)
const bindUnitId = ref<number | null>(null)

/* ---------- FAQ 行为 ---------- */

async function loadFaqs(): Promise<void> {
  faqLoading.value = true
  faqError.value = false
  try {
    const res = await faqApi.listFaqs({ status: faqStatus.value, q: faqQuery.value || undefined, page: faqPage.value, size: 20 })
    faqs.value = res.items
    faqTotal.value = res.total
  } catch {
    faqError.value = true
  } finally {
    faqLoading.value = false
  }
}

async function runMining(): Promise<void> {
  miningRunning.value = true
  miningState.value = '已登记，等待执行…'
  try {
    const run = await faqApi.runMining(newIdempotencyKey(), 'default')
    await pollMining(run.run_id, 0)
  } catch (err) {
    miningRunning.value = false
    miningState.value = ''
    toast.error(err, '触发挖掘失败')
  }
}

async function pollMining(runId: number, tries: number): Promise<void> {
  if (tries > 60) {
    miningRunning.value = false
    miningState.value = '轮询超时，请稍后在服务端查看运行状态'
    return
  }
  try {
    const state = await faqApi.getMiningRun(runId)
    miningState.value = `状态 ${state.status} · 已消费 ${state.consumed} · 候选 ${state.candidates} · 失败 ${state.failed}${
      state.error_code ? ` · ${state.error_code}` : ''
    }`
    if (['succeeded', 'failed', 'cancelled'].includes(state.status)) {
      miningRunning.value = false
      await loadFaqs()
      return
    }
  } catch {
    // 单次失败继续轮询
  }
  setTimeout(() => void pollMining(runId, tries + 1), 2000)
}

function editFaq(row: FaqRow): void {
  const question = window.prompt('规范化问题（1—4000 字符）', row.question)
  if (question === null) return
  const answer = window.prompt('参考答案（1—20000 字符）', row.answer)
  if (answer === null) return
  const sourceIds = row.source_refs.map((s) => s.unit_id)
  void (async () => {
    try {
      await faqApi.editCandidate(row.id, {
        question,
        answer,
        source_ids: sourceIds,
        expected_revision: row.revision,
      })
      ElMessage.success('候选已保存')
      await loadFaqs()
    } catch (err) {
      toast.error(err, '保存候选失败')
    }
  })()
}

async function publish(row: FaqRow): Promise<void> {
  if (!(await toast.confirm('发布后该 FAQ 可在授权问答中直出，且必须逐个来源都可读。确认发布？'))) return
  try {
    await faqApi.publishFaq(row.id, row.revision)
    ElMessage.success('已发布')
    await loadFaqs()
  } catch (err) {
    toast.error(err, '发布失败')
  }
}

async function changeStatus(row: FaqRow, action: 'reject' | 'offline' | 'resubmit'): Promise<void> {
  const reason = window.prompt('原因（1—1000 字符）', '')
  if (reason === null) return
  try {
    await faqApi.changeFaqStatus(row.id, { action, reason, expected_revision: row.revision })
    ElMessage.success('状态已更新')
    await loadFaqs()
  } catch (err) {
    toast.error(err, '状态流转失败')
  }
}

async function toggleCache(row: FaqRow): Promise<void> {
  try {
    const res = await faqApi.setCacheEnabled(row.id, true, row.revision)
    ElMessage.success(`缓存开关已更新，审核状态仍为 ${res.status}`)
    await loadFaqs()
  } catch (err) {
    toast.error(err, '缓存开关更新失败')
  }
}

/* ---------- 缺口行为 ---------- */

async function loadGaps(): Promise<void> {
  gapLoading.value = true
  gapError.value = false
  try {
    const res = await gapApi.listGaps({ status: gapStatus.value || null, page: gapPage.value, size: 20 })
    gaps.value = res.items
    gapTotal.value = res.total
  } catch {
    gapError.value = true
  } finally {
    gapLoading.value = false
  }
}

async function convert(row: GapRow): Promise<void> {
  if (!(await toast.confirm('将把该缺口转为补充资料任务；重复点击不会创建第二个任务。'))) return
  try {
    const res = await gapApi.convertGap(row.id, newIdempotencyKey())
    ElMessage.success(`已创建补充任务 #${res.task_id}`)
    await Promise.all([loadGaps(), loadTasks(row.id)])
  } catch (err) {
    toast.error(err, '转建失败')
  }
}

async function loadTasks(gapId?: number): Promise<void> {
  taskLoading.value = true
  try {
    const res = await gapApi.listSupplementTasks({ gap_id: gapId ?? null, page: 1, size: 20 })
    tasks.value = res.items
  } catch {
    tasks.value = []
  } finally {
    taskLoading.value = false
  }
}

async function bindSource(task: SupplementTask): Promise<void> {
  if (bindUnitId.value === null) {
    ElMessage.warning('请输入用于补档的知识单元 ID')
    return
  }
  try {
    const res = await gapApi.bindSupplementSource(task.id, bindUnitId.value, task.revision)
    ElMessage.success(`已绑定单元 #${res.unit_id}（目标版本 ${res.target_version}）`)
    await loadTasks(task.gap_id)
  } catch (err) {
    toast.error(err, '绑定失败：请确认该单元存在且未删除')
  }
}

async function verify(row: GapRow): Promise<void> {
  try {
    const res = await gapApi.verifyGap(row.id)
    if (res.passed) {
      ElMessage.success('回放验证通过，缺口已关闭')
    } else {
      ElMessage.warning(`回放未通过，仍保持处理中：${res.reason ?? res.state}`)
    }
    await loadGaps()
  } catch (err) {
    toast.error(err, '回放验证失败')
  }
}

function switchFaqStatus(status: 'candidate' | 'published'): void {
  faqStatus.value = status
  faqPage.value = 1
  void loadFaqs()
}

function switchTab(next: 'faq' | 'gap'): void {
  tab.value = next
  if (next === 'faq') void loadFaqs()
  else void Promise.all([loadGaps(), loadTasks()])
}

onMounted(() => {
  if (canReview.value || canPublish.value) void loadFaqs()
  else {
    tab.value = 'gap'
    void Promise.all([loadGaps(), loadTasks()])
  }
})
</script>

<template>
  <div class="kb-page">
    <header class="kb-page__head">
      <div>
        <h2 class="kb-page__title">沉淀运营</h2>
        <p class="kb-page__desc">
          FAQ 与知识缺口是两条独立闭环：FAQ 需逐个来源当前可读；缺口仅在正常检索但证据不足时产生，权限不足与服务异常不会生成缺口。
        </p>
      </div>
    </header>

    <el-tabs v-model="tab" style="margin-top: var(--kb-sp-4)" @tab-change="(n: string) => switchTab(n as 'faq' | 'gap')">
      <!-- ============ FAQ ============ -->
      <el-tab-pane v-if="canReview || canPublish" label="FAQ 沉淀" name="faq">
        <div class="kb-panel">
          <div class="kb-panel__head">
            <div class="kb-toolbar">
              <el-radio-group :model-value="faqStatus" @change="(v: string | number | boolean | undefined) => switchFaqStatus(v as 'candidate' | 'published')">
                <el-radio-button value="candidate">候选</el-radio-button>
                <el-radio-button value="published">已发布</el-radio-button>
              </el-radio-group>
              <el-input
                v-model="faqQuery"
                placeholder="搜索问题或答案"
                clearable
                style="width: 220px"
                :prefix-icon="Search"
                @keyup.enter="loadFaqs"
              />
              <el-button :icon="Refresh" @click="loadFaqs">刷新</el-button>
            </div>
            <el-button v-if="canReview" type="primary" :loading="miningRunning" @click="runMining">
              运行增量挖掘
            </el-button>
          </div>

          <div class="kb-panel__body">
            <el-alert
              v-if="miningState"
              type="info"
              :closable="false"
              show-icon
              :title="`挖掘运行：${miningState}`"
              style="margin-bottom: var(--kb-sp-3)"
            />

            <el-skeleton v-if="faqLoading && faqs.length === 0" :rows="5" animated />
            <EmptyState v-else-if="faqError" variant="error" title="FAQ 列表加载失败" @retry="loadFaqs" />
            <EmptyState
              v-else-if="faqs.length === 0"
              :title="faqStatus === 'candidate' ? '暂无候选 FAQ' : '暂无已发布 FAQ'"
              description="候选来自高频问题聚类；只有逐个来源都可读时才允许发布。"
            />

            <div v-else class="kb-faq-list">
              <article v-for="row in faqs" :key="row.id" class="kb-faq">
                <header class="kb-faq__head">
                  <StatusTag :value="row.status" />
                  <span class="kb-faint">频次 {{ row.frequency }} · 置信度 {{ row.confidence.toFixed(2) }} · 命中 {{ row.hit_count }}</span>
                </header>
                <p class="kb-faq__q">{{ row.question }}</p>
                <p class="kb-faq__a">{{ row.answer }}</p>

                <div class="kb-faq__sources">
                  <span class="kb-faint">来源（逐个校验，任一不可读则整条不返回）：</span>
                  <el-tag v-for="s in row.source_refs" :key="`${s.unit_id}-${s.version}`" size="small" style="margin-left: 4px">
                    单元 {{ s.unit_id }} · v{{ s.version }}{{ s.chunk_id !== null ? ` · 切片 ${s.chunk_id}` : '' }}
                  </el-tag>
                  <span v-if="row.source_refs.length === 0" class="kb-upload__error">无来源，不可发布</span>
                </div>

                <div class="kb-toolbar" style="margin-top: var(--kb-sp-2)">
                  <el-button v-if="canReview" text size="small" @click="editFaq(row)">编辑候选</el-button>
                  <el-button v-if="canReview && row.status === 'candidate'" text size="small" type="primary" @click="publish(row)">
                    发布
                  </el-button>
                  <el-button v-if="canReview && row.status === 'candidate'" text size="small" @click="changeStatus(row, 'reject')">
                    驳回
                  </el-button>
                  <el-button v-if="canPublish && row.status === 'published'" text size="small" @click="changeStatus(row, 'offline')">
                    下线
                  </el-button>
                  <el-button
                    v-if="canReview && ['rejected', 'offline', 'stale'].includes(String(row.status))"
                    text
                    size="small"
                    @click="changeStatus(row, 'resubmit')"
                  >
                    重新提交审核
                  </el-button>
                  <el-button v-if="canReview" text size="small" @click="toggleCache(row)">缓存开关</el-button>
                </div>

                <p v-if="row.status === 'stale'" class="kb-faq__stale">
                  来源正文已变更或删除，需重新审核后发布；缓存已失效。
                </p>
                <p v-else-if="row.status === 'published'" class="kb-faq__stale kb-faint">
                  已发布答案如需修改，请先下线，再重新审核发布。
                </p>
              </article>
            </div>

            <el-pagination
              v-if="faqTotal > 20"
              class="kb-pager"
              layout="total, prev, pager, next"
              :total="faqTotal"
              :current-page="faqPage"
              :page-size="20"
              @current-change="(p: number) => { faqPage = p; loadFaqs() }"
            />
          </div>
        </div>
      </el-tab-pane>

      <!-- ============ 缺口 ============ -->
      <el-tab-pane v-if="canGap" label="知识缺口" name="gap">
        <div class="kb-panel">
          <div class="kb-panel__head">
            <div class="kb-toolbar">
              <el-select v-model="gapStatus" style="width: 140px" @change="loadGaps">
                <el-option label="待处理" value="open" />
                <el-option label="处理中" value="processing" />
                <el-option label="已关闭" value="closed" />
                <el-option label="全部" value="" />
              </el-select>
              <el-button :icon="Refresh" @click="loadGaps">刷新</el-button>
            </div>
            <span class="kb-faint">仅正常检索但证据不足会入缺口；权限不足与服务异常不入缺口。</span>
          </div>

          <div class="kb-panel__body">
            <el-skeleton v-if="gapLoading && gaps.length === 0" :rows="5" animated />
            <EmptyState v-else-if="gapError" variant="error" title="缺口列表加载失败" @retry="loadGaps" />
            <EmptyState v-else-if="gaps.length === 0" title="暂无缺口" description="没有证据不足的正常问答记录。" />

            <el-table v-else :data="gaps">
              <el-table-column prop="question" label="问题" min-width="240" show-overflow-tooltip />
              <el-table-column label="部门" width="90">
                <template #default="{ row }">{{ row.dept_id ?? '—' }}</template>
              </el-table-column>
              <el-table-column prop="recent_frequency" label="近期频次" width="100" class-name="kb-num" />
              <el-table-column label="最高相似度" width="110">
                <template #default="{ row }">
                  <span v-if="row.max_similarity === null" class="kb-faint">—</span>
                  <span v-else class="kb-num">{{ row.max_similarity.toFixed(3) }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="suggested_category" label="建议分类" width="130" show-overflow-tooltip />
              <el-table-column prop="last_seen_at" label="最近出现" width="170" />
              <el-table-column label="状态" width="110">
                <template #default="{ row }"><StatusTag :value="row.status" /></template>
              </el-table-column>
              <el-table-column label="操作" width="190" fixed="right">
                <template #default="{ row }">
                  <el-button text size="small" @click="convert(row)">转建补档</el-button>
                  <el-button text size="small" :disabled="row.status === 'closed'" @click="verify(row)">回放验证</el-button>
                </template>
              </el-table-column>
            </el-table>

            <el-pagination
              v-if="gapTotal > 20"
              class="kb-pager"
              layout="total, prev, pager, next"
              :total="gapTotal"
              :current-page="gapPage"
              :page-size="20"
              @current-change="(p: number) => { gapPage = p; loadGaps() }"
            />
          </div>
        </div>

        <div class="kb-panel" style="margin-top: var(--kb-sp-4)">
          <div class="kb-panel__head">
            <span class="kb-panel__title">补充任务</span>
            <div class="kb-toolbar">
              <el-input-number v-model="bindUnitId" :min="1" :controls="false" placeholder="知识单元 ID" style="width: 150px" />
              <el-button :icon="Refresh" @click="loadTasks()">刷新</el-button>
            </div>
          </div>
          <div class="kb-panel__body">
            <el-skeleton v-if="taskLoading" :rows="3" animated />
            <EmptyState
              v-else-if="tasks.length === 0"
              title="暂无补充任务"
              description="在缺口列表点击「转建补档」后，任务会出现在这里。关联补充任务是独立实体，不代表知识已重建。"
            />
            <el-table v-else :data="tasks">
              <el-table-column prop="id" label="任务" width="90" class-name="kb-num" />
              <el-table-column prop="gap_id" label="缺口" width="90" class-name="kb-num" />
              <el-table-column label="状态" width="120">
                <template #default="{ row }"><StatusTag :value="row.status" /></template>
              </el-table-column>
              <el-table-column label="绑定单元" width="120">
                <template #default="{ row }">
                  <span v-if="row.unit_id === null" class="kb-faint">未绑定</span>
                  <span v-else class="kb-num">{{ row.unit_id }} · v{{ row.target_version }}</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="140" fixed="right">
                <template #default="{ row }">
                  <el-button text size="small" @click="bindSource(row)">绑定文档</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.kb-page__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--kb-sp-4);
}
.kb-pager {
  margin-top: var(--kb-sp-4);
  justify-content: flex-end;
}
.kb-faq-list {
  display: flex;
  flex-direction: column;
  gap: var(--kb-sp-3);
}
.kb-faq {
  padding: var(--kb-sp-3) var(--kb-sp-4);
  border: 1px solid var(--kb-border);
  border-radius: var(--kb-r-3);
  background: var(--kb-surface);
}
.kb-faq__head {
  display: flex;
  align-items: center;
  gap: var(--kb-sp-2);
  font-size: var(--kb-fs-12);
}
.kb-faq__q {
  margin: var(--kb-sp-2) 0 var(--kb-sp-1);
  font-weight: 500;
}
.kb-faq__a {
  margin: 0;
  font-size: var(--kb-fs-13);
  color: var(--kb-text-2);
  white-space: pre-wrap;
}
.kb-faq__sources {
  margin-top: var(--kb-sp-2);
  font-size: var(--kb-fs-12);
}
.kb-faq__stale {
  margin: var(--kb-sp-2) 0 0;
  padding: var(--kb-sp-2) var(--kb-sp-3);
  font-size: var(--kb-fs-12);
  border-radius: var(--kb-r-2);
  background: var(--kb-info-soft);
  color: var(--kb-info);
}
</style>
