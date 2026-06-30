# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`ticket-dispatcher` is a **generic ticket dispatch framework** — a manual dispatch console + distributed Worker fleet for orchestrating black-box processes. It does NOT execute any ticket-buying logic itself; that's the job of user-implemented **Drivers**.

The framework provides: Bus abstraction (Redis/In-memory), Worker lifecycle management, Job dispatching, result collection, SQLite persistence, real-time event streaming (WebSocket), and a Vue 3 dashboard.

## Commands

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt  # pytest

python main.py api                # API node (FastAPI + Collector, 0.0.0.0:8000)
python main.py worker             # Worker agent node
python main.py status             # View account pool + worker heartbeats
DRYRUN=1 python main.py api       # DryRunDriver, no real binary

pytest tests/ -q                  # All tests
pytest tests/test_parser.py -q    # Single test file
pytest tests/test_assign.py -q -k stop   # Filter by name

cd dashboard && npm install && npm run dev   # Frontend dev (port 3000, proxy to 8000)
docker-compose up -d              # redis + api + worker
```

## Architecture

Two roles communicate via **Bus** (Redis Streams or InMemory). No scheduler process — assignment is an HTTP push, Collector is a passive background thread.

- **`src/driver/base.py`** — Driver ABC: `prepare(job) -> str`, `start(job, slot_idx) -> handle`, `poll(handle) -> (RunState, AttemptResult)`, `cancel(handle)`, `cleanup(handle)`, `new_output(handle) -> list`
- **`src/driver/__init__.py`** — Driver registry: `register_driver(program, cls)` / `get_driver(program)`
- **`src/driver/dryrun.py`** — DryRunDriver (built-in reference implementation)
- **`src/driver/_parse.py`** — Shared stdout parser: `parse_target_output()`, `SUCCESS_KEYWORDS`, `add_success_keywords()`
- **`src/driver/_proctree.py`** — `kill_process_tree()` for cleaning up child processes
- **`src/converters/__init__.py`** — Converter registry: `register_converter(program, converter)` / `get_converter(program)`
- **`src/models.py`** — `Buyer`, `Account`, `Job`, `AttemptResult`, `ControlSignal`, `WorkerConfig`
- **`src/bus.py`** — `InMemoryBus` + `get_bus()` factory
- **`src/queue.py`** — `RedisBus` (Streams consumer groups + PEL recovery)
- **`src/worker_agent.py`** — Worker main loop: register → heartbeat → consume → execute → report
- **`src/executor.py`** — Manages concurrent Driver handles (max_slots)
- **`src/api.py`** — FastAPI endpoints (Worker CRUD, account CRUD, assign/stop/pause/resume, events/WS) + `register_api_router()` for platform extensions
- **`src/platform/bilibili.py`** — Bilibili platform API extension (QR login, event info, stock, buyer list, account refresh). Registered via `register_api_router()`, mounted at `/bilibili/`
- **`src/collector.py`** — Passive thread: drain results → store + broadcast events
- **`src/store.py`** — SQLite persistence (attempts/results/worker_events)
- **`src/notify.py`** — Push notifications (configurable channel)
- **`src/timeutil.py`** — Time utilities (CST timezone)

## Extending

To add a new ticket platform:
1. Implement `Driver` subclass (see `src/driver/example.py` and `docs/driver_development.md`)
2. Optionally implement `Converter` (see `src/converters/example.py` and `docs/converter_development.md`)
3. Optionally add platform API endpoints (QR login, event lookup, etc.) via `register_api_router()` (see `src/platform/bilibili.py`)
4. Register everything in your entry point before starting the worker

## Testing

Tests use `InMemoryBus` + `ScriptedDriver` (test double) + `FakeClock` (virtual time). No real binaries or Redis needed.

Key test patterns:
- `test_assign.py` — end-to-end assign chain with ScriptedDriver
- `test_driver_contract.py` — Driver ABC contract (prepare/state/cancel idempotency)
- `test_parser.py` — offline stdout parser tests
- `test_redis_bus.py` — fakeredis contract (consumer groups + PEL)

## Conventions

- Code comments, logs, and docs are in **Chinese** — match that style.
- Money is in **分 (cents)**.
- `config/runtime/{worker_id}/{job_id}/` is per-job working directory (auto-created by Driver).
- `.gitignore` excludes secrets (`config/accounts.yaml`, `config/settings.yaml`).
