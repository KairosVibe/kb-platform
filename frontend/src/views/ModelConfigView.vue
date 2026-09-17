<script setup lang="ts">
/**
 * 模型配置面板（M09，FRONTEND-SPEC §4.7）
 *
 * - PATCH 只提交变更字段；**patch 白名单之外字段一律 422**，故界面不提供额外可编辑项；
 * - 密钥只显示"已配置/未配置"，不回显、不可编辑、不写日志；
 * - embedding_model 变更若返回 409 REINDEX_REQUIRED，必须显式提示需重建索引（换向量空间）；
 * - 探测结果不缓存，展示实测延迟；就绪检查逐项列出。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import * as configApi from '@/api/config'
import { ApiError } from '@/api/client'
import type { ModelConfig, ProbeResult } from '@/api/types'
import { useToast } from '@/composables/useToast'
import EmptyState from '@/components/EmptyState.vue'
import StatusTag from '@/components/StatusTag.vue'

const toast = useToast()

const config = ref<ModelConfig | null>(null)
const loading = ref(false)
const loadError = ref('')
const saving = ref(false)
const probing = ref<string>('')
const probeResult = ref<Record<string, ProbeResult>>({})
const readiness = ref<{ ready: boolean; checks: Record<string, boolean>; version: string } | null>(null)
const readinessError = ref('')

/** 可编辑副本，用于对比出真正的变更字段 */
const form = ref({
  llm_model: '',
  embedding_model: '',
  rerank_model: '',
  faq_threshold: 0.5,
  cluster_threshold: 0.5,
  gap_threshold: 0.5,
  top_k: 5,
  min_frequency: 3,
  mining_interval_seconds: 3600,
})

const baseline = ref({ ...form.value })

const dirtyKeys = computed(() => {
  const keys: string[] = []
  for (const key of Object.keys(form.value) as Array<keyof typeof form.value>) {
    if (form.value[key] !== baseline.value[key]) keys.push(String(key))
  }
  return keys
})

function pick(obj: Record<string, unknown> | undefined, key: string, fallback: string | number): string | number {
  const v = obj?.[key]
  if (typeof v === 'string' || typeof v === 'number') return v
  return fallback
}

function hydrate(cfg: ModelConfig): void {
  form.value = {
    llm_model: String(pick(cfg.models, 'llm_model', '')),
    embedding_model: String(pick(cfg.models, 'embedding_model', '')),
    rerank_model: String(pick(cfg.models, 'rerank_model', '')),
    faq_threshold: Number(pick(cfg.thresholds, 'faq_threshold', 0.5)),
    cluster_threshold: Number(pick(cfg.thresholds, 'cluster_threshold', 0.5)),
    gap_threshold: Number(pick(cfg.thresholds, 'gap_threshold', 0.5)),
    top_k: Number(pick(cfg.limits, 'top_k', 5)),
    min_frequency: Number(pick(cfg.limits, 'min_frequency', 3)),
    mining_interval_seconds: Number(pick(cfg.limits, 'mining_interval_seconds', 3600)),
  }
  baseline.value = { ...form.value }
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const cfg = await configApi.readModelConfig()
    config.value = cfg
    hydrate(cfg)
  } catch (err) {
    loadError.value = err instanceof ApiError ? err.message : '配置加载失败'
  } finally {
    loading.value = false
  }
}

async function loadReadiness(): Promise<void> {
  readinessError.value = ''
  try {
    readiness.value = await configApi.readReadiness()
  } catch {
    readiness.value = null
    readinessError.value = '就绪检查不可用'
  }
}

async function save(): Promise<void> {
  if (!config.value) return
  if (dirtyKeys.value.length === 0) {
    ElMessage.info('没有需要保存的变更')
    return
  }

  // 只提交变更字段；embedding_model 变更风险单独确认
  const patch: Record<string, unknown> = {}
  for (const key of dirtyKeys.value) patch[key] = form.value[key as keyof typeof form.value]

  if (patch.embedding_model !== undefined) {
    const ok = await toast.confirm(
      '更换 embedding 模型视为切换向量空间，需要重建索引后才能生效，且禁止与旧向量混查。确认提交？',
      '确认更换向量空间',
    )
    if (!ok) return
  }

  saving.value = true
  try {
    const res = await configApi.updateModelConfig(patch, config.value.revision)
    ElMessage.success(`已保存，配置 revision ${res.revision}`)
    await load()
  } catch (err) {
    if (err instanceof ApiError && err.code === 'REINDEX_REQUIRED') {
      ElMessage.error('该 embedding 变更需要先重建索引（REINDEX_REQUIRED），配置未生效。')
      return
    }
    if (err instanceof ApiError && err.status === 409) {
      const refresh = await toast.confirmRevisionConflict()
      if (refresh) await load()
      return
    }
    toast.error(err, '保存失败')
  } finally {
    saving.value = false
  }
}

async function probe(provider: string): Promise<void> {
  probing.value = provider
  try {
    const res = await configApi.probeProvider(provider)
    probeResult.value = { ...probeResult.value, [provider]: res }
  } catch (err) {
    probeResult.value = {
      ...probeResult.value,
      [provider]: { ok: false, latency_ms: 0, error_code: err instanceof ApiError ? err.code : 'PROBE_FAILED' },
    }
  } finally {
    probing.value = ''
  }
}

onMounted(() => {
  void load()
  void loadReadiness()
})
</script>

<template>
  <div class="kb-page">
    <header class="kb-page__head">
      <div>
        <h2 class="kb-page__title">模型配置与运行控制</h2>
        <p class="kb-page__desc">
          只提交发生变更的字段；白名单之外的字段后端会返回 422。密钥由部署期管理，界面只显示是否已配置。
        </p>
      </div>
      <div class="kb-toolbar">
        <el-button :icon="Refresh" @click="load">刷新</el-button>
        <el-button type="primary" :loading="saving" :disabled="dirtyKeys.length === 0" @click="save">
          保存变更{{ dirtyKeys.length ? `（${dirtyKeys.length}）` : '' }}
        </el-button>
      </div>
    </header>

    <el-skeleton v-if="loading && !config" :rows="6" animated />
    <EmptyState v-else-if="loadError" variant="error" :title="loadError" @retry="load" />

    <template v-else-if="config">
      <div class="kb-cfg">
        <section class="kb-panel">
          <div class="kb-panel__head">
            <span class="kb-panel__title">模型</span>
            <span class="kb-faint">revision {{ config.revision }}</span>
          </div>
          <div class="kb-panel__body">
            <el-form label-width="120px" label-position="left">
              <el-form-item label="对话模型">
                <div class="kb-cfg__row">
                  <el-input v-model="form.llm_model" />
                  <el-button :loading="probing === 'llm'" @click="probe('llm')">探测</el-button>
                </div>
                <p v-if="probeResult.llm" class="kb-faint">
                  {{ probeResult.llm.ok ? `连通，${probeResult.llm.latency_ms} ms` : `失败：${probeResult.llm.error_code}` }}
                </p>
              </el-form-item>

              <el-form-item label="Embedding 模型">
                <div class="kb-cfg__row">
                  <el-input v-model="form.embedding_model" />
                  <el-button :loading="probing === 'embedding'" @click="probe('embedding')">探测</el-button>
                </div>
                <p class="kb-faint">
                  维度已冻结为 1024。更换该模型等于切换向量空间，必须重建索引；不存在"配额耗尽热降级到本地模型"的路径。
                </p>
                <p v-if="probeResult.embedding" class="kb-faint">
                  {{ probeResult.embedding.ok ? `连通，${probeResult.embedding.latency_ms} ms` : `失败：${probeResult.embedding.error_code}` }}
                </p>
              </el-form-item>

              <el-form-item label="重排模型">
                <div class="kb-cfg__row">
                  <el-input v-model="form.rerank_model" />
                  <el-button :loading="probing === 'rerank'" @click="probe('rerank')">探测</el-button>
                </div>
                <p v-if="probeResult.rerank" class="kb-faint">
                  {{ probeResult.rerank.ok ? `连通，${probeResult.rerank.latency_ms} ms` : `失败：${probeResult.rerank.error_code}` }}
                </p>
              </el-form-item>

              <el-form-item label="接口密钥">
                <StatusTag :value="config.key_configured ? 'succeeded' : 'pending'" />
                <span class="kb-faint" style="margin-left: var(--kb-sp-2)">
                  {{ config.key_configured ? '已配置（不回显、不可编辑）' : '未配置，需在部署期设置 secret_ref' }}
                </span>
              </el-form-item>
            </el-form>
          </div>
        </section>

        <section class="kb-panel">
          <div class="kb-panel__head"><span class="kb-panel__title">阈值</span></div>
          <div class="kb-panel__body">
            <p class="kb-faint" style="margin-top: 0">
              阈值绑定 score_type 与 model_version，不能把 RRF 分数当概率使用；下列为待校准的初值。
            </p>
            <el-form label-width="120px" label-position="left">
              <el-form-item label="FAQ 阈值">
                <el-slider v-model="form.faq_threshold" :min="0" :max="1" :step="0.01" show-input />
              </el-form-item>
              <el-form-item label="聚类阈值">
                <el-slider v-model="form.cluster_threshold" :min="0" :max="1" :step="0.01" show-input />
              </el-form-item>
              <el-form-item label="缺口阈值">
                <el-slider v-model="form.gap_threshold" :min="0" :max="1" :step="0.01" show-input />
              </el-form-item>
            </el-form>
          </div>
        </section>

        <section class="kb-panel">
          <div class="kb-panel__head"><span class="kb-panel__title">限额与频次</span></div>
          <div class="kb-panel__body">
            <el-form label-width="120px" label-position="left">
              <el-form-item label="答案条数 top_k">
                <el-input-number v-model="form.top_k" :min="1" :max="100" />
                <p class="kb-faint">
                  top_k 是 answer_top_k 的别名；vector_top_k / keyword_top_k / max_per_route / rrf_k 在受控配置中独立，
                  不能用这一个字段同时修改四值。
                </p>
              </el-form-item>
              <el-form-item label="最低频次">
                <el-input-number v-model="form.min_frequency" :min="1" />
              </el-form-item>
              <el-form-item label="挖掘间隔（秒）">
                <el-input-number v-model="form.mining_interval_seconds" :min="60" :step="60" />
              </el-form-item>
            </el-form>
          </div>
        </section>

        <section class="kb-panel">
          <div class="kb-panel__head">
            <span class="kb-panel__title">运行就绪检查</span>
            <el-button text size="small" @click="loadReadiness">重新检查</el-button>
          </div>
          <div class="kb-panel__body">
            <EmptyState v-if="readinessError" variant="error" :title="readinessError" @retry="loadReadiness" />
            <template v-else-if="readiness">
              <p>
                总体：
                <StatusTag :value="readiness.ready ? 'succeeded' : 'failed'" />
                <span class="kb-faint" style="margin-left: var(--kb-sp-2)">版本 {{ readiness.version }}</span>
              </p>
              <ul class="kb-checks">
                <li v-for="(ok, name) in readiness.checks" :key="name">
                  <span class="kb-mono">{{ name }}</span>
                  <StatusTag :value="ok ? 'succeeded' : 'failed'" />
                </li>
              </ul>
            </template>
          </div>
        </section>
      </div>
    </template>
  </div>
</template>

<style scoped>
.kb-page__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--kb-sp-4);
}
.kb-cfg {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
  gap: var(--kb-sp-4);
  margin-top: var(--kb-sp-4);
  align-items: start;
}
.kb-cfg__row {
  display: flex;
  gap: var(--kb-sp-2);
  width: 100%;
}
.kb-checks {
  list-style: none;
  margin: var(--kb-sp-3) 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--kb-sp-2);
}
.kb-checks li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: var(--kb-fs-13);
}
</style>
