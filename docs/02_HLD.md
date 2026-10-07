# OpenClaw SAAS — High-Level Design
### `docs/02_HLD.md` | Version 1.0 | 2026-06-18

---

## §1 Network Topology

> End-to-end view of how a browser request travels through the system.

```mermaid
graph TB
    classDef client fill:#3b82f6,stroke:#1d4ed8,stroke-width:2px,color:#fff
    classDef backend fill:#f97316,stroke:#c2410c,stroke-width:2px,color:#fff
    classDef gateway fill:#22c55e,stroke:#15803d,stroke-width:2px,color:#fff
    classDef data fill:#a855f7,stroke:#7e22ce,stroke-width:2px,color:#fff

    BROWSER[🖥️ Browser\nlocalhost:3000]:::client

    subgraph NEXTJS["Next.js — Port 3000"]
        PAGES[Pages & Components]:::client
        REWRITE[next.config.ts\nRewrite /api/* → :8001]:::client
    end

    subgraph FASTAPI["FastAPI — Port 8001"]
        ROUTER_AUTH[/api/auth]:::backend
        ROUTER_INST[/api/instances]:::backend
        MIDDLEWARE[CORS + Rate Limiter\nJWT Extractor]:::backend
    end

    subgraph SERVICES["Service Layer"]
        LAUNCHER[launcher.py\nProcess Manager]:::backend
        CONFIG[config_builder.py\nConfig Sanitizer]:::backend
        FILESVC[file_storage_service.py]:::backend
    end

    subgraph PERSISTENCE["Persistence Layer"]
        DB[(SQLite\npromptig.db)]:::data
        WORKSPACE[instances/{name}/\npromptilaw.json\nAGENTS.md\ngateway.log\ndata/]:::data
        GWHOME[.promptclaw_instances/{name}/\nIsolated gateway home]:::data
    end

    subgraph GATEWAYS["OpenClaw Gateway Processes"]
        GW_A[Port 18789\nInstance A]:::gateway
        GW_B[Port 18790\nInstance B]:::gateway
    end

    subgraph LLMS["External LLM APIs"]
        OAI[OpenAI]:::gateway
        ANT[Anthropic]:::gateway
        GRQ[Groq + 27 others]:::gateway
    end

    BROWSER ==>|"HTTP :3000"| PAGES
    PAGES -->|"SWR fetch /api/*"| REWRITE
    REWRITE ==>|"Proxy HTTP :8001"| MIDDLEWARE
    MIDDLEWARE --> ROUTER_AUTH
    MIDDLEWARE --> ROUTER_INST
    ROUTER_INST --> SERVICES
    SERVICES --> PERSISTENCE
    LAUNCHER -->|"subprocess.Popen"| GW_A
    LAUNCHER -->|"subprocess.Popen"| GW_B
    BROWSER ==>|"Direct HTTP :18789"| GW_A
    BROWSER ==>|"Direct HTTP :18790"| GW_B
    GW_A -->|"HTTPS"| OAI
    GW_B -->|"HTTPS"| ANT
    GW_A -->|"HTTPS"| GRQ
```

---

## §2 Technology Stack

### Backend

| Component | Technology | Version |
|-----------|-----------|---------|
| Web Framework | FastAPI | 0.115.5 |
| ASGI Server | Uvicorn | 0.32.1 |
| ORM | SQLAlchemy | 1.4.54 |
| Database (dev) | SQLite | — |
| Database (prod) | PostgreSQL | — |
| Migrations | Alembic | 1.13.1 |
| Auth | python-jose (JWT HS256) | 3.3.0 |
| Password Hashing | passlib + bcrypt | 1.7.4 / 3.2.2 |
| Validation | Pydantic v2 | 2.10.3 |
| Rate Limiting | slowapi | 0.1.9 |
| Process Monitor | psutil | 6.1.0 |
| HTTP Client | httpx | 0.28.0 |
| Encryption | cryptography (Fernet) | 43.0.3 |
| Config | python-dotenv | 1.0.1 |

### Frontend

| Component | Technology | Version |
|-----------|-----------|---------|
| Framework | Next.js | 16.2.4 |
| UI Library | React | 19.2.4 |
| Data Fetching | SWR | 2.4.1 |
| Icons | Lucide React | 1.8.0 |
| Notifications | react-hot-toast | 2.6.0 |
| Language | TypeScript | 5.x |
| Styling | CSS Modules + inline styles | — |

### Runtime

| Component | Technology |
|-----------|-----------|
| Agent Runtime | OpenClaw (Node.js binary) |
| Gateway Command | `openclaw gateway --port {N} --allow-unconfigured` |
| Config File | `promptclaw.json` (JSON) |
| System Prompt | `AGENTS.md` (Markdown) |

---

## §3 Application Layers

```mermaid
graph LR
    classDef client fill:#3b82f6,stroke:#1d4ed8,stroke-width:2px,color:#fff
    classDef backend fill:#f97316,stroke:#c2410c,stroke-width:2px,color:#fff
    classDef gateway fill:#22c55e,stroke:#15803d,stroke-width:2px,color:#fff
    classDef data fill:#a855f7,stroke:#7e22ce,stroke-width:2px,color:#fff

    L1[Presentation\nNext.js Pages\nReact Components]:::client
    L2[API Gateway\nNext.js Rewrites\n/api/* → :8001]:::client
    L3[Application\nFastAPI Routers\nPydantic Validation]:::backend
    L4[Business Logic\nService Layer\nlauncher / config / files]:::backend
    L5[Data Access\nCRUD + SQLAlchemy ORM]:::backend
    L6[Persistence\nSQLite or PostgreSQL\nFilesystem]:::data
    L7[Runtime\nOpenClaw Gateways\nNode.js Processes]:::gateway

    L1 --> L2 --> L3 --> L4 --> L5 --> L6
    L4 -->|"spawn/kill"| L7
    L7 -->|"reads"| L6
```

---

## §4 Component Breakdown

### Backend Modules

| Module | File | Responsibility |
|--------|------|---------------|
| Entry Point | `main.py` | FastAPI app init, lifespan hooks, admin seed, CORS, router registration |
| Auth Utilities | `auth.py` | JWT create/decode, bcrypt hash/verify, `get_current_user` dependency |
| Auth Router | `routers/auth.py` | `/register`, `/login`, `/logout`, `/me` |
| Instances Router | `routers/instances.py` | 20+ instance management endpoints |
| CRUD | `crud.py` | All DB read/write ops, config masking, UI field attachment |
| Database | `database.py` | SQLAlchemy engine, `SessionLocal`, `get_db` dependency |
| Models | `models.py` | ORM: User, Tenant, Instance, InstanceConfig, InstanceFile |
| Schemas | `schemas.py` | Pydantic v2 request/response models |
| Settings | `settings.py` | Frozen dataclass config from ENV vars |
| Limiter | `limiter.py` | slowapi `Limiter` instance |
| Launcher | `services/launcher.py` | `start_instance`, `stop_instance`, `is_instance_running` |
| Config Builder | `services/config_builder.py` | `activate_instance`, `_sanitize_config`, `load_disk_config` |
| File Storage | `services/file_storage_service.py` | Upload, list, delete, path-traversal guard |
| Encryption | `services/encryption.py` | Fernet key management |

### Frontend Modules

| Module | Path | Responsibility |
|--------|------|---------------|
| Root Layout | `app/layout.tsx` | `<Toaster>` provider, global CSS |
| Home | `app/page.tsx` | Redirect to `/dashboard/openclaw` |
| Login | `app/login/page.tsx` | Email/password form, JWT storage |
| Register | `app/register/page.tsx` | Account creation form |
| Dashboard | `app/dashboard/openclaw/page.tsx` | Instance grid, SWR polling |
| Instance Detail | `app/dashboard/openclaw/[id]/page.tsx` | Single instance view |
| Chat | `app/dashboard/openclaw/[id]/chat/page.tsx` | WebChat via gateway URL |
| Configure | `app/dashboard/openclaw/[id]/configure/page.tsx` | Config editor |
| AuthGuard | `components/AuthGuard.tsx` | JWT check, redirect to login |
| InstanceCard | `components/InstanceCard.tsx` | Status, start/stop, logs |
| ConfigForm | `components/ConfigForm.tsx` | Provider, model, tools, Slack tabs |
| API Client | `lib/api.ts` | Typed `fetch` wrapper, all endpoint methods |
| Auth Utils | `lib/auth.ts` | `getToken`, `setToken`, `getAuthHeaders`, `isLoggedIn` |
| Runtime | `lib/runtime.ts` | Gateway URL builder |

---

## §5 Database Schema

```mermaid
erDiagram
    Tenant {
        UUID id PK
        string name
        datetime created_at
    }
    User {
        UUID id PK
        string name
        string email UK
        string hashed_password
        UUID tenant_id FK
        boolean is_active
        datetime created_at
    }
    Instance {
        UUID id PK
        UUID tenant_id FK
        string name
        string description
        string status
        int port
        int pid
        datetime created_at
        datetime updated_at
    }
    InstanceConfig {
        UUID id PK
        UUID instance_id FK
        JSON config_json
        datetime created_at
        datetime updated_at
    }
    InstanceFile {
        UUID id PK
        UUID instance_id FK
        string file_name
        string file_path
        bigint file_size
        string file_type
        datetime uploaded_at
    }

    Tenant ||--o{ User : "has"
    Tenant ||--o{ Instance : "owns"
    Instance ||--|| InstanceConfig : "has"
    Instance ||--o{ InstanceFile : "stores"
```

---

## §6 API Endpoint Catalogue

### Auth — `/api/auth`

| Method | Path | Description | Auth Required |
|--------|------|-------------|---------------|
| `POST` | `/api/auth/register` | Create user + tenant, issue JWT | No |
| `POST` | `/api/auth/login` | Verify credentials, issue JWT | No |
| `POST` | `/api/auth/logout` | Clear auth cookie | No |
| `GET` | `/api/auth/me` | Return current user | Yes |

### Instances — `/api/instances`

| Method | Path | Description | Auth Required |
|--------|------|-------------|---------------|
| `GET` | `/api/instances` | List tenant's instances | Yes |
| `POST` | `/api/instances` | Create new instance | Yes |
| `GET` | `/api/instances/{id}` | Get instance detail | Yes |
| `DELETE` | `/api/instances/{id}` | Delete instance + all data | Yes |
| `POST` | `/api/instances/{id}/start` | Spawn gateway process | Yes |
| `POST` | `/api/instances/{id}/stop` | Kill gateway process | Yes |
| `GET` | `/api/instances/{id}/config` | Get config (masked secrets) | Yes |
| `POST` | `/api/instances/{id}/config` | Update config | Yes |
| `POST` | `/api/instances/{id}/config/apply-fixes` | Apply canonical schema fixes | Yes |
| `POST` | `/api/instances/{id}/reset` | Reset to default template | Yes |
| `GET` | `/api/instances/{id}/runtime` | Get port + auth token | Yes |
| `GET` | `/api/instances/{id}/logs` | Get gateway.log contents | Yes |
| `GET` | `/api/instances/{id}/files` | List uploaded files | Yes |
| `POST` | `/api/instances/{id}/upload` | Upload data files | Yes |
| `DELETE` | `/api/instances/{id}/files/{file_id}` | Delete uploaded file | Yes |

### Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Basic health check |
| `GET` | `/health/runtime` | Node.js, OpenClaw, storage status |

---

## §7 Workload Paths

### Path A — User Login

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant FE as Next.js :3000
    participant BE as FastAPI :8001
    participant DB as SQLite

    User->>FE: POST /api/auth/login {email, password}
    FE->>BE: Proxy → POST /api/auth/login
    BE->>BE: Rate limit check (10/min)
    BE->>DB: SELECT user WHERE email = ?
    DB-->>BE: User row
    BE->>BE: bcrypt.verify(password, hash)
    BE->>BE: jwt.encode({sub, tenant_id, exp})
    BE-->>FE: 200 {access_token, user} + Set-Cookie: access_token
    FE->>FE: localStorage.setToken(access_token)
    FE->>FE: localStorage.setStoredUser(user)
    FE-->>User: Redirect → /dashboard/openclaw
```

### Path B — Instance Start

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant FE as Next.js :3000
    participant BE as FastAPI :8001
    participant DB as SQLite
    participant FS as Filesystem
    participant GW as OpenClaw Gateway

    User->>FE: POST /api/instances/{id}/start
    FE->>BE: Proxy → POST with Bearer JWT
    BE->>BE: get_current_user() — decode JWT
    BE->>DB: SELECT instance WHERE id=? AND tenant_id=?
    DB-->>BE: Instance row
    BE->>FS: Read promptclaw.json (disk priority)
    BE->>DB: Read config_json (merge DB-only fields)
    BE->>BE: _sanitize_config() — strip UI keys
    BE->>FS: Write sanitized config to gateway home
    BE->>FS: Write AGENTS.md (system prompt)
    BE->>GW: subprocess.Popen(["openclaw","gateway","--port",N])
    GW-->>BE: PID returned
    BE->>DB: UPDATE instance SET status=running, pid=PID
    BE-->>FE: 200 {success, instance}
    FE-->>User: Status badge → Running
```

### Path C — Chat with Agent

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant FE as Next.js :3000
    participant BE as FastAPI :8001
    participant GW as OpenClaw :18789
    participant LLM as LLM Provider

    User->>FE: Open Chat page for instance
    FE->>BE: GET /api/instances/{id}/runtime
    BE-->>FE: {port: 18789, token: "..."}
    FE->>FE: Build gateway URL: http://localhost:18789
    User->>FE: Type message, press Send
    FE->>GW: POST http://localhost:18789/chat {message}
    GW->>GW: Load context from AGENTS.md
    GW->>LLM: HTTPS POST to provider API
    LLM-->>GW: Stream tokens
    GW-->>FE: Stream response
    FE-->>User: Render streaming response
```

### Path D — File Upload

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant FE as Next.js :3000
    participant BE as FastAPI :8001
    participant FS as Filesystem
    participant DB as SQLite

    User->>FE: Select file(s) in Upload modal
    FE->>BE: POST /api/instances/{id}/upload (multipart)
    BE->>BE: Validate MIME type & size ≤ 10 MB
    BE->>BE: Resolve path (traversal guard)
    BE->>FS: Write to instances/{name}/data/{filename}
    BE->>DB: INSERT InstanceFile record
    BE->>BE: Enable exec/process/fs tools in config
    BE->>FS: Update AGENTS.md with file listing
    BE-->>FE: 200 {success, files:[...]}
    FE-->>User: File appears in Files panel
```
