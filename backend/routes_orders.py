import asyncio
import json
import os
import secrets
from typing import List, Optional

import razorpay
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field, field_validator
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from core import db, now, now_iso, new_id, get_optional_user, get_settings, rate_limit, logger
from pricing import valid_phone, valid_pincode, coupon_discount, compute_totals, MAX_QTY_PER_LINE, razorpay_payment_signature_ok, razorpay_webhook_signature_ok
from routes_catalog import bust_cache

router = APIRouter(prefix="/api", tags=["orders"])


def rzp_keys():
    return os.environ.get("RAZORPAY_KEY_ID", ""), os.environ.get("RAZORPAY_KEY_SECRET", "")


def payment_configured():
    k, s = rzp_keys()
    return bool(k and s)


class LineIn(BaseModel):
    product_id: str = Field(max_length=64)
    size: str = Field(max_length=10)
    qty: int = Field(ge=1, le=MAX_QTY_PER_LINE)


class CartIn(BaseModel):
    items: List[LineIn] = Field(max_length=30)
    coupon_code: Optional[str] = Field(None, max_length=30)


class AddressIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(max_length=16)
    line1: str = Field(min_length=3, max_length=160)
    line2: str = Field("", max_length=160)
    city: str = Field(min_length=2, max_length=60)
    state: str = Field(min_length=2, max_length=60)
    pincode: str = Field(max_length=6)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v):
        if not valid_phone(v):
            raise ValueError("Enter a valid 10-digit Indian mobile number")
        return v.replace(" ", "")[-10:]

    @field_validator("pincode")
    @classmethod
    def _pin(cls, v):
        if not valid_pincode(v):
            raise ValueError("Enter a valid 6-digit PIN code")
        return v


class CheckoutIn(CartIn):
    email: EmailStr
    shipping: AddressIn
    billing_same: bool = True
    billing: Optional[AddressIn] = None
    idempotency_key: str = Field(min_length=12, max_length=64)
    save_address: bool = False


async def price_cart(items, coupon_code):
    ids = list({i.product_id for i in items})
    prods = {p["id"]: p for p in await db.products.find({"id": {"$in": ids}}, {"_id": 0}).to_list(len(ids) or 1)}
    lines, issues = [], []
    for i in items:
        p = prods.get(i.product_id)
        if not p or p.get("status") != "published":
            issues.append({"product_id": i.product_id, "size": i.size, "issue": "unavailable", "message": "This item is no longer available."})
            continue
        size = next((s for s in p.get("sizes", []) if s["size"] == i.size), None)
        stock = size.get("stock", 0) if size else 0
        if stock <= 0:
            issues.append({"product_id": i.product_id, "size": i.size, "issue": "out_of_stock", "message": f"{p['title']} in size {i.size} is out of stock."})
            continue
        qty = min(i.qty, stock, MAX_QTY_PER_LINE)
        if qty < i.qty:
            issues.append({"product_id": i.product_id, "size": i.size, "issue": "qty_reduced", "message": f"Only {qty} available for {p['title']} ({i.size})."})
        img = (p.get("images") or [{}])[0].get("path", "")
        lines.append({"product_id": p["id"], "slug": p["slug"], "sku": p["sku"], "title": p["title"], "size": i.size, "color": p.get("color", {}).get("name", ""),
                      "qty": qty, "unit_price": p["price"], "compare_at_price": p.get("compare_at_price"), "line_total": p["price"] * qty, "image": img, "max_qty": min(stock, MAX_QTY_PER_LINE)})
    subtotal = sum(l["line_total"] for l in lines)
    discount, coupon_error, coupon = 0, None, None
    if coupon_code:
        coupon = await db.coupons.find_one({"code": coupon_code.strip().upper()}, {"_id": 0})
        discount, coupon_error = coupon_discount(coupon, subtotal)
    settings = await get_settings()
    totals = compute_totals(subtotal, discount, settings)
    return {"lines": lines, "issues": issues, "totals": totals, "coupon": {"code": coupon["code"], "description": coupon.get("description", "")} if coupon and not coupon_error else None,
            "coupon_error": coupon_error, "free_shipping_threshold": settings.get("free_shipping_threshold")}


@router.post("/cart/validate", dependencies=[Depends(rate_limit("cart", 120, 60))])
async def validate_cart(body: CartIn):
    return await price_cart(body.items, body.coupon_code)


def order_number():
    return "ANR" + now().strftime("%y%m") + str(secrets.randbelow(900000) + 100000)


def public_order(o, full=True):
    keys = ["id", "order_number", "email", "status", "payment_status", "lines", "totals", "coupon", "shipping", "created_at", "timeline", "tracking", "paid_at"]
    out = {k: o.get(k) for k in keys}
    if not full:
        out.pop("email")
    return out


async def create_rzp_order(order):
    key, secret = rzp_keys()
    rc = razorpay.Client(auth=(key, secret))
    payload = {"amount": order["totals"]["total"] * 100, "currency": "INR", "receipt": order["order_number"], "notes": {"order_id": order["id"]}}
    try:
        rz = await asyncio.wait_for(asyncio.to_thread(rc.order.create, payload), timeout=15)
    except Exception as e:
        logger.error(f"razorpay order create failed {order['order_number']}: {e}")
        raise HTTPException(502, "Payment gateway is temporarily unavailable. Your bag is saved — please try again shortly.")
    await db.orders.update_one({"id": order["id"]}, {"$set": {"razorpay_order_id": rz["id"]}, "$push": {"payment_attempts": {"razorpay_order_id": rz["id"], "at": now_iso()}}})
    return {"key_id": key, "order_id": rz["id"], "amount": rz["amount"], "currency": "INR"}


@router.post("/checkout", dependencies=[Depends(rate_limit("checkout", 15, 600))])
async def checkout(body: CheckoutIn, request: Request):
    user = await get_optional_user(request)
    existing = await db.orders.find_one({"idempotency_key": body.idempotency_key}, {"_id": 0})
    if existing:
        if existing["payment_status"] == "paid":
            return {"order": public_order(existing), "access_token": existing["access_token"], "payment_configured": payment_configured(), "razorpay": None}
        rz = await create_rzp_order(existing) if payment_configured() else None
        return {"order": public_order(existing), "access_token": existing["access_token"], "payment_configured": payment_configured(), "razorpay": rz}
    priced = await price_cart(body.items, body.coupon_code)
    if priced["issues"]:
        raise HTTPException(409, {"message": "Some items in your bag have changed. Please review your bag.", "issues": priced["issues"]})
    if body.coupon_code and priced["coupon_error"]:
        raise HTTPException(400, priced["coupon_error"])
    if not priced["lines"]:
        raise HTTPException(400, "Your bag is empty.")
    ship = body.shipping.model_dump()
    order = {"id": new_id(), "order_number": order_number(), "access_token": secrets.token_urlsafe(24), "idempotency_key": body.idempotency_key,
             "user_id": user["id"] if user else None, "email": body.email.lower(), "lines": priced["lines"], "totals": priced["totals"],
             "coupon": priced["coupon"], "shipping": ship, "billing": ship if body.billing_same or not body.billing else body.billing.model_dump(),
             "status": "pending_payment", "payment_status": "pending", "created_at": now_iso(), "timeline": [{"status": "pending_payment", "at": now_iso(), "note": "Order placed, awaiting payment"}],
             "tracking": None, "payment_attempts": [], "is_test": not os.environ.get("RAZORPAY_KEY_ID", "").startswith("rzp_live")}
    for _ in range(3):
        try:
            await db.orders.insert_one(dict(order))
            break
        except DuplicateKeyError as e:
            if "idempotency_key" in str(e):
                existing = await db.orders.find_one({"idempotency_key": body.idempotency_key}, {"_id": 0})
                return {"order": public_order(existing), "access_token": existing["access_token"], "payment_configured": payment_configured(), "razorpay": None}
            order["order_number"] = order_number()
    if user and body.save_address:
        addrs = user.get("addresses", [])
        if not any(a.get("line1") == ship["line1"] and a.get("pincode") == ship["pincode"] for a in addrs):
            await db.users.update_one({"id": user["id"]}, {"$push": {"addresses": {"id": new_id(), **ship, "is_default": not addrs}}})
    rz = await create_rzp_order(order) if payment_configured() else None
    return {"order": public_order(order), "access_token": order["access_token"], "payment_configured": payment_configured(), "razorpay": rz}


async def mark_paid(razorpay_order_id, payment_id, source):
    """Idempotent: only the first successful transition decrements stock."""
    o = await db.orders.find_one_and_update(
        {"razorpay_order_id": razorpay_order_id, "payment_status": {"$ne": "paid"}},
        {"$set": {"payment_status": "paid", "status": "confirmed", "paid_at": now_iso(), "payment_id": payment_id, "paid_via": source},
         "$push": {"timeline": {"status": "confirmed", "at": now_iso(), "note": "Payment verified"}}},
        return_document=ReturnDocument.AFTER)
    if not o:
        return await db.orders.find_one({"razorpay_order_id": razorpay_order_id}, {"_id": 0})
    stock_issue = False
    for l in o["lines"]:
        r = await db.products.update_one({"id": l["product_id"], "sizes": {"$elemMatch": {"size": l["size"], "stock": {"$gte": l["qty"]}}}},
                                         {"$inc": {"sizes.$.stock": -l["qty"], "sales_count": l["qty"]}})
        if r.modified_count == 0:
            stock_issue = True
    if o.get("coupon"):
        await db.coupons.update_one({"code": o["coupon"]["code"]}, {"$inc": {"used_count": 1}})
    if stock_issue:
        await db.orders.update_one({"id": o["id"]}, {"$set": {"stock_issue": True}})
        logger.warning(f"Order {o['order_number']} paid but stock insufficient — needs admin review")
    bust_cache()
    logger.info(f"Order {o['order_number']} marked paid via {source}")
    return o


class VerifyIn(BaseModel):
    order_id: str = Field(max_length=64)
    access_token: str = Field(max_length=64)
    razorpay_order_id: str = Field(max_length=64)
    razorpay_payment_id: str = Field(max_length=64)
    razorpay_signature: str = Field(max_length=256)


@router.post("/checkout/verify", dependencies=[Depends(rate_limit("verify", 30, 600))])
async def verify(body: VerifyIn):
    o = await db.orders.find_one({"id": body.order_id, "access_token": body.access_token}, {"_id": 0})
    if not o or o.get("razorpay_order_id") != body.razorpay_order_id:
        raise HTTPException(404, "Order not found")
    _, secret = rzp_keys()
    if not razorpay_payment_signature_ok(body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature, secret):
        await db.orders.update_one({"id": o["id"]}, {"$push": {"timeline": {"status": "payment_failed", "at": now_iso(), "note": "Signature verification failed"}}})
        raise HTTPException(400, "We could not verify this payment. If money was debited, it will be reconciled automatically or refunded.")
    o = await mark_paid(body.razorpay_order_id, body.razorpay_payment_id, "checkout_verify")
    return {"order": public_order(o)}


class FailIn(BaseModel):
    order_id: str = Field(max_length=64)
    access_token: str = Field(max_length=64)
    reason: str = Field("", max_length=300)


@router.post("/checkout/failed")
async def payment_failed(body: FailIn):
    r = await db.orders.update_one({"id": body.order_id, "access_token": body.access_token, "payment_status": {"$ne": "paid"}},
                                   {"$push": {"timeline": {"status": "payment_failed", "at": now_iso(), "note": body.reason[:300] or "Payment not completed"}}})
    return {"ok": bool(r.matched_count)}


class RetryIn(BaseModel):
    order_id: str = Field(max_length=64)
    access_token: str = Field(max_length=64)


@router.post("/checkout/retry", dependencies=[Depends(rate_limit("retry", 10, 600))])
async def retry(body: RetryIn):
    o = await db.orders.find_one({"id": body.order_id, "access_token": body.access_token}, {"_id": 0})
    if not o:
        raise HTTPException(404, "Order not found")
    if o["payment_status"] == "paid" or o["status"] == "cancelled":
        raise HTTPException(400, "This order can no longer be paid.")
    if not payment_configured():
        raise HTTPException(503, "Online payment is not yet configured for this store.")
    return {"razorpay": await create_rzp_order(o), "order": public_order(o)}


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    body = await request.body()
    if len(body) > 256 * 1024:
        raise HTTPException(413, "Payload too large")
    if not razorpay_webhook_signature_ok(body, request.headers.get("X-Razorpay-Signature"), os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")):
        raise HTTPException(400, "Invalid signature")
    event_id = request.headers.get("X-Razorpay-Event-Id") or new_id()
    try:
        await db.webhook_events.insert_one({"event_id": event_id, "received_at": now_iso()})
    except DuplicateKeyError:
        return {"status": "duplicate"}
    evt = json.loads(body)
    kind = evt.get("event", "")
    pay = evt.get("payload", {}).get("payment", {}).get("entity", {})
    if kind in ("payment.captured", "order.paid") and pay.get("order_id"):
        await mark_paid(pay["order_id"], pay.get("id"), f"webhook:{kind}")
    elif kind == "payment.failed" and pay.get("order_id"):
        await db.orders.update_one({"razorpay_order_id": pay["order_id"], "payment_status": {"$ne": "paid"}},
                                   {"$push": {"timeline": {"status": "payment_failed", "at": now_iso(), "note": pay.get("error_description", "Payment failed")[:200]}}})
    return {"status": "ok"}


@router.get("/orders/view/{order_id}")
async def view_order(order_id: str, t: str):
    o = await db.orders.find_one({"id": order_id[:64], "access_token": t[:64]}, {"_id": 0})
    if not o:
        raise HTTPException(404, "Order not found")
    return {"order": public_order(o), "payment_configured": payment_configured()}


class TrackIn(BaseModel):
    order_number: str = Field(min_length=6, max_length=20)
    email: EmailStr


@router.post("/orders/track", dependencies=[Depends(rate_limit("track", 20, 600))])
async def track(body: TrackIn):
    o = await db.orders.find_one({"order_number": body.order_number.strip().upper(), "email": body.email.lower()}, {"_id": 0})
    if not o:
        raise HTTPException(404, "We couldn't find an order with those details.")
    out = public_order(o, full=False)
    out["shipping"] = {"city": o["shipping"]["city"], "pincode": o["shipping"]["pincode"]}
    return out
