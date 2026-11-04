# Dashboard authentication (experimental; NOT production-ready)

The existing nginx Basic Auth is unchanged. The new login is opt-in and
**must not be enabled in production** until rate limiting, CSRF hardening,
credential provisioning, passkey enrollment, and integration tests are complete.

To enable only in a controlled test environment, configure both
`OCPP_REPORTS_AUTH_DATABASE` (writable SQLite path) and
`OCPP_REPORTS_AUTH_TENANT` (tenant scope). Existing dashboard source allowlists
remain mandatory for tenant isolation. Do not share one dashboard process across
unrelated tenant source allowlists.

The current implementation provides a password login at /login, session cookie,
POST /logout, and route authorization. It does NOT implement WebAuthn yet.
The account provisioning function is available for test fixtures only;
an administrative CLI and safe account migration are pending.

The role model is admin (full scoped reports), operator (operational reports
without events or sync-source diagnostics), and user (future owned sessions,
not yet exposed via dashboard routes). Public QR pages are out of scope.
