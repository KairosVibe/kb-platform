<script setup lang="ts">
/**
 * 导入抽屉（M03 / H32 / F-03.01—F-03.05，FRONTEND-SPEC §4.3）
 *
 * 硬性要求：
 * - **传输进度与解析/索引进度分开显示**，不得把"传输完成"当"索引完成"；
 * - 批量逐文件状态，部分失败可单独重试；
 * - 相对路径只作展示与分组，预校验拒绝绝对路径/盘符/`..`/NUL。
 */
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'
import * as knowledgeApi from '@/api/knowledge'
import { ApiError } from '@/api/client'
import type { IndexTaskState } from '@/api/types'
import {
  expandDrop,
  newIdempotencyKey,
  uploadBatch,
  uploadSingle,
  validateManifest,
  type DropEntry,
} from '@/composables/useUpload'
import StatusTag from './StatusTag.vue'

type RowState = 'pending' | 'uploading' | 'accepted' | 'failed'

interface Row {
  relative_path: string
  size: number
  file: File
  state: RowState
  progress: number
  unitId: number | null
  taskId: number | null
  errorCode: string | null
  /** 幂等键：重试复用同一值，不对已接受文件创建第二任务 */
  clientUploadId: string
  task: IndexTaskState | null
}

const props = defineProps<{ modelValue: boolean; defaultCategory?: string }>()
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'imported'): void
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const category = ref(props.defaultCategory ?? '')
const rows = ref<Row[]>([])
const dragging = ref(false)
const uploading = ref(false)
const batchProgress = ref(0)
const manifestError = ref('')

let pollTimer: ReturnType<typeof setTimeout> | null = null

const totalBytes = computed(() => rows.value.reduce((sum, r) => sum + r.size, 0))
const fileInput = ref<HTMLInputElement | null>(null)
const dirInput = ref<HTMLInputElement | null>(null)

function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MiB`
}

function addEntries(entries: DropEntry[]): void {
  const check = validateManifest(entries)
  manifestError.value = check.errors.join('；')
  if (!check.ok && entries.length === 0) return

  for (const e of entries) {
    rows.value.push({
      relative_path: e.relative_path,
      size: e.file.size,
      file: e.file,
      state: 'pending',
      progress: 0,
      unitId: null,
      taskId: null,
      errorCode: null,
      clientUploadId: newIdempotencyKey(),
      task: null,
    })
  }
}

async function onDrop(event: DragEvent): Promise<void> {
  event.preventDefault()
  dragging.value = false
  const dt = event.dataTransfer
  if (!dt) return
  const items = Array.from(dt.items)
  const entries = items.length > 0 ? await expandDrop(items) : await expandDrop(dt.files)
  addEntries(entries)
}

async function onPickFiles(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  if (!input.files) return
  addEntries(await expandDrop(input.files))
  input.value = ''
}

async function onPickDir(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  if (!input.files) return
  const list: DropEntry[] = Array.from(input.files).map((file) => ({
    file,
    relative_path: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name,
  }))
  addEntries(list)
  input.value = ''
}

function removeRow(index: number): void {
  rows.value.splice(index, 1)
}

function clearAll(): void {
  rows.value = []
  manifestError.value = ''
  batchProgress.value = 0
}

/** 单文件上传：走 F-03.01，重试复用同一 client_upload_id */
async function uploadRow(row: Row): Promise<void> {
  row.state = 'uploading'
  row.progress = 0
  row.errorCode = null
  try {
    const res = await uploadSingle(row.file, category.value, row.clientUploadId, (p) => {
      row.progress = p
    })
    row.unitId = res.unit_id
    row.taskId = res.task_id
    row.state = 'accepted'
  } catch (err) {
    row.state = 'failed'
    row.errorCode = err instanceof ApiError ? err.code : 'TRANSFER_FAILED'
  }
}

/** 一行的完整重试：已接受过则只重试任务，不重复上传 */
async function retryRow(row: Row): Promise<void> {
  if (row.unitId !== null && row.taskId !== null) {
    await retryTask(row)
    return
  }
  await uploadRow(row)
  if (row.taskId !== null) schedulePolling()
}

async function retryTask(row: Row): Promise<void> {
  if (row.taskId === null || row.task === null) return
  try {
    await knowledgeApi.retryIndexTask(row.taskId, row.task.revision)
    ElMessage.success('已提交重试')
    schedulePolling()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '重试失败')
  }
}

async function startUpload(): Promise<void> {
  const pending = rows.value.filter((r) => r.state === 'pending' || r.state === 'failed')
  if (pending.length === 0) {
    ElMessage.info('没有待上传的文件')
    return
  }

  uploading.value = true
  batchProgress.value = 0

  try {
    if (pending.length === 1) {
      const row = pending[0]!
      await uploadRow(row)
      batchProgress.value = row.state === 'accepted' ? 100 : 0
    } else {
      const entries: DropEntry[] = pending.map((r) => ({ file: r.file, relative_path: r.relative_path }))
      const batchId = newIdempotencyKey()
      for (const r of pending) r.state = 'uploading'

      const res = await uploadBatch(entries, category.value, batchId, (p) => {
        batchProgress.value = p
      })

      // 批量响应逐项映射回行；有效文件可独立成功，部分失败可单独重试
      res.items.forEach((item, idx) => {
        const row = pending[idx]
        if (!row) return
        row.unitId = item.unit_id
        row.taskId = item.task_id
        row.errorCode = item.error_code
        row.state = item.error_code ? 'failed' : 'accepted'
      })
    }

    const accepted = rows.value.filter((r) => r.state === 'accepted').length
    ElMessage.success(`已接受 ${accepted} 个文件。任务开始解析与索引，进度见下方"任务阶段"。`)
    emit('imported')
    schedulePolling()
  } catch (err) {
    for (const r of pending) if (r.state === 'uploading') r.state = 'failed'
    ElMessage.error(err instanceof ApiError ? err.message : '上传失败，请检查网络后重试')
  } finally {
    uploading.value = false
  }
}

/** 轮询任务阶段（F-03.03）——与传输进度是两条独立信息 */
function schedulePolling(): void {
  if (pollTimer) clearTimeout(pollTimer)
  const targets = rows.value.filter((r) => r.taskId !== null && r.state === 'accepted')
  if (targets.length === 0) return

  pollTimer = setTimeout(async () => {
    let active = 0
    for (const row of targets) {
      if (row.taskId === null) continue
      try {
        const state = await knowledgeApi.getIndexTask(row.taskId)
        row.task = state
        if (state.status === 'succeeded' || state.status === 'failed' || state.status === 'superseded') {
          if (state.status === 'succeeded') row.state = 'accepted'
        } else {
          active += 1
        }
      } catch {
        // 单次轮询失败不阻断其他行
      }
    }
    if (active > 0) schedulePolling()
  }, 1500)
}

function close(): void {
  stopPolling()
  visible.value = false
}

/** 抽屉关闭前必须停掉轮询，避免离开页面后仍有定时器在跑 */
function beforeClose(done: () => void): void {
  stopPolling()
  done()
}

function stopPolling(): void {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}
</script>

<template>
  <el-drawer v-model="visible" title="导入知识" size="720px" :before-close="beforeClose">
    <div class="kb-upload">
      <div class="kb-upload__row">
        <el-input v-model="category" placeholder="分类（可选，最长 100 字符）" maxlength="100" clearable style="max-width: 320px" />
        <el-button @click="fileInput?.click()">选择文件</el-button>
        <el-button @click="dirInput?.click()">选择文件夹</el-button>
        <el-button v-if="rows.length" @click="clearAll">清空清单</el-button>
      </div>

      <input ref="fileInput" type="file" multiple hidden @change="onPickFiles" />
      <input ref="dirInput" type="file" webkitdirectory directory multiple hidden @change="onPickDir" />

      <div
        class="kb-upload__drop"
        :class="{ 'is-dragging': dragging }"
        @dragover.prevent="dragging = true"
        @dragleave.prevent="dragging = false"
        @drop="onDrop"
      >
        <el-icon class="kb-upload__icon"><UploadFilled /></el-icon>
        <p class="kb-upload__hint">把文件或文件夹拖到这里</p>
        <p class="kb-upload__sub">
          支持 DOCX / 文本 PDF / Markdown / TXT；单文件 ≤ 20 MiB，批次 ≤ 100 个文件且 ≤ 200 MiB，并发 3。
          文件夹的相对路径只用于展示与分组。
        </p>
      </div>

      <p v-if="manifestError" class="kb-upload__error">{{ manifestError }}</p>

      <div v-if="rows.length" class="kb-upload__summary kb-faint">
        共 {{ rows.length }} 个文件，合计 {{ fmtSize(totalBytes) }}
        <span v-if="uploading"> · 传输进度 {{ batchProgress }}%</span>
      </div>

      <el-table v-if="rows.length" :data="rows" size="small" class="kb-compact" style="margin-top: var(--kb-sp-3)">
        <el-table-column prop="relative_path" label="相对路径" min-width="200" show-overflow-tooltip />
        <el-table-column label="大小" width="96">
          <template #default="{ row }">{{ fmtSize(row.size) }}</template>
        </el-table-column>
        <el-table-column label="传输" width="120">
          <template #default="{ row }">
            <el-progress
              v-if="row.state === 'uploading'"
              :percentage="row.progress"
              :stroke-width="6"
              :show-text="false"
            />
            <span v-else class="kb-faint">{{ row.state === 'pending' ? '待上传' : row.state === 'failed' ? '失败' : '已完成' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="任务阶段" min-width="180">
          <template #default="{ row }">
            <template v-if="row.task">
              <StatusTag :value="row.task.status" />
              <span class="kb-faint" style="margin-left: var(--kb-sp-1)">{{ row.task.stage }}</span>
              <span v-if="row.task.progress !== null" class="kb-faint"> · {{ row.task.progress }}%</span>
              <span v-if="row.task.error_code" class="kb-upload__error"> · {{ row.task.error_code }}</span>
            </template>
            <span v-else-if="row.errorCode" class="kb-upload__error">{{ row.errorCode }}</span>
            <span v-else class="kb-faint">未开始</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row, $index }">
            <el-button v-if="row.state === 'failed' || row.task?.status === 'failed'" text size="small" @click="retryRow(row)">
              重试
            </el-button>
            <el-button text size="small" @click="removeRow($index)">移除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <p class="kb-upload__note">
        传输完成不等于索引完成：解析与索引进度由后端任务状态单独给出，刷新页面后仍会显示实际进度与错误。
      </p>
    </div>

    <template #footer>
      <el-button @click="close">关闭</el-button>
      <el-button type="primary" :loading="uploading" @click="startUpload">开始上传</el-button>
    </template>
  </el-drawer>
</template>

<style scoped>
.kb-upload__row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--kb-sp-2);
  align-items: center;
}
.kb-upload__drop {
  margin-top: var(--kb-sp-4);
  padding: var(--kb-sp-8) var(--kb-sp-4);
  border: 1px dashed var(--kb-border);
  border-radius: var(--kb-r-3);
  text-align: center;
  background: var(--kb-surface-2);
}
.kb-upload__drop.is-dragging {
  border-color: var(--kb-primary);
  background: var(--kb-primary-soft);
}
.kb-upload__icon {
  font-size: 32px;
  color: var(--kb-text-3);
}
.kb-upload__hint {
  margin: var(--kb-sp-2) 0 var(--kb-sp-1);
  font-size: var(--kb-fs-14);
}
.kb-upload__sub,
.kb-upload__note {
  margin: 0;
  font-size: var(--kb-fs-12);
  color: var(--kb-text-3);
  line-height: 1.7;
}
.kb-upload__note {
  margin-top: var(--kb-sp-3);
}
.kb-upload__summary {
  margin-top: var(--kb-sp-3);
  font-size: var(--kb-fs-12);
}
.kb-upload__error {
  color: var(--kb-danger);
  font-size: var(--kb-fs-12);
}
</style>
