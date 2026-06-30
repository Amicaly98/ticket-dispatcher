<template>
  <div>
    <div class="card">
      <div class="card-head">
        <span>Worker 控制台</span>
        <button class="btn btn-dark" style="float:right; font-size:.8em; padding:4px 12px" @click="openCreate">+ 创建 Worker</button>
      </div>
      <p class="tip" style="margin-bottom:14px">
        手动把配好的抢票单指派给指定 worker。空闲 = 待命，忙碌 = 正在抢，离线 = 心跳超时。
        抢到/失败/停止后自动回到空闲。号-worker 自由指派，无限制。
      </p>

      <div v-if="!list.length" class="empty">暂无 Worker。点「创建 Worker」声明一个，或在那台机器起 <code>python main.py worker</code> 自注册。</div>

      <div class="wk-grid">
        <div v-for="w in list" :key="w.worker_id" class="wk" :class="w.status">
          <div class="wk-top">
            <span class="wk-id">{{ w.worker_id }}</span>
            <span class="tag" :class="'tag-'+w.status">{{ stText(w.status) }}</span>
          </div>

          <div class="wk-rows">
            <div class="wk-row"><span class="wk-lbl">程序</span><span class="wk-val">{{ w.program || '—' }}</span></div>
            <div class="wk-row"><span class="wk-lbl">并发</span><span class="wk-val">{{ w.busy_slots||0 }} / {{ w.max_slots||'?' }}</span></div>
            <div class="wk-row"><span class="wk-lbl">出口</span><span class="wk-val">{{ w.public_ip || '—' }}</span></div>
            <div class="wk-row"><span class="wk-lbl">状态</span>
              <span class="wk-val" :class="statusCls(w)" :title="statusTitle(w)"
                @click="w.last_result && w.last_result.detail && (detailFor = w.worker_id)"
                :style="w.last_result && w.last_result.detail ? 'cursor:pointer; text-decoration:underline dotted' : ''">
                {{ statusTxt(w) }}
              </span>
            </div>
            <div class="wk-row"><span class="wk-lbl">心跳</span><span class="wk-val" :class="hbCls(w.ts)">{{ hbTxt(w.ts) }}</span></div>
          </div>

          <!-- 当前在抢什么（富信息）-->
          <div v-if="w.current" class="wk-current">
            <div class="cur-line"><b>{{ w.current.label || ('项目'+w.current.project_id) }}</b></div>
            <div class="cur-meta">
              <span v-if="w.current.screen_name">{{ w.current.screen_name }}</span>
              <span v-if="w.current.sku_desc"> · {{ w.current.sku_desc }}</span>
              <span v-if="!w.current.screen_name && !w.current.sku_desc && w.current.screen_id">
                场次 {{ w.current.screen_id }} · 票档 {{ w.current.sku_id }}
              </span>
            </div>
            <div class="cur-meta">
              <span v-if="w.current.account">{{ w.current.account }}</span>
              <span v-if="w.current.buyers && w.current.buyers.length"> · 购票人 {{ w.current.buyers.join('、') }}</span>
            </div>
            <div class="cur-meta">
              <span v-if="w.current.assigned_at">已指派 {{ elapsed(w.current.assigned_at) }}</span>
              <span v-if="w.current.started_at"> · 已运行 {{ elapsed(w.current.started_at) }}</span>
              <span v-if="w.current.deadline" class="cur-deadline"> · 开票 {{ fmtDeadline(w.current.deadline) }}</span>
            </div>
          </div>
          <div v-else class="wk-current wk-idle-hint">空闲 · 待命中</div>

          <div class="wk-btns">
            <button class="btn btn-dark" @click="openAssign(w.worker_id)" :disabled="w.status==='offline'||w.paused">指派</button>
            <button class="btn btn-danger" @click="stop(w.worker_id)" :disabled="w.status!=='busy'">停止</button>
            <button class="btn" @click="togglePause(w)" :disabled="w.status==='offline'"
              :title="w.paused ? '恢复消费新单' : '暂停接新单（在飞任务继续）'">{{ w.paused ? '恢复' : '暂停' }}</button>
            <button class="btn" @click="logFor = w.worker_id">日志</button>
            <button class="btn" @click="removeWorker(w)" title="从控制台移除（不会杀远端进程）">删除</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 创建 Worker 弹窗 -->
    <div v-if="creating" class="modal-mask" @click.self="closeCreate">
      <div class="modal" style="width:460px">
        <div class="card-head">创建 Worker（声明）</div>
        <p class="tip" style="margin-bottom:14px">
          只是登记一个 worker（占离线位 + 设默认值）。它真正上线还要在那台机器上起进程——
          创建后会给你那条启动命令。
        </p>
        <p v-if="createError" class="buyer-error">{{ createError }}</p>
        <div class="field">
          <label>Worker ID</label>
          <input v-model="newWorker.worker_id" placeholder="如 worker_cloud" />
        </div>
        <div class="field">
          <label>程序</label>
          <input v-model="newWorker.program" placeholder="Driver 名称（如 dryrun）" />
        </div>
        <div class="field">
          <label>并发 slot 数</label>
          <input v-model="newWorker.max_slots" placeholder="1" />
        </div>
        <div class="field">
          <label>出口 IP（可选，仅展示）</label>
          <input v-model="newWorker.public_ip" placeholder="1.2.3.4" />
        </div>
        <p v-if="createHint" class="hint-box">{{ createHint }}</p>
        <div class="actions">
          <button class="btn" @click="closeCreate">{{ createHint ? '关闭' : '取消' }}</button>
          <button v-if="!createHint" class="btn btn-dark" :disabled="!newWorker.worker_id" @click="doCreate">创建</button>
        </div>
      </div>
    </div>

    <!-- 指派弹窗 -->
    <div v-if="assignFor" class="modal-mask" @click.self="closeAssign">
      <div class="modal">
        <div class="card-head">指派给 {{ assignFor }}</div>
        <p v-if="assignError" class="buyer-error">{{ assignError }}</p>

        <!-- 1. 账号 -->
        <div class="field">
          <label>账号（号池）</label>
          <select v-model="selAccount" @change="onAccountChange">
            <option value="">选择账号</option>
            <option v-for="a in accounts" :key="a.id" :value="a.id">{{ a.username || a.id }} (uid={{ a.uid }})</option>
          </select>
        </div>

        <!-- 2. 活动 ID + 加载场次票档 -->
        <div class="field">
          <label>活动 ID</label>
          <div class="quick-picks">
            <button class="btn btn-quick" @click="quickPick('1001701')">BML</button>
            <button class="btn btn-quick" @click="quickPick('1001653')">BW</button>
          </div>
          <div style="display:flex; gap:8px">
            <input v-model="projectId" placeholder="project_id（如 1001742）" style="flex:1" />
            <button class="btn" @click="loadEvent" :disabled="!projectId || loadingInfo">
              {{ loadingInfo ? '加载中…' : '加载场次' }}
            </button>
          </div>
          <span v-if="infoError" style="color:#c0392b; font-size:.78em">{{ infoError }}</span>
        </div>

        <!-- 3. 场次 + 票档 -->
        <div class="field" v-if="screens.length">
          <label>场次 + 票档</label>
          <div class="sku-grid">
            <div v-for="s in screens" :key="s.screen_id" class="sku-group">
              <div class="sku-screen-label">{{ s.name }}</div>
              <div v-for="sku in s.skus" :key="sku.sku_id"
                class="sku-item" :class="{selected: isSku(s.screen_id, sku.sku_id), 'pre-sale': !sku.clickable}"
                @click="pickSku(s, sku)">
                <span>{{ sku.desc || '票档'+sku.sku_id }}</span>
                <span class="sku-price" v-if="sku.price">{{ (sku.price/100).toFixed(0) }}元</span>
                <span class="sku-stock">{{ sku.stock }}</span>
                <span v-if="!sku.clickable" class="sku-presale">未开售</span>
              </div>
            </div>
          </div>
        </div>

        <!-- 4. 购票人 -->
        <div class="field" v-if="selAccount">
          <label>购票人（可多选）</label>
          <div v-if="buyerError" class="buyer-error">
            {{ buyerError }}
            <button class="btn" style="margin-left:8px" @click="loadBuyers">重试</button>
          </div>
          <div v-else-if="buyerList.length" class="buyer-grid">
            <div v-for="b in buyerList" :key="b.id" class="buyer-item"
              :class="{selected: selBuyers.includes(b.id)}" @click="toggleBuyer(b.id)">
              <span>{{ b.name }}</span><span class="buyer-tel">{{ b.tel }}</span>
            </div>
          </div>
          <button v-else class="btn" @click="loadBuyers" :disabled="loadingBuyers">
            {{ loadingBuyers ? '加载中…' : '加载购票人' }}
          </button>
        </div>

        <!-- 5. 蹲票模式 -->
        <div class="field" v-if="selAccount">
          <label style="display:flex; align-items:center; gap:8px; cursor:pointer">
            <input type="checkbox" v-model="enableCheckStock" />
            蹲票模式（已开售无票时自动查库存等退票/补票，有票立即下单）
          </label>
          <div v-if="enableCheckStock" style="display:flex; align-items:center; gap:8px; margin-top:6px">
            <span style="font-size:.82em; color:#888">查库存间隔（秒）</span>
            <input v-model="checkStockInterval" type="number" min="1" max="60" style="width:60px" />
          </div>
        </div>

        <div class="actions">
          <button class="btn" @click="closeAssign">取消</button>
          <button class="btn btn-dark" :disabled="!canAssign" @click="doAssign">
            指派（{{ selBuyers.length }} 人）
          </button>
        </div>
      </div>
    </div>

    <!-- 失败详情弹窗 -->
    <div v-if="detailFor" class="modal-mask" @click.self="detailFor = ''">
      <div class="modal">
        <div class="card-head">{{ detailFor }} · 上次失败详情</div>
        <div v-if="detailResult" class="det-body">
          <div class="det-line"><span class="det-lbl">原因</span><span>{{ detailResult.reason || '—' }}</span></div>
          <div class="det-line" v-if="detailResult.order_id"><span class="det-lbl">订单</span><span>{{ detailResult.order_id }}</span></div>
          <div class="det-line"><span class="det-lbl">时间</span><span>{{ fmtTs(detailResult.ts) }}</span></div>
          <label style="font-size:.82em; font-weight:600; margin:10px 0 4px; display:block">日志尾巴</label>
          <pre class="det-log">{{ detailResult.detail || '（无详细输出）' }}</pre>
        </div>
        <div class="actions">
          <button class="btn" @click="detailFor = ''">关闭</button>
        </div>
      </div>
    </div>

    <!-- 实时日志弹窗 -->
    <div v-if="logFor" class="modal-mask" @click.self="logFor = ''">
      <div class="modal modal-wide">
        <div class="card-head">{{ logFor }} · 实时日志（黑盒输出）</div>
        <pre class="det-log log-live" ref="logPre">{{ logText }}</pre>
        <div class="actions">
          <label style="margin-right:auto; font-size:.8em; color:var(--t3)">
            <input type="checkbox" v-model="logAutoScroll" /> 自动滚动到底部
          </label>
          <button class="btn" @click="clearLogs(logFor)">清空</button>
          <button class="btn" @click="logFor = ''">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick } from 'vue'
import { api } from '../api'
import { useSchedulerStore } from '../stores/scheduler'

const store = useSchedulerStore()
const list = computed(() => Object.entries(store.workers).map(([id, w]) => ({ worker_id: id, ...w })).sort((a, b) => a.worker_id.localeCompare(b.worker_id)))
const accounts = computed(() => Object.entries(store.accounts || {}).map(([id, a]) => ({ id, ...a })))

// 创建 Worker 表单
const creating = ref(false)
const createError = ref('')
const createHint = ref('')
const newWorker = ref({ worker_id: '', program: '', max_slots: '1', public_ip: '' })

function openCreate() {
  creating.value = true; createError.value = ''; createHint.value = ''
  newWorker.value = { worker_id: '', program: '', max_slots: '1', public_ip: '' }
}
function closeCreate() { creating.value = false }

async function doCreate() {
  createError.value = ''
  try {
    const r = await api.workerCreate({
      worker_id: newWorker.value.worker_id.trim(),
      program: newWorker.value.program,
      max_slots: Number(newWorker.value.max_slots || 1),
      public_ip: newWorker.value.public_ip.trim(),
    })
    createHint.value = r.hint || '已创建。去 worker 机器上起进程即可上线。'
    store.refreshAll()
  } catch (e) {
    createError.value = '创建失败: ' + (e.message || e)
  }
}

async function removeWorker(w) {
  const tip = w.status === 'offline'
    ? `删除 worker「${w.worker_id}」？（仅从控制台移除）`
    : `「${w.worker_id}」仍在线/活跃。删除只移除控制台条目，不会杀掉那台机器上的进程——它下次心跳会重新出现。确定删除？`
  if (!confirm(tip)) return
  try { await api.workerDelete(w.worker_id); store.refreshAll() }
  catch (e) { alert('删除失败: ' + (e.message || e)) }
}

const detailFor = ref('')        // 查看失败 detail 的 worker_id（空 = 不显示）
const detailResult = computed(() => {
  const w = list.value.find(x => x.worker_id === detailFor.value)
  return w ? w.last_result : null
})

const logFor = ref('')           // 查看实时日志的 worker_id（空 = 不显示）
const logAutoScroll = ref(true)
const logText = computed(() => {
  const buf = store.workerLogs[logFor.value] || []
  if (!buf.length) return '（暂无日志。worker 拉起黑盒后实时输出会出现在这里。）'
  return buf.map(e => (e.error ? '⚠ ' : '') + e.line).join('\n')
})
function clearLogs(id) { store.workerLogs[id] = [] }

const logPre = ref(null)
watch(logText, () => {
  if (!logAutoScroll.value) return
  nextTick(() => { if (logPre.value) logPre.value.scrollTop = logPre.value.scrollHeight })
})

// 日志按需订阅：打开面板时注册 watch（续期 TTL），关闭时停止。
// RedisBus 下 worker 仅在被 watch 时才推 worker_log（100 个 worker 不再同时洪流）；
// InMemoryBus 恒推（no-op），不影响单机开发。
let _watchTimer = null
watch(logFor, (to, from) => {
  if (_watchTimer) { clearInterval(_watchTimer); _watchTimer = null }
  if (to) {
    api.workerWatchLogs(to).catch(() => {})
    _watchTimer = setInterval(() => api.workerWatchLogs(to).catch(() => {}), 15000)
  }
})

// 指派表单状态
const assignFor = ref('')        // 目标 worker_id（空 = 弹窗关闭）
const assignError = ref('')
const selAccount = ref('')
const projectId = ref('')
const screens = ref([])
const loadingInfo = ref(false)
const infoError = ref('')
const selScreen = ref('')
const selSku = ref('')
const selSkuMeta = ref({})
const buyerList = ref([])
const loadingBuyers = ref(false)
const buyerError = ref('')
const selBuyers = ref([])
const enableCheckStock = ref(false)    // 蹲票模式开关
const checkStockInterval = ref(3)      // 查库存间隔秒数

const canAssign = computed(() => assignFor.value && selAccount.value && projectId.value
  && selScreen.value && selSku.value && selBuyers.value.length > 0)

function openAssign(workerId) {
  assignFor.value = workerId
  assignError.value = ''
  // 不重置已选账号/活动，方便给多个 worker 连续派同一活动
}
function closeAssign() { assignFor.value = ''; enableCheckStock.value = false; checkStockInterval.value = 3 }

function onAccountChange() {
  buyerList.value = []; selBuyers.value = []; buyerError.value = ''
  if (selAccount.value) loadBuyers()
}

function quickPick(id) {
  projectId.value = id
  loadEvent()
}

async function loadEvent() {
  loadingInfo.value = true; infoError.value = ''
  try {
    const r = await api.bilibiliEventInfo(projectId.value)
    if (r.error) infoError.value = r.error
    else screens.value = r.screens || []
  } catch (e) {
    infoError.value = '加载失败: ' + (e.message || e)
    console.error('[Console] 加载活动失败', e)
  }
  loadingInfo.value = false
}

function isSku(sid, skid) {
  return String(selScreen.value) === String(sid) && String(selSku.value) === String(skid)
}
function pickSku(s, sku) {
  if (isSku(s.screen_id, sku.sku_id)) { selScreen.value = ''; selSku.value = ''; selSkuMeta.value = {} }
  else {
    selScreen.value = String(s.screen_id); selSku.value = String(sku.sku_id)
    selSkuMeta.value = { screen_name: s.name, sku_desc: sku.desc }
  }
}

async function loadBuyers() {
  if (!selAccount.value) return
  loadingBuyers.value = true; buyerError.value = ''
  try {
    const r = await api.bilibiliBuyerList(selAccount.value)
    if (r.error) { buyerError.value = r.error; buyerList.value = [] }
    else { buyerList.value = r.buyers || [] }
  } catch (e) {
    buyerError.value = '查询失败: ' + (e.message || e)
    console.error('[Console] 加载购票人失败', e)
  }
  loadingBuyers.value = false
}
function toggleBuyer(id) {
  const i = selBuyers.value.indexOf(id)
  if (i >= 0) selBuyers.value.splice(i, 1); else selBuyers.value.push(id)
}

async function doAssign() {
  if (!canAssign.value) return
  assignError.value = ''
  try {
    await api.workerAssign(assignFor.value, {
      account_id: selAccount.value,
      buyer_ids: [...selBuyers.value],
      project_id: Number(projectId.value),
      screen_id: Number(selScreen.value),
      sku_id: Number(selSku.value),
      screen_name: selSkuMeta.value.screen_name || '',
      sku_desc: selSkuMeta.value.sku_desc || '',
      label: selSkuMeta.value.sku_desc || `项目${projectId.value}`,
      enable_check_stock: enableCheckStock.value,
      check_interval: Number(checkStockInterval.value),
    })
    closeAssign()
    selBuyers.value = []
    store.refreshAll()
  } catch (e) {
    assignError.value = '指派失败: ' + (e.message || e)
    console.error('[Console] 指派失败', e)
  }
}

async function stop(id) {
  try { await api.workerStop(id); store.refreshAll() }
  catch (e) { alert('停止失败: ' + (e.message || e)) }
}

async function togglePause(w) {
  try {
    if (w.paused) await api.workerResume(w.worker_id)
    else await api.workerPause(w.worker_id)
    store.refreshAll()
  } catch (e) { alert('操作失败: ' + (e.message || e)) }
}

function stText(s) { return { idle: '空闲', busy: '忙碌', paused: '已暂停', offline: '离线' }[s] || s }
function statusTxt(w) {
  if (w.status === 'busy') return '运行中'
  if (w.status === 'paused') return '已暂停'
  if (w.status === 'offline') return '离线'
  // idle：有 last_result → 显示成功/失败，否则空闲
  const r = w.last_result
  if (!r) return '空闲'
  if (r.success) return `成功 ${r.order_id || ''}`
  return `失败 ${r.reason || ''}`
}
function statusCls(w) {
  if (w.status === 'busy') return 'ok'
  if (w.status === 'offline') return 'stale'
  const r = w.last_result
  if (!r) return ''
  return r.success ? 'ok' : 'stale'
}
function statusTitle(w) {
  const r = w.last_result
  if (!r) return ''
  return r.detail ? '点击查看详情' : (r.reason || '')
}
function hbTxt(ts) { if (!ts) return '—'; const a = Math.round(Date.now() / 1000 - ts); return a < 10 ? '刚刚' : a < 60 ? a + 's 前' : Math.round(a / 60) + 'm 前' }
function hbCls(ts) { if (!ts) return ''; const a = Date.now() / 1000 - ts; return a > 30 ? 'stale' : a > 10 ? 'warn' : 'ok' }
function fmtTs(ts) { if (!ts) return '—'; return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) }
function fmtDeadline(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const mm = String(d.getUTCMonth() + 8 > 12 ? d.getUTCMonth() - 4 : d.getUTCMonth() + 1).padStart(2, '0')
  const dd = String(d.getUTCDate()).padStart(2, '0')
  const hh = String((d.getUTCHours() + 8) % 24).padStart(2, '0')
  const mi = String(d.getUTCMinutes()).padStart(2, '0')
  // 用 Intl 取北京时间，比手动算可靠
  const parts = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).formatToParts(d)
  const p = Object.fromEntries(parts.map(x => [x.type, x.value]))
  return `${p.month}-${p.day} ${p.hour}:${p.minute}`
}
function elapsed(ts) {
  if (!ts) return ''
  const a = Math.round(Date.now() / 1000 - ts)
  return a < 60 ? a + 's' : Math.round(a / 60) + 'm'
}
</script>

<style scoped>
.wk-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
@media (max-width: 1100px) { .wk-grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 700px) { .wk-grid { grid-template-columns: 1fr; } }
.wk { border: 1px solid var(--border); border-radius: var(--r); padding: 14px; border-left: 3px solid var(--border); transition: var(--transition); }
.wk:hover { box-shadow: 0 2px 8px rgba(0,0,0,.06); }
.wk.idle { border-left-color: var(--t3); }
.wk.busy { border-left-color: var(--t1); }
.wk.paused { border-left-color: #e6a23c; }
.wk.offline { opacity: .45; }
.wk-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.wk-id { font-weight: 600; font-size: .9em; }
.wk-rows { margin-bottom: 12px; }
.wk-row { display: flex; justify-content: space-between; font-size: .82em; padding: 3px 0; }
.wk-lbl { color: var(--t3); }
.wk-val { max-width: 60%; text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wk-btns { display: flex; gap: 6px; }
.ok { color: var(--t1); } .warn { color: var(--t3); } .stale { color: #c0392b; }

.wk-current { background: var(--accent-light); border-radius: 4px; padding: 8px 10px; margin-bottom: 12px; font-size: .8em; }
.wk-current.wk-idle-hint { background: transparent; color: var(--t3); font-style: italic; }
.cur-line { margin-bottom: 3px; }
.cur-meta { color: var(--t3); font-size: .92em; line-height: 1.5; }
.cur-deadline { color: var(--accent); font-weight: 600; }

.det-body { font-size: .85em; }
.det-line { display: flex; gap: 10px; padding: 3px 0; }
.det-lbl { color: var(--t3); min-width: 48px; }
.det-log { background: #1e1e1e; color: #d4d4d4; padding: 12px; border-radius: 4px; font-size: .8em;
  max-height: 360px; overflow: auto; white-space: pre-wrap; word-break: break-all; line-height: 1.5; }
.modal-wide { width: 820px; }
.log-live { max-height: 480px; }

.modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,.4); display: flex; align-items: flex-start; justify-content: center; z-index: 50; overflow-y: auto; padding: 40px 16px; }
.modal { background: var(--card); border-radius: var(--r); padding: 20px; width: 560px; max-width: 100%; box-shadow: 0 8px 32px rgba(0,0,0,.2); }
.field { margin-bottom: 14px; }
.field label { display: block; font-size: .82em; font-weight: 600; margin-bottom: 6px; }
.field select, .field input { width: 100%; padding: 7px 10px; border: 1px solid var(--border); border-radius: 4px; font-size: .85em; font-family: inherit; }
.actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 8px; }
.sku-grid { display: flex; flex-direction: column; gap: 10px; }
.sku-group { border: 1px solid var(--border); border-radius: 4px; padding: 10px; }
.sku-screen-label { font-size: .82em; font-weight: 600; margin-bottom: 6px; }
.sku-item { display: inline-flex; align-items: center; gap: 6px; padding: 5px 10px; border: 1px solid var(--border); border-radius: 4px; margin: 2px; cursor: pointer; font-size: .82em; }
.sku-item:hover { border-color: var(--t1); }
.sku-item.selected { border-color: var(--t1); background: var(--accent-light); }
.sku-item.pre-sale { opacity: .7; border-style: dashed; }
.sku-presale { font-size: .7em; color: #e6a23c; font-weight: 600; }
.sku-price { color: var(--t2); } .sku-stock { font-size: .75em; color: var(--t3); }
.buyer-grid { display: flex; flex-wrap: wrap; gap: 6px; }
.buyer-item { display: flex; align-items: center; gap: 6px; padding: 6px 12px; border: 1px solid var(--border); border-radius: 4px; cursor: pointer; font-size: .85em; }
.buyer-item:hover { border-color: var(--t1); }
.buyer-item.selected { border-color: var(--t1); background: var(--accent-light); }
.buyer-tel { font-size: .78em; color: var(--t3); }
.quick-picks { display: flex; gap: 6px; margin-bottom: 8px; }
.btn-quick { padding: 3px 14px; font-size: .78em; font-weight: 600; border-radius: 12px;
  background: var(--accent-light); border: 1px solid var(--border); cursor: pointer; }
.btn-quick:hover { border-color: var(--t1); background: var(--accent-light); }
.buyer-error { padding: 8px 10px; background: #fef2f2; border: 1px solid #fca5a5; border-radius: 4px; font-size: .82em; color: #c0392b; margin-bottom: 10px; }
.hint-box { padding: 10px 12px; background: var(--accent-light); border: 1px solid var(--border); border-radius: 4px; font-size: .8em; line-height: 1.5; word-break: break-all; margin-bottom: 12px; font-family: monospace; }
</style>
