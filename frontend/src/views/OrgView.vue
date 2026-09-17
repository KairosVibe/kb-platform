<script setup lang="ts">
/**
 * 组织配置页（M02，FRONTEND-SPEC §4.2）
 *
 * - 三个面板**各自独立按权限码渲染**（sys:dept / sys:user / sys:role），无权限的面板整块不渲染；
 * - 部门仅按**直属部门**匹配，不继承祖先或子孙；不能移动到自身后代；
 * - 删除部门遇引用返回 409，需先迁移成员或撤销引用；
 * - 新建角色**默认不勾选任何权限码**（上游未规定其余角色的默认码清单）；
 * - 停用账号对所有人（含创建者与管理员）不可检索，需显式确认。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Delete, Plus, Refresh } from '@element-plus/icons-vue'
import * as orgApi from '@/api/org'
import { ApiError } from '@/api/client'
import type { Department, PermissionCode, RoleRow, UserRow } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useToast } from '@/composables/useToast'
import EmptyState from '@/components/EmptyState.vue'

interface DeptNode extends Department {
  children: DeptNode[]
}

const auth = useAuthStore()
const toast = useToast()

const canDept = computed(() => auth.can('sys:dept'))
const canUser = computed(() => auth.can('sys:user'))
const canRole = computed(() => auth.can('sys:role'))

/* ---------- 部门 ---------- */
const departments = ref<Department[]>([])
const deptLoading = ref(false)
const deptError = ref(false)
const selectedDeptId = ref<number | null>(null)

const deptTree = computed<DeptNode[]>(() => {
  const nodes = new Map<number, DeptNode>()
  for (const d of departments.value) nodes.set(d.id, { ...d, children: [] })
  const roots: DeptNode[] = []
  for (const node of nodes.values()) {
    if (node.parent_id !== null && nodes.has(node.parent_id)) nodes.get(node.parent_id)!.children.push(node)
    else roots.push(node)
  }
  return roots
})

function descendantIds(id: number): Set<number> {
  const out = new Set<number>()
  const walk = (pid: number): void => {
    for (const d of departments.value) {
      if (d.parent_id === pid && !out.has(d.id)) {
        out.add(d.id)
        walk(d.id)
      }
    }
  }
  walk(id)
  return out
}

async function loadDepartments(): Promise<void> {
  deptLoading.value = true
  deptError.value = false
  try {
    const res = await orgApi.listDepartments()
    departments.value = res.items
  } catch {
    deptError.value = true
  } finally {
    deptLoading.value = false
  }
}

async function createDept(parentId: number | null): Promise<void> {
  const name = window.prompt('部门名称（≤100 字符）', '')
  if (!name) return
  try {
    await orgApi.createDepartment(parentId, name)
    ElMessage.success('已创建')
    await loadDepartments()
  } catch (err) {
    toast.error(err, '创建部门失败')
  }
}

async function renameDept(node: Department): Promise<void> {
  const name = window.prompt('部门名称', node.name)
  if (name === null) return
  try {
    await orgApi.updateDepartment(node.id, { parent_id: node.parent_id, name, expected_revision: node.revision })
    ElMessage.success('已保存')
    await loadDepartments()
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      const refresh = await toast.confirmRevisionConflict()
      if (refresh) await loadDepartments()
      return
    }
    toast.error(err, '保存失败')
  }
}

async function moveDept(node: Department): Promise<void> {
  const target = window.prompt('移动到哪个部门的 ID 下（留空表示设为根部门）', node.parent_id === null ? '' : String(node.parent_id))
  if (target === null) return
  const parentId = target.trim() === '' ? null : Number(target)
  if (parentId !== null && !Number.isFinite(parentId)) {
    ElMessage.warning('请输入合法的部门 ID')
    return
  }
  // 前端预检：不能移动到自身或自身后代（后端仍强制）
  if (parentId === node.id || (parentId !== null && descendantIds(node.id).has(parentId))) {
    ElMessage.error('不能把部门移动到自身或自身后代之下')
    return
  }
  try {
    await orgApi.updateDepartment(node.id, { parent_id: parentId, name: node.name, expected_revision: node.revision })
    ElMessage.success('已移动')
    await loadDepartments()
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      const refresh = await toast.confirmRevisionConflict()
      if (refresh) await loadDepartments()
      return
    }
    toast.error(err, '移动失败')
  }
}

async function removeDept(node: Department): Promise<void> {
  if (!(await toast.confirm(`删除部门「${node.name}」？若仍被用户或知识引用，后端会拒绝（409），需要先迁移成员或撤销引用。`))) return
  try {
    await orgApi.deleteDepartment(node.id, node.revision)
    ElMessage.success('已删除')
    await loadDepartments()
    if (selectedDeptId.value === node.id) selectedDeptId.value = null
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      ElMessage.error('该部门仍被引用，请先迁移成员或撤销引用')
      return
    }
    toast.error(err, '删除失败')
  }
}

/* ---------- 用户 ---------- */
const users = ref<UserRow[]>([])
const userTotal = ref(0)
const userLoading = ref(false)
const userError = ref(false)
const userQuery = ref('')
const userPage = ref(1)

async function loadUsers(): Promise<void> {
  userLoading.value = true
  userError.value = false
  try {
    const res = await orgApi.listUsers({ q: userQuery.value || undefined, page: userPage.value, size: 20 })
    users.value = res.items
    userTotal.value = res.total
  } catch {
    userError.value = true
  } finally {
    userLoading.value = false
  }
}

async function createUser(): Promise<void> {
  const username = window.prompt('用户名（3—64 字符）', '')
  if (!username) return
  const password = window.prompt('初始密码（12—72 字节，不 trim 不截断）', '')
  if (!password) return
  try {
    await orgApi.createUser({ username, password, dept_id: selectedDeptId.value, role_ids: [] })
    ElMessage.success('已创建用户')
    await loadUsers()
  } catch (err) {
    toast.error(err, '创建用户失败')
  }
}

async function toggleUser(row: UserRow): Promise<void> {
  const next = !row.enabled
  if (!next && !(await toast.confirm('停用后该账号对所有人（含创建者与系统管理员）都不可检索，且无法登录。确认停用？'))) {
    return
  }
  try {
    const res = await orgApi.updateUser(row.id, {
      dept_id: row.dept_id,
      role_ids: row.role_ids,
      enabled: next,
      expected_revision: row.revision,
    })
    row.enabled = res.enabled
    row.revision = res.revision
    ElMessage.success(next ? '已启用' : '已停用')
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      const refresh = await toast.confirmRevisionConflict()
      if (refresh) await loadUsers()
      return
    }
    toast.error(err, '操作失败')
  }
}

/* ---------- 角色与功能树 ---------- */
const roles = ref<RoleRow[]>([])
const rolesLoading = ref(false)
const rolesError = ref(false)
const permissionCodes = ref<PermissionCode[]>([])

const activeRole = ref<RoleRow | null>(null)
const roleName = ref('')
const roleCodes = ref<string[]>([])
const roleSaving = ref(false)

const permGroups = computed(() => {
  const map = new Map<string, PermissionCode[]>()
  for (const p of permissionCodes.value) {
    const key = p.module || '其他'
    if (!map.has(key)) map.set(key, [])
    map.get(key)!.push(p)
  }
  return [...map.entries()]
})

async function loadRoles(): Promise<void> {
  rolesLoading.value = true
  rolesError.value = false
  try {
    const [rolePage, codes] = await Promise.all([orgApi.listRoles({ page: 1, size: 50 }), orgApi.listPermissionCodes()])
    roles.value = rolePage.items
    permissionCodes.value = codes.items
  } catch {
    rolesError.value = true
  } finally {
    rolesLoading.value = false
  }
}

function selectRole(role: RoleRow | null): void {
  activeRole.value = role
  // 新建角色默认不勾选任何权限码
  roleName.value = role?.name ?? ''
  roleCodes.value = role ? [...role.codes] : []
}

async function saveRole(): Promise<void> {
  if (!roleName.value.trim()) {
    ElMessage.warning('请输入角色名称（≤100 字符）')
    return
  }
  roleSaving.value = true
  try {
    const res = await orgApi.saveRole({
      id: activeRole.value?.id ?? null,
      name: roleName.value.trim(),
      codes: roleCodes.value,
      expected_revision: activeRole.value?.revision ?? null,
    })
    ElMessage.success('已保存角色')
    await loadRoles()
    const saved = roles.value.find((r) => r.id === res.id) ?? null
    if (saved) selectRole(saved)
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      const refresh = await toast.confirmRevisionConflict()
      if (refresh) await loadRoles()
      return
    }
    toast.error(err, '保存角色失败')
  } finally {
    roleSaving.value = false
  }
}

async function removeRole(role: RoleRow): Promise<void> {
  if (!(await toast.confirm(`删除角色「${role.name}」？仍被用户引用时后端会拒绝，需要先撤销引用。`))) return
  try {
    await orgApi.deleteRole(role.id, role.revision)
    ElMessage.success('已删除')
    if (activeRole.value?.id === role.id) selectRole(null)
    await loadRoles()
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      ElMessage.error('该角色仍被用户引用，请先撤销引用')
      return
    }
    toast.error(err, '删除失败')
  }
}

onMounted(() => {
  if (canDept.value) void loadDepartments()
  if (canUser.value) void loadUsers()
  if (canRole.value) void loadRoles()
})
</script>

<template>
  <div class="kb-page">
    <header class="kb-page__head">
      <div>
        <h2 class="kb-page__title">组织配置</h2>
        <p class="kb-page__desc">
          三个面板按 sys:dept / sys:user / sys:role 独立授权；部门权限仅按直属部门精确匹配，不继承祖先或子孙。
        </p>
      </div>
    </header>

    <div class="kb-org">
      <!-- 部门 -->
      <section v-if="canDept" class="kb-panel">
        <div class="kb-panel__head">
          <span class="kb-panel__title">部门树</span>
          <div>
            <el-button text size="small" :icon="Plus" @click="createDept(null)">根部门</el-button>
            <el-button text size="small" :icon="Refresh" @click="loadDepartments" />
          </div>
        </div>
        <div class="kb-panel__body">
          <el-skeleton v-if="deptLoading && departments.length === 0" :rows="5" animated />
          <EmptyState v-else-if="deptError" variant="error" title="部门加载失败" @retry="loadDepartments" />
          <EmptyState v-else-if="departments.length === 0" title="暂无部门" description="先创建根部门，再逐层建立组织结构。" />
          <el-tree
            v-else
            :data="deptTree"
            node-key="id"
            default-expand-all
            highlight-current
            :props="{ label: 'name', children: 'children' }"
            @node-click="(node: Department) => (selectedDeptId = node.id)"
          >
            <template #default="{ data }">
              <span class="kb-dept">
                <span class="kb-truncate">{{ data.name }}</span>
                <span class="kb-dept__ops">
                  <el-button text size="small" @click.stop="createDept(data.id)">子部门</el-button>
                  <el-button text size="small" @click.stop="renameDept(data)">改名</el-button>
                  <el-button text size="small" @click.stop="moveDept(data)">移动</el-button>
                  <el-button text size="small" :icon="Delete" @click.stop="removeDept(data)" />
                </span>
              </span>
            </template>
          </el-tree>
        </div>
      </section>

      <!-- 用户 -->
      <section v-if="canUser" class="kb-panel">
        <div class="kb-panel__head">
          <span class="kb-panel__title">用户</span>
          <div class="kb-toolbar">
            <el-input v-model="userQuery" placeholder="搜索用户名" clearable style="width: 150px" @keyup.enter="loadUsers" />
            <el-button text size="small" :icon="Plus" @click="createUser">新建用户</el-button>
          </div>
        </div>
        <div class="kb-panel__body">
          <p v-if="selectedDeptId !== null" class="kb-faint" style="margin-bottom: var(--kb-sp-2)">
            新建用户将挂到部门 #{{ selectedDeptId }}
          </p>
          <el-skeleton v-if="userLoading && users.length === 0" :rows="5" animated />
          <EmptyState v-else-if="userError" variant="error" title="用户列表加载失败" @retry="loadUsers" />
          <EmptyState v-else-if="users.length === 0" title="暂无用户" />
          <el-table v-else :data="users" size="small">
            <el-table-column prop="username" label="用户名" min-width="120" show-overflow-tooltip />
            <el-table-column label="部门" width="90">
              <template #default="{ row }">{{ row.dept_id ?? '—' }}</template>
            </el-table-column>
            <el-table-column label="角色" min-width="120">
              <template #default="{ row }">
                <el-tag v-for="r in row.role_ids" :key="r" size="small" style="margin-right: 4px">{{ r }}</el-tag>
                <span v-if="!row.role_ids.length" class="kb-faint">无</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <span :class="row.enabled ? '' : 'kb-faint'">{{ row.enabled ? '启用' : '停用' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="90" fixed="right">
              <template #default="{ row }">
                <el-button text size="small" @click="toggleUser(row)">{{ row.enabled ? '停用' : '启用' }}</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-pagination
            v-if="userTotal > 20"
            class="kb-pager"
            layout="total, prev, pager, next"
            :total="userTotal"
            :current-page="userPage"
            :page-size="20"
            @current-change="(p: number) => { userPage = p; loadUsers() }"
          />
        </div>
      </section>

      <!-- 角色 -->
      <section v-if="canRole" class="kb-panel">
        <div class="kb-panel__head">
          <span class="kb-panel__title">角色与功能权限</span>
          <div class="kb-toolbar">
            <el-button text size="small" :icon="Plus" @click="selectRole(null)">新建角色</el-button>
            <el-button text size="small" :icon="Refresh" @click="loadRoles" />
          </div>
        </div>
        <div class="kb-panel__body">
          <el-skeleton v-if="rolesLoading && roles.length === 0" :rows="5" animated />
          <EmptyState v-else-if="rolesError" variant="error" title="角色加载失败" @retry="loadRoles" />
          <template v-else>
            <div class="kb-roles">
              <div
                v-for="role in roles"
                :key="role.id"
                class="kb-role"
                :class="{ 'is-active': activeRole?.id === role.id }"
                @click="selectRole(role)"
              >
                <span class="kb-truncate">{{ role.name }}</span>
                <span class="kb-faint">{{ role.codes.length }} 项</span>
                <el-button text size="small" :icon="Delete" @click.stop="removeRole(role)" />
              </div>
              <p v-if="roles.length === 0" class="kb-faint">暂无角色，点击「新建角色」开始。</p>
            </div>

            <el-divider content-position="left">{{ activeRole ? `编辑：${activeRole.name}` : '新建角色' }}</el-divider>

            <el-form label-width="72px" label-position="left">
              <el-form-item label="角色名">
                <el-input v-model="roleName" maxlength="100" placeholder="角色名称" />
              </el-form-item>
              <el-form-item label="功能码">
                <div class="kb-perm">
                  <div v-for="[group, codes] in permGroups" :key="group" class="kb-perm__group">
                    <p class="kb-perm__group-title">{{ group }}</p>
                    <el-checkbox-group v-model="roleCodes">
                      <el-checkbox v-for="c in codes" :key="c.code" :value="c.code" :label="c.code">
                        {{ c.label }} <span class="kb-faint kb-mono">{{ c.code }}</span>
                      </el-checkbox>
                    </el-checkbox-group>
                  </div>
                </div>
              </el-form-item>
              <el-form-item>
                <el-button type="primary" :loading="roleSaving" @click="saveRole">保存角色</el-button>
              </el-form-item>
            </el-form>
            <p class="kb-faint">
              权限码由后端固定 14 项并按 module 返回，前端只做分组展示。新建角色默认不勾选任何功能码。
            </p>
          </template>
        </div>
      </section>
    </div>

    <EmptyState
      v-if="!canDept && !canUser && !canRole"
      variant="error"
      title="没有组织配置权限"
      description="需要 sys:dept、sys:user 或 sys:role 中的至少一项。"
    />
  </div>
</template>

<style scoped>
.kb-page__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
}
.kb-org {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: var(--kb-sp-4);
  margin-top: var(--kb-sp-4);
  align-items: start;
}
.kb-dept {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  gap: var(--kb-sp-2);
}
.kb-dept__ops {
  display: none;
}
.kb-dept:hover .kb-dept__ops {
  display: inline-flex;
}
.kb-roles {
  display: flex;
  flex-direction: column;
  gap: var(--kb-sp-1);
}
.kb-role {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--kb-sp-2);
  padding: var(--kb-sp-2) var(--kb-sp-3);
  border: 1px solid var(--kb-border);
  border-radius: var(--kb-r-2);
  font-size: var(--kb-fs-13);
  cursor: pointer;
}
.kb-role.is-active {
  border-color: var(--kb-primary);
  background: var(--kb-primary-soft);
}
.kb-perm {
  display: flex;
  flex-direction: column;
  gap: var(--kb-sp-3);
  max-height: 360px;
  overflow: auto;
  width: 100%;
}
.kb-perm__group-title {
  margin: 0 0 var(--kb-sp-1);
  font-size: var(--kb-fs-12);
  color: var(--kb-text-3);
}
.kb-pager {
  margin-top: var(--kb-sp-3);
  justify-content: flex-end;
}
</style>
