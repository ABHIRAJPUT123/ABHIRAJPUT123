import hashlib
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field

from core import (db, now, now_iso, new_id, hash_password, verify_password, create_access_token, create_refresh_token,
                  set_auth_cookies, decode_token, public_user, get_current_user, rate_limit, client_ip, logger)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class ForgotIn(BaseModel):
    email: EmailStr


class ResetIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=8, max_length=128)


def _issue(response: Response, user):
    set_auth_cookies(response, create_access_token(user["id"], user["email"], user.get("role", "customer")), create_refresh_token(user["id"]))


@router.post("/register", dependencies=[Depends(rate_limit("register", 10, 3600))])
async def register(body: RegisterIn, response: Response):
    email = body.email.lower()
    if await db.users.find_one({"email": email}):
        raise HTTPException(400, "An account with this email already exists.")
    user = {"id": new_id(), "email": email, "name": body.name.strip(), "password_hash": hash_password(body.password),
            "role": "customer", "addresses": [], "wishlist": [], "created_at": now_iso()}
    await db.users.insert_one(user)
    await db.orders.update_many({"email": email, "user_id": None}, {"$set": {"user_id": user["id"]}})
    _issue(response, user)
    return public_user(user)


@router.post("/login", dependencies=[Depends(rate_limit("login", 20, 300))])
async def login(body: LoginIn, request: Request, response: Response):
    email = body.email.lower()
    ident = f"{client_ip(request)}:{email}"
    att = await db.login_attempts.find_one({"identifier": ident})
    if att and att.get("count", 0) >= 5 and att.get("locked_until") and att["locked_until"] > now().isoformat():
        raise HTTPException(429, "Too many failed attempts. Please try again in 15 minutes.")
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user.get("password_hash", "")):
        cnt = (att or {}).get("count", 0) + 1
        upd = {"count": cnt, "identifier": ident}
        if cnt >= 5:
            upd["locked_until"] = (now() + timedelta(minutes=15)).isoformat()
        await db.login_attempts.update_one({"identifier": ident}, {"$set": upd}, upsert=True)
        raise HTTPException(401, "Invalid email or password.")
    await db.login_attempts.delete_one({"identifier": ident})
    _issue(response, user)
    return public_user(user)


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/api/auth")
    return {"ok": True}


@router.get("/me")
async def me(user=Depends(get_current_user)):
    return public_user(user)


@router.post("/refresh")
async def refresh(request: Request, response: Response):
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(401, "No session")
    payload = decode_token(token, "refresh")
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0})
    if not user:
        raise HTTPException(401, "User not found")
    set_auth_cookies(response, create_access_token(user["id"], user["email"], user.get("role", "customer")))
    return public_user(user)


@router.post("/forgot-password", dependencies=[Depends(rate_limit("forgot", 5, 900))])
async def forgot(body: ForgotIn, request: Request):
    user = await db.users.find_one({"email": body.email.lower()})
    if user:
        token = secrets.token_urlsafe(32)
        await db.password_reset_tokens.insert_one({"token_hash": hashlib.sha256(token.encode()).hexdigest(), "user_id": user["id"],
                                                   "expires_at": now() + timedelta(hours=1), "used": False})
        origin = request.headers.get("origin", "")
        # Email service not configured: link is logged for the operator (see README > Email).
        logger.info(f"PASSWORD_RESET_LINK {origin}/reset-password?token={token}")
    return {"ok": True, "message": "If an account exists for this email, a reset link has been sent."}


@router.post("/reset-password", dependencies=[Depends(rate_limit("reset", 10, 900))])
async def reset(body: ResetIn):
    th = hashlib.sha256(body.token.encode()).hexdigest()
    rec = await db.password_reset_tokens.find_one({"token_hash": th, "used": False})
    if not rec or rec["expires_at"].replace(tzinfo=now().tzinfo) < now():
        raise HTTPException(400, "This reset link is invalid or has expired.")
    await db.users.update_one({"id": rec["user_id"]}, {"$set": {"password_hash": hash_password(body.password)}})
    await db.password_reset_tokens.update_one({"_id": rec["_id"]}, {"$set": {"used": True}})
    return {"ok": True}
