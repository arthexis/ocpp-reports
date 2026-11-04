"""Authorization policy for Reports dashboards.

Roles are not a substitute for tenant scope. An empty scope never grants access.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    OPERATOR = "operator"
    USER = "user"


class Permission(StrEnum):
    DASHBOARD_VIEW = "dashboard.view"
    TRANSACTIONS_VIEW = "transactions.view"
    CONNECTORS_VIEW = "connectors.view"
    EVENTS_VIEW = "events.view"
    SOURCES_VIEW = "sources.view"
    USERS_MANAGE = "users.manage"
    OWN_SESSION_VIEW = "charging_session.own_view"


_ROLE_PERMISSIONS = {
    Role.ADMIN: frozenset(Permission),
    Role.OPERATOR: frozenset({Permission.DASHBOARD_VIEW, Permission.TRANSACTIONS_VIEW, Permission.CONNECTORS_VIEW}),
    Role.USER: frozenset({Permission.OWN_SESSION_VIEW}),
}


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: Role
    tenant_ids: frozenset[str]

    def __post_init__(self) -> None:
        if not self.user_id.strip():
            raise ValueError("user_id is required")
        if not self.tenant_ids or any(not item.strip() for item in self.tenant_ids):
            raise ValueError("at least one valid tenant scope is required")


def authorized(principal: Principal | None, permission: Permission, *, tenant_id: str,
               session_owner_id: str | None = None) -> bool:
    """Fail closed on missing principals, tenant scope and session ownership."""
    if principal is None or not tenant_id or tenant_id not in principal.tenant_ids:
        return False
    if permission not in _ROLE_PERMISSIONS[principal.role]:
        return False
    if permission is Permission.OWN_SESSION_VIEW and principal.role is not Role.ADMIN:
        return bool(session_owner_id) and session_owner_id == principal.user_id
    return True
