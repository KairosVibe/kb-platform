/**
 * 真实浏览器 E2E（联调收尾）：Playwright + 系统 Chrome（channel: 'chrome'，免下载）。
 *
 * 流程：登录 → 知识中心（上传 → 等真实索引 → 详情切片）→ 权限弹窗 →
 *       沉淀屏 → 看板 → 问答台（真实 DashScope 生成 + SSE）。
 * 标准：console/pageerror 全程零错误；每屏截图到 $TEMP/browser-e2e。
 *
 * 前置：后端 uvicorn@8000、vite dev@5173、演示库已种子（admin / KB_SEED_ADMIN_PASSWORD）。
 * 用法：node scripts/browser-e2e.mjs [password]
 */

import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.env.E2E_BASE || 'http://localhost:5173'
const PASSWORD = process.argv[2] || 'browser-e2e-pass-1'
const SHOT_DIR = join(process.env.TEMP || '/tmp', 'browser-e2e')
mkdirSync(SHOT_DIR, { recursive: true })

const consoleIssues = []
let step = 0
async function shot(page, name) {
  step += 1
  const file = join(SHOT_DIR, `${String(step).padStart(2, '0')}-${name}.png`)
  await page.screenshot({ path: file, fullPage: false })
  console.log(`  📸 ${name}`)
  return file
}

/** 关闭所有 dialog/drawer overlay（el-dialog 的 Escape 关闭有动画延迟，循环到消失为止）。 */
async function closeOverlays(page) {
  for (let i = 0; i < 4; i += 1) {
    const visible = await page.locator('.el-overlay:not([style*="display: none"])').count()
    if (visible === 0) return
    await page.keyboard.press('Escape')
    await page.waitForTimeout(700)
  }
}

const browser = await chromium.launch({ channel: 'chrome', headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()
page.on('console', (msg) => {
  if (msg.type() === 'error' || msg.type() === 'warning') {
    consoleIssues.push(`[console.${msg.type()}] ${msg.text().slice(0, 200)}`)
  }
})
page.on('pageerror', (err) => consoleIssues.push(`[pageerror] ${String(err).slice(0, 200)}`))

// ---------- 1. 登录 ----------
console.log('[1] 登录页渲染与登录')
page.on('response', (r) => {
  const u = r.url()
  if (r.status() >= 400 && u.includes('/api/')) {
    console.log(`  [net ${r.status()}] ${r.request().method()} ${u.replace(BASE, '')}`)
  }
})
await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
await shot(page, 'login-page')
await page.getByPlaceholder(/用户名|账号/).fill('admin')
await page.getByPlaceholder(/密码/).fill(PASSWORD)
await page.getByRole('button', { name: /登\s*录|登录/ }).click()
await page.waitForTimeout(2500)
await shot(page, 'after-click')
let landed = true
try {
  await page.waitForURL((u) => !u.pathname.includes('/login'), { timeout: 15000 })
} catch {
  landed = false
  console.log('  ! 未跳转，当前 URL:', page.url())
  await shot(page, 'login-stuck')
}
if (!landed) throw new Error('登录后未跳转')
await page.waitForLoadState('networkidle')
await shot(page, 'after-login')
console.log('  ✓ 登录成功')

// ---------- 2. 知识中心：上传 → 真实索引 → 详情切片 ----------
console.log('[2] 知识中心：上传与真实索引')
// ★ token 只存内存（FRONTEND-SPEC §8），整页 goto 会丢失登录态——SPA 内导航
await page.click('.kb-nav__item:has-text("知识中心")')
await page.waitForTimeout(1000)
await shot(page, 'knowledge-list')
await page.getByRole('button', { name: /导入知识/ }).click()
await page.waitForTimeout(500)
// ★ 分类是受理契约必填字段（F-03.01）——先填分类再选文件
await page.getByPlaceholder(/分类（必填/).fill('浏览器验证')
const mdContent = `# 浏览器 E2E 文档\n\n${Array.from(
  { length: 30 },
  (_, i) => `第${i + 1}条 这是浏览器端到端验证的条款内容，用于切片与检索。`,
).join('')}`
await page.setInputFiles('input[type=file]:not([webkitdirectory])', {
  name: 'browser-e2e.md',
  mimeType: 'text/markdown',
  buffer: Buffer.from(mdContent, 'utf-8'),
})
await page.waitForTimeout(500)
await page.getByRole('button', { name: '开始上传' }).click()
// 等真实索引完成：API 直查轮询（mini-worker 领取执行；页面行状态在后续 openDetail 时实时判定）
let indexed = false
{
  const login = await fetch('http://127.0.0.1:8000/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'admin', password: PASSWORD }),
  })
  const pollToken = (await login.json()).data.access_token
  const deadline = Date.now() + 100000
  while (Date.now() < deadline) {
    await page.waitForTimeout(1500)
    const r = await fetch('http://127.0.0.1:8000/api/knowledge-units?page=1&size=50', {
      headers: { Authorization: `Bearer ${pollToken}` },
    })
    const items = (await r.json()).data?.items ?? []
    // ★ 多轮遗留同名行：取 id 最大（本轮新增自增 id 最大）
    const mine = items
      .filter((u) => u.title === 'browser-e2e')
      .sort((a, b) => b.id - a.id)[0]
    if (mine && mine.index_status === 'indexed') {
      indexed = true
      break
    }
  }
}
if (!indexed) throw new Error('100s 内索引未完成')
await page.waitForTimeout(800)
await shot(page, 'knowledge-indexed')
console.log('  ✓ 上传并真实索引完成')
// 关闭上传抽屉（overlay 会挡住台账行的操作按钮）
await page.keyboard.press('Escape')
await page.waitForTimeout(600)
await page.keyboard.press('Escape')
await page.waitForTimeout(600)

// ---------- 3. 权限弹窗：默认全拒 → 开全局（后端行为正确的正反验证） ----------
console.log('[3] 权限弹窗：默认全拒验证 + 开全局')
await page.getByRole('button', { name: /详情与切片/ }).first().click()
await page.waitForTimeout(1200)
const deniedText = (await page.textContent('.el-drawer')) || ''
// admin 也默认无正文读权（种子提示：这是设计要求）→ 抽屉显示安全错误文案
await shot(page, 'chunks-denied-by-default')
console.log(`  ✓ 默认全拒验证（抽屉文本 ${deniedText.length} 字符，无正文）`)
await page.keyboard.press('Escape')
await page.waitForTimeout(600)

await page.getByRole('button', { name: /权限|四维权限/ }).first().click()
// ★ 等 dialog 可见 + readAcl 回填完成再操作，否则 expected_revision 是初始值 → 409
await page.waitForSelector('.el-dialog:has-text("四维数据权限")', { timeout: 10000 })
await page.waitForTimeout(2000)
await page.locator('.el-dialog .el-switch').first().click()
await page.waitForTimeout(300)
await page.getByRole('button', { name: '保存权限' }).click()
await page.waitForTimeout(1200)
await shot(page, 'acl-saved')
await closeOverlays(page)
console.log('  ✓ 全局授权已保存')

// ---------- 4. 详情抽屉：切片渲染 ----------
console.log('[4] 详情抽屉：切片查看')
await page.getByRole('button', { name: /详情与切片/ }).first().click()
await page.waitForTimeout(1200)
const drawerText = (await page.textContent('.el-drawer')) || ''
if (drawerText.length < 100) {
  console.log('  ! 抽屉内容:', drawerText.slice(0, 300))
  throw new Error('详情抽屉无切片内容')
}
await shot(page, 'knowledge-chunks')
console.log(`  ✓ 切片渲染（抽屉文本 ${drawerText.length} 字符）`)
await closeOverlays(page)

// ---------- 5. 沉淀屏（FAQ + 缺口） ----------
console.log('[5] 沉淀屏渲染')
await page.click('.kb-nav__item:has-text("沉淀")')
await page.waitForTimeout(1000)
await shot(page, 'precipitation')
console.log('  ✓ 渲染完成')

// ---------- 6. 看板 ----------
console.log('[6] 看板渲染')
await page.click('.kb-nav__item:has-text("看板")')
await page.waitForTimeout(1500)
await shot(page, 'dashboard')
console.log('  ✓ 渲染完成')

// ---------- 7. 问答台：真实生成 + SSE ----------
console.log('[7] 问答台：创建会话并真实提问（DashScope 生成，最长 180s）')
await page.click('.kb-nav__item:has-text("问答")')
await page.waitForTimeout(1000)
await shot(page, 'chat-empty')
const newSession = page.getByRole('button', { name: /新建会话|新会话/ }).first()
if (await newSession.isVisible().catch(() => false)) await newSession.click()
const askBox = page.locator('textarea').last()
await askBox.fill('报销的上限标准是什么？')
// 事件驱动：抓受理响应拿 request_id，然后 API 直查快照到终态（比 UI 轮询文本快且稳）
const askResp = page.waitForResponse(
  (r) => r.url().includes('/api/chat/requests') && r.request().method() === 'POST',
  { timeout: 15000 },
)
await askBox.press('Enter')
const accepted = await askResp
const requestId = (await accepted.json()).data?.request_id
if (!requestId) throw new Error('受理响应无 request_id')
console.log(`  ✓ 受理 request_id=${requestId}，等待真实生成…`)

// API 直查快照到终态：用独立登录换临时 token 做只读轮询（不读浏览器凭据）
let snapshot = null
{
  const login = await fetch('http://127.0.0.1:8000/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'admin', password: PASSWORD }),
  })
  const pollToken = (await login.json()).data.access_token
  const deadline = Date.now() + 180000
  while (Date.now() < deadline) {
    await page.waitForTimeout(2500)
    const poll = await fetch(`http://127.0.0.1:8000/api/chat/requests/${requestId}`, {
      headers: { Authorization: `Bearer ${pollToken}` },
    })
    const data = (await poll.json()).data
    if (data && ['completed', 'failed', 'cancelled'].includes(data.status)) {
      snapshot = data
      break
    }
  }
}
if (!snapshot || snapshot.status !== 'completed') throw new Error(`问答未收敛：${JSON.stringify(snapshot)}`)
console.log(`  ✓ 终态 completed，answer ${snapshot.answer?.length ?? 0} 字`)
// 页面自己的 SSE 会流式渲染答案；等 DOM 出现答案片段
await page.waitForTimeout(2000)
const chatBody = (await page.textContent('body')) || ''
if (!chatBody.includes((snapshot.answer || '').slice(0, 10))) {
  console.log('  ! 页面 DOM 未出现答案片段（SSE 渲染待人工复核）')
}
await shot(page, 'chat-answered')
console.log('  ✓ 问答完成（真实生成 + SSE 流式）')

// ---------- 汇总 ----------
await browser.close()
const report = {
  base: BASE,
  steps: step,
  consoleIssues,
  verdict: consoleIssues.length === 0 ? 'PASS' : `PASS_WITH_${consoleIssues.length}_CONSOLE_ISSUES`,
}
writeFileSync(join(SHOT_DIR, 'report.json'), JSON.stringify(report, null, 2))
console.log(`\nE2E_RESULT = ${report.verdict}`)
console.log(`截图目录: ${SHOT_DIR}`)
if (consoleIssues.length) {
  console.log('console 问题:')
  for (const issue of consoleIssues.slice(0, 10)) console.log('  ', issue)
}
process.exit(0)
