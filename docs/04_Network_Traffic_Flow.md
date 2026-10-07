# OpenClaw SAAS — Network & Traffic Flow
### `docs/04_Network_Traffic_Flow.md` | Version 1.0 | 2026-06-18

---

## §1 Port Map

| Service | Host | Port | Protocol | Notes |
|---------|------|------|----------|-------|
| Next.js Dev Server | localhost | 3000 | HTTP | Frontend — `npm run dev` |
| FastAPI / Uvicorn | 127.0.0.1 | 8001 | HTTP | Backend — `uvicorn main:app` |
| OpenClaw Gateway (base) | 127.0.0.1 | 18789 | HTTP | First instance |
| OpenClaw Gateway (+1) | 127.0.0.1 | 18790 | HTTP | Second instance |
| OpenClaw Gateway (+N) | 127.0.0.1 | 18789+N | HTTP | Nth instance |
| LLM Providers | external | 443 | HTTPS | OpenAI, Anthropic, Groq, etc. |

---

## §2 Full Traffic Flow — Dashboard Load

```mermaid
sequenceDiagram
    autonumber
    participant BR as Browser :3000
    participant NX as Next.js :3000
    participant FA as FastAPI :8001
    participant DB as SQLite
    participant FS as Filesystem

    BR->>NX: GET /dashboard/openclaw
    NX->>NX: AuthGuard — check localStorage token
    NX->>NX: Token valid → render Dashboard

    NX->>FA: GET /api/instances (Bearer JWT)
    Note over NX,FA: Rewrite: /api/* → http://127.0.0.1:8001/api/*
    FA->>FA: _extract_token() — cookie → Bearer
    FA->>FA: jwt.decode → {sub, tenant_id}
    FA->>DB: SELECT instances WHERE tenant_id = ?
    DB-->>FA: Instance rows
    FA->>FA: _attach_ui_fields() per instance
    FA-->>NX: 200 [Instance, ...]
    NX-->>BR: Render InstanceCards
```

---

## §3 Traffic Flow — Instance Start

```mermaid
sequenceDiagram
    autonumber
    participant BR as Browser :3000
    participant NX as Next.js :3000
    participant FA as FastAPI :8001
    participant DB as SQLite
    participant FS as Filesystem
    participant GW as OpenClaw :18789

    BR->>NX: POST /api/instances/{id}/start
    NX->>FA: Proxy POST (Bearer JWT)
    FA->>FA: Authenticate + tenant check
    FA->>DB: SELECT instance + config
    DB-->>FA: Instance + config_json
    FA->>FS: Read instances/{name}/promptclaw.json
    FA->>FA: Merge disk config + DB-only fields
    FA->>FA: _sanitize_config() — strip UI keys
    FA->>FS: Write sanitized config → gateway home
    FA->>FS: Write system prompt → AGENTS.md
    FA->>GW: subprocess.Popen(["openclaw","gateway","--port","18789"])
    Note over FA,GW: env: OPENCLAW_HOME, OPENCLAW_CONFIG_PATH
    GW-->>FA: PID = 12345
    FA->>DB: UPDATE Instance SET status=running, pid=12345
    FA-->>NX: 200 {success: true, instance: {...}}
    NX-->>BR: Badge → "Running", Chat button enabled
```

---

## §4 Traffic Flow — Chat Session

```mermaid
sequenceDiagram
    autonumber
    participant BR as Browser :3000
    participant NX as Next.js :3000
    participant FA as FastAPI :8001
    participant GW as OpenClaw :18789
    participant LLM as LLM API :443

    BR->>NX: Navigate to /dashboard/openclaw/{id}/chat
    NX->>FA: GET /api/instances/{id}/runtime
    FA-->>NX: {id, port: 18789, token: "..."}
    NX->>NX: Build URL: http://localhost:18789

    BR->>GW: POST http://localhost:18789/chat\n{"message": "Hello"}
    Note over BR,GW: Direct connection — bypasses Next.js proxy
    GW->>GW: Load AGENTS.md (system prompt)
    GW->>GW: Apply tool config from promptclaw.json
    GW->>LLM: HTTPS POST /v1/chat/completions\n{model, messages, stream: true}
    LLM-->>GW: SSE stream of tokens
    GW-->>BR: SSE stream → rendered in chat UI
```

---

## §5 Traffic Flow — File Upload

```mermaid
sequenceDiagram
    autonumber
    participant BR as Browser :3000
    participant NX as Next.js :3000
    participant FA as FastAPI :8001
    participant FS as Filesystem
    participant DB as SQLite

    BR->>NX: POST /api/instances/{id}/upload (multipart/form-data)
    NX->>FA: Proxy POST
    FA->>FA: Validate MIME type ∈ {csv,json,xlsx,txt}
    FA->>FA: Validate size ≤ 10 MB
    FA->>FA: Resolve path — guard against ../escape
    FA->>FS: Write → instances/{name}/data/{filename}
    FA->>DB: INSERT InstanceFile {name, path, size, type}
    FA->>FA: Update config: enable exec/process/fs tools
    FA->>FS: Rewrite instances/{name}/AGENTS.md\n(append file listing)
    FA-->>NX: 200 {success, files:[{id, name, size, ...}]}
    NX-->>BR: File appears in Files panel
```

---

## §6 Traffic Flow — Logout

```mermaid
sequenceDiagram
    autonumber
    participant BR as Browser
    participant NX as Next.js :3000
    participant FA as FastAPI :8001

    BR->>NX: Click Logout button
    NX->>NX: auth.clearToken()\n(removes localStorage keys)
    NX->>FA: POST /api/auth/logout
    FA->>FA: response.delete_cookie("access_token")
    FA-->>NX: 200 {success: true}
    NX->>BR: window.location.href = "/login"
```

---

## §7 CORS & Security Headers

```
Allowed Origins (CORS_ALLOWED_ORIGINS env):
  Development: http://localhost:3000, http://127.0.0.1:3000
  Production:  set explicitly — no wildcard

Rate Limiting (slowapi):
  POST /api/auth/register  → 5 requests / minute / IP
  POST /api/auth/login     → 10 requests / minute / IP

Cookie Security:
  name:     access_token
  httpOnly: True              (JS cannot read)
  samesite: lax
  secure:   True only if APP_ENV=production
  max_age:  10080 * 60 seconds (7 days)

JWT:
  algorithm: HS256
  payload:   {sub: user_id, tenant_id, exp}
  secret:    JWT_SECRET env var (must be 32+ chars in production)
```

---

## §8 Network Isolation Model

```
┌────────────────────────────────────────────────────┐
│  TRUSTED ZONE (loopback 127.0.0.1)                 │
│                                                    │
│  Browser ──────→ Next.js :3000                     │
│                       │                            │
│                       │ rewrite                    │
│                       ▼                            │
│               FastAPI :8001                        │
│                   │       │                        │
│                   │       └──→ Gateway :18789       │
│                   │       └──→ Gateway :18790       │
│                   ▼                                │
│              SQLite DB                             │
│              Filesystem                            │
│                                                    │
└────────────────────────────────────────────────────┘
                    │ HTTPS only
┌────────────────────────────────────────────────────┐
│  EXTERNAL ZONE (internet)                          │
│  LLM Provider APIs (OpenAI, Anthropic, Groq, ...)  │
└────────────────────────────────────────────────────┘

Note: LLM API keys travel:
  Browser → (never exposed)
  FastAPI → (stored in DB, masked in API responses)
  Gateway → (reads from promptclaw.json at startup)
  LLM API → (sent via HTTPS in Authorization header)
```
