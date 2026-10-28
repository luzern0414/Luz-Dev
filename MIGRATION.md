# Migration record

## 2026-10-26 · Split from Luz Design

- Backend implementation moved from `Luz-Design/backend/` to `Luz-Dev/backend/`.
- Agent runtime and protocol moved from `Luz-Design/agent/` to `Luz-Dev/agent/`.
- Agent interface behavior moved to `$LUZ_DESIGN_ROOT/references/system/agents.md`; Design now owns the contract.
- The combined implementation workflow was replaced by a target-routed development workflow.
- `backend/MODULES-DESIGN.md` became `backend/MODULES.md` to distinguish technical module catalog from product design.
- Added implementation guides for WeChat mini-programs, merchant admin, mobile apps, backend, agents, quality, and deployment.
- Added a mirrored design handoff schema, manifest validator, implementation record, and project templates.

## 2026-10-26 · Plan-first delivery and experience learning

- Every Luz Dev invocation now creates or reuses a numbered technical-plan HTML before task execution.
- Technical plans default to `D:\Codes\skill输出\Luz-Dev` and use `NN-主题.html` naming.
- Added a standard-library plan generator with collision-resistant numbering, topic sanitization, and template validation.
- Added an experience lifecycle with candidate, verified, and deprecated states; verification requires evidence.
- Added Playbook and reusable-asset locations under `experience/`.
- Added automation tests and package checks for plan generation, experience promotion, evidence gates, asset references, and secrets.

## 2026-10-26 · Research, approval gate, and standards

- Technical plans are data-first: `_data/NN-主题/plan.json` is canonical and `NN-主题.html` is rendered from it by `scripts/tech_plan.py` with Luz Design tokens read from `$LUZ_DESIGN_ROOT` at every render. `create_tech_plan.py` remains the entry point for new drafts; `tech_plan.py adopt` converts older HTML-only plans and keeps the original as `legacy.html`.
- Plan lifecycle: draft → 待审核 (`submit`, blocked by incomplete sections, research below threshold, unhandled MUST rules, or dangling `[S#]`) → 已批准 (`approve`, only with the user's words) → `gate` before any change; edits after approval fail the gate until `revise` and re-approval.
- Research: `tech_plan.py research` drives Luz-Crawl — official pages first (`references/research-sources.json` → `officialUrls`), then searches — and imports sources with tier and read state. Guide: `references/research.md`.
- Standards: `references/standards.json` (91 rules in 23 domains, 42 feature baselines) with generated `references/standards.html`. Applicable rules and baselines become each implementation plan's checklist.
- WeChat Pay: `backend/09-wechat-pay/MODULE.md` rewritten from official pages read on 2026-10-26; `wechat_pay/apiv3.py` adds APIv3 request signing, pay-sign, callback verification, sign-probe handling, AES-256-GCM decryption, fact checks and trade-state mapping with 17 tests.
- The Admin v2 page shell moved to `assets/shared/luz-shell.css`, shared by plan and standards pages.

## 2026-10-26 · Full backend knowledge and system-level research

- Research now covers the whole system in three layers: comparable systems (`systemArchetypes` mall / food / booking / membership-store / saas-admin with open-source reference repositories), every business module (33 modules with search routes), and official platform pages. `tech_plan.py new --archetype` adds an archetype's modules; implementation plans must read at least one open-source comparable system.
- The research runner interleaves searches by site, splits them into batches (`--batch`, `--pause`), imports each batch as it lands, records cooled-down searches in `retryQueries` and completed ones in `doneQueries`, and only accepts Luz-Crawl folders carrying its own name (other sessions may crawl at the same time).
- New pattern library `references/patterns/` (13 documents) with tested reference code: inventory reservation and ledger (`07-inventory/reservation.py`), order timeout job (`08-order/jobs.py`), pricing and allocation (`17-campaign/pricing`), bookings (`13-reservation/booking`), wallet (`15-recharge/wallet`), after-sales (`10-after-sales`), and the shared `luzback.StateMachine`. Backend tests: 101 → 140.
- Standards: 103 rules (new: ORDER-007/008/009, RFD-006, MKT-005, MBR-003, RSV-001/002, JOB-001, CACHE-001, SECKILL-001, AUTHZ-004) and 47 baselines; modules map to their pattern documents, and new plans open the business-module section with that reading list.
- Function catalog: 13 modules (new: wallet, pricing, after-sales, jobs; inventory, reservation and order updated to implemented).
- All generated files are written with LF line endings.

## 2026-10-26 · Development to launch, Luz Design frontend, project folders, knowledge base

- Scope: Luz Dev now covers development through launch — operations (`references/operations.md`, `assets/deploy/`), qualifications, filings and licences (`references/compliance.md`), user agreements and consent (`references/legal-documents.md`, `assets/legal/`, `backend/27-legal`). Implementation and operations plans gain a “上线、运维与合规” section.
- Frontend: Luz Design owns the experience; Luz Dev integrates its approved sessions. `scripts/design_intake.py` verifies approvals against SHA-256, extracts stable IDs (P/A/D/I/FL/S/R…), builds `design-bindings.json`, and checks frontend coverage and leftover prototype data. `tech_plan.py design NN --session` records the session in the plan; frontend plans cannot be submitted without it.
- Luz Design was restructured (theme and handoff schema moved under `references/`). Luz Dev now depends only on its `SKILL.md`, a theme file in any known location, and its session outputs; the old handoff-schema equality check was removed.
- Output layout: `D:\Codes\skill输出\Luz-Dev\NN-主题\NN-主题.html` + `raw\` (plan.json, research inputs, logs, design intake). `tech_plan.py migrate-layout` moved plans 01–06.
- Plan pages open with “系统功能：基础功能 + 定制功能”: base features from the standards baselines (now including profile editing and user management) and custom features recorded with `tech_plan.py feature`; implementation plans must list them.
- Knowledge base `knowledge/` (`scripts/knowledge.py`): every source read, distilled facts, searches already run, review periods by kind, generated `index.html`. New plans reuse fresh knowledge; research skips fresh pages and searches and writes new sources back automatically. Seeded with 72 sources and 24 verified facts.
- Standards: 116 rules and 55 baselines (new INT-*, OPS-*, COMP-*, LIC-*, LEGAL-*).
