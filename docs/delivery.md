# B4: operator-only SMTP delivery

The first release sends reports **only to the configured operator address**. The operator reviews and forwards them to customers manually. No customer recipient management, reconciliation workflow, or automatic retries.

A small SQLite outbox retains immutable payloads and deduplicates by operator recipient, subject and report revision. Status is `pending`, `sending`, `sent` or `failed`. `sending` is an in-progress state, retained if the process crashes. `sent` means SMTP accepted the message, **not** that it arrived in the inbox.

Failed sends are not automatically retried. If the scheduled email does not arrive, the operator can check status and deliberately retry a failed or interrupted send. A retry can duplicate an email if the server previously accepted it; inspect the inbox first. Deterministic Message-ID assists manual identification, but is not an exactly-once guarantee.

SMTP requires TLS (STARTTLS or implicit TLS); credentials are never stored in the outbox.

**Remaining work:** minimal CLI for queue/status/manual retry, one configured operator recipient, attachments, SMTP config validation and fake-server tests. Scheduling belongs to B5. Provisional reports must be labeled and must not be represented as final.
