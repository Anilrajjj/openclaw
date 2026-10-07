


# OpenClaw SAAS — Simplified Architecture Map
### `docs/06_Simplified_Architecture_Map.md` | Version 1.0 | 2026-06-18

---

## One-Page Architecture

```mermaid
graph TB
    classDef client fill:#3b82f6,stroke:#1d4ed8,stroke-width:2px,color:#fff
    classDef backend fill:#f97316,stroke:#c2410c,stroke-width:2px,color:#fff
    classDef gateway fill:#22c55e,stroke:#15803d,stroke-width:2px,color:#fff
    classDef data fill:#a855f7,stroke:#7e22ce,stroke-width:2px,color:#fff

    U[👤 User\nBrowser]:::client

    subgraph FE["Next.js :3000"]
        DASH[Dashboard]:::client
        CHAT[Chat UI]:::client
    end

    subgraph BE["FastAPI :8001"]
        AUTH[/api/auth]:::backend
        INST[/api/instances]:::backend
    end

    subgraph DB["Persistence"]
        SQL[(SQLite / PG)]:::data
        FS[/instances/]:::data
    end

    subgraph GW["OpenClaw Gateways"]
        G1[:18789]:::gateway
        G2[:18790]:::gateway
    end

    LLM[🤖 LLM APIs\nOpenAI · Anthropic\nGroq · 27 more]:::gateway

    U ==>|"HTTP"| FE
    FE ==>|"Proxy /api/*"| BE
    BE --> SQL
    BE --> FS
    BE -->|"spawn"| G1
    BE -->|"spawn"| G2
    CHAT ==>|"Direct HTTP"| G1
    G1 -->|"HTTPS"| LLM
    G2 -->|"HTTPS"| LLM
```

---

## Layers at a Glance

| Layer | Technology | Port | Purpose |
|-------|-----------|------|---------|
| **Client** | Next.js 16 + React 19 | 3000 | UI, auth state, API proxy |
| **API** | FastAPI + Uvicorn | 8001 | Auth, instance CRUD, file ops |
| **Database** | SQLite (dev) / PostgreSQL (prod) | — | Users, tenants, instances, configs |
| **Filesystem** | OS filesystem | — | Config files, AGENTS.md, uploads |
| **Runtime** | OpenClaw (Node.js) | 18789+ | AI agent gateway per instance |
| **LLM** | 30+ external APIs | 443 | Language model inference |

---

## Key Modules

```
backend/
  auth.py           JWT encode/decode, bcrypt, get_current_user
  models.py         SQLAlchemy: User / Tenant / Instance / InstanceConfig / InstanceFile
  crud.py           All DB operations + config masking
  routers/
    auth.py         POST /register /login /logout  GET /me
    instances.py    20+ endpoints: CRUD, start/stop, config, files, logs
  services/
    launcher.py     spawn & kill gateway processes via psutil
    config_builder  sanitize config, write AGENTS.md, manage workspace
    file_storage    upload/list/delete with path-traversal guard

frontend/
  lib/api.ts        Typed fetch wrapper for all backend endpoints
  lib/auth.ts       localStorage JWT management
  components/
    AuthGuard       Redirects to /login if token missing
    InstanceCard    Per-instance UI card with status, start/stop, logs
    ConfigForm      LLM provider, model, system prompt, tools, Slack
```

---

## Quick cURL Reference

```bash
# Set base URL and get a token first:
BASE="http://127.0.0.1:8001"
TOKEN=$(curl -s -X POST $BASE/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"change-this-before-production"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Health
curl -s $BASE/health

# List instances
curl -s $BASE/api/instances -H "Authorization: Bearer $TOKEN"

# Create instance
curl -s -X POST $BASE/api/instances \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"My Bot","description":"Test"}'

# Start instance (replace ID)
curl -s -X POST $BASE/api/instances/{ID}/start \
  -H "Authorization: Bearer $TOKEN"

# Get runtime (port + token)
curl -s $BASE/api/instances/{ID}/runtime \
  -H "Authorization: Bearer $TOKEN"

# Upload a file
curl -s -X POST $BASE/api/instances/{ID}/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@mydata.csv"

# Stop instance
curl -s -X POST $BASE/api/instances/{ID}/stop \
  -H "Authorization: Bearer $TOKEN"
```

---

## Config Sanitization — 3 Layers

```
DB (raw)     → includes: systemPrompt, mcp, exec.enabled, plaintext API keys
API (masked) → replaces: API keys with ****...last4
Runtime      → strips:   systemPrompt (→ AGENTS.md), mcp, exec.enabled
               forces:   models.mode = "replace"
               disables: exec/search/fetch/slack if not explicitly enabled
```

---

## Instance Lifecycle

```
[Create] → status: stopped, port assigned
    ↓
[Configure] → LLM provider, model, system prompt, tools
    ↓
[Start] → sanitize config → write files → spawn OpenClaw process → status: running
    ↓
[Chat] → browser connects directly to gateway port
    ↓
[Stop] → kill process + children → status: stopped
    ↓
[Delete] → remove DB records + workspace files
```

---

## Environment Variables (Critical)

| Variable | Dev Default | Production Requirement |
|----------|-------------|----------------------|
| `DATABASE_URL` | `sqlite:///./promptiq.db` | PostgreSQL URL |
| `JWT_SECRET` | `change-me` | 32+ char random string |
| `ADMIN_EMAIL` | `admin@example.com` | Your admin email |
| `ADMIN_PASSWORD` | `change-this-before-production` | Strong password |
| `INSTANCES_ROOT` | `C:\Openclaw_SAAS\instances` | Writable path |
| `PROMPTCLAW_HOME_ROOT` | `~/.promptclaw_instances` | Writable path |
| `OPENCLAW_ENTRYPOINT` | `openclaw` | Path to binary |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:3000` | Production domain(s) |
