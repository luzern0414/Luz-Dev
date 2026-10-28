# Design handoff consumer contract

Luz System owns `HANDOFF.json`; Luz Dev consumes it without redefining approved product or visual behavior. Schema version `1.0` is mirrored byte-for-byte at `contracts/handoff.schema.json` and must match `D:\Codes\Luz-System\references\handoff.schema.json`. The paired Luz Design package below supplies only theme tokens and the document checker.

## Resolve the paired Design package

Use the first available source:

1. CLI override: `--design-root PATH`
2. Environment override: `LUZ_DESIGN_ROOT`
3. Default: `../Luz-Design` relative to the Luz Dev package

```powershell
python -X utf8 scripts/validate_skill_package.py --design-root "D:\Codes\Luz-Design"
$env:LUZ_DESIGN_ROOT = "D:\Codes\Luz-Design"
python -X utf8 scripts/validate_skill_package.py
```

## Validate a handoff

```powershell
python -X utf8 scripts/validate_handoff.py path/to/HANDOFF.json
```

The manifest uses the authoritative JSON Schema. Its top-level required fields are:

- `schemaVersion`: exactly `1.0`
- `project`: product id, name, summary, audience, outcome, goals, non-goals, assumptions, and sources
- `surfaces`: one or more of mini-program, admin, or mobile-app
- `roles`: the actors whose permissions or behavior differ
- `designSystem`: positioning, token sources, component library, and accessibility evidence
- `flows`: actor, entry screen, outcome, ordered steps, and recovery paths
- `screens`: surface, route, roles, flows, purpose, implementation states, components, assets, API intents, and source
- `components`: variants, states, content limits, and acceptance checks
- `assets`: production assets with source, license, text-in-image status, and alt text
- `apiIntents`: screen-level data, actions, and displayed error states
- `acceptance`: reviewable criteria and evidence
- `openDecisions`: only unresolved decisions allowed by the schema

Do not add undocumented top-level fields. Stable IDs are looked up by their schema constraints; preserve them from Design through routes, components, contracts, tests, and release notes.

Every completed flow should identify its result surface. Payment, application, multi-step form, asynchronous job, and partial-success screens must expose server-backed outcomes and recovery paths through the screen states and API intentions rather than relying on a client redirect.

Asset paths are resolved relative to `HANDOFF.json`. Missing files and paths escaping that directory are rejected. Screen `source` values and other evidence strings remain producer-owned references and are not treated as local runtime paths.

## Intake

1. Resolve and validate the paired Design package and handoff.
2. Read only the project, roles, screens, flows, components, assets, API intents, acceptance criteria, and open decisions touched by the task.
3. Resolve every blocking open decision before implementing affected behavior.
4. Cross-check the `result-flow` catalog module and `references/result-patterns.md` for payment, form, async, partial-success, and failure closures.
5. Preserve stable IDs and observable design semantics.
6. Map design IDs to implementation artifacts without changing the contract silently.

## Implementation record

For each delivered screen, return:

| Design screen ID | Route/component | Source files | State coverage | Tests/evidence | Status | Deviation |
|---|---|---|---|---|---|---|

Also report:

- Endpoint and schema changes
- Permission or platform capability changes
- Feature flags and defaults
- Migrations and rollout constraints
- Known limitations and owner
- Screenshots or automated visual evidence

## Allowed implementation decisions

Implementation owns language, libraries, data structures, transaction boundaries, caching, deployment, and internal module design while observable behavior and approved UI remain within contract.

## Return to Design before shipping

Return to Design when implementation would change:

- A screen's purpose, hierarchy, primary action, or navigation
- A visible state or recovery path
- Labels, content limits, legal copy, or asset treatment
- Permissions shown to the user
- The meaning of a stable ID
- An approved interaction that the platform cannot support safely

## Schema ownership

Luz System owns the contract. Luz Dev may propose fields through a reviewed schema change. Until both copies match and the producer manifest uses the change, implementation does not rely on an undocumented extension.
