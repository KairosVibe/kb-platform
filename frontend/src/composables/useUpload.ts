/**
 * H32 frontend.expand_drop（FUNCTION-MAP §4 / FRONTEND-SPEC §4.3）
 *
 * 处理逻辑：递归枚举目录 → 保留展示路径 → 清单预校验。
 * 两类进度必须分开（FUNCTION-MAP §2.4）：
 * - 传输进度：XMLHttpRequest.upload.onprogress；
 * - 解析/索引进度：由 F-03.03 轮询，**不得把传输完成当索引完成**。
 */

import { apiUrl, currentAccessToken, defaultMessage } from '@/api/client'
import type { ApiEnvelope, BatchAccepted, UploadAccepted } from '@/api/types'

export interface DropEntry {
  file: File
  /** 仅用于展示与分组，绝不作为磁盘路径（API-CONTRACTS §3） */
  relative_path: string
}

export const MAX_FILE_BYTES = 20 * 1024 * 1024
export const MAX_BATCH_FILES = 100
export const MAX_BATCH_BYTES = 200 * 1024 * 1024
export const UPLOAD_CONCURRENCY = 3

/** 相对路径预校验：拒绝绝对路径、盘符、`..` 与 NUL（API-CONTRACTS §3） */
export function isSafeRelativePath(path: string): boolean {
  if (!path) return false
  if (path.includes('\0')) return false
  if (path.startsWith('/') || path.startsWith('\\')) return false
  if (/^[a-zA-Z]:/.test(path)) return false
  const parts = path.split(/[/\\]/)
  return parts.every((p) => p !== '..' && p !== '')
}

type FileSystemEntryLike = {
  isFile: boolean
  isDirectory: boolean
  name: string
  file?: (cb: (f: File) => void, err?: (e: unknown) => void) => void
  createReader?: () => { readEntries: (cb: (entries: FileSystemEntryLike[]) => void, err?: (e: unknown) => void) => void }
}

function readEntries(reader: ReturnType<NonNullable<FileSystemEntryLike['createReader']>>): Promise<FileSystemEntryLike[]> {
  return new Promise((resolve, reject) => {
    const all: FileSystemEntryLike[] = []
    const next = (): void => {
      reader.readEntries((batch) => {
        if (batch.length === 0) {
          resolve(all)
          return
        }
        all.push(...batch)
        next()
      }, reject)
    }
    next()
  })
}

function readFile(entry: FileSystemEntryLike): Promise<File> {
  return new Promise((resolve, reject) => {
    entry.file?.((f) => resolve(f), reject)
  })
}

/** 递归枚举拖拽条目；目录时保留相对路径（仅作展示元数据） */
export async function expandDrop(entries: DropEntry[] | DataTransferItem[] | FileList): Promise<DropEntry[]> {
  // 普通 FileList / File[]：直接取 name
  if (entries instanceof FileList) {
    return Array.from(entries).map((file) => ({ file, relative_path: file.name }))
  }

  if (Array.isArray(entries) && entries.length > 0 && (entries as DropEntry[])[0]?.file instanceof File) {
    return (entries as DropEntry[]).filter((e) => isSafeRelativePath(e.relative_path))
  }

  const items = entries as DataTransferItem[]
  const out: DropEntry[] = []

  for (const item of items) {
    const anyItem = item as unknown as { webkitGetAsEntry?: () => FileSystemEntryLike | null }
    const entry = typeof anyItem.webkitGetAsEntry === 'function' ? anyItem.webkitGetAsEntry() : null
    if (!entry) {
      const file = item.getAsFile()
      if (file) out.push({ file, relative_path: file.name })
      continue
    }
    await walk(entry, '', out)
  }

  return out.filter((e) => isSafeRelativePath(e.relative_path))
}

async function walk(entry: FileSystemEntryLike, prefix: string, out: DropEntry[]): Promise<void> {
  const path = prefix ? `${prefix}/${entry.name}` : entry.name
  if (entry.isFile) {
    const file = await readFile(entry)
    out.push({ file, relative_path: path })
    return
  }
  if (entry.isDirectory && entry.createReader) {
    const reader = entry.createReader()
    const children = await readEntries(reader)
    for (const child of children) {
      await walk(child, path, out)
    }
  }
}

/** 清单预校验：文件数、总大小、单文件大小、相对路径安全 */
export interface ManifestCheck {
  ok: boolean
  errors: string[]
  totalBytes: number
}

export function validateManifest(entries: DropEntry[]): ManifestCheck {
  const errors: string[] = []
  let totalBytes = 0

  if (entries.length === 0) errors.push('请选择至少一个文件')
  if (entries.length > MAX_BATCH_FILES) errors.push(`单批次最多 ${MAX_BATCH_FILES} 个文件`)

  for (const e of entries) {
    if (!isSafeRelativePath(e.relative_path)) {
      errors.push(`路径不合法：${e.relative_path}`)
      continue
    }
    if (e.file.size > MAX_FILE_BYTES) {
      errors.push(`单文件超过 20 MiB：${e.relative_path}`)
    }
    totalBytes += e.file.size
  }

  if (totalBytes > MAX_BATCH_BYTES) errors.push('批次总大小超过 200 MiB')
  return { ok: errors.length === 0, errors, totalBytes }
}

/* ---------- 传输层：XHR（唯一能拿到真实上传进度的通道） ---------- */

function parseEnvelope<T>(text: string, status: number): ApiEnvelope<T> | null {
  try {
    return JSON.parse(text) as ApiEnvelope<T>
  } catch {
    return null
  }
}

function xhrSend<T>(
  url: string,
  form: FormData,
  onProgress?: (percent: number) => void,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', url, true)

    const token = currentAccessToken()
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)

    if (onProgress && xhr.upload) {
      xhr.upload.onprogress = (evt) => {
        if (evt.lengthComputable) onProgress(Math.round((evt.loaded / evt.total) * 100))
      }
    }

    xhr.onload = () => {
      const env = parseEnvelope<T>(xhr.responseText, xhr.status)
      if (xhr.status >= 200 && xhr.status < 300 && env && env.code === 'OK') {
        resolve(env.data)
        return
      }
      reject(new Error(env?.message || defaultMessage(xhr.status)))
    }
    xhr.onerror = () => reject(new Error(defaultMessage(0)))
    xhr.ontimeout = () => reject(new Error(defaultMessage(504)))

    xhr.send(form)
  })
}

const uuid = (): string =>
  typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `uuid-${Date.now()}-${Math.random().toString(16).slice(2)}`

/** F-03.01 单文件上传 —— client_upload_id 幂等键由调用方持有，重试复用同一值 */
export function uploadSingle(
  file: File,
  category: string,
  clientUploadId: string,
  onProgress?: (percent: number) => void,
): Promise<UploadAccepted> {
  const form = new FormData()
  form.append('file', file)
  form.append('category', category)
  form.append('client_upload_id', clientUploadId)
  return xhrSend<UploadAccepted>(apiUrl('/uploads'), form, onProgress)
}

/** F-03.02 批量上传 —— files 与 manifest.items 长度与顺序严格匹配 */
export function uploadBatch(
  entries: DropEntry[],
  category: string,
  clientBatchId: string,
  onProgress?: (percent: number) => void,
): Promise<BatchAccepted> {
  const form = new FormData()
  const items = entries.map((e) => ({
    client_file_id: uuid(),
    relative_path: e.relative_path,
    category,
  }))

  for (const e of entries) form.append('files', e.file, e.file.name)

  form.append(
    'manifest',
    JSON.stringify({ client_batch_id: clientBatchId, items }),
  )

  return xhrSend<BatchAccepted>(apiUrl('/upload-batches'), form, onProgress)
}

/** 并发池：前端并发上限 3（API-CONTRACTS §3） */
export async function runWithConcurrency<T>(
  tasks: Array<() => Promise<T>>,
  limit = UPLOAD_CONCURRENCY,
): Promise<Array<PromiseSettledResult<T>>> {
  const results: Array<PromiseSettledResult<T>> = new Array(tasks.length)
  let cursor = 0

  const worker = async (): Promise<void> => {
    for (;;) {
      const index = cursor
      cursor += 1
      if (index >= tasks.length) return
      try {
        results[index] = { status: 'fulfilled', value: await tasks[index]!() }
      } catch (reason) {
        results[index] = { status: 'rejected', reason }
      }
    }
  }

  const workers = Array.from({ length: Math.min(limit, tasks.length) }, () => worker())
  await Promise.all(workers)
  return results
}

export { uuid as newIdempotencyKey }
