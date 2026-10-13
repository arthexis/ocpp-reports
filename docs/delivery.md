# B4: durable SMTP outbox

The outbox uses a local SQLite database and immutable email payloads. Enqueue is idempotent by recipient, subject and report revision. A second enqueue with the same key and different content is rejected.

The send lifecycle is `pending → sending → sent`. The claim is committed **before** SMTP starts. If SMTP raises an exception, the delivery is marked `uncertain`; if the process crashes, it remains `sending`. Neither state is retried automatically, because SMTP may have accepted the message before the client lost its connection. An operator must reconcile the state and deliberately retry (which may result in duplicate email). A deterministic Message-ID helps trace attempts but is **not** an SMTP exactly-once guarantee.

SMTP supports STARTTLS and implicit TLS, requires explicit host/port/sender, and never stores passwords in SQLite. The caller should source credentials from environment or a secure secret store. No scheduler or background worker is started.

**Next B4 tasks:** CLI enqueue/list/reconcile commands, safe attachment handling, SMTP config validation, explicit retry policy for known pre-send failures, and integration tests with a fake SMTP server. Do not send provisional reports automatically.
