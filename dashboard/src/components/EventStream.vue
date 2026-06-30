<template>
  <div class="card">
    <div class="card-head">事件流</div>
    <p class="tip" style="margin-bottom:10px">系统内部事件实时推送。黑底 = 成功，灰底 = 失败，左边线 = 调度/Worker。</p>
    <div class="stream" ref="el">
      <div v-if="!events.length" class="empty">等待事件。启动任务后这里会实时显示调度和下单结果。</div>
      <div v-for="(ev,i) in events" :key="i" class="ev" :class="cls(ev)">
        <span class="ev-ts">{{ ts(ev.ts) }}</span>
        <span class="ev-tp">{{ tp(ev.type) }}</span>
        <span class="ev-dt">{{ dt(ev) }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, watch, ref, nextTick } from 'vue'
import { useSchedulerStore } from '../stores/scheduler'
const store = useSchedulerStore()
const el = ref(null)
const events = computed(() => store.events.slice(-200).reverse())
watch(() => store.events.length, () => nextTick(() => { if(el.value) el.value.scrollTop=0 }))

function cls(ev) {
  if(ev.type==='worker_error') return 'err'
  if(ev.type==='job_result') return ev.success?'ok':'fail'
  if(ev.type==='job_dispatched') return 'disp'
  if(ev.type?.includes('worker')) return 'wk'
  return ''
}
function tp(t) {
  return {job_dispatched:'指派',job_result:'结果',worker_registered:'上线',worker_offline:'离线',
          worker_stop_requested:'停止',worker_error:'错误',account_saved:'存号',account_deleted:'删号'}[t]||t
}
function dt(ev) {
  if(ev.type==='worker_error') return `${ev.worker_id} ${ev.reason||''}：${(ev.detail||'').slice(0,80)}`
  if(ev.type==='job_result') return `${ev.job_id} ${ev.success?'成功':'失败'} ${ev.order_id||''} ${ev.reason||''}`
  if(ev.type==='job_dispatched') return `${ev.task_id||ev.job_id} -> ${ev.worker_id}`
  if(ev.type?.includes('worker')) return ev.worker_id
  return ev.account_id||ev.task_id||''
}
function ts(t) { return t?new Date(t*1000).toLocaleTimeString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}):'' }
</script>

<style scoped>
.stream {
  height: 380px; overflow-y: auto; border: 1px solid var(--border);
  border-radius: 4px; background: #fcfcfc; padding: 6px;
}
.ev {
  padding: 3px 8px; margin-bottom: 1px; border-radius: 2px;
  font-size: .78em; font-family: "SF Mono", "Cascadia Code", "Fira Code", Menlo, monospace;
  display: flex; gap: 10px; align-items: baseline;
  border-left: 2px solid transparent; transition: var(--transition);
}
.ev.ok   { background: #f7f7f7; border-left-color: var(--t1); }
.ev.fail { background: #fafafa; border-left-color: #ccc; }
.ev.disp { border-left-color: var(--t1); }
.ev.wk   { border-left-color: #bbb; }
.ev.err  { background: #fef2f2; border-left-color: #c0392b; color: #c0392b; }
.ev-ts { color: var(--t3); white-space: nowrap; min-width: 64px; font-size: .95em; }
.ev-tp { font-weight: 600; white-space: nowrap; min-width: 32px; }
.ev-dt { color: var(--t2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
