# VCP Data Plane

High-concurrency OCPP 1.6J / 2.0.1 Virtual Charging Point (VCP) simulation worker for the VCP Fleet Simulator platform.

---

## Overview

The **VCP Data Plane** is the runtime engine that drives thousands of simulated EV charging stations (Virtual Charging Points). Each worker process manages a fleet of VCPs, maintaining persistent WebSocket connections to a CSMS (Central System Management Software), executing realistic charge-point behaviour, and responding to commands from the Control Plane.

Key design goals:

- **Scale** — a single Python asyncio worker handles 500+ VCPs. Horizontal scaling to 10 000+ VCPs is achieved by running multiple workers behind a sharding coordinator.
- **Protocol fidelity** — full OCPP 1.6J and 2.0.1 message flows including multi-EVSE, multi-connector layouts, authorisation, metering, diagnostics, and firmware simulation.
- **Realistic behaviour** — brand/model profiles drive connector type, power envelope, ramp curves, and charge cycle timing. Meter values include AC three-phase calculations and DC power curves.
- **Fault injection** — eight fault types (network disconnect, reconnect storm, message timeout, malformed payload, connector fault, power outage, slow response, duplicate messages) available at runtime via command.
- **Observable** — Prometheus metrics, Redis Stream OCPP traces, and a FastAPI control API per worker.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        VCP Worker Process                       │
│                                                                 │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────┐   │
│  │  Command     │   │  VCP Runtime │   │  Worker API      │   │
│  │  Listener   ─┼──►│  (per VCP)   │   │  (FastAPI :8080) │   │
│  │  (Redis      │   │              │   └──────────────────┘   │
│  │   Streams)   │   │  Connection  │                           │
│  └──────────────┘   │  Manager     │─── WebSocket ──► CSMS    │
│                      │  OCPP Router │                           │
│  ┌──────────────┐   │  State       │                           │
│  │  Trace       │   │  Machine     │                           │
│  │  Publisher   │   │  Behavior    │                           │
│  │  (Redis      │◄──│  Engine      │                           │
│  │   Streams)   │   │  Meter       │                           │
│  └──────────────┘   │  Generator   │                           │
│                      │  Fault       │                           │
│  ┌──────────────┐   │  Injector    │                           │
│  │  Metrics     │   │  Scenario    │                           │
│  │  Prometheus  │   │  Runner      │                           │
│  │  (:9090)     │   └──────────────┘                           │
│  └──────────────┘                                               │
└─────────────────────────────────────────────────────────────────┘
         │                                    │
         ▼                                    ▼
     Redis (command bus,              CSMS (real or mock)
     state cache, traces)
```

### Component summary

| Component | Module | Responsibility |
|---|---|---|
| ConnectionManager | `connection/manager.py` | WebSocket lifecycle, TLS, exponential-backoff reconnect |
| OcppRouter | `ocpp/router.py` | CALL/CALLRESULT/CALLERROR correlation, inbound dispatch |
| V16Adapter / V201Adapter | `ocpp/v16/adapter.py`, `ocpp/v201/adapter.py` | Protocol-version message builders |
| V16Handlers / V201Handlers | `ocpp/v16/handlers.py`, `ocpp/v201/handlers.py` | CSMS→CP inbound message handlers |
| VcpStateMachine | `state/machine.py` | 9-state FSM with legal-transition enforcement |
| VcpRegistry | `state/registry.py` | Redis-backed state publication and worker registration |
| BehaviorEngine | `behavior/engine.py` | Charge cycle orchestration, CSMS command callbacks |
| MeterGenerator | `behavior/meter.py` | AC/DC power ramp curves, energy accumulation, sampled values |
| VcpTimerScheduler | `behavior/timer.py` | Heartbeat, meter-value, session-timeout, authorize-timeout timers |
| FaultInjector | `fault/injector.py` | Eight fault-injection modes with recovery |
| ScenarioRunner | `scenario/scenarios.py` | Eight built-in named scenarios as async generators |
| CommandListener | `command/listener.py` | Redis Streams XREADGROUP consumer, XAUTOCLAIM recovery |
| TracePublisher | `trace/publisher.py` | Per-VCP OCPP trace to Redis Streams |
| WorkerMetrics | `metrics/collector.py` | Prometheus counters, gauges, and histograms |
| Worker API | `api/worker_api.py` | FastAPI endpoints: health, status, VCP commands, fleet commands |
| WorkerRunner | `worker/runner.py` | Top-level wiring: starts all components, dispatches commands |
| CLI | `cli.py` | `vcp-worker run / validate / list-profiles` |

---

## Installation

### Prerequisites

- Python 3.11+
- Redis 7+

### Development install

```bash
git clone <repo-url>
cd vcp_data_plane
pip install -e ".[dev]"
```

### Production install

```bash
pip install .
```

---

## Quick Start

### 1. Start Redis

```bash
docker run -d --name redis -p 6379:6379 redis:7-alpine
```

### 2. Create a worker config

```bash
cp configs/worker.example.yaml configs/worker.yaml
# Edit worker.yaml: set csms.url to point at your CSMS
```

### 3. Validate the config

```bash
vcp-worker validate configs/worker.yaml
# ✓ Config valid: worker-01 (120 VCPs)
```

### 4. Run the worker

```bash
vcp-worker run configs/worker.yaml
# Worker worker-01 running: API http://0.0.0.0:8080  Metrics http://0.0.0.0:9090
```

### 5. Check health

```bash
curl http://localhost:8080/health
# {"status": "ok", "worker_id": "worker-01"}

curl http://localhost:8080/status
# {"worker_id": "worker-01", "vcp_total": 120, "state_distribution": {...}}
```

---

## Docker

### Build

```bash
docker build -f docker/Dockerfile -t vcp-data-plane:latest .
```

### Run with Docker Compose

```bash
# Start Redis + two workers (AC fleet + DC fleet)
docker compose up

# Also start RedisInsight GUI (http://localhost:8001)
docker compose --profile tools up
```

The compose file expects `configs/worker-ac.yaml` and `configs/worker-dc.yaml`. Copy and customise from `configs/worker.example.yaml`.

---

## Configuration Reference

### Worker config (`worker.yaml`)

| Key | Type | Description |
|---|---|---|
| `worker_id` | string | Unique worker identifier (used in Redis keys, metrics labels) |
| `csms.url` | string | WebSocket URL of the CSMS (`ws://` or `wss://`) |
| `csms.security_profile` | int 0–3 | 0=plain, 1=HTTP Basic, 2=TLS, 3=mTLS |
| `redis.host` | string | Redis host (supports `${ENV_VAR}` substitution) |
| `redis.port` | int | Redis port (default 6379) |
| `redis.password` | string | Redis AUTH password (optional) |
| `profile_dir` | path | Directory containing charger profile YAML files |
| `fleet.ranges` | list | Auto-generate VCP IDs from numeric ranges |
| `vcps` | list | Explicit VCP entries (combined with fleet ranges) |
| `sharding.total_shards` | int | Number of workers in the cluster |
| `sharding.shard_index` | int | This worker's zero-based shard index |
| `boot_jitter_ms` | int | Max random delay before first connect (spread load) |
| `control_api.port` | int | FastAPI control port (default 8080) |
| `metrics.port` | int | Prometheus scrape port (default 9090) |

### Charger profile (`profiles/*.yaml`)

| Key | Description |
|---|---|
| `profile_id` | Unique identifier referenced by VCP entries |
| `vendor` / `model` | Vendor and model names sent in BootNotification |
| `firmware_version` | Firmware version string |
| `ocpp_version` | `"1.6J"` or `"2.0.1"` |
| `evses[].evse_id` | EVSE identifier (1-based) |
| `evses[].connectors[]` | Connector type, max power, voltage, phases, current |
| `capabilities.*` | Feature flags (remote_start_stop, smart_charging, …) |
| `behavior.*` | Runtime timing defaults (heartbeat, meter value interval, ramp duration, …) |

---

## Horizontal Scaling

To run 10 000 VCPs across 20 workers:

```yaml
# worker-00.yaml
sharding:
  total_shards: 20
  shard_index: 0

# worker-01.yaml
sharding:
  total_shards: 20
  shard_index: 1
# … and so on
```

Each worker claims its shard of the VCP list by consistent hash. All workers share the same Redis instance and the same consumer group (`data-plane-workers`). The Control Plane sends per-VCP commands to the stream `vcp:commands:worker:{worker_id}` and broadcasts to `vcp:commands:broadcast`.

Kubernetes deployment with HPA on CPU or Redis queue depth is the recommended production topology. A sample Helm chart is maintained in the sibling `vcp-helm` repository.

---

## Control API

The worker exposes a FastAPI app on port 8080 (configurable). All endpoints except `/health` and `/ready` require a `Authorization: Bearer <token>` header when `control_api.require_auth: true`.

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness probe |
| GET | `/ready` | Readiness probe (VCPs registered) |
| GET | `/status` | Worker summary and state distribution |
| GET | `/vcps` | List all VCPs with current state snapshot |
| GET | `/vcps/{vcp_id}` | Single VCP state detail |
| POST | `/vcps/{vcp_id}/command` | Send a typed command to one VCP |
| POST | `/fleet/command` | Broadcast a command to all (or a group of) VCPs |
| POST | `/vcps/{vcp_id}/scenario` | Run a named scenario on one VCP |

### Example — start a charge session

```bash
curl -X POST http://localhost:8080/vcps/SIM-AC-001/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "plug_ev", "payload": {"evse_id": 1, "connector_id": 1}}'

curl -X POST http://localhost:8080/vcps/SIM-AC-001/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "start_charge", "payload": {"id_tag": "TEST-TAG"}}'
```

### Example — run a fleet scenario

```bash
curl -X POST http://localhost:8080/fleet/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "run_scenario", "payload": {"scenario": "morning_rush", "params": {}}}'
```

---

## Command Types

Commands arrive on `vcp:commands:worker:{worker_id}` (targeted) or `vcp:commands:broadcast`
(fleet / group) as a JSON `WorkerCommand` in the `command_json` field — see
[Control-plane bus contract (v2)](#control-plane-bus-contract-v2). Every command produces a
`command_result` event.

| Command | Payload | Description |
|---|---|---|
| `connect` / `disconnect` / `reconnect` | – | WebSocket control |
| `send_boot_notification` | – | Re-run the boot sequence |
| `plug_ev` | `id_tag`, `evse_id`, `connector_id` | Simulate EV cable plug-in |
| `authorize` | `id_tag` | Authorize at the CSMS |
| `start_charge` / `stop_charge` | `reason` (stop) | Start / end a transaction |
| `unplug_ev` | – | Simulate EV unplug |
| `inject_fault` | `fault_type`, fault params | Inject a fault (below) |
| `recover` | – | Clear the active fault |
| `send_meter_values` | `evse_id`, `connector_id` | Send one MeterValues |
| `run_scenario` | `scenario_id`, `execution_id`, scenario params | Start a scenario (below) |
| `stop_scenario` | – | Cancel the running scenario |
| `get_state` | – | Return the state snapshot in the result |
| `reset` | `type`: `Soft` \| `Hard` | Simulated reboot: end sessions, drop link, reconnect, BootNotification |
| `change_state` | `desired_state`: `Available` \| `Unavailable`, `connector_id?` | Change availability + StatusNotification |
| `unlock_connector` | `connector_id` | End session if any, unplug, unlock |
| `get_configuration` | `keys?` | Return configuration keys in the result |

`reset`, `change_state`, `unlock_connector` and `get_configuration` are CSMS→CP requests in
OCPP; here the operator triggers the equivalent **charger-side** behaviour and the VCP reports
it to the CSMS exactly as a real charger would.

---

## Fault Injection

| Fault type | Effect |
|---|---|
| `network_disconnect` | Drops the WebSocket connection immediately |
| `reconnect_storm` | Rapidly reconnects multiple times |
| `message_timeout` | Delays CALL responses to trigger CSMS timeout |
| `malformed_payload` | Sends syntactically invalid JSON |
| `connector_fault` | Transitions VCP to Faulted state |
| `power_outage` | Simulates loss of power (disconnect + offline state) |
| `slow_response` | Introduces configurable latency on all outbound messages |
| `duplicate_messages` | Sends each CALL twice |

---

## Built-in Scenarios

| Scenario | Key params | Description |
|---|---|---|
| `boot_and_wait` | `wait_s` | Baseline connectivity check |
| `plug_authorize_charge` | `charge_duration_s`, `id_tag` | Full charge cycle (alias `basic_charge_cycle`, `duration_s`) |
| `remote_start_charge` | `wait_timeout_s`, `charge_duration_s` | Wait for CSMS RemoteStart (alias `remote_start_stop`) |
| `fault_and_recover` | `fault_type`, `error_code`, `fault_duration_s` | Inject, hold, recover |
| `reconnect_storm` | `cycles`, `interval_s` | Rapid reconnects (alias `rapid_reconnect`) |
| `load_ramp` | `ramp_steps`, `step_duration_s` | Session held over ramp steps |
| `transaction_timeout` | `timeout_s` | Let the profile's max duration stop the session |
| `csms_initiated_stop` | `wait_for_stop_s` | Charge until the CSMS sends RemoteStop |
| `boot_storm` | `jitter_ms`, `down_s`, `boot_timeout_s` | Drop + reconnect + re-boot; run fleet-wide for a BootNotification surge |
| `meter_value_flood` | `duration_s`, `interval_s` | High-rate MeterValues during a session |

Each VCP reports progress as `scenario_status` events (`running` → `step`… → `completed` \|
`failed` \| `cancelled`) carrying the control plane's `execution_id`.

Run a scenario via the worker API:

```bash
curl -X POST http://localhost:8080/vcps/SIM-00001/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "run_scenario", "payload": {"scenario_id": "boot_storm", "jitter_ms": 500}}'
```

---

## Prometheus Metrics

Scraped at `http://localhost:9090/metrics`.

| Metric | Type | Description |
|---|---|---|
| `vcp_worker_vcps_total` | Gauge | Total VCPs managed by this worker |
| `vcp_worker_vcps_connected` | Gauge | VCPs with active WebSocket connections |
| `vcp_worker_connections_established_total` | Counter | Successful WS connections |
| `vcp_worker_connections_failed_total` | Counter | Failed WS connection attempts |
| `vcp_worker_reconnect_attempts_total` | Counter | Reconnect attempts |
| `vcp_worker_ocpp_messages_sent_total` | Counter | OCPP messages sent (by action) |
| `vcp_worker_ocpp_messages_received_total` | Counter | OCPP messages received (by action) |
| `vcp_worker_ocpp_message_latency_seconds` | Histogram | Round-trip CALL/CALLRESULT latency |
| `vcp_worker_transactions_started_total` | Counter | Charging transactions started |
| `vcp_worker_transactions_stopped_total` | Counter | Charging transactions stopped |
| `vcp_worker_active_transactions` | Gauge | VCPs currently in a charging transaction |
| `vcp_worker_vcps_by_state` | Gauge | VCP count per state (by worker_id, state) |
| `vcp_worker_faults_injected_total` | Counter | Fault injections (by fault_type) |
| `vcp_worker_commands_received_total` | Counter | Commands received (by command_type) |
| `vcp_worker_heartbeats_sent_total` | Counter | Heartbeats sent |

---

## Redis Stream Keys

| Key pattern | Purpose |
|---|---|
| `vcp:commands:worker:{worker_id}` | Per-worker command stream (group `data-plane`) |
| `vcp:commands:broadcast` | Fleet / group broadcast (one group per worker: `data-plane:{worker_id}`) |
| `vcp:events:worker:{worker_id}` | Worker → control-plane events (contract v2) |
| `vcp:trace:{vcp_id}` | Per-VCP OCPP message trace |
| `vcp:state:{vcp_id}` | Per-VCP state hash (HSET) |
| `vcp:registry` | Global VCP → worker_id mapping |
| `vcp:worker:{worker_id}` | Set of VCP IDs managed by this worker |

---

## Control-plane bus contract (v2)

The single source of truth is [`src/vcp_data_plane/contract.py`](src/vcp_data_plane/contract.py);
`vcp_control_plane/bus/contract.py` is an identical copy and both test suites pin it.

**Commands** — entry fields: `command_json` (JSON `WorkerCommand`: `command_id`, `command_type`,
`vcp_id`, `group_id`, `payload`, `issued_by`, `issued_at`) plus `command_type` / `vcp_id`
duplicated for readability. The legacy v1 flat layout is still accepted.
Targeting: `vcp_id` → that VCP (ignored by workers that don't own it); `group_id` → VCPs of
that group on every worker; neither → every VCP. Commands run concurrently (serialised per VCP)
so a slow command never blocks the stream.

**Events** — `XADD vcp:events:worker:{worker_id}` with `event`, `vcp_id`, `worker_id`,
`payload` (JSON), `ts`:

| Event | Payload |
|---|---|
| `worker_status` | `status` (online \| heartbeat \| offline), `vcp_count`, `vcps[]` (on online), `contract_version` |
| `state_change` | `actual_state`, `connection_state`, `connector_state`, ids/profile/group |
| `meter_value` | `energy_wh`, `power_w`, `current_a`, `voltage_v`, `soc_pct`, `transaction_id` |
| `ocpp_trace` | `dir` (→ ← ✗), `action` (`BootNotification`, `BootNotification.conf`, …), `payload` |
| `command_result` | `command_id`, `command_type`, `ok`, `error`, `result` |
| `scenario_status` | `execution_id`, `scenario_id`, `status`, `message` |

Normalised states: `connection_state` ∈ connected · connecting · disconnected;
`actual_state` ∈ idle · preparing · authorized · charging · suspended · finishing · faulted ·
unavailable · offline · unknown.

`ocpp_trace` forwarding can be switched off per worker (`redis.forward_traces_to_events: false`)
for very large fleets; `vcp:trace:{vcp_id}` is always written.

### Mock CSMS

For local runs and demos without a real CSMS:

```bash
python -m vcp_data_plane.devtools.mock_csms --port 7000   # ws://localhost:9000/<cp_id>
```

---

## Development

### Running tests

```bash
pip install -e ".[dev]"
pytest
```

### Code style

```bash
ruff check src tests
```

### Project structure

```
vcp_data_plane/
├── pyproject.toml
├── docker-compose.yml
├── docker/
│   └── Dockerfile
├── configs/
│   ├── worker.example.yaml
│   └── profiles/
│       ├── generic_ac_ocpp16.yaml
│       └── generic_dc_ocpp201.yaml
├── src/
│   └── vcp_data_plane/
│       ├── cli.py
│       ├── api/
│       │   └── worker_api.py
│       ├── behavior/
│       │   ├── engine.py
│       │   ├── meter.py
│       │   └── timer.py
│       ├── command/
│       │   └── listener.py
│       ├── config/
│       │   ├── loader.py
│       │   └── schema.py
│       ├── connection/
│       │   ├── manager.py
│       │   └── transport.py
│       ├── fault/
│       │   └── injector.py
│       ├── metrics/
│       │   └── collector.py
│       ├── ocpp/
│       │   ├── common.py
│       │   ├── router.py
│       │   ├── v16/
│       │   │   ├── adapter.py
│       │   │   ├── handlers.py
│       │   │   └── messages.py
│       │   └── v201/
│       │       ├── adapter.py
│       │       ├── handlers.py
│       │       └── messages.py
│       ├── scenario/
│       │   └── scenarios.py
│       ├── state/
│       │   ├── machine.py
│       │   └── registry.py
│       ├── trace/
│       │   └── publisher.py
│       └── worker/
│           └── runner.py
└── tests/
    ├── conftest.py
    └── unit/
        ├── test_state_machine.py
        ├── test_meter_generator.py
        ├── test_profile_loader.py
        ├── test_ocpp_v16.py
        ├── test_ocpp_v201.py
        ├── test_fault_injector.py
        └── test_timer_scheduler.py
```

---

## Relation to vcp_fleet_simulator (Control Plane)

This project is the **Data Plane** counterpart to `vcp_fleet_simulator` (the Control Plane). They communicate exclusively through Redis:

```
vcp_fleet_simulator          vcp_data_plane
(Control Plane)              (Data Plane)
      │                            │
      │── vcp:commands:* ─────────►│  Commands to workers (command_json)
      │◄─ vcp:events:worker:* ─────│  Events (contract v2)
      │◄─ vcp:trace:*   ───────────│  OCPP message traces
      │◄─ vcp:state:*   ───────────│  Current VCP state (HASH)
```

The Control Plane is responsible for: scenario orchestration, the web UI, user-facing REST API, authentication, fleet configuration management, and analytics. The Data Plane is responsible for: WebSocket connections, OCPP protocol execution, state machine transitions, metering, and fault injection.

---

## License

Proprietary. All rights reserved.
