"""Auth router: register, login, logout, me."""
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from database import get_db
import models, schemas
from auth import (
    COOKIE_NAME, EXPIRE_MIN,
    hash_password, verify_password, create_access_token, get_current_user,
)
from limiter import limiter
from settings import settings

router = APIRouter(prefix="/auth", tags=["auth"])

_COOKIE_OPTS = dict(
    key=COOKIE_NAME,
    httponly=True,
    samesite="lax",
    path="/",
    secure=settings.is_production,
    max_age=EXPIRE_MIN * 60,
)


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(value=token, **_COOKIE_OPTS)


# ─── Register ────────────────────────────────────────────────────────────────
@router.post("/register", response_model=schemas.Token, status_code=201)
@limiter.limit("5/minute")
def register(
    request: Request,
    payload: schemas.UserRegister,
    response: Response,
    db: Session = Depends(get_db),
):
    existing = db.query(models.User).filter(models.User.email == payload.email.lower()).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    tenant_id = str(uuid.uuid4())
    tenant = models.Tenant(id=tenant_id, name=f"{payload.name}'s Workspace")
    db.add(tenant)
    db.flush()

    user = models.User(
        name=payload.name,
        email=payload.email.lower(),
        hashed_password=hash_password(payload.password),
        tenant_id=tenant_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token({"sub": user.id, "tenant_id": user.tenant_id})
    _set_auth_cookie(response, token)
    return schemas.Token(access_token=token, user=schemas.UserOut.model_validate(user))


# ─── Login ───────────────────────────────────────────────────────────────────
@router.post("/login", response_model=schemas.Token)
@limiter.limit("10/minute")
def login(
    request: Request,
    payload: schemas.UserLogin,
    response: Response,
    db: Session = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.email == payload.email.lower()).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled.")

    token = create_access_token({"sub": user.id, "tenant_id": user.tenant_id})
    _set_auth_cookie(response, token)
    return schemas.Token(access_token=token, user=schemas.UserOut.model_validate(user))


# ─── Logout ──────────────────────────────────────────────────────────────────
@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"success": True}


# ─── Me ──────────────────────────────────────────────────────────────────────
@router.get("/me", response_model=schemas.UserOut)
def me(current_user: models.User = Depends(get_current_user)):
    return current_user
