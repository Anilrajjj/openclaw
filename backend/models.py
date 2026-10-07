import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Integer, Float, JSON, Boolean, BigInteger
from sqlalchemy.orm import relationship, Session
from sqlalchemy.ext.hybrid import hybrid_property
from database import Base


def _gen_uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id              = Column(String, primary_key=True, default=_gen_uuid)
    name            = Column(String, nullable=False)
    email           = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    tenant_id       = Column(String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime, default=datetime.utcnow)

    tenant = relationship("Tenant", back_populates="users")


class Tenant(Base):
    __tablename__ = "tenants"

    id         = Column(String, primary_key=True, default=_gen_uuid)
    name       = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    instances     = relationship("Instance", back_populates="tenant", cascade="all, delete-orphan")
    users         = relationship("User", back_populates="tenant", cascade="all, delete-orphan")
    agents        = relationship("Agent", back_populates="tenant", cascade="all, delete-orphan")
    chat_sessions = relationship("ChatSession", back_populates="tenant", cascade="all, delete-orphan")


class Instance(Base):
    __tablename__ = "instances"

    id          = Column(String,  primary_key=True, default=_gen_uuid)
    tenant_id   = Column(String,  ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    name        = Column(String,  nullable=False)
    description = Column(String,  nullable=True, default="")
    status      = Column(String,  default="stopped")   # "running" | "stopped"
    port        = Column(Integer, nullable=True)        # assigned gateway port
    pid         = Column(Integer, nullable=True)        # running process PID
    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant = relationship("Tenant", back_populates="instances")
    config = relationship(
        "InstanceConfig",
        back_populates="instance",
        uselist=False,
        cascade="all, delete-orphan",
    )


class InstanceConfig(Base):
    __tablename__ = "instance_configs"

    id          = Column(String, primary_key=True, default=_gen_uuid)
    instance_id = Column(
        String, ForeignKey("instances.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    config_json = Column(JSON, nullable=False)
    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    instance = relationship("Instance", back_populates="config")


class Agent(Base):
    """Lyzr-style lightweight agent: a flat config record executed by the shared runtime."""
    __tablename__ = "agents"

    id                 = Column(String, primary_key=True, default=_gen_uuid)
    tenant_id          = Column(String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name               = Column(String, nullable=False)
    description        = Column(String, nullable=True, default="")
    agent_role         = Column(String, nullable=True, default="")
    agent_goal         = Column(String, nullable=True, default="")
    agent_instructions = Column(Text, nullable=True, default="")
    examples           = Column(Text, nullable=True, default="")

    provider           = Column(String, nullable=False, default="openai")
    model              = Column(String, nullable=False, default="gpt-4o-mini")
    temperature        = Column(Float, nullable=False, default=0.7)
    top_p              = Column(Float, nullable=False, default=1.0)
    api_key_encrypted  = Column(String, nullable=True)

    features           = Column(JSON, nullable=False, default=list)   # [{"type": "MEMORY", "config": {...}}]
    tools              = Column(JSON, nullable=False, default=list)   # reserved for built-in tools
    managed_agents     = Column(JSON, nullable=False, default=list)   # sub-agent ids this agent can delegate to

    created_at         = Column(DateTime, default=datetime.utcnow)
    updated_at         = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant   = relationship("Tenant", back_populates="agents")
    sessions = relationship("ChatSession", back_populates="agent", cascade="all, delete-orphan")


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id         = Column(String, primary_key=True, default=_gen_uuid)
    agent_id   = Column(String, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id  = Column(String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id    = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title      = Column(String, nullable=True, default="New chat")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    agent    = relationship("Agent", back_populates="sessions")
    tenant   = relationship("Tenant", back_populates="chat_sessions")
    messages = relationship(
        "ChatMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id         = Column(String, primary_key=True, default=_gen_uuid)
    session_id = Column(String, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    role       = Column(String, nullable=False)          # "user" | "assistant" | "tool"
    content    = Column(Text, nullable=False, default="")
    meta       = Column(JSON, nullable=True)             # delegation traces, tool-call info
    created_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("ChatSession", back_populates="messages")


class InstanceFile(Base):
    __tablename__ = "instance_files"

    id           = Column(String, primary_key=True, default=_gen_uuid)
    instance_id  = Column(String, ForeignKey("instances.id", ondelete="CASCADE"), nullable=False)
    file_name    = Column(String, nullable=False)
    file_path    = Column(String, nullable=False)
    file_size    = Column(BigInteger, nullable=False)
    file_type    = Column(String, nullable=False)
    uploaded_at  = Column(DateTime, default=datetime.utcnow)

    instance = relationship("Instance", backref="files")
