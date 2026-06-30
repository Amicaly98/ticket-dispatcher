"""Worker 声明 CRUD 测试（前端「创建/删除 worker」的后端逻辑）。

声明式：save/delete 只动 settings.yaml + bus 心跳，不远程拉进程。
这里焊死三件事：声明写盘往返、删除幂等、bus.forget_worker 清心跳。
"""
import os

from src.bus import InMemoryBus
from src.config import delete_worker_decl, load_settings, load_workers, save_worker_decl


def _tmp_settings(tmp_path) -> str:
    p = tmp_path / "settings.yaml"
    p.write_text("workers: []\nproxy_pools:\n  residential: []\n", encoding="utf-8")
    return str(p)


def test_save_worker_decl_roundtrip(tmp_path):
    path = _tmp_settings(tmp_path)
    save_worker_decl({"worker_id": "w_cloud", "program": "other",
                      "max_slots": 2, "public_ip": "9.9.9.9"}, path=path)
    workers = load_workers(load_settings(path))
    assert len(workers) == 1
    w = workers[0]
    assert w.worker_id == "w_cloud"
    assert w.program == "other"
    assert w.max_slots == 2
    assert w.public_ip == "9.9.9.9"


def test_save_worker_decl_updates_in_place(tmp_path):
    path = _tmp_settings(tmp_path)
    save_worker_decl({"worker_id": "w1", "program": "test_driver", "max_slots": 1}, path=path)
    save_worker_decl({"worker_id": "w1", "program": "other", "max_slots": 3}, path=path)
    workers = load_workers(load_settings(path))
    assert len(workers) == 1, "同 id 应原地更新而非追加"
    assert workers[0].program == "other"
    assert workers[0].max_slots == 3


def test_delete_worker_decl(tmp_path):
    path = _tmp_settings(tmp_path)
    save_worker_decl({"worker_id": "w1"}, path=path)
    save_worker_decl({"worker_id": "w2"}, path=path)
    assert delete_worker_decl("w1", path=path) is True
    remaining = {w.worker_id for w in load_workers(load_settings(path))}
    assert remaining == {"w2"}


def test_delete_worker_decl_missing_returns_false(tmp_path):
    path = _tmp_settings(tmp_path)
    assert delete_worker_decl("nope", path=path) is False


def test_bus_forget_worker_clears_heartbeat():
    bus = InMemoryBus()
    bus.heartbeat("zombie", {"worker_id": "zombie", "status": "idle"})
    assert "zombie" in bus.list_workers()
    bus.forget_worker("zombie")
    assert "zombie" not in bus.list_workers()
    # 幂等：再删不报错
    bus.forget_worker("zombie")
