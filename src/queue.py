"""RedisBus：跨进程/跨机器的 Bus 实现，用于真正的分布式部署。

Job 队列  → Redis Streams（可靠、支持消费者组，定向 worker_id）
Result    → Redis Streams（单消费者组，API 侧 Collector 拉取）
Control   → Redis List（定向 worker_id，非阻塞排空）
状态快照  → Redis Hash（worker 心跳状态）
事件      → Redis Pub/Sub（实时推送 + 最近 N 条 List 缓存）

降级后无共享可变状态（无防重 claim 集合）。契约测试见 tests/test_redis_bus.py（fakeredis）。

用法：bus = RedisBus(host="localhost", password=None); bus.ping() 确认可用。
密码经 REDIS_PASSWORD 环境变量传入（见 bus.get_bus），省掉「无密码只能挂 SSH 隧道」的部署约束。
"""
from __future__ import annotations
import json
import logging
import time
from typing import Optional

from .bus import Bus
from .models import AttemptResult, ControlSignal, Job

try:
    from redis.exceptions import RedisError
except Exception:  # redis 未安装时 queue.py 不会被真正使用，退化为基类
    RedisError = Exception

log = logging.getLogger("queue")


class RedisBus(Bus):
    def __init__(self, host: str = "localhost", port: int = 6379, db: int = 0,
                 password: Optional[str] = None):
        import redis as redis_lib
        # protocol=2（RESP2）：redis-py 4+ 默认 RESP3 会发 HELLO 握手，老 Redis（<6，如 Windows 3.x 端口）
        # 不认 HELLO 直接报错。本 bus 不用任何 RESP3 专属特性，钉死 RESP2 兼容任意 Redis ≥2.x。
        self.redis = redis_lib.Redis(host=host, port=port, db=db, password=password,
                                     protocol=2, decode_responses=True)
        # pending 消息状态（ack 时机控制）
        self._pending_job_id = None
        self._pending_job_stream = None
        self._pending_job_group = None
        self._ensured_groups: set = set()  # 已创建过的消费者组（避免每 tick 重发 XGROUP CREATE）
        # Streams / groups
        self._job_stream = "ts:jobs"
        self._result_stream = "ts:results"
        self._result_group = "scheduler"
        self._signal_prefix = "ts:signal:"
        self._worker_hset = "ts:workers"
        self._event_list = "ts:events"
        self._event_channel = "ts:events_pub"
        self._event_seq_key = "ts:events_seq"   # INCR 计数器：单调事件序号（WS 游标用，不靠墙钟 ts）
        self._logwatch_prefix = "ts:logwatch:"  # 日志按需订阅：key 存在=有人正在看该 worker 的实时日志
        # XREAD 游标：从最新消息开始，只读新到的（避免重启时回放全部历史）
        self._last_result_id = "0-0"
        self._init_last_result_id()
        self._ensure_groups()

    def ping(self) -> bool:
        return self.redis.ping()

    def _init_last_result_id(self):
        """启动时把游标跳到 stream 末尾，只消费重启后新到的消息。"""
        try:
            entries = self.redis.xrevrange(self._result_stream, count=1)
            if entries:
                self._last_result_id = entries[0][0]
                log.info("result 游标初始化: %s", self._last_result_id)
        except RedisError:
            pass

    def _ensure_groups(self):
        for stream, group in [(self._result_stream, self._result_group)]:
            try:
                self.redis.xgroup_create(stream, group, id="0", mkstream=True)
            except Exception:
                pass  # 已存在

    # ── Job 队列（定向 worker_id：每个 worker 一个 Stream）──
    # 使用消费者组：Worker 崩溃后 Job 留在 PEL，重连时自动恢复，executor.start 后才 ack。
    _job_group = "workers"

    def _job_stream_for(self, worker_id: str) -> str:
        return f"ts:jobs:{worker_id}"

    def _ensure_job_group(self, worker_id: str):
        stream = self._job_stream_for(worker_id)
        # 缓存：已确保过的 group 不再重复 XGROUP CREATE（每 tick 调一次、每次返回 BUSYGROUP 错误）
        cache_key = f"{stream}:{self._job_group}"
        if cache_key in self._ensured_groups:
            return
        try:
            self.redis.xgroup_create(stream, self._job_group, id="0", mkstream=True)
        except Exception:
            pass  # 已存在
        self._ensured_groups.add(cache_key)

    def push_job(self, job: Job) -> None:
        stream = self._job_stream_for(job.worker_id)
        self.redis.xadd(stream, {"data": json.dumps(job.to_dict(), ensure_ascii=False)})

    def pop_job(self, worker_id: str, timeout: float = 1.0) -> Optional[Job]:
        """从 worker 专属 stream 消费 Job。优先恢复 PEL 中未 ack 的（崩溃恢复），再读新消息。"""
        self._ensure_job_group(worker_id)
        stream = self._job_stream_for(worker_id)
        group = self._job_group
        try:
            # 先恢复 PEL 中未 ack 的 pending 消息（Worker 崩溃恢复路径）
            # 注意：redis-py 的签名是 xreadgroup(groupname, consumername, streams, ...)，
            # 必须用位置参数——没有 group=/consumer= 关键字。
            pending = self.redis.xreadgroup(
                group, worker_id, {stream: "0"}, count=1)
            if pending:
                for _stream_name, messages in pending:
                    for msg_id, fields in messages:
                        # pending 消息直接消费，不在这里 ack（executor.start 后 ack）
                        self._pending_job_id = msg_id
                        self._pending_job_stream = stream
                        self._pending_job_group = group
                        return Job.from_dict(json.loads(fields["data"]))
            # 再读新消息（>）
            # block=0 在 Redis 里是「永远阻塞」不是「非阻塞」——timeout=0 时不传 block 参数
            xread_kwargs = {"count": 1}
            if timeout > 0:
                xread_kwargs["block"] = int(timeout * 1000)
            results = self.redis.xreadgroup(
                group, worker_id, {stream: ">"}, **xread_kwargs)
            if not results:
                return None
            for _stream_name, messages in results:
                for msg_id, fields in messages:
                    self._pending_job_id = msg_id
                    self._pending_job_stream = stream
                    self._pending_job_group = group
                    return Job.from_dict(json.loads(fields["data"]))
            return None
        except RedisError as e:
            # 只吞 redis 自身的错误（连接/响应），当作「队列暂不可用」。
            # 编程错误（如调用签名写错）不再被静默——会冒泡到 worker 主循环日志。
            log.debug("pop_job redis 异常(视作空队列): %s", e)
            return None

    def ack_job(self) -> None:
        """ack 最近一次 pop_job 拿到的消息（在 executor.start 成功后调用）。"""
        if self._pending_job_id and self._pending_job_stream and self._pending_job_group:
            try:
                self.redis.xack(self._pending_job_stream, self._pending_job_group,
                                self._pending_job_id)
            except Exception as e:
                log.debug("ack_job 失败(消息将留在 PEL 待恢复，可能被重复消费): %s", e)
            self._pending_job_id = None
            self._pending_job_stream = None
            self._pending_job_group = None

    def get_job_context(self, job_id: str, worker_id: str) -> Optional[dict]:
        """从 worker 的 job stream 中查找已派发 job 的上下文（活动/场次/票档/账号/购票人）。

        用途：attempts 表缺记录时（job 由另一台 API 指派），从 Redis job stream 取富化上下文供推送用。
        按 worker_id 定位 stream，从后往前扫（最近的 job 更可能匹配）。
        """
        stream = self._job_stream_for(worker_id)
        try:
            # 从后往前扫最近 50 条（避免扫整个 stream）
            entries = self.redis.xrevrange(stream, count=50)
            for _eid, fields in entries:
                try:
                    d = json.loads(fields.get("data", "{}"))
                    if d.get("job_id") == job_id:
                        # 提取推送需要的上下文字段
                        acc = d.get("account", {})
                        return {
                            "task_id": d.get("task_id", ""),
                            "screen_name": d.get("screen_name", ""),
                            "sku_desc": d.get("sku_desc", ""),
                            "account_id": acc.get("account_id", ""),
                            "account_label": acc.get("username") or acc.get("account_id", ""),
                            "buyer_names": [b.get("name", "") for b in d.get("buyers", [])],
                        }
                except (json.JSONDecodeError, KeyError):
                    continue
        except RedisError as e:
            log.debug("get_job_context redis 异常: %s", e)
        return None

    # ── Result ──
    def push_result(self, result: AttemptResult) -> None:
        self.redis.xadd(self._result_stream, {
            "data": json.dumps(result.to_dict(), ensure_ascii=False)})

    def pop_result(self, timeout: float = 1.0) -> Optional[AttemptResult]:
        # 用 XREAD（非 XREADGROUP）避免消费者组的 "消息被消费但返回空" 问题。
        # 手动跟踪 last_id 确保每条消息只处理一次。
        try:
            kwargs: dict = {"count": 1}
            if timeout > 0:
                kwargs["block"] = int(timeout * 1000)
            results = self.redis.xread(
                {self._result_stream: self._last_result_id},
                **kwargs)
        except RedisError as e:
            log.debug("pop_result redis 异常(视作空队列): %s", e)
            return None
        if not results:
            return None
        for _stream_name, messages in results:
            for msg_id, fields in messages:
                self._last_result_id = msg_id
                try:
                    r = AttemptResult.from_dict(json.loads(fields["data"]))
                    log.info("pop_result: job=%s success=%s order=%s", r.job_id, r.success, r.order_id)
                    return r
                except Exception as e:
                    log.error("pop_result 解析失败 msg_id=%s: %s", msg_id, e)
                    return None
        return None

    # ── Control（List per worker）──
    def push_signal(self, sig: ControlSignal) -> None:
        self.redis.rpush(self._signal_prefix + sig.worker_id,
                         json.dumps(sig.to_dict(), ensure_ascii=False))

    def drain_signals(self, worker_id: str) -> list:
        key = self._signal_prefix + worker_id
        out = []
        while True:
            raw = self.redis.lpop(key)
            if raw is None:
                break
            out.append(ControlSignal.from_dict(json.loads(raw)))
        return out

    # ── Worker 心跳 ──
    def heartbeat(self, worker_id: str, status: dict) -> None:
        self.redis.hset(self._worker_hset, worker_id,
                        json.dumps({**status, "ts": time.time()}, ensure_ascii=False))

    def list_workers(self) -> dict:
        raw = self.redis.hgetall(self._worker_hset)
        return {k: json.loads(v) for k, v in raw.items()}

    def forget_worker(self, worker_id: str) -> None:
        self.redis.hdel(self._worker_hset, worker_id)

    # ── 日志按需订阅（前端打开某 worker 日志面板时 mark，周期续期；worker 据此决定推不推流）──
    def mark_log_interest(self, worker_id: str, ttl: float = 30.0) -> None:
        self.redis.set(self._logwatch_prefix + worker_id, "1", ex=max(1, int(ttl)))

    def log_wanted(self, worker_id: str) -> bool:
        return bool(self.redis.exists(self._logwatch_prefix + worker_id))

    # ── 事件 ──
    def publish(self, event: dict) -> None:
        seq = self.redis.incr(self._event_seq_key)
        data = json.dumps({**event, "ts": event.get("ts", time.time()), "seq": seq},
                          ensure_ascii=False)
        self.redis.rpush(self._event_list, data)
        self.redis.ltrim(self._event_list, -1000, -1)
        try:
            self.redis.publish(self._event_channel, data)
        except Exception as e:
            log.debug("事件 pub/sub 推送失败(已落 list，不影响 recent_events): %s", e)

    def recent_events(self, n: int = 100) -> list:
        raw = self.redis.lrange(self._event_list, -n, -1)
        return [json.loads(x) for x in raw]

    # 增量过滤 Lua 脚本：服务端过滤 seq > given_seq 的事件，只返回匹配项，
    # 避免把 1000 条全量 JSON 拉到 Python 再过滤（每秒数十次 = 大量无用带宽）。
    _LUA_EVENTS_SINCE = """
    local key = KEYS[1]
    local after_seq = tonumber(ARGV[1])
    local scan_n = tonumber(ARGV[2])
    local raw = redis.call('lrange', key, -scan_n, -1)
    local out = {}
    for i = 1, #raw do
        -- 查找 "seq": 后的数字（JSON 格式固定：..."seq": 12345,...）
        local pat_start, pat_end = string.find(raw[i], '"seq":%s*')
        if pat_start then
            local num_start = pat_end + 1
            local non_digit = string.find(raw[i], '[^%d]', num_start)
            local num_str
            if non_digit then
                num_str = string.sub(raw[i], num_start, non_digit - 1)
            else
                num_str = string.sub(raw[i], num_start)
            end
            local val = tonumber(num_str)
            if val and val > after_seq then
                out[#out + 1] = raw[i]
            end
        end
    end
    return out
    """

    def events_since(self, seq: int, n: int = 1000) -> list:
        """返回 seq > given_seq 的事件（WS 增量推送用）。

        用 Lua 脚本在 Redis 服务端过滤，只返回匹配的 JSON 条目——避免把 1000 条全量
        序列化/传输到 Python 再过滤。每个 WS 客户端每秒 4 次调用 × 1000 条 × 133B ≈
        520KB/s 的无用传输降到接近 0（无新事件时返回空列表不传数据）。
        """
        try:
            raw = self.redis.eval(
                self._LUA_EVENTS_SINCE, 1,
                self._event_list, seq, n)
            return [json.loads(x) for x in raw]
        except RedisError:
            # Lua 脚本失败时退化到基类实现（全量 + Python 过滤）
            return [ev for ev in self.recent_events(n) if ev.get("seq", 0) > seq]

    # 类型过滤 Lua 脚本：在 Redis 内按 type 字段过滤，只返回匹配的事件。
    # 用于 WS 连接时回放（只回放 worker_log/worker_error），避免把 400 条全量拉到 Python 再过滤。
    _LUA_RECENT_FILTERED = """
    local key = KEYS[1]
    local n = tonumber(ARGV[1])
    local ntypes = tonumber(ARGV[2])
    local types = {}
    for i = 1, ntypes do
        types[ARGV[2 + i]] = true
    end
    local raw = redis.call('lrange', key, -n, -1)
    local out = {}
    for i = 1, #raw do
        for t in pairs(types) do
            if string.find(raw[i], '"type"%s*:%s*"' .. t .. '"') then
                out[#out + 1] = raw[i]
                break
            end
        end
    end
    return out
    """

    def _recent_events_filtered(self, n: int, types: tuple) -> list:
        """在 Redis 内按 type 字段过滤，只返回匹配的事件（不传全量到 Python）。"""
        try:
            raw = self.redis.eval(
                self._LUA_RECENT_FILTERED, 1,
                self._event_list, n, len(types), *types)
            return [json.loads(x) for x in raw]
        except RedisError:
            return [ev for ev in self.recent_events(n)
                    if ev.get("type") in types]

    def close(self) -> None:
        pass  # Redis 客户端会自动回收
