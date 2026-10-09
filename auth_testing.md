# Auth testing playbook (ANORA)
1. Mongo: `mongosh test_database --eval 'db.users.find({role:"admin"},{password_hash:1,email:1})'` — hash starts with `$2b$`. Indexes: users.email unique, login_attempts.identifier, password_reset_tokens.expires_at TTL.
2. API:
```
API=https://anora-maison.preview.emergentagent.com
curl -c c.txt -X POST $API/api/auth/login -H "Content-Type: application/json" -d '{"email":"admin@anora.in","password":"Anora@Admin2026"}'
curl -b c.txt $API/api/auth/me
```
Login returns user and sets httpOnly `access_token` (path /) + `refresh_token` (path /api/auth) cookies, SameSite=Lax, Secure.
3. Brute force: 5 failed logins for same ip+email → 429 for 15 min.
4. Reset: POST /api/auth/forgot-password then read `PASSWORD_RESET_LINK` from /var/log/supervisor/backend.err.log.
5. Unsafe requests with a foreign `Origin` header are rejected with 403 (CSRF guard).
