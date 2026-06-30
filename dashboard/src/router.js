import { createRouter, createWebHistory } from 'vue-router'

import WorkerConsole from './components/WorkerConsole.vue'
import AccountPanel from './components/AccountPanel.vue'
import EventStream from './components/EventStream.vue'
import AuditPanel from './components/AuditPanel.vue'

const routes = [
  { path: '/',         name: 'console',  component: WorkerConsole },
  { path: '/accounts', name: 'accounts', component: AccountPanel },
  { path: '/events',   name: 'events',   component: EventStream },
  { path: '/audit',    name: 'audit',    component: AuditPanel },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
