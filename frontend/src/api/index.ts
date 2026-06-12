const BASE = ''

export async function fetchJson<T = any>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(BASE + url, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`HTTP ${res.status}: ${text}`)
  }
  return res.json()
}

export async function postJson<T = any>(url: string, body?: any): Promise<T> {
  return fetchJson<T>(url, {
    method: 'POST',
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
}

// ── Statistics ──
export const getOverview = (month?: string) =>
  fetchJson('/api/statistics/overview' + (month ? `?month=${month}` : ''))

export const getByRegion = (month?: string) =>
  fetchJson('/api/statistics/by-region' + (month ? `?month=${month}` : ''))

export const getByType = (month?: string) =>
  fetchJson('/api/statistics/by-type' + (month ? `?month=${month}` : ''))

export const getByStatus = (month?: string) =>
  fetchJson('/api/statistics/by-status' + (month ? `?month=${month}` : ''))

export const getMonthlyTrend = (month?: string) =>
  fetchJson('/api/statistics/monthly-trend' + (month ? `?month=${month}` : ''))

export const getTriggers = (month?: string) =>
  fetchJson('/api/statistics/triggers' + (month ? `?month=${month}` : ''))

// ── Work Orders ──
export const getWorkOrders = (params: Record<string, any> = {}) => {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') qs.set(k, String(v))
  }
  return fetchJson('/api/work-orders?' + qs.toString())
}

export const getPendingWorkOrders = () =>
  fetchJson('/api/work-orders/pending')

// ── Monitor ──
export const getDispatchPending = (refresh = false) =>
  fetchJson('/api/monitor/dispatch-pending' + (refresh ? '?refresh=true' : ''))

export const manualDispatch = (recordId: string) =>
  postJson(`/api/monitor/dispatch/${recordId}`)

export const getEmailPending = (refresh = false) =>
  fetchJson('/api/monitor/email-pending' + (refresh ? '?refresh=true' : ''))

export const getEmailToolUrl = () =>
  fetchJson('/api/monitor/email-tool-url')

// ── Email Tool ──
export const extractPdfInfo = (formData: FormData) =>
  fetch('/api/email-tool/extract', { method: 'POST', body: formData }).then(r => r.json())

export const reExtractPdfInfo = (fileIds: string) =>
  fetch('/api/email-tool/re-extract', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: `file_ids=${encodeURIComponent(fileIds)}` }).then(r => r.json())

export const convertNameToEmail = (name: string) =>
  fetch('/api/email-tool/convert-name', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: `name=${encodeURIComponent(name)}` }).then(r => r.json())

export const sendInspectionEmail = (data: { to_emails: string; subject: string; body: string; file_ids: string; cc_emails?: string; record_id?: string; customer?: string; product?: string }) =>
  fetch('/api/email-tool/send', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams(data as any).toString(),
  }).then(r => r.json())

export const getEmailConfig = () =>
  fetchJson('/api/email-tool/config')

export const fetchAitableAttachments = (recordId: string) =>
  fetchJson(`/api/email-tool/fetch-aitable-attachments/${recordId}`)

// ── Email Pre-Analysis ──
export const getPreAnalysisStatus = () =>
  fetchJson('/api/email-tool/pre-analysis')

export const runPreAnalysis = () =>
  fetch('/api/email-tool/pre-analysis/run', { method: 'POST' }).then(r => r.json())

export const reAnalyzeRecord = (recordId: string) =>
  fetch(`/api/email-tool/pre-analysis/re-analyze/${recordId}`, { method: 'POST' }).then(r => r.json())

export const sendDirectEmail = (data: { record_id: string; extra_emails?: string }) =>
  fetch('/api/email-tool/send-direct', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams(data as any).toString(),
  }).then(r => r.json())

export const previewEmailContent = (recordId: string) =>
  fetchJson(`/api/email-tool/preview/${recordId}`)

export const triggerClosureCheck = () =>
  postJson('/api/monitor/closure-check')

export const syncClosureStatus = () =>
  postJson('/api/monitor/sync-closure-status')

// ── Triggers ──
export const triggerYunji = (id: string) => postJson(`/api/triggers/yunji/${id}`)

export const triggerEmail = (id: string) => postJson(`/api/triggers/email/${id}`)

export const getTriggerLogs = (limit = 50) =>
  fetchJson(`/api/triggers/logs?limit=${limit}`)

// ── Sync ──
export const runSync = (syncMonth?: string) =>
  postJson('/api/sync/run' + (syncMonth ? `?sync_month=${syncMonth}` : ''))

export const pushToDingtalk = (syncMonth?: string) =>
  postJson('/api/sync/push' + (syncMonth ? `?sync_month=${syncMonth}` : ''))

export const batchPushToDingtalk = (data: { work_order_ids: string[] }) =>
  postJson('/api/sync/batch-push', data)

export const adjustPlannedCompletion = (data: { work_order_ids?: string[]; month?: string }) =>
  postJson('/api/work-orders/adjust-planned-completion', data)

export const getSyncLogs = (limit = 50) =>
  fetchJson(`/api/sync/logs?limit=${limit}`)

// ── Health ──
export const getHealth = () => fetchJson('/api/health')

// ── WebSocket ──
export function createEventStream(onEvent: (type: string, data: any) => void) {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const ws = new WebSocket(`${protocol}//${location.host}/api/ws`)
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data)
      onEvent(msg.type, msg.data)
    } catch {}
  }
  ws.onerror = () => {}
  ws.onclose = () => {
    // Auto reconnect after 5s
    setTimeout(() => createEventStream(onEvent), 5000)
  }
  return ws
}
