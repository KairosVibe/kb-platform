<script setup lang="ts">
/**
 * 运营看板（M08，FRONTEND-SPEC §4.6）
 *
 * 口径红线：
 * - PV 只统计唯一被接受的问答请求（含 FAQ/拒答/后续失败；不含未认证与幂等重发）；UV 为这些请求的去重用户；
 * - FAQ 命中率与覆盖率分母都是 PV；**覆盖率 ≠ 纯检索召回率**，两者分开展示；
 * - 服务端已按 Asia/Shanghai 日/周分桶，**前端不二次按本机时区聚合**；
 * - 失败时显示错误态与重试，**不把上次缓存图冒充当前范围结果**；
 * - 空样本为 null 显示"—"，零分母显示 0 并标注"无样本"。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import type { EChartsOption } from 'echarts'
import * as metricsApi from '@/api/metrics'
import { ApiError } from '@/api/client'
import type { AuditRow, DashboardCharts, DashboardSummary } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import EmptyState from '@/components/EmptyState.vue'
import EChart from '@/components/EChart.vue'

const auth = useAuthStore()

const range = ref<metricsApi.DashRange>('day')
const anchorDate = ref<string>(todayIso())

const summary = ref<DashboardSummary | null>(null)
const charts = ref<DashboardCharts | null>(null)
const loading = ref(false)
const loadError = ref('')
/** 失败时不清空也不回落到旧数据，由本标记阻止渲染陈旧图表 */
const stale = ref(false)

const topN = ref(10)

const tab = ref<'charts' | 'audit'>('charts')
const auditRows = ref<AuditRow[]>([])
const auditTotal = ref(0)
const auditLoading = ref(false)
const auditRequestId = ref<number | null>(null)
const auditAction = ref('')

function todayIso(): string {
  const d = new Date()
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

function pct(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toFixed(2)}%`
}

function num(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return String(value)
}

const summaryCards = computed(() => {
  const s = summary.value
  if (!s) return []
  return [
    { label: '问答请求 PV', value: num(s.pv), hint: '唯一被接受的问答请求，不含未认证与幂等重发' },
    { label: '去重用户 UV', value: num(s.uv), hint: '上述请求涉及的去重用户数' },
    { label: 'FAQ 命中率', value: pct(s.faq_hit_rate), hint: '成功 FAQ 直出 / PV' },
    { label: '知识覆盖率', value: pct(s.coverage), hint: '(有效 FAQ + 有足够授权证据的 RAG 回答) / PV；不等于纯检索召回率' },
    { label: '知识总数', value: num(s.knowledge_count), hint: '未删除的知识单元数' },
    { label: '错误率', value: pct(s.error_rate), hint: '失败终态 / PV' },
    { label: '用量未知', value: num(s.unknown_usage_count), hint: '用量缺失的调用数；已知无调用记 0，缺失记 unknown' },
  ]
})

const trafficOption = computed<EChartsOption>(() => {
  const data = charts.value?.traffic ?? []
  return {
    tooltip: { trigger: 'axis' },
    legend: { bottom: 0 },
    grid: { left: 48, right: 16, top: 16, bottom: 44 },
    xAxis: { type: 'category', data: data.map((d) => d.date) },
    yAxis: { type: 'value' },
    series: [
      { name: 'PV', type: 'line', smooth: true, data: data.map((d) => d.pv) },
      { name: 'UV', type: 'line', smooth: true, data: data.map((d) => d.uv) },
    ],
  }
})

function barOption(rows: Array<{ label: string; count: number }>): EChartsOption {
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 120, right: 24, top: 16, bottom: 24 },
    xAxis: { type: 'value' },
    yAxis: { type: 'category', data: rows.map((r) => r.label) },
    series: [{ type: 'bar', data: rows.map((r) => r.count), barMaxWidth: 18 }],
  }
}

const usageOption = computed<EChartsOption>(() => {
  const data = charts.value?.usage ?? []
  return {
    tooltip: { trigger: 'axis' },
    legend: { bottom: 0 },
    grid: { left: 56, right: 16, top: 16, bottom: 44 },
    xAxis: { type: 'category', data: data.map((d) => String(d.date)) },
    yAxis: { type: 'value' },
    series: [
      { name: '生成 tokens', type: 'bar', stack: 'usage', data: data.map((d) => Number(d.prompt_tokens ?? 0) + Number(d.completion_tokens ?? 0)) },
      { name: 'embedding tokens', type: 'bar', stack: 'usage', data: data.map((d) => Number(d.embedding_tokens ?? 0)) },
      { name: 'rerank units', type: 'bar', stack: 'usage', data: data.map((d) => Number(d.rerank_units ?? 0)) },
      // unknown 在堆叠图中单独成段，且固定使用中性灰，不与已知值混色
      { name: '未知用量', type: 'bar', stack: 'usage', data: data.map((d) => Number(d.unknown_count ?? 0)) },
    ],
  }
})

const latencyOption = computed<EChartsOption>(() => {
  const data = charts.value?.latency ?? []
  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 48, right: 16, top: 16, bottom: 44 },
    xAxis: {
      type: 'category',
      data: data.map((d) => `${d.lower_ms}—${d.upper_ms === null ? '∞' : d.upper_ms} ms`),
      axisLabel: { rotate: 30 },
    },
    yAxis: { type: 'value' },
    series: [{ type: 'bar', data: data.map((d) => d.count), barMaxWidth: 24 }],
  }
})

const countsOption = computed<EChartsOption>(() => {
  const c = charts.value?.knowledge_counts ?? {}
  const rows = [
    { name: '未删除', value: Number(c.total ?? 0) },
    { name: '已启用', value: Number(c.enabled ?? 0) },
    { name: '已索引', value: Number(c.indexed ?? 0) },
  ]
  return {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0 },
    series: [{ type: 'pie', radius: ['45%', '70%'], data: rows, label: { formatter: '{b}: {c}' } }],
  }
})

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const [s, c] = await Promise.all([
      metricsApi.getSummary({ range: range.value, anchor_date: anchorDate.value }),
      metricsApi.getCharts({ range: range.value, anchor_date: anchorDate.value, top_n: topN.value }),
    ])
    summary.value = s
    charts.value = c
    stale.value = false
  } catch (err) {
    loadError.value = err instanceof ApiError ? err.message : '看板加载失败'
    stale.value = true
    // 不保留旧图冒充当前范围结果
    summary.value = null
    charts.value = null
  } finally {
    loading.value = false
  }
}

async function loadAudit(): Promise<void> {
  auditLoading.value = true
  try {
    const res = await metricsApi.searchAudit({
      request_id: auditRequestId.value,
      action: auditAction.value || null,
      page: 1,
      size: 20,
    })
    auditRows.value = res.items
    auditTotal.value = res.total
  } catch {
    auditRows.value = []
    auditTotal.value = 0
  } finally {
    auditLoading.value = false
  }
}

watch([range, anchorDate, topN], () => void load())
watch(tab, (t) => {
  if (t === 'audit' && auditRows.value.length === 0) void loadAudit()
})

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="kb-page">
    <header class="kb-page__head">
      <div>
        <h2 class="kb-page__title">运营看板</h2>
        <p class="kb-page__desc">
          时间按 Asia/Shanghai 自然日 / ISO 周分桶，由服务端聚合；本页不按本机时区二次计算。数字口径见每张卡的说明。
        </p>
      </div>
      <div class="kb-toolbar">
        <el-radio-group v-model="range">
          <el-radio-button label="day">日</el-radio-button>
          <el-radio-button label="week">周</el-radio-button>
        </el-radio-group>
        <el-date-picker v-model="anchorDate" type="date" value-format="YYYY-MM-DD" :clearable="false" style="width: 150px" />
        <el-select v-model="topN" style="width: 120px">
          <el-option :value="5" label="Top 5" />
          <el-option :value="10" label="Top 10" />
          <el-option :value="20" label="Top 20" />
          <el-option :value="50" label="Top 50" />
        </el-select>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>
    </header>

    <el-tabs v-model="tab" style="margin-top: var(--kb-sp-4)">
      <el-tab-pane label="指标与图表" name="charts">
        <el-skeleton v-if="loading && !summary" :rows="6" animated />

        <EmptyState
          v-else-if="loadError"
          variant="error"
          :title="loadError"
          description="当前范围的数据未取到，页面不会用上一次的缓存结果冒充本次范围。"
          @retry="load"
        />

        <template v-else-if="summary">
          <div class="kb-cards">
            <div v-for="card in summaryCards" :key="card.label" class="kb-card kb-panel">
              <p class="kb-card__label">{{ card.label }}</p>
              <p class="kb-card__value kb-num">{{ card.value }}</p>
              <p class="kb-card__hint">{{ card.hint }}</p>
            </div>
          </div>

          <div class="kb-charts">
            <section class="kb-panel">
              <div class="kb-panel__head"><span class="kb-panel__title">访问趋势（PV / UV）</span></div>
              <div class="kb-panel__body"><EChart :option="trafficOption" /></div>
            </section>

            <section class="kb-panel">
              <div class="kb-panel__head"><span class="kb-panel__title">高频问题</span></div>
              <div class="kb-panel__body">
                <EChart :option="barOption(charts?.questions ?? [])" />
                <p class="kb-card__hint">只显示你本人提问与脱敏主题计数；知识标题仅在你有读权时显示，否则合并为「受限知识」。</p>
              </div>
            </section>

            <section class="kb-panel">
              <div class="kb-panel__head"><span class="kb-panel__title">引用热度</span></div>
              <div class="kb-panel__body"><EChart :option="barOption(charts?.knowledge_heat ?? [])" /></div>
            </section>

            <section class="kb-panel">
              <div class="kb-panel__head"><span class="kb-panel__title">用量趋势</span></div>
              <div class="kb-panel__body">
                <EChart :option="usageOption" />
                <p class="kb-card__hint">未知用量单独成段计数，不伪造成 0。</p>
              </div>
            </section>

            <section class="kb-panel">
              <div class="kb-panel__head"><span class="kb-panel__title">延迟分布（首字 / 终态分桶）</span></div>
              <div class="kb-panel__body"><EChart :option="latencyOption" /></div>
            </section>

            <section class="kb-panel">
              <div class="kb-panel__head"><span class="kb-panel__title">知识总量</span></div>
              <div class="kb-panel__body"><EChart :option="countsOption" /></div>
            </section>
          </div>
        </template>
      </el-tab-pane>

      <el-tab-pane label="审计" name="audit">
        <div class="kb-panel">
          <div class="kb-panel__head">
            <div class="kb-toolbar">
              <el-input-number v-model="auditRequestId" :min="1" :controls="false" placeholder="请求 ID" style="width: 150px" />
              <el-input v-model="auditAction" placeholder="操作类型" clearable style="width: 160px" />
              <el-button type="primary" @click="loadAudit">查询</el-button>
            </div>
            <span class="kb-faint">按分项权限脱敏：原始审计正文仅本人且当前来源可读时展示。</span>
          </div>
          <div class="kb-panel__body">
            <el-skeleton v-if="auditLoading" :rows="5" animated />
            <EmptyState v-else-if="auditRows.length === 0" title="暂无审计记录" />
            <el-table v-else :data="auditRows" size="small">
              <el-table-column prop="id" label="ID" width="80" class-name="kb-num" />
              <el-table-column prop="action" label="操作" width="180" show-overflow-tooltip />
              <el-table-column prop="actor_id" label="操作者" width="100" class-name="kb-num" />
              <el-table-column prop="resource_id" label="资源" width="100" class-name="kb-num" />
              <el-table-column prop="at" label="时间" width="190" />
              <el-table-column prop="status" label="状态" width="110" />
              <el-table-column prop="request_id" label="业务请求" width="110" class-name="kb-num" />
            </el-table>
            <p v-if="auditTotal > 20" class="kb-faint" style="margin-top: var(--kb-sp-3)">
              共 {{ auditTotal }} 条，仅显示前 20 条。
            </p>
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
.kb-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: var(--kb-sp-3);
}
.kb-card {
  padding: var(--kb-sp-4);
}
.kb-card__label {
  margin: 0;
  font-size: var(--kb-fs-13);
  color: var(--kb-text-2);
}
.kb-card__value {
  margin: var(--kb-sp-2) 0 var(--kb-sp-1);
  font-size: var(--kb-fs-32);
  font-weight: 600;
  line-height: 1.1;
}
.kb-card__hint {
  margin: 0;
  font-size: var(--kb-fs-12);
  color: var(--kb-text-3);
  line-height: 1.6;
}
.kb-charts {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
  gap: var(--kb-sp-4);
  margin-top: var(--kb-sp-4);
}
</style>
