# OpenClaw SAAS — API Testing Guide
### `docs/05_API_Testing_Guide.md` | Version 1.0 | 2026-06-18

---

## Prerequisites

- Backend running: `uvicorn main:app --reload --port 8001` (from `backend/` with venv activated)
- Base URL: `http://127.0.0.1:8001`
- Interactive API docs: `http://127.0.0.1:8001/docs`

---

## §1 Authentication Endpoints

### 1.1 Register

```bash
curl -s -X POST http://127.0.0.1:8001/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test User",
    "email": "test@example.com",
    "password": "Test1234!"
  }' | python -m json.tool
```

**Expected Response (201):**
```json
{
  "access_token": "<jwt_token>",
  "token_type": "bearer",
  "user": {
    "id": "uuid-here",
    "name": "Test User",
    "email": "test@example.com",
    "tenant_id": "uuid-here",
    "created_at": "2026-06-18T10:00:00"
  }
}
```

---

### 1.2 Login

```bash
curl -s -X POST http://127.0.0.1:8001/api/auth/login \
  -H "Content-Type: application/json" \
  -c cookies.txt \
  -d '{
    "email": "admin@example.com",
    "password": "change-this-before-production"
  }' | python -m json.tool
```

> `-c cookies.txt` saves the HTTP-only cookie for subsequent requests.

**Expected Response (200):**
```json
{
  "access_token": "<jwt_token>",
  "token_type": "bearer",
  "user": { ... }
}
```

**Store the token:**
```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8001/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"change-this-before-production"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
echo "TOKEN=$TOKEN"
```

---

### 1.3 Get Current User

```bash
curl -s http://127.0.0.1:8001/api/auth/me \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

---

### 1.4 Logout

```bash
curl -s -X POST http://127.0.0.1:8001/api/auth/logout \
  -b cookies.txt | python -m json.tool
```

---

## §2 Instance Endpoints

> All instance endpoints require `Authorization: Bearer <token>`.

### 2.1 List Instances

```bash
curl -s http://127.0.0.1:8001/api/instances \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

---

### 2.2 Create Instance

```bash
curl -s -X POST http://127.0.0.1:8001/api/instances \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Chatbot",
    "description": "A general-purpose assistant"
  }' | python -m json.tool
```

**Store the instance ID:**
```bash
INSTANCE_ID=$(curl -s -X POST http://127.0.0.1:8001/api/instances \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Test Bot","description":"Test"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "INSTANCE_ID=$INSTANCE_ID"
```

---

### 2.3 Get Instance

```bash
curl -s http://127.0.0.1:8001/api/instances/$INSTANCE_ID \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

---

### 2.4 Start Instance

```bash
curl -s -X POST http://127.0.0.1:8001/api/instances/$INSTANCE_ID/start \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

**Expected Response (200):**
```json
{
  "success": true,
  "message": "Instance started",
  "instance": {
    "status": "running",
    "pid": 12345,
    "port": 18789
  }
}
```

---

### 2.5 Stop Instance

```bash
curl -s -X POST http://127.0.0.1:8001/api/instances/$INSTANCE_ID/stop \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

---

### 2.6 Get Configuration

```bash
curl -s http://127.0.0.1:8001/api/instances/$INSTANCE_ID/config \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

> API keys are masked: `"apiKey": "****...XXXX"`

---

### 2.7 Update Configuration

```bash
curl -s -X POST http://127.0.0.1:8001/api/instances/$INSTANCE_ID/config \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "agents": {
      "defaults": {
        "systemPrompt": "You are a helpful HR assistant.",
        "model": {
          "primary": "openai/gpt-4o-mini"
        }
      }
    },
    "models": {
      "providers": {
        "openai": {
          "baseUrl": "https://api.openai.com/v1",
          "api": "openai-completions",
          "apiKey": "sk-your-key-here",
          "models": [
            {
              "id": "gpt-4o-mini",
              "name": "GPT-4o Mini",
              "contextWindow": 128000,
              "input": ["text"],
              "cost": {"input": 0, "output": 0}
            }
          ]
        }
      }
    }
  }' | python -m json.tool
```

---

### 2.8 Reset Configuration to Defaults

```bash
curl -s -X POST http://127.0.0.1:8001/api/instances/$INSTANCE_ID/reset \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

---

### 2.9 Apply Schema Fixes

```bash
curl -s -X POST http://127.0.0.1:8001/api/instances/$INSTANCE_ID/config/apply-fixes \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

---

### 2.10 Get Runtime Info (Port + Token)

```bash
curl -s http://127.0.0.1:8001/api/instances/$INSTANCE_ID/runtime \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

**Expected Response:**
```json
{
  "id": "uuid",
  "port": 18789,
  "token": "gateway-auth-token"
}
```

---

### 2.11 Get Logs

```bash
curl -s http://127.0.0.1:8001/api/instances/$INSTANCE_ID/logs \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

---

### 2.12 Upload Files

```bash
curl -s -X POST http://127.0.0.1:8001/api/instances/$INSTANCE_ID/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@/path/to/employees.csv" \
  -F "files=@/path/to/policies.json" | python -m json.tool
```

---

### 2.13 List Files

```bash
curl -s http://127.0.0.1:8001/api/instances/$INSTANCE_ID/files \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

---

### 2.14 Delete File

```bash
FILE_ID="uuid-of-file"
curl -s -X DELETE http://127.0.0.1:8001/api/instances/$INSTANCE_ID/files/$FILE_ID \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

---

### 2.15 Delete Instance

```bash
curl -s -X DELETE http://127.0.0.1:8001/api/instances/$INSTANCE_ID \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

---

## §3 Health Endpoints

### 3.1 Basic Health Check

```bash
curl -s http://127.0.0.1:8001/health | python -m json.tool
```

**Expected:**
```json
{"status": "ok"}
```

### 3.2 Runtime Health Check

```bash
curl -s http://127.0.0.1:8001/health/runtime | python -m json.tool
```

**Expected:**
```json
{
  "app_env": "development",
  "node_available": true,
  "openclaw_available": true,
  "instances_root": "C:\\Openclaw_SAAS\\instances",
  "instances_root_writable": true,
  "instance_base_port": 18789
}
```

---

## §4 Error Responses

| Status | Meaning | Example |
|--------|---------|---------|
| `400` | Bad request / validation failure | Missing required field |
| `401` | Not authenticated | Missing or expired JWT |
| `403` | Forbidden | Instance belongs to another tenant |
| `404` | Not found | Instance ID doesn't exist |
| `409` | Conflict | Email already registered |
| `422` | Validation error | Pydantic model mismatch |
| `429` | Rate limited | Too many login attempts |
| `500` | Server error | Gateway failed to start |

---

## §5 Postman Collection Setup

### Environment Variables

Create a Postman Environment with these variables:

| Variable | Initial Value |
|----------|--------------|
| `base_url` | `http://127.0.0.1:8001` |
| `token` | *(empty — set by login test)* |
| `instance_id` | *(empty — set by create test)* |

### Auto-Token Script (Post-Login Test Script)

Paste into the **Tests** tab of the Login request:

```javascript
const res = pm.response.json();
if (res.access_token) {
    pm.environment.set("token", res.access_token);
    console.log("Token stored:", res.access_token.substring(0, 20) + "...");
}
```

### Auto-Instance-ID Script (Post-Create Test Script)

Paste into the **Tests** tab of the Create Instance request:

```javascript
const res = pm.response.json();
if (res.id) {
    pm.environment.set("instance_id", res.id);
    console.log("Instance ID stored:", res.id);
}
```

### Request Template (All Protected Endpoints)

Set **Authorization** → Type: `Bearer Token` → Token: `{{token}}`

### Recommended Test Order

1. `POST /api/auth/login` → stores `{{token}}`
2. `POST /api/instances` → stores `{{instance_id}}`
3. `GET /api/instances`
4. `GET /api/instances/{{instance_id}}/config`
5. `POST /api/instances/{{instance_id}}/start`
6. `GET /api/instances/{{instance_id}}/runtime`
7. `POST /api/instances/{{instance_id}}/upload`
8. `GET /api/instances/{{instance_id}}/files`
9. `POST /api/instances/{{instance_id}}/stop`
10. `DELETE /api/instances/{{instance_id}}`

---

## §6 Full E2E Smoke Test Script

```bash
#!/bin/bash
# OpenClaw SAAS — Full smoke test
BASE="http://127.0.0.1:8001"

echo "=== 1. Login ==="
TOKEN=$(curl -s -X POST $BASE/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"change-this-before-production"}' \
  | python -c "import sys,json; print(json.load(sys.stdin).get('access_token','FAILED'))")
echo "Token: ${TOKEN:0:30}..."

echo "=== 2. Create Instance ==="
INSTANCE_ID=$(curl -s -X POST $BASE/api/instances \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"smoke-test","description":"Smoke test instance"}' \
  | python -c "import sys,json; print(json.load(sys.stdin).get('id','FAILED'))")
echo "Instance ID: $INSTANCE_ID"

echo "=== 3. Get Config ==="
curl -s $BASE/api/instances/$INSTANCE_ID/config \
  -H "Authorization: Bearer $TOKEN" | python -c "import sys,json; d=json.load(sys.stdin); print('Config keys:', list(d.get('config_json',{}).keys())[:5])"

echo "=== 4. Health Check ==="
curl -s $BASE/health | python -m json.tool

echo "=== 5. Cleanup ==="
curl -s -X DELETE $BASE/api/instances/$INSTANCE_ID \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool

echo "=== Done ==="
```
