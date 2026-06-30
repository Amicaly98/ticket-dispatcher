"""Store 持久化测试：detail 字段 roundtrip + 老库迁移（ALTER TABLE）。

results 表新增 detail 列。新库直接带列；老库（无 detail）打开时需 ALTER 补列，
否则 INSERT 报错、历史失败原文丢失。这里把这两条都焊死。
"""
import sqlite3

from src.models import AttemptResult, Buyer, Job
from src.store import Store


def _result(job_id="job-1", success=False, reason="sold_out", detail="日志尾巴\n(exit=1)"):
    return AttemptResult(
        job_id=job_id, task_id="t1", worker_id="w1",
        success=success, reason=reason, detail=detail, buyers_secured=[])


def _job(job_id="job-1", project_id=42, screen_id=7, sku_id=99):
    return Job.build(
        job_id=job_id, task_id="ComicQuest VIP", worker_id="w1", program="test",
        project_id=project_id, screen_id=screen_id, sku_id=sku_id, count=1, pay_money=39800,
        deadline=1000.0,
        account={"account_id": "test_1", "uid": 1, "username": "阿强", "cookie": {}},
        buyers=[Buyer(id=501, name="甲"), Buyer(id=502, name="乙")])


def test_detail_roundtrip(tmp_path):
    """新库：detail 写入后能从 get_recent_results 读回。"""
    store = Store(db_path=str(tmp_path / "new.db"))
    store.record_result(_result(detail="获取项目票务信息失败\n(exit=1)"))
    rows = store.get_recent_results(10)
    store.close()
    assert len(rows) == 1
    assert rows[0]["detail"] == "获取项目票务信息失败\n(exit=1)"
    assert rows[0]["reason"] == "sold_out"


def test_success_result_persisted(tmp_path):
    store = Store(db_path=str(tmp_path / "ok.db"))
    store.record_result(_result(job_id="job-ok", success=True, reason="ok", detail=""))
    rows = store.get_recent_results(10)
    store.close()
    assert rows[0]["success"] == 1
    assert rows[0]["detail"] in ("", None)


def test_migration_from_old_schema(tmp_path):
    """老库（results 无 detail 列）打开时应自动 ALTER 补列，且后续写入正常。"""
    db = str(tmp_path / "old.db")
    # 手造一个老 schema 的库（无 detail 列）
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE results (
            job_id TEXT PRIMARY KEY, task_id TEXT, worker_id TEXT NOT NULL,
            success INTEGER NOT NULL, order_id TEXT, reason TEXT,
            buyers_secured TEXT, ts REAL
        );
    """)
    conn.execute("INSERT INTO results VALUES (?,?,?,?,?,?,?,?)",
                 ("old-job", "t0", "w0", 0, "", "timeout", "[]", 1.0))
    conn.commit()
    conn.close()

    # 打开 Store → 应迁移补列，不报错
    store = Store(db_path=db)
    store.record_result(_result(job_id="new-job", detail="新写入的 detail"))
    rows = {r["job_id"]: r for r in store.get_recent_results(10)}
    store.close()

    assert "detail" in rows["new-job"]
    assert rows["new-job"]["detail"] == "新写入的 detail"
    assert rows["old-job"]["detail"] in ("", None)   # 老行迁移后 detail 为 NULL


# ── 审计字段（attempts 扩列 + JOIN）──

def test_audit_fields_join(tmp_path):
    """record_attempt 落活动/场次/票档/账号/购票人名，get_recent_results JOIN 带出。"""
    store = Store(db_path=str(tmp_path / "audit.db"))
    store.record_attempt(_job(job_id="job-a", project_id=42, screen_id=7, sku_id=99))
    store.record_result(_result(job_id="job-a", success=True, reason="ok", detail=""))
    rows = store.get_recent_results(10)
    store.close()
    assert len(rows) == 1
    r = rows[0]
    assert r["project_id"] == 42 and r["screen_id"] == 7 and r["sku_id"] == 99
    assert r["pay_money"] == 39800
    assert r["account_label"] == "阿强"
    assert r["buyer_names"] == ["甲", "乙"]   # JSON 落库后反解为 list


def test_result_without_attempt_left_join(tmp_path):
    """没有对应 attempts 行的结果（LEFT JOIN 兜底）：审计字段为 None / 空 list，不报错。"""
    store = Store(db_path=str(tmp_path / "lonely.db"))
    store.record_result(_result(job_id="orphan", success=False))
    rows = store.get_recent_results(10)
    store.close()
    assert rows[0]["project_id"] is None
    assert rows[0]["buyer_names"] == []


def test_attempts_migration_from_old_schema(tmp_path):
    """老 attempts 库（仅 6 列）打开时自动补审计列，record_attempt 不报错且字段写入。"""
    db = str(tmp_path / "old_attempts.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE attempts (
            job_id TEXT PRIMARY KEY, task_id TEXT, worker_id TEXT NOT NULL,
            program TEXT, buyer_ids TEXT, created_at REAL
        );
    """)
    conn.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?)",
                 ("old-att", "t0", "w0", "test_driver", "[1]", 1.0))
    conn.commit()
    conn.close()

    store = Store(db_path=db)
    store.record_attempt(_job(job_id="new-att", project_id=7))
    store.record_result(_result(job_id="new-att", success=True, reason="ok", detail=""))
    rows = {r["job_id"]: r for r in store.get_recent_results(10)}
    store.close()
    assert rows["new-att"]["project_id"] == 7
    assert rows["new-att"]["buyer_names"] == ["甲", "乙"]


# ── metrics() SQL 聚合 ──

def test_metrics_basic(tmp_path):
    """metrics() 返回正确的总数/成功数/reason 分布。"""
    store = Store(db_path=str(tmp_path / "met.db"))
    store.record_result(_result(job_id="j1", success=True, reason="ok", detail=""))
    store.record_result(_result(job_id="j2", success=False, reason="sold_out", detail=""))
    store.record_result(_result(job_id="j3", success=False, reason="sold_out", detail=""))
    store.record_result(_result(job_id="j4", success=False, reason="cancelled", detail=""))
    m = store.metrics()
    assert m["total_attempts"] == 4
    assert m["success_count"] == 1
    assert abs(m["success_rate"] - 0.25) < 1e-9
    assert m["reasons"] == {"ok": 1, "sold_out": 2, "cancelled": 1}
    store.close()


def test_metrics_empty(tmp_path):
    store = Store(db_path=str(tmp_path / "empty.db"))
    m = store.metrics()
    assert m["total_attempts"] == 0
    assert m["success_count"] == 0
    assert m["success_rate"] == 0
    assert m["reasons"] == {}
    store.close()
