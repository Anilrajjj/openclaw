from __future__ import annotations
import re
from datetime import datetime
from typing import List, Optional, Any, Dict
from pydantic import BaseModel, EmailStr, field_validator


# ─── Auth / Users ────────────────────────────

class UserRegister(BaseModel):
    name: str
    email: EmailStr
    password: str

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name cannot be empty.")
        if len(v) > 128:
            raise ValueError("Name cannot exceed 128 characters.")
        return v

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters.")
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter.")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one digit.")
        return v

class UserLogin(BaseModel):
    email: str
    password: str

class UserOut(BaseModel):
    id: str
    name: str
    email: str
    tenant_id: str
    created_at: datetime

    class Config:
        from_attributes = True

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


# ─── Tenant ──────────────────────────────────

class TenantBase(BaseModel):
    name: str

class TenantCreate(TenantBase):
    pass

class Tenant(TenantBase):
    id: str
    created_at: datetime

    class Config:
        from_attributes = True


# ─── Instance ────────────────────────────────

class InstanceBase(BaseModel):
    name: str
    description: Optional[str] = ""

class InstanceCreate(InstanceBase):
    tenant_id: Optional[str] = None

class InstanceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    port: Optional[int] = None
    pid: Optional[int] = None

class Instance(InstanceBase):
    id: str
    tenant_id: str
    status: str
    port: Optional[int]
    pid: Optional[int]
    created_at: datetime
    updated_at: datetime
    
    # UI Convenience (calculated from JSON)
    llm_provider: Optional[str] = None
    model_name: Optional[str] = None
    slack_enabled: Optional[bool] = False
    gateway_auth_token: Optional[str] = None

    class Config:
        from_attributes = True


# ─── Config ──────────────────────────────────

class InstanceConfigOut(BaseModel):
    instance_id: str
    config_json: Dict[str, Any]
    updated_at: datetime

    class Config:
        from_attributes = True


class InstanceRuntime(BaseModel):
    id: str
    port: int
    token: str


# ─── Agents (Lyzr-style) ─────────────────────

class AgentBase(BaseModel):
    name: str
    description: Optional[str] = ""
    agent_role: Optional[str] = ""
    agent_goal: Optional[str] = ""
    agent_instructions: Optional[str] = ""
    examples: Optional[str] = ""
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    temperature: float = 0.7
    top_p: float = 1.0
    features: List[Dict[str, Any]] = []
    tools: List[str] = []
    managed_agents: List[str] = []

    @field_validator("name")
    @classmethod
    def agent_name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Agent name cannot be empty.")
        if len(v) > 128:
            raise ValueError("Agent name cannot exceed 128 characters.")
        return v

    @field_validator("temperature")
    @classmethod
    def temperature_range(cls, v: float) -> float:
        if not 0.0 <= v <= 2.0:
            raise ValueError("Temperature must be between 0 and 2.")
        return v

    @field_validator("top_p")
    @classmethod
    def top_p_range(cls, v: float) -> float:
        if not 0.0 < v <= 1.0:
            raise ValueError("Top P must be between 0 and 1.")
        return v


class AgentCreate(AgentBase):
    api_key: Optional[str] = None      # plaintext in, encrypted at rest


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    agent_role: Optional[str] = None
    agent_goal: Optional[str] = None
    agent_instructions: Optional[str] = None
    examples: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    features: Optional[List[Dict[str, Any]]] = None
    tools: Optional[List[str]] = None
    managed_agents: Optional[List[str]] = None
    api_key: Optional[str] = None


class AgentOut(AgentBase):
    id: str
    tenant_id: str
    api_key_set: bool = False
    api_key_masked: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ─── Chat ────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None

    @field_validator("message")
    @classmethod
    def message_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Message cannot be empty.")
        return v


class DelegationTrace(BaseModel):
    agent_id: str
    agent_name: str
    request: str
    response: str


class ChatResponse(BaseModel):
    session_id: str
    agent_id: str
    response: str
    delegations: List[DelegationTrace] = []


class ChatMessageOut(BaseModel):
    id: str
    role: str
    content: str
    meta: Optional[Dict[str, Any]] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ChatSessionOut(BaseModel):
    id: str
    agent_id: str
    title: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ─── Instance Files ──────────────────────────

class InstanceFileOut(BaseModel):
    id: str
    instance_id: str
    file_name: str
    file_path: str
    file_size: int
    file_type: str
    uploaded_at: datetime

    class Config:
        from_attributes = True


class UploadResponse(BaseModel):
    success: bool
    message: str
    files: List[InstanceFileOut]
