# Backend implementation

The service baseline is `../backend/SPEC.md`. Read `../backend/MODULES.md` to choose only the modules the product enables, then read each selected `../backend/NN-name/MODULE.md`.

## Core rules

- PostgreSQL is the source of truth. PostgreSQL plus a transaction is preferred to distributed coordination when it can satisfy the requirement.
- Store money as integer minor units, with currency and rounding rules explicit.
- Guard inventory, balances, quotas, coupon state, and order transitions with conditional atomic writes. Never read, calculate, and blindly overwrite a concurrent value.
- Support idempotency for retried consequential writes. Scope keys to tenant, actor, operation, and request identity.
- Publish durable side effects through an outbox or a transactionally consistent mechanism.
- Retry only failures that are safe to retry. Every external dependency has a timeout, bounded retry with jitter, and a circuit-breaker or degradation policy.
- Keep authentication, authorization, audit, and data scope separate concerns.
- Validate request and response schemas at the boundary and keep error codes stable.

## Module changes

Define the module boundary first: owned data, commands, queries, events, permissions, failure modes, and tests. Cross-module behavior goes through an explicit contract. Do not make one module read another module's private tables.

## API changes

Update the OpenAPI schema and compatibility notes before implementation when multiple clients are affected. Additive fields are preferred; breaking changes require versioning or a migration plan. Ensure generated types and screen handoff intents remain linked.

## Database changes

Migrations are forward-only in production unless a tested rollback plan exists. Avoid long locks, destructive backfills in a single transaction, and incompatible expand/contract gaps. Test migrations from an empty database and from the production-like previous version.

## Tests and operations

Cover concurrency, duplicate requests, invalid state transitions, authorization, timeout, rollback, retry, and recovery. Verify readiness, metrics, logs, traces, rate limits, backup, restore, and rollback before release.
