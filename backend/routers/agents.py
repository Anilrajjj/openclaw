"""Lyzr-style agents: CRUD, chat inference, and session history."""
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

import models, schemas
from auth import get_current_user
from database import get_db
from limiter import limiter
from services import encryption
from services.agent_runtime import (
    AgentRuntimeError,
    has_feature,
    load_session_history,
    run_chat_turn,
)
from services.llm_client import SUPPORTED_PROVIDERS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agents", tags=["agents"])


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _owned_agent(agent_id: str, current_user: models.User, db: Session) -> models.Agent:
    agent = db.query(models.Agent).filter(models.Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")
    return agent


def _validate_managed_agents(db: Session, tenant_id: str, ids: List[str], self_id: str = "") -> None:
    for sub_id in ids:
        if sub_id == self_id:
            raise HTTPException(status_code=400, detail="An agent cannot manage itself.")
        exists = (
            db.query(models.Agent)
            .filter(models.Agent.id == sub_id, models.Agent.tenant_id == tenant_id)
            .first()
        )
        if not exists:
            raise HTTPException(status_code=400, detail=f"Managed agent '{sub_id}' not found.")


def _agent_out(agent: models.Agent) -> schemas.AgentOut:
    out = schemas.AgentOut.model_validate(agent)
    out.api_key_set = bool(agent.api_key_encrypted)
    if agent.api_key_encrypted:
        try:
            out.api_key_masked = encryption.mask(encryption.decrypt(agent.api_key_encrypted))
        except Exception:
            out.api_key_masked = "••••••••"
    return out


# ─── Meta ─────────────────────────────────────────────────────────────────────

@router.get("/providers")
def list_providers(current_user: models.User = Depends(get_current_user)):
    return {"providers": SUPPORTED_PROVIDERS}


# ─── CRUD ─────────────────────────────────────────────────────────────────────

@router.get("", response_model=List[schemas.AgentOut])
def list_agents(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agents = (
        db.query(models.Agent)
        .filter(models.Agent.tenant_id == current_user.tenant_id)
        .order_by(models.Agent.created_at.desc())
        .all()
    )
    return [_agent_out(a) for a in agents]


@router.post("", response_model=schemas.AgentOut, status_code=201)
def create_agent(
    payload: schemas.AgentCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _validate_managed_agents(db, current_user.tenant_id, payload.managed_agents)

    agent = models.Agent(
        tenant_id=current_user.tenant_id,
        name=payload.name,
        description=payload.description or "",
        agent_role=payload.agent_role or "",
        agent_goal=payload.agent_goal or "",
        agent_instructions=payload.agent_instructions or "",
        examples=payload.examples or "",
        provider=payload.provider,
        model=payload.model,
        temperature=payload.temperature,
        top_p=payload.top_p,
        features=payload.features,
        tools=payload.tools,
        managed_agents=payload.managed_agents,
        api_key_encrypted=encryption.encrypt(payload.api_key) if payload.api_key else None,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    logger.info(f"Created agent {agent.id} ({agent.name}) for tenant {agent.tenant_id}")
    return _agent_out(agent)


@router.get("/{agent_id}", response_model=schemas.AgentOut)
def get_agent(
    agent_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _agent_out(_owned_agent(agent_id, current_user, db))


@router.put("/{agent_id}", response_model=schemas.AgentOut)
def update_agent(
    agent_id: str,
    payload: schemas.AgentUpdate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = _owned_agent(agent_id, current_user, db)
    updates = payload.model_dump(exclude_unset=True)

    if "managed_agents" in updates and updates["managed_agents"] is not None:
        _validate_managed_agents(
            db, current_user.tenant_id, updates["managed_agents"], self_id=agent.id
        )

    api_key = updates.pop("api_key", None)
    if api_key is not None:
        agent.api_key_encrypted = encryption.encrypt(api_key) if api_key else None

    for field, value in updates.items():
        if value is not None:
            setattr(agent, field, value)
            if field in ("features", "tools", "managed_agents"):
                flag_modified(agent, field)

    db.commit()
    db.refresh(agent)
    return _agent_out(agent)


@router.delete("/{agent_id}")
def delete_agent(
    agent_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = _owned_agent(agent_id, current_user, db)

    # Remove this agent from any manager's managed_agents list.
    managers = (
        db.query(models.Agent)
        .filter(models.Agent.tenant_id == current_user.tenant_id)
        .all()
    )
    for mgr in managers:
        if agent.id in (mgr.managed_agents or []):
            mgr.managed_agents = [a for a in mgr.managed_agents if a != agent.id]
            flag_modified(mgr, "managed_agents")

    db.delete(agent)
    db.commit()
    return {"success": True, "message": f"Agent '{agent.name}' deleted."}


# ─── Chat ─────────────────────────────────────────────────────────────────────

@router.post("/{agent_id}/chat", response_model=schemas.ChatResponse)
@limiter.limit("30/minute")
def chat_with_agent(
    request: Request,
    agent_id: str,
    payload: schemas.ChatRequest,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = _owned_agent(agent_id, current_user, db)

    session = None
    if payload.session_id:
        session = (
            db.query(models.ChatSession)
            .filter(
                models.ChatSession.id == payload.session_id,
                models.ChatSession.agent_id == agent.id,
                models.ChatSession.tenant_id == current_user.tenant_id,
            )
            .first()
        )
        if not session:
            raise HTTPException(status_code=404, detail="Chat session not found")
    if session is None:
        session = models.ChatSession(
            agent_id=agent.id,
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            title=payload.message.strip()[:80],
        )
        db.add(session)
        db.flush()

    history = load_session_history(session) if has_feature(agent, "MEMORY") else []

    try:
        result = run_chat_turn(db, agent, payload.message, history=history)
    except AgentRuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc))

    db.add(models.ChatMessage(session_id=session.id, role="user", content=payload.message))
    db.add(
        models.ChatMessage(
            session_id=session.id,
            role="assistant",
            content=result["response"],
            meta={"delegations": result["delegations"]} if result["delegations"] else None,
        )
    )
    db.commit()

    return schemas.ChatResponse(
        session_id=session.id,
        agent_id=agent.id,
        response=result["response"],
        delegations=result["delegations"],
    )


# ─── Sessions ─────────────────────────────────────────────────────────────────

@router.get("/{agent_id}/sessions", response_model=List[schemas.ChatSessionOut])
def list_sessions(
    agent_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = _owned_agent(agent_id, current_user, db)
    return (
        db.query(models.ChatSession)
        .filter(
            models.ChatSession.agent_id == agent.id,
            models.ChatSession.user_id == current_user.id,
        )
        .order_by(models.ChatSession.updated_at.desc())
        .all()
    )


@router.get("/{agent_id}/sessions/{session_id}/messages", response_model=List[schemas.ChatMessageOut])
def list_session_messages(
    agent_id: str,
    session_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = _owned_agent(agent_id, current_user, db)
    session = (
        db.query(models.ChatSession)
        .filter(
            models.ChatSession.id == session_id,
            models.ChatSession.agent_id == agent.id,
            models.ChatSession.tenant_id == current_user.tenant_id,
        )
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return session.messages
