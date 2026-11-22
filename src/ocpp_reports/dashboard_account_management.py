"""Authenticated tenant-scoped account mutations for the dashboard.

The existing CLI remains a separate privileged path. Web mutations are atomic,
invalidate affected sessions, and cannot remove the last active administrator.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from pathlib import Path

from .dashboard_auth import Role

USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9._-]{2,63}\Z")


class AccountError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _username(value: object) -> str:
    if not isinstance(value, str) or not USERNAME.fullmatch(value):
        raise AccountError(422, "El usuario debe tener entre 3 y 64 caracteres: letras, números, punto, guion o guion bajo.")
    return value


def _role(value: str) -> str:
    if value not in (Role.ADMIN.value, Role.OPERATOR.value):
        raise AccountError(422, "Selecciona un rol válido.")
    return value


def _password(value: str) -> str:
    if not isinstance(value, str) or not 12 <= len(value) <= 1024:
        raise AccountError(422, "La contraseña debe tener entre 12 y 1024 caracteres.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(value.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ":" + digest.hex()



def csrf_token(session_cookie: str, tenant: str) -> str:
    """Bind POST forms to the unpredictable HttpOnly login session."""
    if not session_cookie or not tenant:
        return ""
    return hmac.new(session_cookie.encode(),
                    ("ocpp-reports:account-management:v1:" + tenant).encode(),
                    hashlib.sha256).hexdigest()


def list_tenant_accounts(path: str | Path, tenant: str) -> list[dict]:
    """Never enumerate other tenants, including on the central dashboard."""
    if not tenant:
        return []
    with _connect(path) as db:
        rows = db.execute(
            "SELECT user_id,role,active,last_login_at FROM dashboard_users "
            "WHERE tenant_id=? ORDER BY user_id LIMIT 500", (tenant,),
        ).fetchall()
    return [{"user_id": user_id, "role": role, "active": bool(active),
             "last_login_at": last_login} for user_id, role, active, last_login in rows]


def _audit(db: sqlite3.Connection, *, actor: str, tenant: str,
           action: str, username: str) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS dashboard_account_audit(
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        actor_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
        action TEXT NOT NULL, target_id TEXT NOT NULL
    )""")
    db.execute(
        "INSERT INTO dashboard_account_audit(actor_id,tenant_id,action,target_id) "
        "VALUES(?,?,?,?)", (actor, tenant, action, username),
    )


def _connect(path: str | Path) -> sqlite3.Connection:
    database = Path(path)
    if not database.is_file():
        raise AccountError(503, "Base de autenticación no disponible.")
    return sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=30)


def _require_actor(db: sqlite3.Connection, actor: str, tenant: str) -> None:
    row = db.execute(
        "SELECT role,active FROM dashboard_users WHERE user_id=? AND tenant_id=?",
        (actor, tenant),
    ).fetchone()
    if not row or row != (Role.ADMIN.value, 1):
        raise AccountError(403, "No hay permisos administrativos vigentes.")


def _existing(db: sqlite3.Connection, username: str, tenant: str) -> tuple[str, int]:
    row = db.execute(
        "SELECT role,active FROM dashboard_users WHERE user_id=? AND tenant_id=?",
        (username, tenant),
    ).fetchone()
    if row is None:
        raise AccountError(404, "La cuenta no existe en este dashboard.")
    return row[0], row[1]


def _preserve_admin(db: sqlite3.Connection, tenant: str, previous: tuple[str, int],
                    new_role: str | None = None, new_active: int | None = None) -> None:
    role, active = previous
    if role == Role.ADMIN.value and active:
        if new_role != Role.ADMIN.value or new_active != 1:
            total = db.execute(
                "SELECT COUNT(*) FROM dashboard_users WHERE tenant_id=? AND role='admin' AND active=1",
                (tenant,),
            ).fetchone()[0]
            if total <= 1:
                raise AccountError(409, "Debe permanecer al menos un administrador activo.")


def _revoke(db: sqlite3.Connection, username: str, tenant: str) -> None:
    db.execute("DELETE FROM dashboard_sessions WHERE user_id=?", (username,))
    identity = hashlib.sha256((tenant + ":" + username.casefold()).encode()).hexdigest()
    db.execute("DELETE FROM dashboard_login_attempts WHERE identity=?", (identity,))


def create_account(path: str | Path, *, actor: str, tenant: str, username: str,
                   role: str, password: str) -> None:
    username = _username(username)
    role = _role(role)
    digest = _password(password)
    if not tenant:
        raise AccountError(403, "Tenant no autorizado.")
    try:
        with _connect(path) as db:
            db.execute("BEGIN IMMEDIATE")
            _require_actor(db, actor, tenant)
            # Keep the global user_id namespace unambiguous across dashboard tenants.
            if db.execute("SELECT 1 FROM dashboard_users WHERE user_id=? COLLATE NOCASE",
                          (username,)).fetchone():
                raise AccountError(409, "No fue posible crear la cuenta con ese nombre.")
            db.execute(
                "INSERT INTO dashboard_users(user_id,password_hash,role,tenant_id,active) "
                "VALUES(?,?,?,?,1)", (username, digest, role, tenant),
            )
            _audit(db, actor=actor, tenant=tenant, action="create", username=username)
    except sqlite3.IntegrityError as exc:
        raise AccountError(409, "No fue posible crear la cuenta con ese nombre.") from exc


def update_account(path: str | Path, *, actor: str, tenant: str, username: str,
                   role: str, active: bool, password: str | None) -> None:
    username = _username(username)
    role = _role(role)
    if actor == username:
        raise AccountError(403, "Modifica tu propia contraseña desde el menú de perfil.")
    digest = _password(password) if password is not None else None
    if not tenant:
        raise AccountError(403, "Tenant no autorizado.")
    with _connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        _require_actor(db, actor, tenant)
        old = _existing(db, username, tenant)
        _preserve_admin(db, tenant, old, role, int(active))
        if digest is not None:
            db.execute(
                "UPDATE dashboard_users SET role=?,active=?,password_hash=? "
                "WHERE user_id=? AND tenant_id=?",
                (role, int(active), digest, username, tenant),
            )
        else:
            db.execute(
                "UPDATE dashboard_users SET role=?,active=? WHERE user_id=? AND tenant_id=?",
                (role, int(active), username, tenant),
            )
        _revoke(db, username, tenant)
        _audit(db, actor=actor, tenant=tenant, action="update", username=username)


def delete_account(path: str | Path, *, actor: str, tenant: str,
                   username: str) -> None:
    username = _username(username)
    if actor == username:
        raise AccountError(403, "No puedes eliminar tu propia cuenta.")
    if not tenant:
        raise AccountError(403, "Tenant no autorizado.")
    with _connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        _require_actor(db, actor, tenant)
        old = _existing(db, username, tenant)
        _preserve_admin(db, tenant, old, None, None)
        _revoke(db, username, tenant)
        db.execute("DELETE FROM dashboard_users WHERE user_id=? AND tenant_id=?",
                   (username, tenant))
        _audit(db, actor=actor, tenant=tenant, action="delete", username=username)
