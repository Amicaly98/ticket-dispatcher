<template>
  <div>
    <!-- 指标 -->
    <div class="card">
      <div class="card-head">全局指标</div>
      <div class="row met-row">
        <div class="met" v-for="m in metSummary" :key="m.lbl">
          <div class="met-v" :class="m.cls||''">{{ m.val }}</div>
          <div class="met-l">{{ m.lbl }}</div>
        </div>
      </div>
      <div v-if="metReasons.length" class="reason-bar">
        <div v-for="r in metReasons" :key="r.lbl" class="reason-item"
          :title="r.lbl + ': ' + r.val">
          <span class="reason-dot" :style="{background: r.color}"></span>
          <span class="reason-lbl">{{ r.lbl }}</span>
          <span class="reason-val">{{ r.val }}</span>
        </div>
      </div>
    </div>

    <!-- 抢到的票（按活动·场次·票档分类）-->
    <div class="card">
      <div class="card-head">抢到的票（按活动·场次·票档）</div>
      <p class="tip">只统计成功的订单，按活动 → 场次 → 票档归类。</p>
      <div v-if="!securedGroups.length" class="empty">暂无抢到的票。</div>
      <table v-else>
        <thead><tr><th>活动</th><th>场次</th><th>票档</th><th>张数</th><th>购票人</th><th>订单号</th></tr></thead>
        <tbody>
          <tr v-for="g in securedGroups" :key="g.key">
            <td>{{ g.label }}<span v-if="g.project_id" class="dim"> #{{ g.project_id }}</span></td>
            <td>{{ g.screen_name || (g.screen_id ?? '—') }}</td>
            <td>{{ g.sku_desc || (g.sku_id ?? '—') }}</td>
            <td>{{ g.count }}</td>
            <td>{{ g.buyers.length ? g.buyers.join('、') : '—' }}</td>
            <td><span v-for="o in g.orders" :key="o" class="ord-chip">{{ o }}</span><span v-if="!g.orders.length">—</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 结果 -->
    <div class="card">
      <div class="card-head">最近结果</div>
      <p class="tip">每次抢票尝试的记录（含配置简述：账号·票档·购票人）。</p>
      <div v-if="!results.length" class="empty">暂无结果。</div>
      <table v-else>
        <thead><tr><th>时间</th><th>活动</th><th>配置</th><th>Worker</th><th>结果</th><th>订单</th><th>原因</th><th>详情</th></tr></thead>
        <tbody>
          <template v-for="r in results" :key="r.job_id">
            <tr :class="r.success?'r-ok':'r-fail'">
              <td>{{ ts(r.ts) }}</td><td>{{ r.task_id }}</td>
              <td class="cfg">{{ cfgSummary(r) }}</td>
              <td>{{ r.worker_id }}</td>
              <td>{{ r.success?'成功':'失败' }}</td><td>{{ r.order_id||'—' }}</td><td>{{ r.reason||'—' }}</td>
              <td>
                <a v-if="r.detail" href="#" @click.prevent="toggle(r.job_id)">
                  {{ expanded===r.job_id ? '收起' : '展开' }}
                </a>
                <span v-else>—</span>
              </td>
            </tr>
            <tr v-if="expanded===r.job_id" :key="r.job_id+'-d'">
              <td colspan="8"><pre class="det-log">{{ r.detail }}</pre></td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>

    <!-- Worker 事件 -->
    <div class="card">
      <div class="card-head">Worker 事件</div>
      <div v-if="!wkEvts.length" class="empty">暂无。</div>
      <table v-else>
        <thead><tr><th>时间</th><th>Worker</th><th>事件</th><th>详情</th></tr></thead>
        <tbody><tr v-for="(e,i) in wkEvts" :key="i">
          <td>{{ ts(e.ts) }}</td><td>{{ e.worker_id }}</td><td>{{ e.event }}</td><td>{{ e.detail||'—' }}</td>
        </tr></tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useSchedulerStore } from '../stores/scheduler'
const store = useSchedulerStore()
const m = computed(() => store.metrics)

// reason 代码 → 中文短标签
const REASON_LABELS = {
  ok: '成功', cancelled: '已取消', no_result: '无结果',
  sold_out: '售罄', realname_failed: '实名失败', not_logged_in: '未登录',
  ticket_info_failed: '票务信息失败', risk_control: '风控', captcha: '验证码',
  rate_limited: '频繁', not_on_sale: '未开售', timeout: '超时',
  exit_1: '异常退出(1)', exit_137: '异常退出(137)', exit_9: '异常退出(9)',
  exit_130: '中断(130)', bypass_failed: '绕过失败',
  unknown: '未知',
}
const REASON_COLORS = {
  ok: '#22c55e', cancelled: '#94a3b8', no_result: '#94a3b8',
  sold_out: '#ef4444', realname_failed: '#f97316', not_logged_in: '#ef4444',
  ticket_info_failed: '#f97316', risk_control: '#ef4444', captcha: '#eab308',
  rate_limited: '#eab308', not_on_sale: '#94a3b8', timeout: '#f97316',
  unknown: '#94a3b8',
}
function reasonLabel(code) {
  if (!code) return '—'
  if (REASON_LABELS[code]) return REASON_LABELS[code]
  // start_error:xxx / exit_N → 截断
  if (code.startsWith('start_error:')) return '启动失败'
  if (code.startsWith('exit_')) return '异常退出(' + code.slice(5) + ')'
  if (code.startsWith('poll_error:')) return '轮询异常'
  if (code.startsWith('prepare_error:')) return '准备失败'
  return code.length > 12 ? code.slice(0, 12) + '…' : code
}
function reasonColor(code) {
  if (REASON_COLORS[code]) return REASON_COLORS[code]
  if (code && code.startsWith('exit_')) return '#f97316'
  if (code && (code.startsWith('start_error') || code.startsWith('poll_error') || code.startsWith('prepare_error'))) return '#ef4444'
  return '#94a3b8'
}

// 顶部摘要指标（总尝试/成功/成功率）
const metSummary = computed(() => {
  const r = m.value||{}
  return [
    {lbl:'总尝试', val: r.total_attempts||0},
    {lbl:'成功', val: r.success_count||0, cls: 'met-ok'},
    {lbl:'成功率', val: r.success_rate ? (r.success_rate*100).toFixed(1)+'%' : '—',
     cls: r.success_rate > 0.5 ? 'met-ok' : ''},
  ]
})
// reason 分布（独立区域，带颜色标签）
const metReasons = computed(() => {
  const reasons = (m.value||{}).reasons || {}
  return Object.entries(reasons)
    .sort((a,b) => b[1] - a[1])  // 按数量降序
    .map(([k,v]) => ({ lbl: reasonLabel(k), val: v, color: reasonColor(k) }))
})
// 审计数据从 Pinia store 读（切页面不丢）
const results = computed(() => store.auditResults)
const wkEvts = computed(() => store.auditWorkerEvents)
const expanded = ref('')
function toggle(id) { expanded.value = expanded.value === id ? '' : id }

// 抢到的票：按 活动(project_id) → 场次(screen_id) → 票档(sku_id) 三级归类（纯前端聚合）
const securedGroups = computed(() => {
  const groups = {}
  for (const r of results.value) {
    if (!r.success) continue
    const key = `${r.project_id ?? ''}|${r.screen_id ?? ''}|${r.sku_id ?? ''}`
    if (!groups[key]) groups[key] = {
      key, label: r.task_id || ('项目' + (r.project_id ?? '?')),
      project_id: r.project_id, screen_id: r.screen_id, sku_id: r.sku_id,
      screen_name: r.screen_name || '', sku_desc: r.sku_desc || '',
      count: 0, buyers: [], orders: [],
    }
    const g = groups[key]
    g.count += 1
    for (const b of (r.buyer_names || [])) if (b && !g.buyers.includes(b)) g.buyers.push(b)
    if (r.order_id) g.orders.push(r.order_id)
  }
  return Object.values(groups)
})

// 最近结果的配置简述：账号 · 票档 · 购票人
function cfgSummary(r) {
  const parts = []
  if (r.account_label) parts.push(r.account_label)
  if (r.sku_desc) parts.push(r.sku_desc)
  else if (r.sku_id) parts.push('票档' + r.sku_id)
  const bs = r.buyer_names || []
  if (bs.length) parts.push(bs.join('、'))
  return parts.length ? parts.join(' · ') : '—'
}
let _auditTimer = null
onMounted(() => {
  store.refreshAudit()   // 首次拉取
  _auditTimer = setInterval(() => store.refreshAudit(), 5000)  // 每 5 秒刷新
})
onUnmounted(() => { if (_auditTimer) clearInterval(_auditTimer) })
function ts(t) { return t?new Date(t*1000).toLocaleTimeString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}):'' }
</script>

<style scoped>
.met-row { display: flex; gap: 24px; margin-bottom: 14px; }
.met { text-align: center; padding: 8px; }
.met-v { font-size: 1.6em; font-weight: 700; line-height: 1.1; }
.met-ok { color: #22c55e; }
.met-l { font-size: .72em; color: var(--t3); margin-top: 3px; }
.reason-bar { display: flex; flex-wrap: wrap; gap: 8px; }
.reason-item { display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px;
  border: 1px solid var(--border); border-radius: 6px; font-size: .8em; }
.reason-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.reason-lbl { color: var(--t2); }
.reason-val { font-weight: 600; }
.r-ok td { background: #fafafa; }
.r-fail td { background: #f8f8f8; }
.dim { color: var(--t3); font-size: .85em; }
.cfg { color: var(--t2); font-size: .82em; }
.ord-chip { display: inline-block; padding: 1px 6px; margin: 1px 3px 1px 0; border: 1px solid var(--border);
  border-radius: 3px; font-size: .76em; }
.det-log { background: #1e1e1e; color: #d4d4d4; padding: 10px; border-radius: 4px; font-size: .78em;
  max-height: 300px; overflow: auto; white-space: pre-wrap; word-break: break-all; line-height: 1.5; margin: 0; }
</style>
