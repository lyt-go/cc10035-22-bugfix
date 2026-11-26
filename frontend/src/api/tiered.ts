/** 冷热分层与跨层导出接口封装。 */
import { request } from './client'

export type TierDay = {
  offset: number
  date: string
  weekday?: string
  tier: 'hot' | 'cold' | 'unavailable'
  tier_name: string
  interval_start: string
  interval_end: string
  read_interval: string
  points: number
  bytes: number
  available: boolean
  schedulable: boolean
}

export type Fragment = {
  fragment_id: string
  index: number
  bytes: number
  sent: boolean
  delivered: boolean
  send_count: number
}

export type Segment = {
  segment_id: string
  date: string
  offset: number
  tier: string
  tier_name: string
  interval_start: string
  interval_end: string
  read_interval: string
  points: number
  bytes: number
  status: string
  state?: string
  state_code?: string
  display_status?: string
  released: boolean
  claimed: boolean
  delivered: boolean
  billable: boolean
  billed_bytes: number
  fragments: Fragment[]
  sent_fragments: number
  delivered_fragments: number
}

export type Billing = {
  billable_days: number
  billable_bytes: number
  billable_points: number
  retrievable_days: number
  retrievable_bytes: number
  actual_available_days: number
  scheduled_days: number
  scheduled_bytes: number
  hot_days: number
  delivered_cold_days: number
  waiting_read_days: number
  unreadable_days: number
  amount: number
}

export type ReadPlan = {
  id?: number
  plan_no: string
  task_id?: number
  task_no?: string
  retention_days: number
  hot_days_configured: number
  hot_days: number
  cold_days: number
  requested_days: number
  schedulable_days: number
  unavailable_days: number
  points_per_day: number
  bytes_per_point: number
  compression: string
  daily_bytes: number
  total_bytes: number
  hot_bytes: number
  cold_bytes: number
  scheduled_bytes: number
  has_conflict: boolean
  conflict: { message: string } | null
  tiers: { tier: string; name: string; days: number; read_interval: string; bytes: number; dates: string[] }[]
  dates: TierDay[]
  segments: Segment[]
  billing: Billing
  message?: string
}

async function postJson<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  return (await response.json()) as T
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

export function fetchCapacity(params: Record<string, string | number>): Promise<ReadPlan & { ok: boolean }> {
  const query = new URLSearchParams(
    Object.fromEntries(Object.entries(params).map(([k, v]) => [k, String(v)])),
  ).toString()
  return getJson(`/api/ts_store/capacity?${query}`)
}

export function createCrossTierExport(body: Record<string, unknown>): Promise<{
  ok: boolean
  message: string
  plan: ReadPlan
  plan_no: string
  task_no: string
  task_id?: number
}> {
  return postJson('/api/tiered-exports', body)
}

export function fetchPlan(ref: string | number): Promise<{ ok: boolean; plan: ReadPlan; read_plan: ReadPlan }> {
  return getJson(`/api/tiered-exports/${encodeURIComponent(String(ref))}`)
}

export function fetchExportDetail(ref: string | number): Promise<{
  ok: boolean
  task: { id: number; 任务编号: string; [k: string]: unknown } | null
  plan: ReadPlan | null
  segments: Segment[]
  billing: Billing | null
  message?: string
}> {
  return getJson(`/api/batch_export/${encodeURIComponent(String(ref))}/detail`)
}

export function claimSegments(
  ref: string | number,
  body: Record<string, unknown>,
): Promise<{ ok: boolean; message: string; segments?: Segment[]; plan?: ReadPlan }> {
  return postJson(`/api/tiered-exports/${encodeURIComponent(String(ref))}/claim`, body)
}

export function segmentAction(
  ref: string | number,
  segmentId: string,
  action: 'read' | 'confirm' | 'complete' | 'claim',
): Promise<{ ok: boolean; message: string; segment?: Segment; billing?: Billing; plan?: ReadPlan }> {
  return postJson(
    `/api/tiered-exports/${encodeURIComponent(String(ref))}/segments/${encodeURIComponent(segmentId)}/${action}`,
  )
}

export function changeRetention(
  ref: string | number,
  body: Record<string, unknown>,
): Promise<{ ok: boolean; message: string; plan?: ReadPlan; conflict?: { message: string } | null }> {
  return postJson(`/api/tiered-exports/${encodeURIComponent(String(ref))}/retention`, body)
}
