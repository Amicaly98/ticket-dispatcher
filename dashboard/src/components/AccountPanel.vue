<template>
  <div>
    <div class="card">
      <div class="card-head">
        <span>账号管理</span>
        <button v-if="accountList.length" class="btn" style="float:right; font-size:.8em; padding:4px 12px"
          @click="refreshAllAccounts" :disabled="refreshingAll">
          {{ refreshingAll ? '刷新中...' : '全部刷新' }}
        </button>
      </div>
      <p class="tip" style="margin-bottom:14px">
        通过扫码或手动输入 cookie 添加账号。登录后自动识别用户名和 UID。
      </p>

      <div v-if="!accountList.length" class="empty">暂无账号。点击下方"扫码登录"或"手动添加"。</div>

      <div v-for="a in visibleAccounts" :key="a.id" class="acc-row">
        <div class="acc-info">
          <span class="acc-name">{{ a.username || a.id }}</span>
          <span class="acc-uid">uid={{ a.uid }}</span>
          <span class="acc-buyer-count">{{ (a.buyers||[]).length }} 人</span>
          <span class="tag" :class="vipCls(a)">{{ vipTxt(a) }}</span>
          <span class="tag" :class="a.hasCookie?'tag-idle':'tag-offline'">{{ a.hasCookie ? '已登录' : '未登录' }}</span>
          <button class="btn" style="margin-left:auto" @click="refreshAccount(a.id)" :disabled="refreshing===a.id">
            {{ refreshing===a.id ? '刷新中...' : '刷新' }}
          </button>
          <button class="btn btn-danger" @click="deleteAccount(a.id)">删除</button>
        </div>
        <div class="acc-buyers" v-if="a.buyers?.length">
          <span v-for="b in a.buyers" :key="b.id" class="buyer-chip">{{ b.name }}</span>
        </div>
        <div class="acc-buyers" v-else>
          <span class="buyer-empty">无购票人 · 点「刷新」拉取</span>
        </div>
      </div>
      <div v-if="accountList.length > 3" class="expand-bar">
        <button class="btn" @click="showAll = !showAll">
          {{ showAll ? '收起' : `查看全部 ${accountList.length} 个账号` }}
        </button>
      </div>
    </div>

    <!-- 扫码登录 -->
    <div class="card">
      <div class="card-head">扫码登录</div>
      <div class="actions" style="margin-bottom:12px">
        <button class="btn btn-dark" @click="startQr" :disabled="qrLoading">
          {{ qrLoading ? '获取中...' : (qrUrl ? '刷新二维码' : '生成二维码') }}
        </button>
      </div>

      <div v-if="qrUrl" class="qr-area">
        <img :src="'https://api.qrserver.com/v1/create-qr-code/?size=180x180&data='+encodeURIComponent(qrUrl)" />
        <div class="qr-msg" :class="qrMsgClass">{{ qrMsg }}</div>
      </div>
    </div>

    <!-- 手动输入 Cookie -->
    <div class="card">
      <div class="card-head">手动输入 Cookie</div>
      <p class="tip" style="margin-bottom:10px">从浏览器 DevTools > Application > Cookies 复制。</p>
      <div class="field">
        <label>SESSDATA</label>
        <input v-model="manualSess" placeholder="粘贴 SESSDATA" />
      </div>
      <div class="field">
        <label>bili_jct</label>
        <input v-model="manualJct" placeholder="粘贴 bili_jct" />
      </div>
      <div class="actions">
        <button class="btn btn-dark" @click="saveManual" :disabled="!manualSess">保存</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useSchedulerStore } from '../stores/scheduler'
import { api } from '../api'

const store = useSchedulerStore()

const showAll = ref(false)
const accountList = computed(() => {
  const snap = store.accounts || {}
  return Object.entries(snap).map(([id, a]) => ({
    id, ...a,
    hasCookie: a.has_cookie ?? false,
  }))
})
const visibleAccounts = computed(() => showAll.value ? accountList.value : accountList.value.slice(0, 3))

// QR login
const qrUrl = ref('')
const qrMsg = ref('')
const qrMsgClass = ref('')
const qrKey = ref('')
const qrLoading = ref(false)
let qrTimer = null

async function startQr() {
  qrMsg.value = ''; qrMsgClass.value = ''; qrUrl.value = ''; qrLoading.value = true
  try {
    const r = await api.bilibiliQrGen()
    qrUrl.value = r.url
    qrKey.value = r.qrcode_key
    if (qrTimer) clearInterval(qrTimer)
    qrTimer = setInterval(pollQr, 2000)
  } catch (e) { qrMsg.value = '获取失败: ' + e.message; qrMsgClass.value = 'err' }
  qrLoading.value = false
}

async function pollQr() {
  if (!qrKey.value) return
  try {
    const r = await api.bilibiliQrPoll(qrKey.value)
    if (r.status === 0) {
      clearInterval(qrTimer); qrTimer = null
      qrMsg.value = '登录成功，正在获取用户信息...'
      qrMsgClass.value = 'ok'
      const sessData = r.cookie?.SESSDATA || ''
      const biliJct = r.cookie?.bili_jct || ''
      if (sessData) {
        try {
          const nav = await api.bilibiliCheckLoginWithCookie(sessData)
          const uid = nav.uid || r.uid || 0
          const username = nav.username || ''
          const accId = `bili_${uid}`
          await api.accountSave({
            account_id: accId, program: '', uid, username,
            cookie: { sess_data: sessData, bili_jct: biliJct }, buyers: [],
          })
          qrMsg.value = `${username} (uid=${uid}) 登录成功，正在拉取购票人...`
          await store.refreshAll()
          try {
            const ref = await api.accountRefresh(accId)
            if (ref.error) qrMsg.value = `${username} 登录成功（刷新失败：${ref.error}）`
            else qrMsg.value = `${username} 登录成功，${ref.buyers} 位购票人已加载`
            await store.refreshAll()
          } catch (e2) {
            qrMsg.value = `${username} 登录成功（刷新购票人失败：${e2.message}）`
          }
        } catch (e) {
          console.warn('[qr] nav 查询失败，回退 QR 数据', e)
          const uid = r.uid || 0
          const accId = `bili_${uid}`
          await api.accountSave({
            account_id: accId, program: '', uid, username: '',
            cookie: { sess_data: sessData, bili_jct: biliJct }, buyers: [],
          })
          qrMsg.value = `uid=${uid} 登录成功（用户名获取失败）`
          await store.refreshAll()
        }
      }
    } else if (r.status === 86038) {
      clearInterval(qrTimer); qrTimer = null
      qrMsg.value = '二维码已过期，请刷新'; qrMsgClass.value = 'err'
    } else if (r.status === 86090) {
      qrMsg.value = '已扫码，请在手机上确认'; qrMsgClass.value = ''
    } else {
      qrMsg.value = r.message || '等待扫码...'
    }
  } catch (e) { console.error('[qr] 轮询失败', e) }
}

async function deleteAccount(id) {
  if (!confirm(`确认删除账号 ${id}？`)) return
  try {
    await api.accountDelete(id)
    await store.refreshAll()
  } catch (e) { alert('删除失败: ' + e.message) }
}

// 大会员徽章
function vipTxt(a) {
  if (!a.vip_status) return '普通用户'
  const base = a.vip_type === 2 ? '年度大会员' : (a.vip_type === 1 ? '月度大会员' : '大会员')
  if (a.vip_due_date) {
    const d = new Date(a.vip_due_date)
    const expired = a.vip_due_date < Date.now()
    return `${base}${expired ? '(已过期)' : ' 至 ' + d.toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai'})}`
  }
  return base
}
function vipCls(a) {
  if (!a.vip_status) return 'tag-offline'
  if (a.vip_due_date && a.vip_due_date < Date.now()) return 'tag-paused'
  return 'tag-vip'
}

const refreshing = ref('')
const refreshingAll = ref(false)
async function refreshAllAccounts() {
  refreshingAll.value = true
  const ids = accountList.value.map(a => a.id)
  let ok = 0, fail = 0
  for (const id of ids) {
    try {
      const r = await api.accountRefresh(id)
      if (r.error) fail++; else ok++
    } catch (e) { fail++ }
  }
  await store.refreshAll()
  refreshingAll.value = false
  alert(`全部刷新完成：成功 ${ok}，失败 ${fail}`)
}
async function refreshAccount(id) {
  refreshing.value = id
  try {
    const r = await api.accountRefresh(id)
    if (r.error) { alert(r.error); return }
    await store.refreshAll()
  } catch (e) { alert('刷新失败: ' + (e.message || e)) }
  finally { refreshing.value = '' }
}

// Manual cookie
const manualSess = ref('')
const manualJct = ref('')

async function saveManual() {
  if (!manualSess.value) return
  try {
    let uid = 0, username = ''
    try {
      const nav = await api.bilibiliCheckLoginWithCookie(manualSess.value)
      uid = nav.uid || 0; username = nav.username || ''
    } catch (e) { console.warn('[account] nav 查询失败', e) }
    const accId = `bili_${uid}`
    await api.accountSave({
      account_id: accId, program: '', uid, username,
      cookie: { sess_data: manualSess.value, bili_jct: manualJct.value }, buyers: [],
    })
    manualSess.value = ''; manualJct.value = ''
    await store.refreshAll()
    try { await api.accountRefresh(accId); await store.refreshAll() } catch (e2) { /* ignore */ }
  } catch (e) { alert('保存失败: ' + e.message) }
}
</script>

<style scoped>
.acc-row { padding: 10px; border: 1px solid var(--border); border-radius: 4px; margin-bottom: 8px; }
.acc-info { display: flex; align-items: center; gap: 10px; font-size: .85em; }
.acc-name { font-weight: 600; }
.acc-uid { color: var(--t3); font-size: .8em; }
.acc-buyer-count { color: var(--t3); font-size: .8em; }
.acc-buyers { margin-top: 6px; display: flex; gap: 6px; flex-wrap: wrap; }
.buyer-chip { padding: 2px 8px; border: 1px solid var(--border); border-radius: 3px; font-size: .78em; }
.buyer-empty { font-size: .76em; color: var(--t3); font-style: italic; }
.expand-bar { text-align: center; padding-top: 4px; }
.field { margin-bottom: 12px; }
.field label { display: block; font-size: .82em; font-weight: 600; margin-bottom: 4px; }
.field input { width: 100%; padding: 7px 10px; border: 1px solid var(--border); border-radius: 4px; font-size: .85em; font-family: inherit; }
.field input:focus { outline: none; border-color: var(--t1); }
.tip { font-size: .78em; color: var(--t3); margin-top: 4px; }
.actions { display: flex; gap: 8px; margin-top: 12px; }
.qr-area { text-align: center; margin-top: 14px; }
.qr-area img { border: 1px solid var(--border); border-radius: 4px; }
.qr-msg { margin-top: 8px; font-size: .85em; color: var(--t2); }
.qr-msg.ok { color: var(--t1); font-weight: 600; }
.qr-msg.err { color: var(--t3); }
</style>
