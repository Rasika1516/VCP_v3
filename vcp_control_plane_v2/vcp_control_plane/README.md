# VCP Fleet Simulator — Control & Management Plane

A React + FastAPI control plane for the VCP Fleet Simulator. Provides a REST/WebSocket API and browser UI for defining and deploying **VCP fleets**, and for monitoring and commanding virtual charge points (VCPs).

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    React SPA (Vite)                          │
│ Dashboard · VCP Fleets · Fleet · VCPs · Profiles · Scenarios │
└────────────────────────┬─────────────────────────────────────┘
                         │ REST + WebSocket
┌────────────────────────▼─────────────────────────────────────┐
│              FastAPI Control Plane  (:8000)                   │
│  /api/v1/fleets /vcps /fleet /profiles /scenarios /commands  │
│  /api/v1/events/ws                                           │
│                                                              │
│  VcpRegistryStore (PostgreSQL)  ←→  Alembic migrations      │
│  FleetService → worker.yaml → Process / Docker launcher ──┐  │
│  CommandPublisher  →  Redis Streams  →  Data Plane Workers │  │
│  EventSubscriber   ←  Redis Streams  ←  Data Plane Workers ◄┘ │
│  WorkerDirectory (static · fleets · discovered)              │
└──────────────────────────────────────────────────────────────┘
```

### Redis Stream Keys (matching vcp_data_plane)

| Direction        | Stream Key Pattern                      |
|------------------|-----------------------------------------|
| Control → Worker | `vcp:commands:worker:{worker_id}`       |
| Control → All    | `vcp:commands:broadcast`                |
| Worker → Control | `vcp:events:worker:{worker_id}`         |

Message layout and event vocabulary: **bus contract v2** — see
[`src/vcp_control_plane/bus/contract.py`](src/vcp_control_plane/bus/contract.py) and the
data-plane README.

## VCP Fleets (define → generate config → deploy)

A **fleet** is one data-plane worker plus the VCPs it simulates.

1. **VCP Fleets → New Fleet**: name, CSMS URL (+ HTTP Basic), profile, OCPP version, number of
   VCPs, ID prefixes, group, launcher. The control plane creates the VCP rows
   (`{idPrefix}{n:05d}` / `{chargePointIdPrefix}{n:05d}`) and allocates worker ports.
2. **Deploy** writes `{VCP_CP_FLEET_CONFIG_DIR}/{fleet}/worker.yaml` (+ the profile YAMLs it
   needs) and starts the worker:
   * `process` — `vcp-worker run worker.yaml` on the control-plane host (needs
     `vcp_data_plane` installed); the worker keeps running across control-plane restarts.
   * `docker` — one container per fleet from `VCP_CP_DOCKER_IMAGE`, config bind-mounted at
     `/config`, via the Docker Engine API (`VCP_CP_DOCKER_SOCKET`).
3. The worker announces itself (`worker_status`) → fleet **running**, VCPs auto-linked.
4. Edits (count, CSMS, profile, group, options) or **Register VCP** mark the fleet
   *redeploy required*; **Redeploy** applies them. **Stop** / **Delete** as expected.
   A crashed worker is detected within ~5 s and the fleet shows **failed** with the log tail.

**Register VCP** (VCPs page) always targets a fleet; the VCP goes live on the fleet's next
deploy / redeploy. Profiles come from the profile registry or `VCP_CP_WORKER_PROFILES_DIR`.

## Quick Start

### Prerequisites

- Python 3.11+
- Node 20+
- Docker + Docker Compose (optional)

### With Docker Compose

```bash
cp .env.example .env        # Edit VCP_CP_API_TOKEN (fleets are deployed from the UI)
docker-compose up -d
# Backend: http://localhost:8000
# Frontend: http://localhost:5173
```

### Local Development

**Backend:**

```bash
cd vcp_control_plane
pip install -e ".[dev]"

# Set environment variables (or copy .env.example → .env and edit)
export VCP_CP_DATABASE_URL="postgresql+asyncpg://user:pass@localhost/vcp_registry"
export VCP_CP_REDIS_HOST=localhost
export VCP_CP_API_TOKEN=my-dev-token

# Run migrations
vcp-control db migrate

# Start the server (hot-reload)
vcp-control run --reload
```

**Frontend:**

```bash
cd vcp_control_plane/frontend
npm install

# Create .env.local
cat > .env.local <<EOF
VITE_API_BASE_URL=http://localhost:8000/api/v1
VITE_WS_BASE_URL=ws://localhost:8000/api/v1
VITE_API_TOKEN=my-dev-token
EOF

npm run dev
```

## Environment Variables

All backend variables use the `VCP_CP_` prefix. See `.env.example` for the full list.

| Variable                     | Default                          | Description                         |
|------------------------------|----------------------------------|-------------------------------------|
| `VCP_CP_API_TOKEN`           | *(none)*                         | Bearer token for API auth           |
| `VCP_CP_REQUIRE_AUTH`        | `true`                           | Enforce bearer token                |
| `VCP_CP_DATABASE_URL`        | `sqlite+aiosqlite:///vcp.db`     | PostgreSQL (asyncpg) URL            |
| `VCP_CP_REDIS_HOST`          | `localhost`                      | Redis host                          |
| `VCP_CP_REDIS_PORT`          | `6379`                           | Redis port                          |
| `VCP_CP_WORKERS`             | *(empty)*                        | Externally managed workers `id=url,…` |
| `VCP_CP_FLEET_CONFIG_DIR`    | `var/fleets`                     | Generated worker configs per fleet  |
| `VCP_CP_DEFAULT_LAUNCHER`    | `process`                        | `process` \| `docker`               |
| `VCP_CP_WORKER_PROFILES_DIR` | *(empty)*                        | Data-plane profile YAMLs for fleets |
| `VCP_CP_COMMAND_STREAM_PREFIX` | `vcp:commands:worker`          | Per-worker command stream prefix    |
| `VCP_CP_BROADCAST_STREAM`    | `vcp:commands:broadcast`         | Fleet broadcast stream key          |
| `VCP_CP_EVENT_STREAM_PREFIX` | `vcp:events:worker`              | Worker event stream prefix          |
| `VCP_CP_PROFILE_DIR`         | `./profiles`                     | Directory of YAML charger profiles  |

## API Reference

Interactive Swagger UI available at `http://localhost:8000/docs`.

### Key Endpoints

| Method | Path                              | Description                        |
|--------|-----------------------------------|------------------------------------|
| GET    | `/api/v1/vcps`                    | Paginated VCP list                 |
| GET    | `/api/v1/fleets`                  | VCP fleets (status, VCP counts)    |
| POST   | `/api/v1/fleets`                  | Create a fleet (generates VCPs)    |
| PATCH  | `/api/v1/fleets/{id}`             | Edit a fleet (→ redeployRequired)  |
| GET    | `/api/v1/fleets/{id}/config`      | Generated worker.yaml (`?format=yaml`) |
| POST   | `/api/v1/fleets/{id}/deploy`      | Write config + start worker        |
| POST   | `/api/v1/fleets/{id}/redeploy`    | Stop + deploy current definition   |
| POST   | `/api/v1/fleets/{id}/stop`        | Stop the worker                    |
| GET    | `/api/v1/fleets/{id}/logs`        | Worker log tail                    |
| GET    | `/api/v1/fleets/launchers`        | Launcher availability              |
| GET    | `/api/v1/fleets/profiles`         | Profiles usable by fleets          |
| POST   | `/api/v1/vcps`                    | Register a VCP into a fleet (`fleetId` required) |
| GET    | `/api/v1/vcps/{id}`               | VCP detail                         |
| PATCH  | `/api/v1/vcps/{id}`               | Update metadata / desired state    |
| POST   | `/api/v1/vcps/{id}/commands`      | Send command to VCP                |
| GET    | `/api/v1/fleet/summary`           | Aggregate fleet counts             |
| POST   | `/api/v1/fleet/commands`          | Bulk / group / broadcast command   |
| GET    | `/api/v1/fleet/workers`           | Known workers + reachability       |
| GET    | `/api/v1/commands`                | Worker command catalogue (contract v2) |
| GET    | `/api/v1/profiles`                | List charger profiles              |
| POST   | `/api/v1/profiles/import-yaml`    | Import profile from YAML upload    |
| GET    | `/api/v1/scenarios`               | Scenario catalogue (10 + v1 aliases) |
| POST   | `/api/v1/scenarios/run`           | Launch scenario execution          |
| GET    | `/api/v1/scenarios/executions/{id}` | Execution + per-VCP report       |
| WS     | `/api/v1/events/ws`               | Real-time event stream             |

### WebSocket Filters

Connect with query parameters to filter the event stream:

```
ws://localhost:8000/api/v1/events/ws?token=TOKEN&vcpId=vcp-001
ws://localhost:8000/api/v1/events/ws?token=TOKEN&groupId=group-alpha
ws://localhost:8000/api/v1/events/ws?token=TOKEN&events=state_change,meter_value
```

## Running Tests

```bash
pip install -e ".[dev]"
pytest -v
```

Tests use an in-memory SQLite database and `fakeredis` — no real infrastructure required.

## CLI Reference

```bash
vcp-control run [--host HOST] [--port PORT] [--reload] [--workers N]
vcp-control db migrate [REVISION]
vcp-control db downgrade [REVISION]
vcp-control db revision -m "message"
vcp-control db history
vcp-control db current
vcp-control profiles import-dir [--dir PATH]
vcp-control version
```

## Project Structure

```
vcp_control_plane/
├── src/vcp_control_plane/
│   ├── api/
│   │   ├── app.py          # FastAPI factory + lifespan
│   │   ├── auth.py         # Bearer token dependency
│   │   ├── deps.py         # DB / Redis / settings dependencies
│   │   ├── schemas.py      # Pydantic v2 request/response models
│   │   └── routers/
│   │       ├── vcps.py     # VCP CRUD + per-VCP commands
│   │       ├── fleet.py    # Fleet summary + bulk commands
│   │       ├── profiles.py # Charger profile management
│   │       ├── scenarios.py # Scenario execution
│   │       └── events.py   # WebSocket event streaming
│   ├── bus/
│   │   ├── publisher.py    # XADD to Redis Streams
│   │   └── subscriber.py   # XREADGROUP → DB + WebSocket
│   ├── config/settings.py  # Pydantic Settings (VCP_CP_ prefix)
│   ├── db/
│   │   ├── base.py         # SQLAlchemy async engine singleton
│   │   └── models.py       # ORM models (4 tables)
│   ├── registry/store.py   # CRUD layer over SQLAlchemy
│   └── cli.py              # Typer CLI
├── alembic/                # Async Alembic migrations
├── frontend/               # React + Vite SPA
│   └── src/
│       ├── api/            # API client modules
│       ├── components/     # Reusable UI components
│       ├── hooks/          # React hooks
│       └── pages/          # Route-level page components
├── tests/                  # pytest async test suite
├── docker/                 # Dockerfiles + entrypoint
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```

## Connecting to vcp_data_plane

Fleets deployed from the UI need no configuration — the control plane knows their worker IDs
and URLs. Workers started **outside** the control plane are picked up in three ways:

```env
# explicit id=url pairs (worker_id = the worker's worker.yaml worker_id)
VCP_CP_WORKERS=worker-01=http://worker-1:8080,worker-02=http://worker-2:8080
# or legacy URL list — IDs resolved from GET <url>/health
VCP_CP_WORKER_URLS=http://worker-1:8080
# and/or automatic discovery of vcp:events:worker:* streams in Redis (default on)
VCP_CP_EVENT_STREAM_DISCOVERY=true
```

VCPs announced by such workers are auto-registered (tagged `auto-registered`).

The control plane will:
1. Publish v2 `WorkerCommand` envelopes to `vcp:commands:worker:{worker_id}` (or the broadcast stream)
2. Consume `vcp:events:worker:{worker_id}` for every known worker: persist state, acknowledge
   commands, close scenario executions, track worker liveness
3. Forward every event to connected WebSocket clients in the React UI
