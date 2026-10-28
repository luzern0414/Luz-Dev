# Delivery and operations

## Release inputs

- Versioned source and migrations
- Environment and secret inventory
- Feature flags and safe defaults
- Build artifacts with provenance
- API and event compatibility notes
- Backup and restore procedure
- Rollback trigger, owner, and steps
- Monitoring and alert destinations

## Deployment order

1. Validate configuration and secrets.
2. Apply backward-compatible schema changes.
3. Deploy tolerant producers and consumers.
4. Shift traffic gradually where the platform supports it.
5. Verify health, core traces, business metrics, and user-visible state.
6. Complete cleanup in a later release.

## Observability

Track availability, latency, error rate, saturation, queue/outbox lag, migration state, authentication failures, authorization denials, payment/order transitions, and agent confirmation and cost metrics where relevant. Use trace IDs across client, API, services, and tools. Redact secrets and personal data.

## Rollback

Rollback application code only when the data contract remains compatible. Use forward fixes for irreversible migrations. Verify the restore path before it is needed. A release is not operationally complete while the rollback path is unknown.
