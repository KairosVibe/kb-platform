<script setup lang="ts">
/**
 * 四维权限弹窗（H33 / F-04.08 / API-S04，FRONTEND-SPEC §4.3）
 *
 * - 回填：GET /api/knowledge-units/{id}/acl；
 * - 关闭 global **保留**其他维度选择；实体去重；
 * - 四维全空时显式提示"无人可读（含创建者与管理员）"，提交仍由后端复核；
 * - 权限变更即时生效，已发送的流内容无法撤回。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import * as knowledgeApi from '@/api/knowledge'
import { ApiError } from '@/api/client'
import type { AclEntity } from '@/api/types'
import { ACL_EMPTY_WARNING, isAclEmpty, normalizeAcl, type AclDraft } from '@/composables/useAcl'
import { useToast } from '@/composables/useToast'

const props = defineProps<{
  modelValue: boolean
  unitId: number
  unitTitle: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'saved', aclVersion: number): void
}>()

const toast = useToast()

const draft = ref<AclDraft>({ global: false, depts: [], roles: [], users: [] })
const revision = ref(0)
const loading = ref(false)
const saving = ref(false)
const loadError = ref('')

const deptOptions = ref<AclEntity[]>([])
const roleOptions = ref<AclEntity[]>([])
const userOptions = ref<AclEntity[]>([])
const searching = ref({ department: false, role: false, user: false })

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const empty = computed(() => isAclEmpty(draft.value))

watch(
  () => props.modelValue,
  async (open) => {
    if (!open) return
    await reload()
  },
)

async function reload(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const acl = await knowledgeApi.readAcl(props.unitId)
    draft.value = { global: acl.global, depts: acl.depts, roles: acl.roles, users: acl.users }
    revision.value = acl.revision
    await Promise.all([searchEntities('department'), searchEntities('role'), searchEntities('user')])
  } catch (err) {
    loadError.value = err instanceof ApiError ? err.message : '权限信息加载失败'
  } finally {
    loading.value = false
  }
}

async function searchEntities(kind: 'department' | 'role' | 'user', q = ''): Promise<void> {
  searching.value[kind] = true
  try {
    const page = await knowledgeApi.listAclEntities({ kind, q, page: 1, size: 50 })
    if (kind === 'department') deptOptions.value = page.items
    else if (kind === 'role') roleOptions.value = page.items
    else userOptions.value = page.items
  } catch {
    // 选择器加载失败不阻断弹窗，用户可稍后重试
  } finally {
    searching.value[kind] = false
  }
}

async function submit(): Promise<void> {
  const normalized = normalizeAcl(draft.value)
  if (isAclEmpty(normalized)) {
    if (!window.confirm(`${ACL_EMPTY_WARNING}\n\n仍要提交吗？`)) return
  }
  saving.value = true
  try {
    const saved = await knowledgeApi.updateAcl(props.unitId, { ...normalized, revision: revision.value, expected_revision: revision.value })
    ElMessage.success(`已保存，ACL 版本 ${saved.acl_version}。权限变更即时生效。`)
    emit('saved', saved.acl_version)
    visible.value = false
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      const refresh = await toast.confirmRevisionConflict()
      if (refresh) await reload()
    } else {
      toast.error(err, '保存权限失败')
    }
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" :title="`四维数据权限 · ${unitTitle}`" width="720px" append-to-body>
    <el-alert
      type="warning"
      :closable="false"
      show-icon
      title="权限变更即时生效"
      description="提交后新请求与输出批次都会按最新权限复核；已经发送给客户端的内容无法撤回。本弹窗的提示不代替后端校验。"
      style="margin-bottom: var(--kb-sp-4)"
    />

    <el-skeleton v-if="loading" :rows="5" animated />
    <el-result v-else-if="loadError" icon="error" :title="loadError">
      <template #extra>
        <el-button @click="reload">重试</el-button>
      </template>
    </el-result>

    <template v-else>
      <el-form label-width="88px" label-position="left">
        <el-form-item label="全局可读">
          <el-switch v-model="draft.global" />
          <span class="kb-hint" style="margin-left: var(--kb-sp-3)">
            开启后所有登录且启用的用户都可读取该知识正文
          </span>
        </el-form-item>

        <el-form-item label="部门">
          <el-select
            v-model="draft.depts"
            multiple
            filterable
            remote
            reserve-keyword
            collapse-tags
            collapse-tags-tooltip
            :loading="searching.department"
            placeholder="仅匹配直属部门，不继承上级或下级"
            :remote-method="(q: string) => searchEntities('department', q)"
            style="width: 100%"
          >
            <el-option v-for="d in deptOptions" :key="d.id" :label="d.label" :value="d.id" />
          </el-select>
        </el-form-item>

        <el-form-item label="角色">
          <el-select
            v-model="draft.roles"
            multiple
            filterable
            remote
            reserve-keyword
            collapse-tags
            collapse-tags-tooltip
            :loading="searching.role"
            placeholder="与用户角色求交集"
            :remote-method="(q: string) => searchEntities('role', q)"
            style="width: 100%"
          >
            <el-option v-for="r in roleOptions" :key="r.id" :label="r.label" :value="r.id" />
          </el-select>
        </el-form-item>

        <el-form-item label="个人">
          <el-select
            v-model="draft.users"
            multiple
            filterable
            remote
            reserve-keyword
            collapse-tags
            collapse-tags-tooltip
            :loading="searching.user"
            placeholder="按用户显式授权"
            :remote-method="(q: string) => searchEntities('user', q)"
            style="width: 100%"
          >
            <el-option v-for="u in userOptions" :key="u.id" :label="u.label" :value="u.id" />
          </el-select>
        </el-form-item>
      </el-form>

      <div v-if="empty" class="kb-restricted" role="alert" style="margin-top: var(--kb-sp-2)">
        <span>{{ ACL_EMPTY_WARNING }}</span>
      </div>
      <p v-else class="kb-hint">四维按"任一命中即放行"判定；部门维度不继承祖先或子孙。</p>
    </template>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="submit">保存权限</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.kb-hint {
  font-size: var(--kb-fs-12);
  color: var(--kb-text-3);
}
</style>
