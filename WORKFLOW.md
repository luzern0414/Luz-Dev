# Luz Dev workflow

Luz Dev runs a product from approved design to production. Development proceeds in vertical slices; launch follows a checklist. Each gate has evidence that a reviewer can inspect.

## 0. Plan, research, and user approval

Every invocation starts a numbered plan with `scripts/create_tech_plan.py` (or `tech_plan.py new`), naming the project types, archetype and modules. Each plan has its own folder `D:\Codes\skill输出\Luz-Dev\NN-主题\`: the reviewable page `NN-主题.html` and `raw\` (canonical `plan.json`, research inputs and logs, design intake). The page lists the system's base features and custom features before the technical content.

### 0a. Research

Start from `knowledge/`: a new plan already carries the fresh sources and facts for its modules. Run `tech_plan.py research NN --run` (see `references/research.md`) for what is missing or stale: Luz-Crawl opens official pages directly, then runs the planned searches in batches; every source is imported with its tier and read state and written back to `knowledge/`. Read the brief, design handoff, repository state, prior verified experience, the function catalog entries, and the standards that apply.

**Gate:** the research thresholds for the mode are met (`references/research-sources.json`), every `officialRequired` module has an official source, and external facts in the plan cite `[S#]`. Research is skipped only with a written reason, and for implementation only with the user's explicit consent.

### 0b. Plan

List the custom features (`tech_plan.py feature`). Register the Luz System session for frontend work (`tech_plan.py design NN --session …`). Fill the launch section: qualifications and filings, agreements, deployment, launch checklist. Complete every other section. Work through the standards checklist: each applicable MUST rule is `planned` with how the plan satisfies it, or `n/a` with why. Each required feature baseline is in scope. Questions only the user can answer are recorded as decisions; blocking ones are marked. When the plan covers user-facing screens without an approved handoff, the screens section comes from $luz-design.

**Gate:** `tech_plan.py submit NN` passes — no `todo` blocks or placeholder text, research thresholds met, custom features listed, design session registered for frontend plans, no unhandled MUST rule, no dangling source reference. The plan status becomes 待审核. Qualifications and filings that need the merchant are handed over now, because they take time.

### 0c. User approval

Present the plan page and the open decisions to the user. Record approval only when the user explicitly approves this plan, quoting their words: `tech_plan.py approve NN --note "…"`. Blocking decisions are recorded with `tech_plan.py decide` before approval.

**Gate:** `tech_plan.py gate NN` passes before any code, data, configuration, deployment, or external state change, and again before each later batch. It fails when the plan is not approved or its approved content has changed.

**Output:** an approved `NN-主题/NN-主题.html` with `raw/plan.json`, the Luz-Crawl evidence folders, updated `knowledge/`, and the decision list.

## 1. Architecture and risk

Choose the target stack only where the project has not already chosen one. Define modules, data ownership, trust boundaries, external dependencies, feature flags, migration strategy, and failure behavior.

**Output:** `docs/IMPLEMENTATION.md` with architecture decisions and risks.

**Gate:** every core flow maps to a component, contract, data owner, and test strategy. Risk controls are proportional to impact.

## 2. Contract first

Define or update OpenAPI, event schemas, design/platform IDs, error codes, permission codes, and feature switches before parallel implementation.

For a module design, select the relevant catalog entries by module ID and function ID, then adapt them to the project rather than copying routes or status words verbatim. Add newly confirmed generic behavior to `references/function-catalog.json`, run `python -X utf8 scripts/build_function_catalog.py`, and review the generated `references/function-catalog.html`. Browser edits are recoverable local drafts; only imported canonical JSON becomes part of the skill. Use the Luz Design Admin v2 layout and `ai-agent` semantic tokens when maintaining the catalog page.

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

Register only implemented, allowlisted tools. Validate inputs and outputs, confirm high-impact actions, bound loops and budgets, surface sources for factual claims, and degrade predictably.

**Gate:** no unconfirmed high-impact execution; duplicate confirmation cannot repeat a write; evaluation sets record pass rates and regressions.

## 7. Integrated verification

Run unit, contract, integration, end-to-end, accessibility, performance, and security checks appropriate to the target. Exercise the dependency failures the design claims to support.

**Gate:** required checks pass; failures have owners and release impact.

## 8. Launch

Follow `references/operations.md`: environments, release flow (CI → staging rehearsal → production backup, migration, health check, observation), HTTPS and hardening, monitoring and alerts, backups with a restore drill, rollback. Complete the qualifications and filings in `references/compliance.md` and publish the agreements in `references/legal-documents.md` with consent records (`backend/27-legal`). Release the mini-program through trial version, review and publication. Work through `assets/deploy/LAUNCH-CHECKLIST.md` with evidence.

**Output:** release notes, health checks, monitoring, `config/` and README, and the implementation record mapped to design IDs. Checklist items are marked `done` with evidence and `tech_plan.py check NN --stage delivery` passes.

**Gate:** every launch-checklist item has a result and evidence; key flows (login, order, real small payment, callback, query, refund, reconciliation) pass in production; rollback is possible; observability exists; any design deviation has been reviewed by Design.

## 9. Experience capture and promotion

At the end of every completed, abandoned, or blocked task, create an experience record with `scripts/record_experience.py`. Link the technical plan, record decisions and failed approaches, list observable evidence, and identify reusable candidates.

For reviews and analyses, capture the finding, method, limitations, and evidence. For implementation, include changed contracts, tests, migration results, recovery behavior, and release constraints.

Record every reusable conclusion from this task's research as a fact in `knowledge/` (`knowledge.py fact`, citing its sources) so the next project does not search again. When the lesson is a general rule (a platform limit, a security requirement, a failure mode, a launch blocker), add or correct it in `references/standards.json` with its source and check date and rebuild `references/standards.html`, so every later plan receives it in its checklist.

**Output:** an `experience/records/EXP-NNNN-主题.md` record (or the existing record on the same `key`, updated in place), an updated `experience/INDEX.md`, new facts in `knowledge/`, any new or corrected standard rule, and any non-sensitive reusable candidate.

### 每次调用后沉淀

Mandatory on every call, whatever its outcome:

1. At the start, `record_experience.py search <关键词> [--kind preference|lesson|fact]` and apply the matching preferences, lessons and facts.
2. Before finishing, `record_experience.py note --kind preference|lesson|fact --key K --text "…" [--why "…"]` for each preference the user stated or corrected, each route, source, parameter or fix that proved effective or failed, and each rule correction. Write the general principle, not task details.
3. The store is `experience/notes.json` (same shape in all four Luz skills; see SKILL.md). Same topic, same key: the new statement replaces the old one (one `previous` kept); the same statement under another key only confirms that note. Caps: preferences 60, lessons 120, facts 120, active records 120; `prune` drops the fewest uses, then the oldest confirmation, and lists notes unconfirmed for 180 days; `check` fails on duplicates, missing fields, private data and overflow.
4. No phone numbers, emails, WeChat IDs, user-home paths, credentials, personal or client names; those stay in a git-ignored `*.local.json`.

**Gate:** candidate experience is not used as a default rule. Promotion requires tests, production evidence, or explicit user confirmation. Reusable code has a source, dependencies, tests, usage example, security boundary, and limitations. Deprecated records state why and what replaces them.

## Change loop

When scope changes after approval, run `tech_plan.py revise NN --note "…"`, update the plan, re-submit, and wait for approval before building the changed part. Do not let code drift from the approved plan.

When a design change arrives, update the handoff or delta record first, identify affected source and endpoints, then implement and test the change. When implementation reveals a platform constraint, send the exact constraint and options back to Design before changing the user experience.

When a task changes existing behavior, search prior experience again, write the new plan or append a revision, and record the experience under the existing topic `key` so the old record is updated in place; use a new key only for a genuinely new topic, and `deprecate` the old record with `--superseded-by` when a different topic replaces it.
