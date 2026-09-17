<script setup lang="ts">
/**
 * 知识中心（M03 / M04，FRONTEND-SPEC §4.3）
 *
 * - 台账列表的最小元数据属于 kb:view；正文/切片另需数据读权，后端独立判定；
 * - **列表返回后保留筛选和页码**：筛选条件同步到 URL query；
 * - 索引状态是 pending/indexed/stale，与 enabled、is_deleted 相互独立；
 * - 四维权限弹窗（H33）与导入抽屉（H32）为独立组件。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Key, Refresh, Search, Upload } from '@element-plus/icons-vue'
import * as knowledgeApi from '@/api/knowledge'
import { ApiError } from '@/api/client'
import type { ChunkRow, KnowledgeUnit } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useToast } from '@/composables/useToast'
import EmptyState from '@/components/EmptyState.vue'
import StatusTag from '@/components/StatusTag.vue'
import UploadDrawer from '@/components/UploadDrawer.vue'
import AclDialog from '@/components/AclDialog.vue'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const toast = useToast()

const items = ref<KnowledgeUnit[]>([])
const total = ref(0)
const loading = ref(false)
const loadError = ref(false)

const q = ref(typeof route.query.q === 'string' ? route.query.q : '')
const category = ref(typeof route.query.category === 'string' ? route.query.category : '')
const enabledFilter = ref<'all' | 'on' | 'off'>(
  route.query.enabled === 'true' ? 'on' : route.query.enabled === 'false' ? 'off' : 'all',
)
const page = ref(Number(route.query.page ?? 1) || 1)
const size = ref(Number(route.query.size ?? 20) || 20)

const uploadVisible = ref(false)
const aclVisible = ref(false)
const aclTarget = ref<KnowledgeUnit | null>(null)

const detailVisible = ref(false)
const detailTarget = ref<KnowledgeUnit | null>(null)
const chunks = ref<ChunkRow[]>([])
const chunksTotal = ref(0)
const chunksLoading = ref(false)
const chunksError = ref('')
const chunksPage = ref(1)

const metaTitle = ref('')
const metaCategory = ref('')

const canUpload = computed(() => auth.can('kb:upload'))
const canEdit = computed(() => auth.can('kb:edit'))
const canDelete = computed(() => auth.can('kb:delete'))
const canPerm = computed(() => auth.can('kb:perm'))

function syncQuery(): void {
  void router.replace({
    query: {
      ...(q.value ? { q: q.value } : {}),
      ...(category.value ? { category: category.value } : {}),
      ...(enabledFilter.value !== 'all' ? { enabled: String(enabledFilter.value === 'on') } : {}),
      ...(page.value !== 1 ? { page: String(page.value) } : {}),
      ...(size.value !== 20 ? { size: String(size.value) } : {}),
    },
  })
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = false
  try {
    const res = await knowledgeApi.listUnits({
      q: q.value || undefined,
      category: category.value || null,
      enabled: enabledFilter.value === 'all' ? null : enabledFilter.value === 'on',
      page: page.value,
      size: size.value,
    })
    items.value = res.items
    total.value = res.total
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}

function search(): void {
  page.value = 1
  syncQuery()
  void load()
}

function changePage(next: number): void {
  page.value = next
  syncQuery()
  void load()
}

/* ---------- 详情与切片 ---------- */

async function openDetail(unit: KnowledgeUnit): Promise<void> {
  detailTarget.value = unit
  metaTitle.value = unit.title
  metaCategory.value = unit.category
  detailVisible.value = true
  chunksPage.value = 1
  await loadChunks()
}

async function loadChunks(): Promise<void> {
  if (!detailTarget.value) return
  chunksLoading.value = true
  chunksError.value = ''
  try {
    const res = await knowledgeApi.readChunks(detailTarget.value.id, { page: chunksPage.value, size: 20 })
    chunks.value = res.items
    chunksTotal.value = res.total
  } catch (err) {
    // 无正文读权时后端返回统一 404，不披露是否存在
    chunksError.value = err instanceof ApiError ? err.message : '正文加载失败'
    chunks.value = []
  } finally {
    chunksLoading.value = false
  }
}

async function saveMetadata(): Promise<void> {
  if (!detailTarget.value) return
  try {
    const res = await knowledgeApi.updateMetadata(detailTarget.value.id, {
      title: metaTitle.value,
      category: metaCategory.value,
      expected_revision: detailTarget.value.revision,
    })
    detailTarget.value.title = metaTitle.value
    detailTarget.value.category = metaCategory.value
    detailTarget.value.revision = res.revision
    const row = items.value.find((i) => i.id === detailTarget.value!.id)
    if (row) {
      row.title = metaTitle.value
      row.category = metaCategory.value
      row.revision = res.revision
    }
    ElMessage.success('已保存')
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      const refresh = await toast.confirmRevisionConflict()
      if (refresh) await load()
      return
    }
    toast.error(err, '保存失败')
  }
}

async function toggleEnabled(unit: KnowledgeUnit): Promise<void> {
  const next = !unit.enabled
  if (!next && !(await toast.confirm('停用后该知识对所有人（含创建者与系统管理员）都不可检索，确认停用？'))) return
  try {
    const res = await knowledgeApi.setEnabled(unit.id, next, unit.revision)
    unit.enabled = res.enabled
    unit.revision = res.revision
    ElMessage.success(next ? '已启用' : '已停用')
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      const refresh = await toast.confirmRevisionConflict()
      if (refresh) await load()
      return
    }
    toast.error(err, '操作失败')
  }
}

async function removeUnit(unit: KnowledgeUnit): Promise<void> {
  if (!(await toast.confirm(`删除《${unit.title}》？删除后进入清理流程，且无法重新激活。`))) return
  try {
    const res = await knowledgeApi.deleteUnit(unit.id, unit.revision)
    ElMessage.success(`已提交删除，清理状态：${res.cleanup_status}`)
    await load()
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      const refresh = await toast.confirmRevisionConflict()
      if (refresh) await load()
      return
    }
    toast.error(err, '删除失败')
  }
}

function openAcl(unit: KnowledgeUnit): void {
  aclTarget.value = unit
  aclVisible.value = true
}

watch(detailVisible, (open) => {
  if (!open) {
    chunks.value = []
    chunksError.value = ''
    detailTarget.value = null
  }
})

watch(chunksPage, () => {
  void loadChunks()
})

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="kb-page">
    <header class="kb-page__head">
      <div>
        <h2 class="kb-page__title">知识中心</h2>
        <p class="kb-page__desc">
          台账元数据属于 kb:view 能力；正文与切片读取另需数据读权。索引状态（待索引 / 已索引 / 已过期）与启用状态相互独立。
        </p>
      </div>
      <el-button v-if="canUpload" type="primary" :icon="Upload" @click="uploadVisible = true">导入知识</el-button>
    </header>

    <div class="kb-panel" style="margin-top: var(--kb-sp-4)">
      <div class="kb-panel__head">
        <div class="kb-toolbar">
          <el-input
            v-model="q"
            placeholder="搜索标题或编码（≤200 字符）"
            maxlength="200"
            clearable
            style="width: 240px"
            :prefix-icon="Search"
            @keyup.enter="search"
          />
          <el-input v-model="category" placeholder="分类" maxlength="100" clearable style="width: 160px" @keyup.enter="search" />
          <el-select v-model="enabledFilter" style="width: 140px">
            <el-option label="全部状态" value="all" />
            <el-option label="仅启用" value="on" />
            <el-option label="仅停用" value="off" />
          </el-select>
          <el-button type="primary" @click="search">查询</el-button>
          <el-button :icon="Refresh" @click="load">刷新</el-button>
        </div>
      </div>

      <div class="kb-panel__body">
        <el-skeleton v-if="loading && items.length === 0" :rows="6" animated />
        <EmptyState v-else-if="loadError" variant="error" title="知识列表加载失败" @retry="load" />
        <EmptyState
          v-else-if="items.length === 0"
          title="暂无知识"
          description="可通过右上角「导入知识」上传 DOCX / 文本 PDF / Markdown / TXT。"
        />

        <template v-else>
          <el-table :data="items" v-loading="loading">
            <el-table-column prop="code" label="编码" width="150" class-name="kb-mono" show-overflow-tooltip />
            <el-table-column prop="title" label="标题" min-width="220" show-overflow-tooltip />
            <el-table-column prop="format" label="格式" width="90" />
            <el-table-column prop="category" label="分类" width="130" show-overflow-tooltip />
            <el-table-column label="ACL 标签" min-width="150">
              <template #default="{ row }">
                <el-tag v-for="t in row.acl_tags" :key="t" size="small" style="margin-right: 4px">{{ t }}</el-tag>
                <span v-if="!row.acl_tags?.length" class="kb-faint">未标注</span>
              </template>
            </el-table-column>
            <el-table-column label="更新时间" width="170">
              <template #default="{ row }"><span class="kb-faint">{{ row.updated_at }}</span></template>
            </el-table-column>
            <el-table-column label="启用" width="90">
              <template #default="{ row }">
                <span :class="row.enabled ? '' : 'kb-faint'">{{ row.enabled ? '启用' : '停用' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="索引状态" width="120">
              <template #default="{ row }"><StatusTag :value="row.index_status" /></template>
            </el-table-column>
            <el-table-column label="操作" width="260" fixed="right">
              <template #default="{ row }">
                <el-button text size="small" @click="openDetail(row)">详情与切片</el-button>
                <el-button v-if="canPerm" text size="small" :icon="Key" @click="openAcl(row)">权限</el-button>
                <el-dropdown v-if="canEdit || canDelete" trigger="click" style="margin-left: 8px">
                  <el-button text size="small">更多</el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item v-if="canEdit" @click="toggleEnabled(row)">
                        {{ row.enabled ? '停用' : '启用' }}
                      </el-dropdown-item>
                      <el-dropdown-item v-if="canDelete" divided @click="removeUnit(row)">删除</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </template>
            </el-table-column>
          </el-table>

          <el-pagination
            class="kb-pager"
            layout="total, prev, pager, next, sizes"
            :total="total"
            :current-page="page"
            :page-size="size"
            :page-sizes="[10, 20, 50, 100]"
            @current-change="changePage"
            @size-change="(s: number) => { size = s; search() }"
          />
        </template>
      </div>
    </div>

    <!-- 详情与切片 -->
    <el-drawer v-model="detailVisible" :title="detailTarget?.title ?? '知识详情'" size="720px">
      <template v-if="detailTarget">
        <el-form label-width="72px" label-position="left">
          <el-form-item label="标题">
            <el-input v-model="metaTitle" maxlength="200" :disabled="!canEdit" />
          </el-form-item>
          <el-form-item label="分类">
            <el-input v-model="metaCategory" maxlength="100" :disabled="!canEdit" />
          </el-form-item>
          <el-form-item label="状态">
            <StatusTag :value="detailTarget.index_status" />
            <span class="kb-faint" style="margin-left: var(--kb-sp-3)">
              revision {{ detailTarget.revision }} · {{ detailTarget.enabled ? '启用' : '停用' }}
            </span>
          </el-form-item>
          <el-form-item v-if="canEdit">
            <el-button type="primary" @click="saveMetadata">保存元数据</el-button>
            <el-button v-if="canPerm" :icon="Key" @click="openAcl(detailTarget)">四维权限</el-button>
          </el-form-item>
        </el-form>

        <el-divider content-position="left">正文切片</el-divider>

        <el-skeleton v-if="chunksLoading" :rows="4" animated />
        <EmptyState v-else-if="chunksError" variant="error" :title="chunksError" @retry="loadChunks" />
        <EmptyState v-else-if="chunks.length === 0" title="暂无切片" description="索引完成后可在此查看实际提取内容。" />
        <template v-else>
          <div v-for="c in chunks" :key="c.chunk_id" class="kb-chunk">
            <div class="kb-chunk__head">
              <span class="kb-mono">#{{ c.seq }}</span>
              <span class="kb-faint">版本 {{ c.version }}</span>
              <span class="kb-faint">
                {{ c.page_no !== null ? `第 ${c.page_no} 页` : c.offset >= 0 ? `偏移 ${c.offset}` : '页级/段级定位' }}
              </span>
            </div>
            <p class="kb-chunk__text">{{ c.text }}</p>
          </div>

          <el-pagination
            class="kb-pager"
            layout="total, prev, pager, next"
            :total="chunksTotal"
            :current-page="chunksPage"
            :page-size="20"
            @current-change="(p: number) => (chunksPage = p)"
          />
        </template>
      </template>
    </el-drawer>

    <UploadDrawer v-model="uploadVisible" @imported="load" />

    <AclDialog
      v-if="aclTarget"
      v-model="aclVisible"
      :unit-id="aclTarget.id"
      :unit-title="aclTarget.title"
      @saved="load"
    />
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
.kb-chunk {
  padding: var(--kb-sp-3) 0;
  border-bottom: 1px solid var(--kb-border);
}
.kb-chunk__head {
  display: flex;
  gap: var(--kb-sp-3);
  font-size: var(--kb-fs-12);
  color: var(--kb-text-2);
}
.kb-chunk__text {
  margin: var(--kb-sp-2) 0 0;
  font-size: var(--kb-fs-13);
  line-height: 1.7;
  white-space: pre-wrap;
}
</style>
