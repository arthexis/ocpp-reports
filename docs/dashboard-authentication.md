# Dashboard authentication — experimental, not production-ready

The login is **opt-in** and shares the dashboard URL for admins and operators.
The authenticated session determines permissions on both HTML and JSON routes;
hiding controls in the template is not a security boundary.

## Roles and scope

| Resource | Admin | Operator |
| --- | --- | --- |
| Dashboard and snapshot | Read | Read (internal events and sources redacted) |
| Transaction details | Read | Read |
| Event group API | Read | Denied |
| Account administration | Local CLI | Denied |
| Owned charging sessions (`user` role) | Future | Future |

Authorization requires both the requested permission and the configured tenant.
The instance's existing source allowlist is an **independent, mandatory** boundary.
Do not share one dashboard process across unrelated tenant source allowlists.
The `user` role is not yet exposed through dashboard routes.

## Controlled test setup

Use a dedicated writable SQLite database with private filesystem permissions:

```sh
ocpp-reports dashboard-accounts init --database /private/path/dashboard-auth.sqlite
ocpp-reports dashboard-accounts create --database /private/path/dashboard-auth.sqlite --username admin-a --tenant tenant-a --role admin
ocpp-reports dashboard-accounts create --database /private/path/dashboard-auth.sqlite --username operator-a --tenant tenant-a --role operator
ocpp-reports dashboard-accounts list --database /private/path/dashboard-auth.sqlite --tenant tenant-a
ocpp-reports dashboard-accounts update --database /private/path/dashboard-auth.sqlite --username operator-a --disable
ocpp-reports dashboard-accounts update --database /private/path/dashboard-auth.sqlite --username operator-a --enable --reset-password
```

Password prompts do not accept password command-line flags. Account updates revoke
existing sessions. Run account management only as the authorized local service
administrator, and restrict write access to the credential store.

Set `OCPP_REPORTS_AUTH_DATABASE` and `OCPP_REPORTS_AUTH_TENANT` for a
**test-only** dashboard process. Both are required. Login is at `/login`;
POST `/logout` invalidates the session. HTTPS and same-origin requests are
required for password submission and logout.

## Production migration gates

**Do not deploy or enable this mode in production yet.** Existing Nginx Basic
Auth is unchanged. Before replacing it:

1. Run the complete CI suite with web dependencies installed and exercise the
   real dashboard endpoints, not only a minimal test FastAPI app.
2. Validate reverse-proxy HTTPS/Host/Origin handling, session cookies, CSRF
   protections and rate limiting under concurrent requests.
3. Verify that source allowlists isolate Audi/Porsche and that each account
   has the intended tenant and role.
4. Provision at least one working administrator, verify login and recovery,
   and confirm that account deactivation and role changes revoke sessions.
5. Prepare an explicit, reversible Nginx Basic Auth migration with rollback.
   Do not remove Basic Auth as part of this experimental branch.
6. Finish the WebAuthn/passkey implementation separately before advertising
   passkey support.

Public QR pages and customer account self-service are out of scope.
