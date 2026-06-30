# ticket-dispatcher 控制台

Vue 3 + Vite + Pinia + Vue Router 前端，对接 FastAPI 后端（手动指派台）。

## 启动

```bash
# 后端（端口 8000）
cd .. && DRYRUN=1 python main.py api

# 前端（端口 3000，自动 proxy /api/* 到后端 8000）
cd dashboard && npm install && npm run dev
```

## 路由

| URL | 页面 | 功能 |
|---|---|---|
| `/` | Worker 控制台 | worker 卡片（状态/当前活动/上次结果/心跳）+「指派」弹窗 +「停止」 |
| `/accounts` | 号池 | 手动添加账号 / 删除 |
| `/events` | 事件流 | 实时系统事件推送（WebSocket） |
| `/audit` | 审计 | 全局指标 + 结果历史 + Worker 事件 |

「指派」弹窗流程：选号 → 填活动 ID 加载场次/票档 → 选购票人 → 填 deadline（ISO 或留空=立即）→ 提交。

## 说明

- 降级为手动指派台后，旧的「任务列表 / 创建向导 / 任务详情 / Worker 管理」四个页面
  （`TaskBoard`/`TaskWizard`/`TaskDetail`/`WorkerPanel`）已删除，合并为单一 `WorkerConsole`。
- cookie 绝不下发前端：`/accounts` 只返回 `has_cookie` 标记，不含真实凭证。
- 已知体验项见根目录 `CLAUDE.md`「Known bottlenecks」（如指派弹窗 deadline 手填）。
