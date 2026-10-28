# Implementation architecture

Map the design handoff to a code architecture that can be changed safely.

## Repository boundaries

Use target boundaries where deployment or ownership differs:

```text
apps/        user-facing runtimes: miniprogram, admin, mobile
services/    backend APIs and agent runtime
packages/    generated contracts, SDKs, shared tokens
infra/       deployment, migrations, observability
docs/        implementation decisions and run evidence
```

Small projects may run services in one process while keeping module boundaries. Split when scaling, release cadence, trust boundaries, data ownership, or operational risk justify it.

## Contract flow

1. Design handoff defines screen IDs, state IDs, component IDs, assets, and logical API intentions.
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

## Architecture record

For each material decision, record context, decision, alternatives, consequences, and evidence. Include diagrams when they clarify trust boundaries, data ownership, or order of operations. Keep diagrams free of unsupported precision.
