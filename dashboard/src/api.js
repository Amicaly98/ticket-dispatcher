/**
 * API 客户端：通过 Vite proxy 转发到 FastAPI 后端。
 * 开发模式：/api/* → http://localhost:8000/*
 */
const API_KEY = import.meta.env.VITE_API_KEY || ''

function headers() {
  const h = { 'Content-Type': 'application/json' }
  if (API_KEY) h['X-API-Key'] = API_KEY
  return h
}

async function get(path) {
  const res = await fetch(`/api${path}`, { headers: headers() })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

async function post(path, body) {
  const res = await fetch(`/api${path}`, {
    method: 'POST', headers: headers(), body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

async function del(path) {
  const res = await fetch(`/api${path}`, { method: 'DELETE', headers: headers() })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

export const api = {
  workers:      () => get('/workers'),
  worker:       (id) => get(`/workers/${id}`),
  workerCreate: (data) => post('/workers', data),
  workerDelete: (id) => del(`/workers/${id}`),
  workerAssign: (id, data) => post(`/workers/${id}/assign`, data),
  workerStop:   (id) => post(`/workers/${id}/stop`, {}),
  workerPause:  (id) => post(`/workers/${id}/pause`, {}),
  workerResume: (id) => post(`/workers/${id}/resume`, {}),
  workerWatchLogs: (id, ttl = 30) => post(`/workers/${id}/logs/watch?ttl=${ttl}`, {}),
  accounts:    () => get('/accounts'),
  accountDelete: (id) => del(`/accounts/${id}`),
  accountSave:  (data) => post('/accounts/save', data),
  status:      () => get('/status'),
  metrics:     () => get('/metrics'),
  historyResults:  (limit = 50) => get(`/history/results?limit=${limit}`),
  historyWorkers:  (workerId, limit = 50) => get(`/history/workers?worker_id=${workerId || ''}&limit=${limit}`),
  events:      (n = 100) => get(`/events?n=${n}`),

  // Bilibili 平台辅助（通过 src/platform/bilibili.py 扩展注册）
  bilibiliQrGen:      () => post('/bilibili/qr/gen', {}),
  bilibiliQrPoll:     (key) => post('/bilibili/qr/poll', {qrcode_key: key}),
  bilibiliEventInfo:  (id) => post('/bilibili/event/info', {project_id: Number(id)}),
  bilibiliEventStock: (id) => post('/bilibili/event/stock', {project_id: Number(id)}),
  bilibiliCheckLogin: (uid) => post('/bilibili/check/login', {uid}),
  bilibiliCheckLoginWithCookie: async (sessData) => {
    const res = await fetch('/api/bilibili/check/login/cookie', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({sess_data: sessData}),
    })
    return res.json()
  },
  bilibiliBuyerList: (accountId) => post('/bilibili/buyer/list', {account_id: accountId}),
  accountRefresh: (accountId) => post('/bilibili/account/refresh', {account_id: accountId}),
}

export function connectWs(onEvent, onStateChange) {
  const wsUrl = `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`
  const ws = new WebSocket(wsUrl)
  ws.onopen = () => onStateChange?.(true)
  ws.onmessage = (e) => {
    try { onEvent(JSON.parse(e.data)) } catch (err) { console.warn('[ws] 丢弃无法解析的事件消息', err) }
  }
  ws.onerror = () => {}   // onclose 会触发，这里只抑制未捕获错误日志
  ws.onclose = () => {
    onStateChange?.(false)
    setTimeout(() => connectWs(onEvent, onStateChange), 3000)
  }
  return ws
}
