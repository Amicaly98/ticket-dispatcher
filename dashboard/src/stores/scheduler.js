import { defineStore } from 'pinia'
import { api, connectWs } from '../api'

export const useSchedulerStore = defineStore('scheduler', {
  state: () => ({
    workers: {},
    accounts: {},
    metrics: {},
    events: [],
    workerLogs: {},   // worker_id -> [{ts, line, error}]，实时黑盒日志（环形，限长）
    connected: false,
    error: null,
    // 审计数据（提升到 Pinia store，切页面不丢）
    auditResults: [],
    auditWorkerEvents: [],
  }),

  actions: {
    async start() {
      try {
        await this.refreshAll()
        this.connected = true
        this.error = null
      } catch (e) {
        this.error = e.message
      }
      connectWs(
        (ev) => this.handleEvent(ev),
        (ok) => { this.connected = ok },
      )
      setInterval(() => this.refreshAll(), 2000)
    },

    async refreshAll() {
      try {
        const [workers, accounts, metrics] = await Promise.all([
          api.workers(), api.accounts(), api.metrics(),
        ])
        this.workers = workers
        this.accounts = accounts
        this.metrics = metrics
      } catch (e) {
        this.error = e.message
      }
    },

    /** 拉审计历史（切页面不丢，定时刷新用） */
    async refreshAudit() {
      try {
        const [results, wkEvts] = await Promise.all([
          api.historyResults(50), api.historyWorkers('', 50),
        ])
        this.auditResults = results
        this.auditWorkerEvents = wkEvts
      } catch (e) { /* 静默 */ }
    },

    handleEvent(ev) {
      // worker_log：实时黑盒日志，单独进环形缓冲，不进通用事件流、不触发刷新
      if (ev.type === 'worker_log') {
        this.appendLogs(ev.worker_id, (ev.lines || []).map(line => ({ ts: ev.ts, line })))
        return
      }
      this.events.push(ev)
      if (this.events.length > 500) this.events.shift()
      // worker_error 也镜像进该 worker 的日志缓冲（红色标记）
      if (ev.type === 'worker_error') {
        this.appendLogs(ev.worker_id, [{ ts: ev.ts, line: `[错误] ${ev.reason}: ${ev.detail || ''}`, error: true }])
      }
      // 派发/结果/worker 变化 → 立即刷新 worker 状态
      if (['job_dispatched', 'job_result', 'worker_stop_requested',
           'worker_registered', 'worker_offline', 'worker_error',
           'worker_declared', 'worker_deleted'].includes(ev.type)) {
        this.refreshAll()
      }
      // 新结果到达 → 立即刷新审计数据
      if (ev.type === 'job_result') {
        this.refreshAudit()
      }
    },

    appendLogs(workerId, entries) {
      if (!workerId || !entries.length) return
      const buf = this.workerLogs[workerId] || []
      buf.push(...entries)
      // 环形：每个 worker 最多留 500 行
      if (buf.length > 500) buf.splice(0, buf.length - 500)
      this.workerLogs[workerId] = buf
    },
  },
})
