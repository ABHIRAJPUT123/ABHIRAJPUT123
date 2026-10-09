# ANORA — Luxury Women's Fashion E-Commerce

React (CRA + Tailwind + shadcn/ui) storefront & admin · FastAPI backend · MongoDB · Emergent Object Storage · Razorpay (test-mode structure).

> **Demo content notice** — the 10 seeded products use the owner's uploaded photography and product facts read from the supplied supplier screenshots. **Retail prices and stock (8 per size) are placeholders** flagged `is_demo: true` and shown with a "Demo" badge in Admin. Confirm real prices/inventory before launch. Policy pages are seeded as **DRAFT** and must be reviewed by the owner/legal counsel.

## Architecture
```
frontend/ (React SPA)            backend/ (FastAPI, 0.0.0.0:8001, all routes /api/*)
  src/context  Auth/Cart/          server.py        app, security headers, CSRF origin guard, body limits, health
    Wishlist/Consent               core.py          db pool, JWT cookies, RBAC deps, rate limiter, object storage + LRU media cache
  src/components Header, Mega,     pricing.py       pure business logic (coupon, totals, phone/PIN, Razorpay HMAC)  ← unit tested
    Bag, Filters, Gallery,         routes_catalog   products, facets, search, media, pages, newsletter, consent log, sitemap
    CookieConsent, ...             routes_orders    cart validation, checkout (idempotent), verify, webhook, retry, tracking
  src/pages  storefront            routes_account   profile, addresses, wishlist, orders
  src/admin  dashboard             routes_admin     products, uploads, orders, refunds, coupons, customers, newsletter, settings, pages
                                   seed.py          idempotent indexes + admin + demo catalog + media upload
```

### Key guarantees
- **Prices & stock validated server-side** (`/api/cart/validate`, `/api/checkout`). The client never sets prices.
- **Orders are only marked paid after Razorpay HMAC verification** (checkout signature or signed webhook). `mark_paid` is an atomic, idempotent transition; duplicate callbacks/webhooks never double-decrement stock. Webhook events are de-duplicated by `X-Razorpay-Event-Id` (unique index). Checkout uses an `idempotency_key` (unique index) so double-submits return the same order.
- **Payment failure recovery** — unpaid orders can be paid later from `/order/:id?t=…` via `/api/checkout/retry`.
- **RBAC on the backend** — every `/api/admin/*` route requires role `admin`/`staff`; deleting products, refunds, customer records and CSV export require `admin` (owner).
- **Auth** — bcrypt hashes, short-lived JWT access cookie (15 min) + refresh cookie (7 days, path-scoped), httpOnly/Secure/SameSite=Lax, brute-force lockout (5 attempts → 15 min), hashed single-use reset tokens (1 h TTL).
- **CSRF** — cookie auth + SameSite=Lax + Origin allow-list check on all unsafe methods.
- **Uploads** — admin only, ≤ 8 MB, verified with Pillow, re-encoded to WebP (600 & 1400 px), stored in object storage; served via `/api/media/*` with `Cache-Control: immutable` (CDN-friendly).
- **Consent** — optional scripts (GA4, Meta Pixel) are injected by `src/lib/trackers.js` **only** after the matching category is granted, and only if their env IDs exist. Recently-viewed (personalisation) is stored only with consent and erased on withdrawal. Choices stored in localStorage + first-party cookie; an anonymous audit record is written to `consent_log`. "Cookie Settings" in the footer reopens preferences.

## Environment variables
backend/.env
| Key | Purpose |
|---|---|
| MONGO_URL, DB_NAME | MongoDB connection (managed by platform) |
| CORS_ORIGINS | Comma-separated allowed origins (also used for CSRF origin check) |
| FRONTEND_URL | Public site URL (sitemap) |
| JWT_SECRET | 64-char random secret — rotate for production |
| ADMIN_EMAIL / ADMIN_PASSWORD | Seeded owner account (password re-synced on boot) |
| EMERGENT_LLM_KEY, APP_NAME | Object storage auth + path prefix |
| RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET | **Empty = payments disabled.** Use `rzp_test_…` first |
| RAZORPAY_WEBHOOK_SECRET | Must match the secret set on the Razorpay webhook (replace the dev placeholder) |
| COOKIE_SECURE | `true` in any HTTPS environment |

frontend/.env: `REACT_APP_BACKEND_URL` (required), optional `REACT_APP_GA_MEASUREMENT_ID`, `REACT_APP_META_PIXEL_ID` (loaded only with consent).

## Setting up payments (Razorpay)
1. Create a Razorpay account → Dashboard → *Test Mode* → Settings → API Keys → generate key.
2. Put `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` in backend env, restart backend.
3. Dashboard → Webhooks → add `https://<your-domain>/api/webhooks/razorpay`, events `payment.captured`, `order.paid`, `payment.failed`, set a secret → `RAZORPAY_WEBHOOK_SECRET`.
4. Test with Razorpay test UPI/cards. Admin → Overview shows mode (TEST/LIVE).
5. Go live: complete KYC, swap to `rzp_live_…` keys + live webhook secret.

## Email (not configured)
Order/shipping emails and password-reset emails are **not sent** yet. The reset link is written to the backend log (`PASSWORD_RESET_LINK`). To enable, add a provider (e.g. Resend/SendGrid) and send from a background task in `routes_auth.forgot` and `routes_orders.mark_paid`.

## Local development
```
cd backend && pip install -r requirements.txt && uvicorn server:app --reload --port 8001
cd frontend && yarn install && yarn start
cd backend && python -m pytest tests -q        # business-logic tests
```
Seeding runs automatically on startup and is idempotent.

## Deployment & operations
- Health check: `GET /api/health` (503 when DB unreachable).
- Logs are structured single-line JSON-ish records; ship to your log platform.
- Put a CDN (e.g. Cloudflare) in front of the domain; `/api/media/*` and static assets are long-cache safe.
- Rate limiting is per-process in memory; when running multiple replicas, add edge rate-limiting (CDN/WAF).
- **Backups**: use your managed MongoDB's automated daily snapshots + point-in-time restore. Manual: `mongodump --uri "$MONGO_URL" --db $DB_NAME --out backup/` and restore with `mongorestore --uri "$MONGO_URL" --nsInclude "$DB_NAME.*" backup/`.
- No load testing has been performed; do not make capacity claims until tested (e.g. k6 against a staging copy).

## SEO
Per-route titles/descriptions/canonical/OG via `useSeo`, Product + Breadcrumb JSON-LD, `/api/sitemap.xml`, `public/robots.txt` (update the sitemap host for your domain). The storefront is a client-rendered SPA; for best crawlability add prerendering (e.g. a prerender service at the CDN) before launch.

## Launch checklist
- [ ] Confirm real prices, stock, size chart and fabric for trousers (marked "to be confirmed")
- [ ] Review & approve policy pages (Admin → Content & Pages), add grievance officer
- [ ] Add Razorpay keys + webhook secret, run test-mode orders end-to-end
- [ ] Configure email provider; set contact email/phone in Admin
- [ ] Rotate `JWT_SECRET`, change admin password, set production `CORS_ORIGINS`/`FRONTEND_URL`
- [ ] Optional analytics IDs; verify they load only after consent
