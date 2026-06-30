<template>
  <div class="app">
    <header class="topbar">
      <div class="topbar-left">
        <div class="logo-mark"></div>
        <div class="logo-text">
          <h1>ticket-scheduler</h1>
          <span class="sub">抢票指派控制台</span>
        </div>
      </div>
      <div class="topbar-right">
        <span class="conn" :class="connected ? 'on' : 'off'">
          <span class="conn-dot"></span>
          {{ connected ? '已连接' : '断开' }}
        </span>
      </div>
    </header>

    <nav class="tabs">
      <router-link v-for="t in tabs" :key="t.to" :to="t.to" :class="{active: isActive(t.to)}">
        <span class="tab-icon" v-html="t.icon"></span>
        {{ t.label }}
      </router-link>
    </nav>

    <main class="content">
      <router-view v-slot="{ Component, route }">
        <Transition name="fade" mode="out-in">
          <component :is="Component" :key="route.path" />
        </Transition>
      </router-view>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSchedulerStore } from './stores/scheduler'
import { api } from './api'

const route = useRoute()
const router = useRouter()
const store = useSchedulerStore()
const connected = computed(() => store.connected)

const tabs = [
  { to: '/',         icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="1" y="3" width="14" height="10" rx="1.5"/><path d="M5 13v2M11 13v2M3 13h10"/></svg>', label: '控制台' },
  { to: '/accounts', icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="5" r="3"/><path d="M2 14c0-3.3 2.7-6 6-6s6 2.7 6 6"/></svg>', label: '账号' },
  { to: '/events',   icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="6"/><path d="M8 5v3l2 2"/></svg>', label: '事件' },
  { to: '/audit',    icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 14V8M6 14V4M10 14V6M14 14V2"/></svg>', label: '审计' },
]

function isActive(to) {
  if (to === '/') return route.path === '/'
  return route.path.startsWith(to)
}

onMounted(() => store.start())
</script>

<style>
*, *::before, *::after { margin:0; padding:0; box-sizing:border-box; }
:root {
  --bg: #fafafa; --card: #fff; --border: #e8e8e8;
  --t1: #111; --t2: #555; --t3: #999;
  --accent: #111; --accent-light: #f5f5f5;
  --ok: #222; --err: #111;
  --r: 6px; --shadow: 0 1px 2px rgba(0,0,0,.06);
  --transition: .2s cubic-bezier(.4,0,.2,1);
}
body {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  background: var(--bg); color: var(--t1); line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
.fade-enter-active, .fade-leave-active { transition: opacity .15s ease, transform .15s ease; }
.fade-enter-from { opacity: 0; transform: translateY(6px); }
.fade-leave-to { opacity: 0; transform: translateY(-6px); }
.topbar {
  background: var(--t1); color: #fff; padding: 0 28px; height: 52px;
  display: flex; justify-content: space-between; align-items: center;
}
.topbar-left { display: flex; align-items: center; gap: 12px; }
.logo-mark { width: 20px; height: 20px; border: 2px solid #fff; border-radius: 4px; position: relative; }
.logo-mark::after { content:''; position:absolute; top:4px; left:4px; width:8px; height:8px; background:#fff; border-radius:2px; }
.logo-text h1 { font-size: .95em; font-weight: 600; letter-spacing: .5px; }
.logo-text .sub { font-size: .7em; opacity: .5; }
.conn { display: flex; align-items: center; gap: 6px; font-size: .78em; }
.conn-dot { width: 7px; height: 7px; border-radius: 50%; }
.conn.on .conn-dot { background: #4ade80; box-shadow: 0 0 6px rgba(74,222,128,.5); }
.conn.off .conn-dot { background: #666; }
.tabs {
  background: var(--card); border-bottom: 1px solid var(--border);
  display: flex; padding: 0 28px; gap: 0;
}
.tabs a {
  padding: 10px 18px; border: none; background: none; cursor: pointer;
  font-size: .82em; color: var(--t3); border-bottom: 2px solid transparent;
  transition: var(--transition); display: flex; align-items: center; gap: 6px;
  text-decoration: none;
}
.tabs a:hover { color: var(--t1); }
.tabs a.active { color: var(--t1); border-bottom-color: var(--t1); font-weight: 600; }
.tab-icon { width: 14px; height: 14px; display: inline-flex; }
.tab-icon svg { width: 100%; height: 100%; }
.content { max-width: 1100px; margin: 20px auto; padding: 0 20px; }
.card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: var(--r); box-shadow: var(--shadow);
  padding: 18px 20px; margin-bottom: 14px;
  transition: var(--transition);
}
.card:hover { box-shadow: 0 2px 8px rgba(0,0,0,.08); }
.card-head { font-size: .88em; font-weight: 600; margin-bottom: 14px; color: var(--t1); display: flex; align-items: center; gap: 8px; }
.btn {
  padding: 5px 12px; border: 1px solid var(--border); border-radius: 4px;
  background: var(--card); cursor: pointer; font-size: .8em; color: var(--t2);
  transition: var(--transition); display: inline-flex; align-items: center; gap: 4px;
  font-family: inherit;
}
.btn:hover { background: var(--accent-light); color: var(--t1); border-color: #ccc; }
.btn:active { transform: scale(.97); }
.btn-dark { background: var(--t1); color: #fff; border-color: var(--t1); }
.btn-dark:hover { background: #333; }
.btn-danger { color: var(--err); border-color: #ddd; }
.btn-danger:hover { background: #fef2f2; border-color: #fca5a5; }
.btn:disabled { opacity: .35; cursor: not-allowed; pointer-events: none; }
.tag {
  display: inline-block; padding: 1px 8px; border-radius: 3px;
  font-size: .72em; font-weight: 600; letter-spacing: .3px;
  border: 1px solid var(--border);
}
.tag-idle     { color: var(--ok); }
.tag-busy     { color: var(--t1); border-color: var(--t1); }
.tag-paused   { color: #e6a23c; border-color: #e6a23c; }
.tag-vip      { color: #fb7299; border-color: #fb7299; }
.tag-offline  { color: var(--t3); opacity: .5; }
.bar { height: 4px; background: var(--border); border-radius: 2px; overflow: hidden; }
.bar-fill { height: 100%; background: var(--t1); border-radius: 2px; transition: width .5s cubic-bezier(.4,0,.2,1); }
table { width: 100%; border-collapse: collapse; font-size: .82em; }
th { text-align: left; padding: 7px 10px; color: var(--t3); font-weight: 500; border-bottom: 1px solid var(--border); font-size: .9em; }
td { padding: 7px 10px; border-bottom: 1px solid #f3f3f3; }
tr:hover td { background: var(--accent-light); }
.tip { font-size: .78em; color: var(--t3); margin-top: 4px; line-height: 1.5; }
.empty { color: var(--t3); text-align: center; padding: 36px 20px; font-size: .88em; }
.row { display: flex; gap: 14px; }
.row > * { flex: 1; }
@media (max-width: 800px) { .row { flex-direction: column; } }
.select {
  padding: 5px 10px; border: 1px solid var(--border); border-radius: 4px;
  font-size: .82em; background: var(--card); color: var(--t1); font-family: inherit;
}
</style>
