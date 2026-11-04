"""Opt-in password login and tenant-scoped HTTP authorization."""
import hashlib
import hmac
import secrets
import sqlite3
import time
from urllib.parse import parse_qs
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from .dashboard_auth import Permission, Principal, Role, authorized

HTML = """<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OCPP Reports · Login</title><style>body{font:16px system-ui;background:#152638;color:white;min-height:100vh;display:grid;place-items:center}main{background:#253a50;padding:2rem;border-radius:12px;width:min(360px,85vw)}label,input,button{display:block;width:100%;box-sizing:border-box;margin:12px 0;padding:9px}button{background:#80baff;border:0;border-radius:6px}</style><main><h1>OCPP Reports</h1><p>Acceso a estaciones de carga</p><!--error--><form method="post"><label>Usuario<input name="username" autocomplete="username" required></label><label>Contraseña<input type="password" name="password" autocomplete="current-password" required></label><button>Iniciar sesión</button></form></main>"""
COOKIE = "reports_session"

def initialize(path):
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS dashboard_users(user_id TEXT PRIMARY KEY,password_hash TEXT NOT NULL,role TEXT NOT NULL,tenant_id TEXT NOT NULL,active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS dashboard_sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires INTEGER NOT NULL);""")

def provision(path, username, password, role, tenant):
    if len(password) < 12 or not username or not tenant: raise ValueError("Invalid account")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1)
    initialize(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO dashboard_users VALUES(?,?,?,?,1)",(username,salt.hex()+":"+digest.hex(),Role(role).value,tenant))

def attach_login(app, *, path, tenant):
    initialize(path)
    @app.middleware("http")
    async def protect(request, next_handler):
        route = request.url.path
        if route in ("/login","/logout"): return await next_handler(request)
        token = request.cookies.get(COOKIE,"")
        with sqlite3.connect(path) as db:
            row = db.execute("""SELECT u.user_id,u.role,u.tenant_id FROM dashboard_sessions s JOIN dashboard_users u ON s.user_id=u.user_id WHERE s.token_hash=? AND s.expires>? AND u.active=1""",(hashlib.sha256(token.encode()).hexdigest(),int(time.time()))).fetchone() if token else None
        principal = Principal(row[0],Role(row[1]),frozenset({row[2]})) if row else None
        request.state.principal = principal
        perm = Permission.EVENTS_VIEW if route=="/api/event-group" else Permission.TRANSACTIONS_VIEW if route.startswith("/transactions/") else Permission.DASHBOARD_VIEW if route in ("/","/api/snapshot") else None
        if perm is None or not authorized(principal,perm,tenant_id=tenant):
            return JSONResponse({"detail":"Forbidden"},status_code=403)
        return await next_handler(request)
    @app.get("/login")
    async def login_page(): return HTMLResponse(HTML,headers={"Cache-Control":"no-store"})
    @app.post("/login")
    async def login(request: Request):
        if request.headers.get("origin") != "https://"+request.headers.get("host",""):
            return JSONResponse({"detail":"Invalid origin"},status_code=403)
        if request.headers.get("content-type","").split(";")[0]!="application/x-www-form-urlencoded":
            return JSONResponse({"detail":"Unsupported content type"},status_code=415)
        fields=parse_qs((await request.body()).decode(),max_num_fields=5)
        username=fields.get("username",[""])[0]
        password=fields.get("password",[""])[0]
        with sqlite3.connect(path) as db:
            row=db.execute("SELECT password_hash FROM dashboard_users WHERE user_id=? AND active=1",(username,)).fetchone()
            valid=False
            if row:
                try:
                    salt,expected=row[0].split(":")
                    digest=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1)
                    valid=hmac.compare_digest(digest,bytes.fromhex(expected))
                except ValueError: pass
            if not valid:
                return HTMLResponse(HTML.replace("<!--error-->","<p role='alert'>Credenciales incorrectas</p>"),status_code=401)
            token=secrets.token_urlsafe(32)
            db.execute("INSERT INTO dashboard_sessions VALUES(?,?,?)",(hashlib.sha256(token.encode()).hexdigest(),username,int(time.time())+43200))
        response=RedirectResponse("/",status_code=303)
        response.set_cookie(COOKIE,token,secure=True,httponly=True,samesite="lax",max_age=43200)
        response.headers["Cache-Control"]="no-store"
        return response
    @app.post("/logout")
    async def logout(request: Request):
        if request.headers.get("origin")!="https://"+request.headers.get("host",""):
            return JSONResponse({"detail":"Invalid origin"},status_code=403)
        token=request.cookies.get(COOKIE)
        if token:
            with sqlite3.connect(path) as db:
                db.execute("DELETE FROM dashboard_sessions WHERE token_hash=?",(hashlib.sha256(token.encode()).hexdigest(),))
        response=RedirectResponse("/login",status_code=303)
        response.delete_cookie(COOKIE)
        return response
