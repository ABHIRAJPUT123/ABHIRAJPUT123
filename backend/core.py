import os
import time
import uuid
import logging
import threading
from collections import defaultdict, deque, OrderedDict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import bcrypt
import jwt
import requests
from dotenv import load_dotenv
from fastapi import HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorClient

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(level=logging.INFO, format='{"t":"%(asctime)s","lvl":"%(levelname)s","src":"%(name)s","msg":"%(message)s"}')
logger = logging.getLogger("anora")

client = AsyncIOMotorClient(os.environ["MONGO_URL"], maxPoolSize=50, serverSelectionTimeoutMS=5000)
db = client[os.environ["DB_NAME"]]

JWT_ALGORITHM = "HS256"
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() == "true"


def now():
    return datetime.now(timezone.utc)


def now_iso():
    return now().isoformat()


def new_id():
    return str(uuid.uuid4())


def clean(doc):
    if doc is None:
        return None
    doc.pop("_id", None)
    return doc


# ---------- Auth ----------
def hash_password(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()


def verify_password(p: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(p.encode(), h.encode())
    except ValueError:
        return False


def _secret():
    return os.environ["JWT_SECRET"]


def create_access_token(uid, email, role):
    return jwt.encode({"sub": uid, "email": email, "role": role, "type": "access",
                       "exp": now() + timedelta(minutes=15)}, _secret(), algorithm=JWT_ALGORITHM)


def create_refresh_token(uid):
    return jwt.encode({"sub": uid, "type": "refresh", "exp": now() + timedelta(days=7)}, _secret(), algorithm=JWT_ALGORITHM)


def set_auth_cookies(response, access, refresh=None):
    response.set_cookie("access_token", access, httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=900, path="/")
    if refresh:
        response.set_cookie("refresh_token", refresh, httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=604800, path="/api/auth")


def decode_token(token, expected_type):
    try:
        payload = jwt.decode(token, _secret(), algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid session")
    if payload.get("type") != expected_type:
        raise HTTPException(401, "Invalid token type")
    return payload


def public_user(u):
    return {"id": u["id"], "email": u["email"], "name": u.get("name", ""), "phone": u.get("phone", ""),
            "role": u.get("role", "customer"), "addresses": u.get("addresses", []), "created_at": u.get("created_at")}


async def get_current_user(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        h = request.headers.get("Authorization", "")
        token = h[7:] if h.startswith("Bearer ") else None
    if not token:
        raise HTTPException(401, "Not authenticated")
    payload = decode_token(token, "access")
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
    if not user or user.get("disabled"):
        raise HTTPException(401, "User not found")
    return user


async def get_optional_user(request: Request):
    try:
        return await get_current_user(request)
    except HTTPException:
        return None


async def require_admin(request: Request):
    user = await get_current_user(request)
    if user.get("role") not in ("admin", "staff"):
        raise HTTPException(403, "Admin access required")
    return user


async def require_owner(request: Request):
    user = await get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(403, "Owner access required")
    return user


# ---------- Rate limiting (per-process sliding window) ----------
_hits = defaultdict(deque)
_lock = threading.Lock()


def client_ip(request: Request):
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def rate_limit(bucket: str, limit: int, window: int):
    async def dep(request: Request):
        key = f"{bucket}:{client_ip(request)}"
        t = time.time()
        with _lock:
            q = _hits[key]
            while q and q[0] < t - window:
                q.popleft()
            if len(q) >= limit:
                raise HTTPException(429, "Too many requests. Please wait a moment and try again.")
            q.append(t)
    return dep


# ---------- Object storage ----------
STORAGE_BASE = (os.environ.get("INTEGRATION_PROXY_URL") or "").strip() or "https://integrations.emergentagent.com"
STORAGE_URL = STORAGE_BASE.rstrip("/") + "/objstore/api/v1/storage"
APP_NAME = os.environ.get("APP_NAME", "anora")
_storage_key = None


def init_storage(force=False):
    global _storage_key
    if _storage_key and not force:
        return _storage_key
    r = requests.post(f"{STORAGE_URL}/init", json={"emergent_key": os.environ.get("EMERGENT_LLM_KEY")}, timeout=30)
    r.raise_for_status()
    _storage_key = r.json()["storage_key"]
    return _storage_key


def put_object(path, data, content_type):
    for attempt in range(2):
        r = requests.put(f"{STORAGE_URL}/objects/{path}", headers={"X-Storage-Key": init_storage(force=attempt > 0), "Content-Type": content_type}, data=data, timeout=120)
        if r.status_code == 404 and attempt == 0:
            continue
        r.raise_for_status()
        return r.json()


class _LRU(OrderedDict):
    def __init__(self, max_bytes=150 * 1024 * 1024):
        super().__init__()
        self.max_bytes, self.size = max_bytes, 0

    def put(self, k, v):
        self[k] = v
        self.size += len(v[0])
        while self.size > self.max_bytes:
            _, old = self.popitem(last=False)
            self.size -= len(old[0])


media_cache = _LRU()


def get_object(path):
    if path in media_cache:
        media_cache.move_to_end(path)
        return media_cache[path]
    for attempt in range(2):
        r = requests.get(f"{STORAGE_URL}/objects/{path}", headers={"X-Storage-Key": init_storage(force=attempt > 0)}, timeout=60)
        if r.status_code == 404 and attempt == 0:
            continue
        r.raise_for_status()
        val = (r.content, r.headers.get("Content-Type", "application/octet-stream"))
        media_cache.put(path, val)
        return val


# ---------- Settings ----------
DEFAULT_SETTINGS = {
    "key": "store",
    "announcement": "New season: The Jaipur Edit — block prints and pure cotton, now online",
    "shipping_flat": 99,
    "free_shipping_threshold": 2500,
    "delivery_estimate": "Usually dispatched within 1–2 business days. Delivery typically 3–7 business days depending on location.",
    "contact_email": "care@anora.in",
    "contact_phone": "",
    "hero": {"eyebrow": "The Jaipur Edit", "title": "Quiet colour, hand-printed in the Pink City", "subtitle": "Pure cottons and fluid rayons, cut for days that move from courtyard to city.", "image": "", "cta_primary": "Shop the Collection", "cta_secondary": "Discover New Arrivals"},
    "editorials": [],
}


async def get_settings():
    s = await db.settings.find_one({"key": "store"}, {"_id": 0})
    return {**DEFAULT_SETTINGS, **(s or {})}
