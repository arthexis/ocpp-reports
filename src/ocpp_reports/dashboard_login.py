"""Opt-in password login and tenant-scoped HTTP authorization."""
import hashlib
import html
import hmac
import secrets
import sqlite3
import time
from urllib.parse import parse_qs
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from .dashboard_auth import Permission, Principal, Role, authorized

PASSWORD_HTML = """<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Cambiar contraseña · OCPP Reports</title><style>body{font:16px system-ui;background:#152638;color:white;min-height:100vh;display:grid;place-items:center}main{background:#253a50;padding:2rem;border-radius:12px;width:min(380px,90vw)}label,input,button{display:block;width:100%;box-sizing:border-box;margin:12px 0;padding:9px}button{background:#80baff;border:0;border-radius:6px}a{color:#bcdcff}</style><main><h1>Cambiar contraseña</h1><p><!--user--></p><!--error--><form method="post" action="/change-password"><label>Contraseña actual<input type="password" name="current_password" autocomplete="current-password" required></label><label>Nueva contraseña<input type="password" name="new_password" autocomplete="new-password" minlength="12" required></label><label>Confirmar nueva contraseña<input type="password" name="confirm_password" autocomplete="new-password" minlength="12" required></label><button type="submit">Guardar contraseña</button></form><a href="/">Volver al dashboard</a></main>"""
HTML = """<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OCPP Reports · Login</title><style>body{font:16px system-ui;background:#152638;color:white;min-height:100vh;display:grid;place-items:center}main{background:#253a50;padding:2rem;border-radius:12px;width:min(360px,85vw)}label,input,button{display:block;width:100%;box-sizing:border-box;margin:12px 0;padding:9px}button{background:#80baff;border:0;border-radius:6px}</style><main><h1>OCPP Reports</h1><p>Acceso a estaciones de carga</p><!--error--><form method="post"><label>Usuario<input name="username" autocomplete="username" required></label><label>Contraseña<input type="password" name="password" autocomplete="current-password" required></label><button>Iniciar sesión</button></form></main>"""
COOKIE = "reports_session"

def initialize(path):
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS dashboard_users(user_id TEXT PRIMARY KEY,password_hash TEXT NOT NULL,role TEXT NOT NULL,tenant_id TEXT NOT NULL,active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS dashboard_sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS dashboard_login_attempts(identity TEXT PRIMARY KEY, failures INTEGER NOT NULL, window_start INTEGER NOT NULL, locked_until INTEGER NOT NULL DEFAULT 0);""")

def provision(path, username, password, role, tenant):
    if len(password) < 12 or not username or not tenant: raise ValueError("Invalid account")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1)
    initialize(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO dashboard_users VALUES(?,?,?,?,1)",(username,salt.hex()+":"+digest.hex(),Role(role).value,tenant))

def manage_user(path, username, *, role=None, active=None, password=None):
    """Manage a provisioned account and revoke existing sessions on any change."""
    if role is None and active is None and password is None:
        raise ValueError("No changes requested")
    initialize(path)
    changes = []
    params = []
    if role is not None:
        changes.append("role=?")
        params.append(Role(role).value)
    if active is not None:
        changes.append("active=?")
        params.append(int(bool(active)))
    if password is not None:
        if len(password) < 12:
            raise ValueError("Password too short")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
        changes.append("password_hash=?")
        params.append(salt.hex() + ":" + digest.hex())
    with sqlite3.connect(path) as db:
        result = db.execute("UPDATE dashboard_users SET " + ", ".join(changes) + " WHERE user_id=?", (*params, username))
        if result.rowcount != 1:
            raise ValueError("Unknown user")
        db.execute("DELETE FROM dashboard_sessions WHERE user_id=?", (username,))


def prune_expired_sessions(path, *, now=None):
    initialize(path)
    with sqlite3.connect(path) as db:
        result = db.execute("DELETE FROM dashboard_sessions WHERE expires<=?", (int(time.time()) if now is None else now,))
        return result.rowcount


# Every registered dashboard route must be classified before authentication is enabled.
_ROUTE_PERMISSIONS = {
    ("/", "GET"): Permission.DASHBOARD_VIEW,
    ("/api/snapshot", "GET"): Permission.DASHBOARD_VIEW,
    ("/api/event-group", "GET"): Permission.EVENTS_VIEW,
    ("/api/v1/event-group", "GET"): Permission.EVENTS_VIEW,
    **{(path, "GET"): Permission.DASHBOARD_VIEW for path in (
        "/api/v1/summary", "/api/v1/connectors", "/api/v1/transactions",
        "/api/v1/stations", "/api/v1/filters", "/api/v1/openapi.json",
        "/api/v1/sources")},
}


def route_permission(path, method):
    if (path, method) in _ROUTE_PERMISSIONS:
        return _ROUTE_PERMISSIONS[(path, method)]
    if method == "GET" and path.startswith("/api/v1/transactions/") and len(path.split("/")) == 7 and all(path.split("/")[4:]):
        return Permission.TRANSACTIONS_VIEW
    if method == "GET" and path.startswith("/transactions/") and len(path.split("/")) == 5 and all(path.split("/")[1:]):
        return Permission.TRANSACTIONS_VIEW
    return None


def attach_login(app, *, path, tenant):
    initialize(path)
    @app.middleware("http")
    async def protect(request, next_handler):
        route = request.url.path
        if (route == "/api/v1/session/rotate" and request.method == "POST") or (route == "/login" and request.method in ("GET", "POST")) or (route == "/logout" and request.method == "POST") or (route == "/change-password" and request.method in ("GET", "POST")):
            return await next_handler(request)
        token = request.cookies.get(COOKIE, "")
        # Native clients can reuse the existing short-lived, revocable session
        # token without creating a second credential store. Bearer is API-only.
        authorization = request.headers.get("authorization", "")
        if route.startswith("/api/v1/") and authorization.startswith("Bearer "):
            token = authorization[7:].strip()
        with sqlite3.connect(path) as db:
            row = db.execute("""SELECT u.user_id,u.role,u.tenant_id FROM dashboard_sessions s JOIN dashboard_users u ON s.user_id=u.user_id WHERE s.token_hash=? AND s.expires>? AND u.active=1 AND u.tenant_id=?""",(hashlib.sha256(token.encode()).hexdigest(),int(time.time()),tenant)).fetchone() if token else None
        principal = None
        if row:
            try:
                principal = Principal(row[0], Role(row[1]), frozenset({row[2]}))
            except ValueError:
                pass
        request.state.principal = principal
        perm = route_permission(route, request.method)
        if perm is None:
            return JSONResponse({"detail":"Forbidden"},status_code=403)
        if row is None and request.method == "GET" and route == "/":
            # Missing, expired and revoked sessions return to login. Invalid roles remain forbidden; APIs keep 403.
            return RedirectResponse("/login", status_code=303, headers={"Cache-Control": "no-store"})
        if not authorized(principal,perm,tenant_id=tenant):
            return JSONResponse({"detail":"Forbidden"},status_code=403)
        return await next_handler(request)

    @app.post("/api/v1/session/rotate")
    async def rotate_session(request: Request):
        """Atomically exchange a valid session for a new short-lived bearer."""
        if request.url.scheme != "https":
            return JSONResponse({"detail": "HTTPS required"}, status_code=403)
        header = request.headers.get("authorization", "")
        bearer = header[7:].strip() if header.startswith("Bearer ") else ""
        cookie = request.cookies.get(COOKIE, "")
        token = bearer or cookie
        if not token:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if not bearer and request.headers.get("origin") != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Invalid origin"}, status_code=403)
        now = int(time.time())
        replacement = secrets.token_urlsafe(32)
        old_hash = hashlib.sha256(token.encode()).hexdigest()
        with sqlite3.connect(path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT s.expires,u.role,u.user_id FROM dashboard_sessions s "
                "JOIN dashboard_users u ON u.user_id=s.user_id "
                "WHERE s.token_hash=? AND s.expires>? AND u.active=1 AND u.tenant_id=?",
                (old_hash, now, tenant)).fetchone()
            if row is None:
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            try:
                permitted = authorized(Principal(row[2], Role(row[1]), frozenset({tenant})),
                                       Permission.DASHBOARD_VIEW, tenant_id=tenant)
            except ValueError:
                permitted = False
            if not permitted:
                return JSONResponse({"detail": "Forbidden"}, status_code=403)
            expiry = min(row[0], now + 43200)
            db.execute("UPDATE dashboard_sessions SET token_hash=?,expires=? WHERE token_hash=?",
                       (hashlib.sha256(replacement.encode()).hexdigest(), expiry, old_hash))
        response = JSONResponse({"access_token": replacement, "token_type": "bearer",
                                 "expires_in": expiry - now})
        response.headers["Cache-Control"] = "no-store"
        if cookie and not bearer:
            response.delete_cookie(COOKIE)
        return response

    @app.get("/login")
    async def login_page(): return HTMLResponse(HTML,headers={"Cache-Control":"no-store"})
    @app.post("/login")
    async def login(request: Request):
        if request.url.scheme != "https" or request.headers.get("origin") != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail":"Invalid origin"},status_code=403)
        if request.headers.get("content-type","").split(";")[0]!="application/x-www-form-urlencoded":
            return JSONResponse({"detail":"Unsupported content type"},status_code=415)
        fields=parse_qs((await request.body()).decode(),max_num_fields=5)
        username=fields.get("username",[""])[0]
        password=fields.get("password",[""])[0]
        with sqlite3.connect(path) as db:
            now = int(time.time())
            identity = hashlib.sha256((tenant + ":" + username.casefold()).encode()).hexdigest()
            attempt = db.execute("SELECT failures,window_start,locked_until FROM dashboard_login_attempts WHERE identity=?", (identity,)).fetchone()
            if attempt and attempt[2] > now:
                return HTMLResponse(HTML.replace("<!--error-->", "<p role='alert'>Intenta más tarde</p>"), status_code=429, headers={"Retry-After": str(attempt[2]-now), "Cache-Control": "no-store"})
            row=db.execute("SELECT password_hash FROM dashboard_users WHERE user_id=? AND tenant_id=? AND active=1",(username,tenant)).fetchone()
            valid=False
            if row:
                try:
                    salt,expected=row[0].split(":")
                    digest=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1)
                    valid=hmac.compare_digest(digest,bytes.fromhex(expected))
                except ValueError: pass
            if not valid:
                if attempt and now - attempt[1] < 900:
                    failures, window_start = attempt[0] + 1, attempt[1]
                else:
                    failures, window_start = 1, now
                locked_until = now + 900 if failures >= 5 else 0
                db.execute(
                    """INSERT INTO dashboard_login_attempts(identity,failures,window_start,locked_until)
                    VALUES(?,?,?,?) ON CONFLICT(identity) DO UPDATE SET
                    failures=excluded.failures,window_start=excluded.window_start,
                    locked_until=excluded.locked_until""",
                    (identity, failures, window_start, locked_until),
                )
                return HTMLResponse(HTML.replace("<!--error-->","<p role='alert'>Credenciales incorrectas</p>"),status_code=401)
            db.execute("DELETE FROM dashboard_login_attempts WHERE identity=?", (identity,))
            db.execute("DELETE FROM dashboard_sessions WHERE expires<=?", (now,))
            token=secrets.token_urlsafe(32)
            db.execute("INSERT INTO dashboard_sessions VALUES(?,?,?)",(hashlib.sha256(token.encode()).hexdigest(),username,int(time.time())+43200))
        response=RedirectResponse("/",status_code=303)
        response.set_cookie(COOKIE,token,secure=True,httponly=True,samesite="lax",max_age=43200)
        response.headers["Cache-Control"]="no-store"
        return response
    def current_account(request):
        token = request.cookies.get(COOKIE, "")
        if not token:
            return None
        with sqlite3.connect(path) as db:
            return db.execute("""SELECT u.user_id,u.password_hash FROM dashboard_sessions s
                JOIN dashboard_users u ON u.user_id=s.user_id
                WHERE s.token_hash=? AND s.expires>? AND u.active=1 AND u.tenant_id=?""",
                (hashlib.sha256(token.encode()).hexdigest(), int(time.time()), tenant)).fetchone()

    def password_matches(password, stored):
        try:
            salt, expected = stored.split(":")
            digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
            return hmac.compare_digest(digest, bytes.fromhex(expected))
        except (ValueError, TypeError):
            return False

    @app.get("/change-password")
    async def password_page(request: Request):
        account = current_account(request)
        if account is None:
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(PASSWORD_HTML.replace("<!--user-->", html.escape(account[0])), headers={"Cache-Control": "no-store"})

    @app.post("/change-password")
    async def change_password(request: Request):
        if request.url.scheme != "https" or request.headers.get("origin") != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Invalid origin"}, status_code=403)
        if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
            return JSONResponse({"detail": "Unsupported content type"}, status_code=415)
        account = current_account(request)
        if account is None:
            return RedirectResponse("/login", status_code=303)
        fields = parse_qs((await request.body()).decode(), max_num_fields=5)
        current = fields.get("current_password", [""])[0]
        new = fields.get("new_password", [""])[0]
        confirm = fields.get("confirm_password", [""])[0]
        error = None
        if not password_matches(current, account[1]):
            error = "Contraseña actual incorrecta"
        elif len(new) < 12 or len(new) > 1024:
            error = "La nueva contraseña debe tener entre 12 y 1024 caracteres"
        elif new != confirm:
            error = "Las nuevas contraseñas no coinciden"
        elif hmac.compare_digest(current, new):
            error = "Elige una contraseña diferente"
        if error:
            page = PASSWORD_HTML.replace("<!--user-->", html.escape(account[0]))
            return HTMLResponse(page.replace("<!--error-->", "<p role='alert'>" + error + "</p>"),
                                status_code=400, headers={"Cache-Control": "no-store"})
        # Revoke every session, including the current session, after password rotation.
        manage_user(path, account[0], password=new)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/logout")
    async def logout(request: Request):
        if request.url.scheme != "https" or request.headers.get("origin") != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail":"Invalid origin"},status_code=403)
        token=request.cookies.get(COOKIE)
        if token:
            with sqlite3.connect(path) as db:
                db.execute("DELETE FROM dashboard_sessions WHERE token_hash=?",(hashlib.sha256(token.encode()).hexdigest(),))
        response=RedirectResponse("/login",status_code=303)
        response.delete_cookie(COOKIE)
        return response
