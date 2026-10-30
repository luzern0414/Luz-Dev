# Implementation architecture

Map the approved Luz System design to a code architecture that can be changed safely. The service baseline is `backend/SPEC.md`; choose modules in `backend/MODULES.md` and read each selected `backend/NN-name/MODULE.md`.

## Repository boundaries

Use the project structure in `SKILL.md` (apps, services, packages, infra, config, legal, docs); split along it where deployment or ownership differs. Small projects may run services in one process while keeping module boundaries. Split when scaling, release cadence, trust boundaries, data ownership, or operational risk justify it.

## Contract flow

1. The approved design defines page, action, state, component and entity IDs (`design-bindings.json`).
2. Backend/frontend owners map those intents to endpoint IDs and schemas.
3. The platform contract records authentication, permissions, feature flags, limits, and error codes.
4. Generated or shared types are used by consumers.
5. Tests assert the contract and user-visible failure behavior.

Do not derive business rules from screen labels. Do not expose database records directly as API contracts.

## Layering

- UI: navigation, presentation, local interaction, accessibility, and state rendering.
- Application: user actions, orchestration, validation, and transaction coordination.
- Domain: business invariants and state transitions.
- Adapters: database, cache, queue, payment, map, notification, model provider.
- Platform: configuration, observability, security, jobs, and deployment.

A layer may depend inward. Inner layers do not depend on framework or transport details.

## Backend rules

- PostgreSQL is the source of truth; a PostgreSQL transaction is preferred to distributed coordination when it can satisfy the requirement.
- Idempotency keys are scoped to tenant, actor, operation and request identity.
- Retry only failures that are safe to retry. Every external dependency has a timeout, bounded retry with jitter, and a circuit-breaker or degradation policy.
- Keep authentication, authorization, audit and data scope separate concerns; validate request and response schemas at the boundary and keep error codes stable (`luzback.errors.ErrorCode`).

## Module, API and database changes

- **Module:** define the boundary first — owned data, commands, queries, events, permissions, failure modes and tests. Cross-module behaviour goes through an explicit contract; no module reads another module's private tables.
- **API:** update the OpenAPI schema and compatibility notes before implementation when several clients are affected. Additive fields are preferred; breaking changes need versioning or a migration plan. Generated types and design bindings stay linked.
- **Database:** migrations are forward-only in production unless a tested rollback exists. Avoid long locks, destructive backfills in one transaction and expand/contract gaps. Test migrations from an empty database and from the production-like previous version.
- **Tests and operations:** cover concurrency, duplicate requests, invalid transitions, authorization, timeout, rollback, retry and recovery; verify readiness, metrics, logs, traces, rate limits, backup, restore and rollback before release (`quality.md`, `operations.md`).

## Architecture record

For each material decision, record context, decision, alternatives, consequences, and evidence. Include diagrams when they clarify trust boundaries, data ownership, or order of operations. Keep diagrams free of unsupported precision.
