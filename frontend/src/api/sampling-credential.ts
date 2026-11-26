/** 采样凭据包接口封装：轮次采样、定稿、凭据申请、签发与下载。 */
import { request } from './client'

const BASE = '/api/sampling-credential'

export type WindowAgg = {
  window_index: number
  window_start: string
  window_end: string
  sample_count: number
  value: number | null
}

export type RoundSummary = {
  round_no: number
  status: string
  reason: string | null
  opened_at: string | null
  settled_at: string | null
  sample_total: number
  windows: WindowAgg[] | null
}

export type ReadingLedger = {
  timestamp: string
  value: number
  arrived_round: number
  window_index: number | null
  included: boolean
}

export type Snapshot = {
  task_id: string
  round_no: number
  source_point: string
  granularity: string
  agg_function: string
  period_start: string
  period_end: string
  windows: (WindowAgg & { samples: { timestamp: string; value: number }[] })[]
  readings: { timestamp: string; value: number; window_index: number }[]
  excluded_count: number
  settled_at: string
}

export type PackageSummary = {
  package_id: string
  task_id: string
  status: string
  applied_at: string
  issued_at: string | null
  adopted_round: number | null
  pending_reason: string | null
  fingerprint: string | null
  filename: string | null
}

export type TaskDetail = {
  task: {
    task_id: string
    source_point: string
    granularity: string
    agg_function: string
    period_start: string
    period_end: string
  }
  rounds: RoundSummary[]
  latest_settled_round: number | null
  open_round: RoundSummary | null
  waiting_reason: string | null
  latest_snapshot: Snapshot | null
  readings_ledger: ReadingLedger[]
  packages: PackageSummary[]
}

async function call(path: string, init?: RequestInit) {
  const response = await request(path, init)
  if (!response.ok) {
    let message = `接口返回 ${response.status}`
    try {
      const body = await response.json()
      if (body?.detail) message = body.detail
    } catch {
      // 非 JSON 错误体，保留默认提示
    }
    throw new Error(message)
  }
  return response.json()
}

export function resetState() {
  return call(`${BASE}/reset`, { method: 'POST' })
}

export function fetchTaskDetail(taskId: string): Promise<TaskDetail> {
  return call(`${BASE}/tasks/${encodeURIComponent(taskId)}`)
}

export function ingestReading(taskId: string, timestamp: string, value: number) {
  return call(`${BASE}/tasks/${encodeURIComponent(taskId)}/readings`, {
    method: 'POST',
    body: JSON.stringify({ timestamp, value }),
  })
}

export function settleRound(taskId: string) {
  return call(`${BASE}/tasks/${encodeURIComponent(taskId)}/rounds/settle`, {
    method: 'POST',
  })
}

export function applyPackage(taskId: string) {
  return call(`${BASE}/tasks/${encodeURIComponent(taskId)}/packages`, {
    method: 'POST',
  })
}

export function issuePackage(packageId: string) {
  return call(`${BASE}/packages/${encodeURIComponent(packageId)}/issue`, {
    method: 'POST',
  })
}

export async function downloadPackage(packageId: string): Promise<void> {
  const response = await request(`${BASE}/packages/${encodeURIComponent(packageId)}/download`)
  if (!response.ok) {
    let message = '材料下载失败'
    try {
      const body = await response.json()
      if (body?.detail) message = body.detail
    } catch {
      // 忽略非 JSON 错误体
    }
    throw new Error(message)
  }
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const matched = disposition.match(/filename="([^"]+)"/)
  const filename = matched ? matched[1] : `${packageId}.csv`
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}
