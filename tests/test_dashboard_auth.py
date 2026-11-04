from ocpp_reports.dashboard_auth import Permission, Principal, Role, authorized
import pytest


def principal(role):
    return Principal('alice', role, frozenset({'tenant-a'}))


@pytest.mark.parametrize('permission', list(Permission))
def test_admin_preserves_all_permissions_within_tenant(permission):
    assert authorized(principal(Role.ADMIN), permission, tenant_id='tenant-a', session_owner_id='alice')
    assert not authorized(principal(Role.ADMIN), permission, tenant_id='tenant-b', session_owner_id='alice')


def test_operator_cannot_read_internal_reports():
    operator = principal(Role.OPERATOR)
    for permission in (Permission.DASHBOARD_VIEW, Permission.TRANSACTIONS_VIEW, Permission.CONNECTORS_VIEW):
        assert authorized(operator, permission, tenant_id='tenant-a')
    for permission in (Permission.EVENTS_VIEW, Permission.SOURCES_VIEW, Permission.USERS_MANAGE):
        assert not authorized(operator, permission, tenant_id='tenant-a')


def test_user_only_owns_session():
    user = principal(Role.USER)
    assert authorized(user, Permission.OWN_SESSION_VIEW, tenant_id='tenant-a', session_owner_id='alice')
    assert not authorized(user, Permission.OWN_SESSION_VIEW, tenant_id='tenant-a', session_owner_id='bob')
    assert not authorized(user, Permission.DASHBOARD_VIEW, tenant_id='tenant-a')
    assert not authorized(None, Permission.DASHBOARD_VIEW, tenant_id='tenant-a')


def test_empty_tenant_scope_rejected():
    with pytest.raises(ValueError):
        Principal('alice', Role.ADMIN, frozenset())
