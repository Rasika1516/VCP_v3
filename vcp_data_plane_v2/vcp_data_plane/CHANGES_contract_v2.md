# VCP Simulator — Control/Data Plane integration fixes (contract v2)

Baseline: `vcp_control_plane_1.zip` + `vcp_data_plane.zip` (both suites green but the two
planes could not talk to each other). Date: 2026-10-28.

## The four integration gaps (fixed)

| # | Gap | Fix |
|---|-----|-----|
| 1 | CP wrote flat fields; worker only read `command_json` → every command ACK'd and dropped | CP publishes the v2 `command_json` envelope; worker also accepts the legacy flat layout. Unusable commands are reported back as `command_result` (no silent drops). |
| 2 | UI commands `reset`, `change_state`, `unlock_connector`, `get_configuration` unknown to worker | Implemented on the worker as charger-side simulations (reboot + BootNotification, availability + StatusNotification, end session/unplug/unlock, return config keys). Full worker command set exposed in UI via `GET /api/v1/commands`. CP rejects unknown command types with 422. |
| 3 | Scenario catalogues disjoint (only `fault_and_recover` shared) | Union of 10: worker's 8 + new `boot_storm`, `meter_value_flood`. v1 IDs `basic_charge_cycle`, `rapid_reconnect`, `remote_start_stop` kept as aliases (param names translated). Executions now complete: workers report `scenario_status` per VCP → `passed / failed / partial`. Cancel sends `stop_scenario`. |
| 4 | Worker never wrote `vcp:events:worker:{id}` → dashboard/events empty | New worker `EventPublisher`: `worker_status`, `state_change`, `meter_value`, `ocpp_trace` (switchable), `command_result`, `scenario_status`. CP subscriber persists state, acknowledges commands, closes executions, auto-registers VCPs, refreshes streams every 5 s. |

Contract is defined once per side (`vcp_data_plane/contract.py`, `vcp_control_plane/bus/contract.py`)
and pinned by tests on both sides plus a cross-check test.

## Further bugs found on the same path (fixed)

- Broadcast commands reached only one worker (shared consumer group) → per-worker group `data-plane:{worker_id}`.
- Group-scoped commands ignored `group_id` → worker filters by group (VCP entries now carry `group_id`).
- `run_scenario` payload keys mismatched (`scenario_id` vs `scenario`/`params`) → both accepted.
- CP polled `GET /fleet/summary` which the worker lacked → endpoint added.
- CP derived event stream names from worker URLs → now uses real worker IDs (`VCP_CP_WORKERS=id=url`, `/health` lookup, Redis discovery).
- Worker could not connect to any `ws://` CSMS (TLS context passed for security profile 1) → TLS only for profiles 2/3 / `wss://`; fixed `auth.auth_type` typo on profile 3.
- OCPP trace publisher was never wired to the router → all frames traced (`vcp:trace:*` + `ocpp_trace` events).
- BootNotification waited a full retry interval at start-up, and was never re-sent after reconnect → boot on (re)connect.
- `unplug_ev` after `stop_charge` raised an illegal Available→Available transition (broke every charge-cycle scenario).
- Prometheus metrics registered with `registry=None` (never exported) → default registry.
- Command audit row was written after publishing → fast results were lost; now written first.
- Vite proxy did not forward WebSockets (Live Events via dev server); shared UI styles lived in lazy page CSS.

## New feature: VCP Fleets (define → generate worker.yaml → deploy)

- Table `vcp_fleet`, column `vcp_instance.fleet_id`, `command_audit.result` (Alembic `002`, verified up/down on PostgreSQL 16).
- `/api/v1/fleets`: create (generates N VCPs + ports), edit (flags *redeploy required*), config preview, deploy, stop, redeploy, logs, delete; `/fleets/launchers`, `/fleets/profiles`.
- Launchers: **process** (`vcp-worker run`, detached, survives CP restart) and **docker** (Engine API over the socket; bridge+published ports, `host`, or a shared network).
- Crash monitor marks a dead worker's fleet **failed** with the log tail (with a deploy grace period).
- **Register VCP** requires a fleet; VCP joins that fleet's config and goes live on deploy/redeploy.
- UI: new *VCP Fleets* page (list, create/edit, deploy/stop/redeploy, overview / generated config / live logs); VCP list fleet & state filters; catalogue-driven command panel with results in the audit; scenario progress & results; worker table with source and live counts.

## Verification

- Data plane: 191 tests (24 new incl. real worker + mock CSMS + fakeredis integration).
- Control plane: 66 tests (21 new incl. a both-planes end-to-end test).
- Live: PostgreSQL 16 + Redis 7 + uvicorn + mock CSMS — 50-VCP fleet via process launcher
  (50/50 connected in ~3–5 s, commands acked, fleet/group scenarios passed, register+redeploy,
  kill -9 → failed, stop) and a 20-VCP OCPP 2.0.1 fleet via the Docker launcher.
- `npm run build` clean; all pages checked in Chromium with no console errors.

## Upgrade notes

- Run `alembic upgrade head` (adds migration 002).
- `POST /api/v1/vcps` now requires `fleetId`.
- Scenario IDs returned by the API are the canonical ones (aliases still accepted on input).
- Worker env `VCP_CP_WORKER_URLS_RAW` (docker-compose) replaced by `VCP_CP_WORKERS` / `VCP_CP_WORKER_URLS`.
- Process launcher needs `vcp_data_plane` installed on the control-plane host; Docker launcher needs the `vcp-data-plane` image and the Docker socket.
- Generated `worker.yaml` contains the CSMS password in clear text (API preview masks it) — keep `VCP_CP_FLEET_CONFIG_DIR` access-restricted.
