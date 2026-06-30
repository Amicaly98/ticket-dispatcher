"""SQLite 持久化：attempts / results / worker_events。

Collector 线程消费结果时写入，供历史/审计。文件路径：config/scheduler.db（自动创建）。
降级后无 task_transitions（无调度模式切换）。
"""
from __future__ import annotations
import json
import logging
import os
import sqlite3
import time
from typing import Optional

from .models import AttemptResult, Job

log = logging.getLogger("store")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "config", "scheduler.db")


class Store:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS attempts (
                job_id        TEXT PRIMARY KEY,
                task_id       TEXT,
                worker_id     TEXT NOT NULL,
                program       TEXT,
                buyer_ids     TEXT,
                created_at    REAL,
                project_id    INTEGER,
                screen_id     INTEGER,
                sku_id        INTEGER,
                pay_money     INTEGER,
                account_id    TEXT,
                account_label TEXT,
                buyer_names   TEXT,
                deadline      REAL
            );
            CREATE TABLE IF NOT EXISTS results (
                job_id          TEXT PRIMARY KEY,
                task_id         TEXT,
                worker_id       TEXT NOT NULL,
                success         INTEGER NOT NULL,
                order_id        TEXT,
                reason          TEXT,
                detail          TEXT,
                buyers_secured  TEXT,
                ts              REAL
            );
            CREATE TABLE IF NOT EXISTS worker_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id   TEXT NOT NULL,
                event       TEXT NOT NULL,
                detail      TEXT,
                ts          REAL
            );
            CREATE INDEX IF NOT EXISTS idx_results_ts ON results(ts);
        """)
        # 迁移：老库的 results 表没有 detail 列，补上（已存在则忽略）
        try:
            self.conn.execute("ALTER TABLE results ADD COLUMN detail TEXT")
        except sqlite3.OperationalError:
            pass  # duplicate column name —— 已是新库
        # 迁移：老库的 attempts 表缺审计列，逐列补上
        for col, decl in (
            ("project_id", "INTEGER"), ("screen_id", "INTEGER"), ("sku_id", "INTEGER"),
            ("screen_name", "TEXT"), ("sku_desc", "TEXT"),
            ("pay_money", "INTEGER"), ("account_id", "TEXT"), ("account_label", "TEXT"),
            ("buyer_names", "TEXT"), ("deadline", "REAL"),
        ):
            try:
                self.conn.execute(f"ALTER TABLE attempts ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # duplicate column name —— 已是新库
        self.conn.commit()

    # ── 写入 ──

    def record_attempt(self, job: Job):
        try:
            acc = job.account or {}
            account_id = acc.get("account_id", "")
            account_label = acc.get("username") or account_id
            buyer_names = json.dumps([b.get("name", "") for b in job.buyers], ensure_ascii=False)
            self.conn.execute(
                "INSERT OR REPLACE INTO attempts "
                "(job_id,task_id,worker_id,program,buyer_ids,created_at,"
                " project_id,screen_id,sku_id,screen_name,sku_desc,"
                " pay_money,account_id,account_label,buyer_names,deadline) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job.job_id, job.task_id, job.worker_id, job.program,
                 json.dumps(job.buyer_ids), job.created_at,
                 job.project_id, job.screen_id, job.sku_id,
                 job.screen_name, job.sku_desc,
                 job.pay_money, account_id, account_label, buyer_names, job.deadline))
            self.conn.commit()
            log.info("record_attempt: job=%s task=%s account=%s screen=%s sku=%s buyers=%s",
                     job.job_id, job.task_id, account_id,
                     job.screen_name or "(无)", job.sku_desc or "(无)",
                     buyer_names)
        except Exception as e:  # noqa: BLE001
            log.warning("record_attempt 失败: %s", e)

    def record_result(self, result: AttemptResult):
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO results "
                "(job_id,task_id,worker_id,success,order_id,reason,detail,buyers_secured,ts) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (result.job_id, result.task_id, result.worker_id,
                 int(result.success), result.order_id, result.reason, result.detail,
                 json.dumps(result.buyers_secured), result.ts))
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            log.warning("record_result 失败: %s", e)

    def record_worker_event(self, worker_id: str, event: str, detail: str = ""):
        try:
            self.conn.execute(
                "INSERT INTO worker_events (worker_id,event,detail,ts) VALUES (?,?,?,?)",
                (worker_id, event, detail, time.time()))
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            log.warning("record_worker_event 失败: %s", e)

    # ── 查询 ──

    def get_attempt(self, job_id: str) -> Optional[dict]:
        """按 job_id 取指派时落库的配置简述（活动/场次/票档/账号/购票人），供购票后推送富化消息。"""
        cur = self.conn.execute(
            "SELECT task_id,project_id,screen_id,sku_id,screen_name,sku_desc,"
            "pay_money,account_id,account_label,buyer_names FROM attempts WHERE job_id=? LIMIT 1",
            (job_id,))
        row = cur.fetchone()
        if not row:
            return None
        cols = ("task_id", "project_id", "screen_id", "sku_id", "screen_name", "sku_desc",
                "pay_money", "account_id", "account_label", "buyer_names")
        d = dict(zip(cols, row))
        if d.get("buyer_names"):
            try:
                d["buyer_names"] = json.loads(d["buyer_names"])
            except Exception:  # noqa: BLE001
                d["buyer_names"] = []
        else:
            d["buyer_names"] = []
        return d

    def get_recent_results(self, limit: int = 50) -> list[dict]:
        # LEFT JOIN attempts 带出配置简述（活动/场次/票档/账号/购票人名）——
        # 审计前端据此「按活动·场次·票档分类抢到的票」+「最近结果显示配置简述」。
        # LEFT 兜底：老结果可能无对应 attempts 行，这些字段为 None。
        cur = self.conn.execute(
            "SELECT r.job_id,r.task_id,r.worker_id,r.success,r.order_id,r.reason,r.detail,"
            "       r.buyers_secured,r.ts,"
            "       a.project_id,a.screen_id,a.sku_id,a.screen_name,a.sku_desc,"
            "       a.pay_money,a.account_label,a.buyer_names "
            "FROM results r LEFT JOIN attempts a ON r.job_id = a.job_id "
            "ORDER BY r.ts DESC LIMIT ?", (limit,))
        cols = ("job_id", "task_id", "worker_id", "success", "order_id", "reason", "detail",
                "buyers_secured", "ts", "project_id", "screen_id", "sku_id",
                "screen_name", "sku_desc", "pay_money",
                "account_label", "buyer_names")
        out = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            # buyer_names 落库时是 JSON 字符串，反解为 list 供前端直接用
            if d.get("buyer_names"):
                try:
                    d["buyer_names"] = json.loads(d["buyer_names"])
                except (ValueError, TypeError):
                    d["buyer_names"] = []
            else:
                d["buyer_names"] = []
            out.append(d)
        return out

    def metrics(self) -> dict:
        """全局指标（SQL 聚合，不拉全表）：总数 / 成功数 / reason 分布。

        旧实现 get_recent_results(limit=10000) 每 2s 拉上万行 + json.loads，随库增长越拖越久、
        长时间持 GIL 卡住事件循环（前端刷新断连的元凶之一）。改成纯 COUNT/SUM/GROUP BY。
        """
        try:
            total, success = self.conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(success),0) FROM results").fetchone()
            reasons = {row[0] or "unknown": row[1] for row in self.conn.execute(
                "SELECT reason, COUNT(*) FROM results GROUP BY reason").fetchall()}
        except Exception as e:  # noqa: BLE001
            log.warning("metrics 聚合失败: %s", e)
            return {"total_attempts": 0, "success_count": 0, "success_rate": 0,
                    "reasons": {}}
        return {
            "total_attempts": total,
            "success_count": success,
            "success_rate": round(success / total, 4) if total else 0,
            "reasons": reasons,
        }

    def get_worker_events(self, worker_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        if worker_id:
            cur = self.conn.execute(
                "SELECT worker_id,event,detail,ts FROM worker_events "
                "WHERE worker_id=? ORDER BY ts DESC LIMIT ?", (worker_id, limit))
        else:
            cur = self.conn.execute(
                "SELECT worker_id,event,detail,ts FROM worker_events "
                "ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(zip(("worker_id", "event", "detail", "ts"), row))
                for row in cur.fetchall()]

    def close(self):
        self.conn.close()
