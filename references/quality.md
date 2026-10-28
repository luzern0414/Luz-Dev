# Quality and verification

Verification is based on observable behavior, not completion claims.

## Test pyramid

- Unit tests for domain rules, reducers, validation, and formatting.
- Contract tests for API schemas, events, permissions, and generated clients.
- Integration tests for transactions, adapters, queues, migrations, and authentication.
- End-to-end tests for core user outcomes on target platforms.
- Visual checks against the approved design states.
- Operational checks for health, metrics, logs, traces, backup, and rollback.

## Required failure cases

- Invalid and missing input
- Expired or missing authentication
- Unauthorized and out-of-scope access
- Duplicate and concurrent requests
- Stale state and invalid transition
- External timeout, failure, rate limit, and recovery
- Offline and reconnection
- Partial success in bulk operations
- Rollback and migration restart

## Completion evidence

A task is ready to report complete when:

- its code and tests exist;
- the relevant tests ran after the final change and passed;
- the UI has been checked at target sizes and states;
- the migration or schema change was exercised;
- logs and screenshots contain no secrets or sensitive personal data;
- implementation deviations from the handoff are recorded and approved;
- remaining limitations and changed contracts are explicit.

Do not run redundant suites merely to create more output. Repeat checks when a change could have invalidated earlier evidence.
