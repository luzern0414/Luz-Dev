# Luz Dev workflow

Luz Dev runs a product from approved design to production. This file holds the gate of each stage — what must be true and what evidence a reviewer can inspect. The order of a call and the commands are in `SKILL.md` (调用协议 and Lifecycle); the sediment rules are in `SKILL.md` › 每次调用后沉淀.

## 0. Rule card and experience first, then plan, research and user approval

### 0a. Rule card and experience first

Every invocation that works on a project starts a numbered plan with `scripts/create_tech_plan.py` (or `tech_plan.py new`), naming the project types, archetype and modules; it saves the rule cards for its mode and project types into `raw/00-rules/` (`--card <kind>` chooses them), and they are read in full before research. The plan generator, the asset templates and the standard modules are skeletons; the card decides. Stored preferences and lessons (`record_experience.py search`, `knowledge.py search`) apply without being asked again. Applied rules go into 采用的规则 (`tech_plan.py rule`), the call's note into 本次沉淀 (`tech_plan.py sediment`).

**Gate:** `tech_plan.py submit` and `check --stage delivery` refuse a plan whose rule cards are missing or out of date (regenerate with `context.py <kind> --session <方案目录>` and re-check), or whose 采用的规则 or 本次沉淀 is empty.

Each plan has its own folder `D:\Codes\skill输出\Luz-Dev\NN-主题\`: the reviewable page `NN-主题.html` and `raw\` (canonical `plan.json`, research inputs and logs, design intake). The page lists the system's base features and custom features before the technical content.

### 0b. Research

Start from `knowledge/`: a new plan already carries the fresh sources and facts for its modules. Run `tech_plan.py research NN --run` (see `references/research.md`) for what is missing or stale: Luz-Crawl opens official pages directly, then runs the planned searches in batches; every source is imported with its tier and read state and written back to `knowledge/`. Read the brief, design handoff, repository state, prior verified experience, the function catalog entries, and the standards that apply.

**Gate:** the research thresholds for the mode are met (`references/research-sources.json`), every `officialRequired` module has an official source, and external facts in the plan cite `[S#]`. Research is skipped only with a written reason, and for implementation only with the user's explicit consent.

### 0c. Plan

List the custom features (`tech_plan.py feature`). When a Luz System session already exists, read its architecture and function list so the modules, APIs and tables match it. Fill the launch section: qualifications and filings, agreements, deployment, launch checklist. Complete every other section. Work through the standards checklist: each applicable MUST rule is `planned` with how the plan satisfies it, or `n/a` with why. Each required feature baseline is in scope. Questions only the user can answer are recorded as decisions; blocking ones are marked. User-facing screens without an approved design go to $luz-system first; the plan does not design them.

**Gate:** `tech_plan.py submit NN` passes — no `todo` blocks or placeholder text, research thresholds met, custom features listed, no unhandled MUST rule, no dangling source reference, current rule cards in `raw/00-rules/`, 采用的规则 filled, and 本次沉淀 naming a note written or confirmed since the plan was created. The plan status becomes 待审核. Qualifications and filings that need the merchant are handed over now, because they take time.

### 0d. User approval

Present the plan page and the open decisions to the user. Record approval only when the user explicitly approves this plan, quoting their words: `tech_plan.py approve NN --note "…"`. Blocking decisions are recorded with `tech_plan.py decide` before approval. Then ask for the frontend address and register it: `tech_plan.py design NN --session <Luz-System 会话> [--frontend <目录>]` (a backend-only task records `--skip "原因"`); before submission it is only a reminder.

**Gate:** `tech_plan.py gate NN` passes before any code, data, configuration, deployment, or external state change, and again before each later batch. It fails when the plan is not approved, its approved content has changed, or a frontend plan has no registered, approved frontend address.

**Output:** an approved `NN-主题/NN-主题.html` with `raw/plan.json`, the Luz-Crawl evidence folders, updated `knowledge/`, and the decision list.

## 1. Architecture and risk

Choose the target stack only where the project has not already chosen one. Define modules, data ownership, trust boundaries, external dependencies, feature flags, migration strategy, and failure behavior.

**Output:** `docs/IMPLEMENTATION.md` with architecture decisions and risks.

**Gate:** every core flow maps to a component, contract, data owner, and test strategy. Risk controls are proportional to impact.

## 2. Contract first

Define or update OpenAPI, event schemas, design/platform IDs, error codes, permission codes, and feature switches before parallel implementation.

For a module design, select the relevant catalog entries by module ID and function ID, then adapt them to the project rather than copying routes or status words verbatim. Add newly confirmed generic behavior to `references/function-catalog.json` (the only source), run `python -X utf8 scripts/build_function_catalog.py`, and review the generated `references/function-catalog.html`, a Luz Design reading page that passes `check_document.py`.

For any flow that completes through payment, form submission, a state change, batch work, or asynchronous processing, define its result contract before frontend work: server state and terminality, result page route, unchanged-form behavior, polling and recovery actions, history semantics, and sanitized display data. Use `references/result-patterns.md` as the decision matrix. Do not treat an HTTP acknowledgement, SDK callback, or client redirect as the final business result.

**Output:** `packages/contracts/` and API contract changes.

**Gate:** consumers and producers can generate or validate against the same schema. No endpoint is implemented against an undocumented response.

## 3. Vertical slice

Implement one end-to-end outcome through the required layers:

1. data and invariants
2. business behavior
3. API or local application service
4. UI state and interaction
5. telemetry and audit where applicable
6. tests and visual verification

**Gate:** happy path, invalid input, unauthorized access, duplicate request, dependency failure, and recovery are exercised.

## 4. Frontend integration (Luz System)

The frontend experience comes from the approved Luz System session (`references/design-integration.md`).

- `design_intake.py inspect --require architecture,system_design,prototype` passes: approvals and hashes match.
- `design_intake.py bind` creates `packages/contracts/design-bindings.json`; every action is bound to an OpenAPI route, a navigation, a local interaction, or a platform capability; every entity and integration is mapped.
- Build production pages from the prototype, keep `data-page-id` / `data-action-id`, and replace every fixture with real API data.

- Implement tokens rather than hard-coded visual values.
- Build the shell, navigation, authentication, and permission-aware routes.
- Implement reusable components before repeating markup.
- Implement all applicable screen states and preserve deep links or restored state.
- For result surfaces, replace submitted routes in history, rebuild from server state, keep invalid forms editable, and give processing/failure outcomes a safe recovery action.
- Verify at real device sizes, zoom, keyboard, long content, and reduced motion.

**Gate:** `design_intake.py check <bindings> --frontend …` passes (100% coverage, no prototype data, no undesigned actions); shipped screens compare with the prototype acceptance screenshots without an undocumented deviation; no screen lacks its API state behavior.

## 5. Service implementation

Apply the backend rules in `backend/SPEC.md` and only the selected module contracts. Keep transaction boundaries and idempotency explicit. Run migrations from an empty database.

**Gate:** concurrency, duplicate delivery, invalid transition, authorization, timeout, and rollback behavior have tests.

## 6. Intelligent agent

Follow `references/agent-foundation.md`; its §17 checklist is the gate evidence. Register only implemented, allowlisted tools. Validate inputs and outputs, confirm high-impact actions, bound loops and budgets, surface sources for factual claims, and degrade predictably.

**Gate:** no unconfirmed high-impact execution; duplicate confirmation cannot repeat a write; evaluation sets record pass rates and regressions.

## 7. Integrated verification

Run unit, contract, integration, end-to-end, accessibility, performance, and security checks appropriate to the target. Exercise the dependency failures the design claims to support.

**Gate:** required checks pass; failures have owners and release impact.

## 8. Launch

Follow `references/operations.md`: environments, release flow (CI → staging rehearsal → production backup, migration, health check, observation), HTTPS and hardening, monitoring and alerts, backups with a restore drill, rollback. Complete the qualifications and filings in `references/compliance.md` and publish the agreements in `references/legal-documents.md` with consent records (`backend/27-legal`). Release the mini-program through trial version, review and publication. Work through `assets/deploy/LAUNCH-CHECKLIST.md` with evidence.

**Output:** release notes, health checks, monitoring, `config/` and README, and the implementation record mapped to design IDs. Checklist items are marked `done` with evidence, this build's note is in 本次沉淀, and `tech_plan.py check NN --stage delivery` passes (current rule cards, 采用的规则, and a note confirmed on or after the approval day).

**Gate:** every launch-checklist item has a result and evidence; key flows (login, order, real small payment, callback, query, refund, reconciliation) pass in production; rollback is possible; observability exists; any design deviation has been reviewed and approved in Luz-System.

## 9. Experience capture and promotion

Every call, whatever its outcome, writes or confirms at least one note and lists it in 本次沉淀 (rules: `SKILL.md` › 每次调用后沉淀). A task that produced a reusable method, a failure analysis or evidence worth keeping also gets an experience record (`record_experience.py record`): link the technical plan, record decisions and failed approaches, list observable evidence, and identify reusable candidates.

For reviews and analyses, capture the finding, method, limitations, and evidence. For implementation, include changed contracts, tests, migration results, recovery behavior, and release constraints.

Record every reusable conclusion from this task's research as a fact in `knowledge/` (`knowledge.py fact`, citing its sources) so the next project does not search again. When the lesson is a general rule (a platform limit, a security requirement, a failure mode, a launch blocker), add or correct it in `references/standards.json` with its source and check date and rebuild `references/standards.html`, so every later plan receives it in its checklist.

**Output:** the call's note in 本次沉淀; when warranted an `experience/records/EXP-NNNN-主题.md` record (or the existing record on the same `key`, updated in place) and an updated `experience/INDEX.md`; new facts in `knowledge/`; any new or corrected standard rule; any non-sensitive reusable candidate.

**Gate:** candidate experience is not used as a default rule. Promotion requires tests, production evidence, or explicit user confirmation. Reusable code has a source, dependencies, tests, usage example, security boundary, and limitations. Deprecated records state why and what replaces them.

## Change loop

When scope changes after approval, run `tech_plan.py revise NN --note "…"`, update the plan, re-submit, and wait for approval before building the changed part. Do not let code drift from the approved plan.

When a design change arrives, it comes as a newly approved Luz-System session: re-run `design_intake.py inspect` and `bind`, list the affected actions, endpoints and data, revise the plan, then implement and test the change. When implementation reveals a platform constraint, send the exact constraint and options back to Luz-System before changing the user experience.

When a task changes existing behavior, search prior experience again, write the new plan or append a revision, and record the experience under the existing topic `key` so the old record is updated in place; use a new key only for a genuinely new topic, and `deprecate` the old record with `--superseded-by` when a different topic replaces it.
