---
name: luz-dev
description: Use when taking a Luz product from approved design to production — backend services, APIs, databases, business logic (orders, inventory and oversell prevention, pricing and coupons, prepaid wallets and points, bookings, after-sales, payments and refunds, scheduled jobs, caching, multi-store isolation), integrating the frontend designed by Luz System into mini-programs, admin systems or apps, deployment and operations, business qualifications and filings, software licences, user agreements and privacy consent, and launch acceptance. Also use when researching how comparable systems are built with Luz-Crawl, writing a technical plan for user approval, applying the Luz security and feature standards, or reviewing, diagnosing and operating an existing Luz system.
---

# Luz Dev

Luz Dev takes a Luz product from approved design to a running, compliant production system. **Luz System owns the system design and the user-facing experience** (module scope, architecture, function list, product UI, interactive prototype and on-site hardware — each approved by the user against a content hash); **Luz Design owns the visual standards and the layout of every page and document** (theme tokens, reading-page rules, `check_document.py`). **Luz Dev owns everything needed to ship it**: research, technical plan, backend and business logic, integrating the approved frontend with real APIs and data, deployment and operations, qualifications and filings, licences, user agreements and consent, and launch acceptance.

The order is fixed: **research → plan → user approval → build → integrate → launch → record experience.** No code, data, configuration, deployment, or external state changes before the user approves the plan.

Knowledge lives in four places, each with tested or verified content:

| Kind | Where |
|---|---|
| Business-logic patterns with reference code | `references/patterns/` + `backend/` (tests: `cd backend; python -m pytest -q`) |
| Module detailed design (features, flows, page inputs, APIs, result pages, data writes, tables and fields, states, exceptions) | `references/module-specs/` (`scripts/module_specs.py`) |
| Rules and feature baselines (verifiable) | `references/standards.json` → `references/standards.html` |
| Launch: operations, qualifications, agreements | `references/operations.md`, `references/compliance.md`, `references/legal-documents.md`, templates in `assets/deploy/` and `assets/legal/` |
| Frontend integration with Luz System | `references/design-integration.md` + `scripts/design_intake.py` |
| Everything research found (sources, facts, searches already run) | `knowledge/` → `knowledge/index.html` (`scripts/knowledge.py`) |

## Mandatory technical plan before execution

Every invocation — implementation, review, diagnosis, analysis, explanation, operations — has exactly one technical plan. A new task gets the next number and its own project folder:

```text
D:\Codes\skill输出\Luz-Dev\
  01-健身房管理系统\
    01-健身房管理系统.html   the finished page the user reviews (Luz Design tokens)
    raw\                    plan.json (canonical), research inputs and logs, design-intake.json, legacy pages
```

**One task, one folder.** A user request produces exactly one project folder. Follow-up messages about the same request revise that plan (`tech_plan.py revise`) and re-render the same HTML; never create a second folder for it, and tell the user the single HTML path to open.

Edit `raw/plan.json`, then re-render; never hand-edit the HTML. The page opens with the conclusion, then **系统功能：基础功能 + 定制功能** — the base features every system of this type needs (login, profile editing, orders, coupons, refunds, user management… from the standards baselines, each marked included or not applicable) followed by this project's custom features — then research, design, architecture and the rest. `tech_plan.py migrate-layout` moves plans from the older flat layout.

### Lifecycle

1. **Create the draft immediately**, before reading widely or changing anything:

   ```powershell
   python -X utf8 scripts/create_tech_plan.py "健身房下单小程序技术方案" --mode implementation `
     --project-type miniprogram,admin,backend --archetype booking --modules face
   ```

   Modes: `implementation`, `review`, `diagnosis`, `analysis`, `operations`. Project types: `miniprogram`, `admin`, `mobile`, `backend`, `agent` (a mini-program or admin project also includes `backend`). `--archetype` (`mall`, `food`, `booking`, `membership-store`, `saas-admin`) adds that kind of system's modules, comparable open-source systems and system-level searches; add launch modules (`deploy`, `compliance`, `legal`, `license`, `design-integration`) when the task goes to production. Module IDs: `references/standards.json` → `modules`.

2. **Check the knowledge base, then research with Luz-Crawl — the whole system.** A new plan already contains the fresh sources and facts in `knowledge/` for its modules (marked “知识库”); research only fetches what is missing or past its review period, and every newly read source is written back to `knowledge/` automatically. Read `references/research.md`, then run `python -X utf8 scripts/tech_plan.py research NN --run --batch 15 --pause 6` in the background. It reads comparable systems, every module's design practice, the official pages of external platforms, and the regulations behind qualifications and agreements. Every implementation, operations and analysis plan also researches the **baseline tracks** whatever its modules — Web and API security, admin login and session security, HTTPS and web-server hardening, container deployment, CI/CD and rollback, server and network security, monitoring and alerting, backup and recovery, secrets and dependency security, data protection and 等级保护 (`research-sources.json` → `baselineTracks`); review plans cover the security tracks. Review is refused until each track has a read source, and an official or standard one where the track requires it (OWASP, Docker, PostgreSQL, nginx, Mozilla). The findings go into the plan's 安全与规范 and 上线、运维与合规 chapters as concrete settings (headers, TLS versions, rate limits, lockout thresholds, backup schedule, alert rules) with `[S#]`. Sources are imported with tier and read state; implementation plans need at least one comparable open-source system read. Searches that hit a site cooldown are retried on the next `--run`. If Luz-Crawl is unavailable, stop and tell the user; implementation research is skipped only with explicit user consent (`--skip "原因" --user-consent`).

3. **Register the design.** For frontend work, `tech_plan.py design NN --session <Luz System 会话目录>` verifies that architecture, system design and prototype are approved and unchanged since approval, and records page / action / entity counts. Without an approved design, ask the user to complete it with **$luz-system** first; a backend-only task records `--skip "原因"`.

4. **List the custom features** — everything the user asked for beyond the base features: `tech_plan.py feature NN --name "人脸入场" --desc "会员刷脸入场，可用会员码替代" --priority P0 --module face` (modules named here join the plan and its checklist). A plan with none records `--none "原因"`.

5. **Write the module detailed design.** An implementation plan starts with a copy of `references/module-specs/` for its modules in `moduleSpecs`; each module becomes its own numbered chapter (1 功能说明, 2 业务流程, 3 页面与输入, 4 接口, 5 结果与页面跳转, 6 数据写入, 7 数据表, 8 状态流转, 9 异常与边界), and the 数据库设计 and 接口总览 chapters are generated from them. Adapt every copied spec to the project — what the user fills in on each page, what each page calls, which page shows after payment, what is written after payment, how refunds are computed — and replace it with `tech_plan.py spec NN --file adapted.json`; `--sync` adds specs for modules added later, `--remove ID` drops one that does not apply. Every table a module writes must be defined; a backend implementation plan cannot be submitted without module specs. Format and conventions: `references/module-specs/README.md`.

6. **Complete the plan** in `plan.json`. Start business modules from the pattern documents the plan lists; adapt them to what research found and state deviations. Fill the “上线、运维与合规” section: qualifications and filings, agreements, environments and deployment, launch checklist. Replace every `todo` block; mark each checklist item (`tech_plan.py mark NN PAY-005 --status planned --note "做法"`, or `n/a` with a reason); record questions only the user or merchant can answer as decisions (`tech_plan.py decide NN D1 --question … --option … --blocking`). Then `tech_plan.py render NN`.

7. **Submit for review**: `tech_plan.py submit NN`. It refuses while a section is incomplete, research is below threshold, custom features are not listed, a MUST rule is unhandled, a frontend plan has no design session, or a `[S#]` reference is missing. Give the user the HTML path and the open decisions.

8. **Approve only on the user's explicit approval**: `tech_plan.py approve NN --note "用户原话"`. Never approve on the user's behalf, from silence, or from an earlier approval of a different plan. Blocking decisions must be decided first.

9. **Gate before building**: `tech_plan.py gate NN` before the first change and before each later batch. It fails if the plan is not approved or changed after approval.

10. **Revise, never drift**: `tech_plan.py revise NN --note "变化与原因"`, update, re-submit, re-approve before building the changed part.

11. **Deliver with evidence and grow the knowledge base**: mark items `done` with `--evidence`, pass `tech_plan.py check NN --stage delivery`, complete the launch checklist, and record every reusable conclusion from this task's research as a fact: `python -X utf8 scripts/knowledge.py fact --kind law|platform|feature|practice|pitfall --topic … --statement … --source <URL 或 KS-编号> --module …`.

Read-only modes do not need approval to run but still create the plan and record findings. `operations` needs approval. Convert an older HTML-only plan with `tech_plan.py adopt NN --mode …`. If a plan cannot be created, researched, or completed, stop and report the blocker.

### Plan pages follow the Luz Design output rules

Every HTML Luz Dev generates (plan pages, `references/standards.html`, `knowledge/index.html`) is a Luz Design reading page: the first visible line is the title, sections follow directly, light surfaces, one accent colour, one font family, sizes 14/16/20/24/32 only, centered container, no application shell (sidebar, top bar, breadcrumbs, footer). The page holds content only — conclusions, features, architecture, interfaces, data, rules and how this project meets them, launch, tests. Process information stays in `raw/plan.json` and in the conversation: sources and research counts, approval state and hashes, revisions, timestamps, file paths, and open decisions (ask the user in the conversation). Write rule notes and content without those words.

`tech_plan.py submit` runs Luz Design's own checker (`$LUZ_DESIGN_ROOT/scripts/check_document.py`, used read-only) on the page; any ERROR blocks submission. After it passes, still apply Luz Design's deletion test to every visible element. Luz Dev never edits the Luz Design or Luz-Crawl skills.

Colour tokens come from the paired Luz Design theme file at every render (`references/themes.json` or `design/shared/themes/themes.json`, theme `ai-agent`; bundled snapshot as fallback); the page style is `assets/shared/luz-reading.css`.

Headings are numbered when rendered (2, 2.1 …) and a contents list follows the title on long plans. Plan content blocks: `p`, `h3`, `list`, `table` (`dense` for number-heavy tables), `callout`, `cards`, `kv`, `code`, `svg` (scripts and event attributes are rejected), and `todo` (blocks submission). Inline text supports `` `code` ``, `**bold**` and `[text](https://…)`; `[S#]` source markers stay in the data and are not shown.

The paired Design package is resolved by `--design-root`, then `LUZ_DESIGN_ROOT`, then `../Luz-Design`. Luz Dev depends only on its `SKILL.md`, its theme file, and the design sessions it produces — not on its internal layout, which changes between releases. Luz-Crawl is resolved by `--crawl-root`, then `LUZ_CRAWL_ROOT`, then `../Luz-Crawl`.

## Frontend: integrating Luz System

Read `references/design-integration.md`. In short:

1. `design_intake.py inspect <会话> --require architecture,system_design,prototype` — approvals and SHA-256 must match.
2. `design_intake.py bind <会话> --out <项目>/packages/contracts/design-bindings.json` — one row per action (`A-`), entity (`D-`), integration (`I-`), page (`P-`); fill API routes together with the OpenAPI contract.
3. Build the production pages from the approved prototype: keep `data-page-id` / `data-action-id`, replace every fixture with real API data, implement every state, use design tokens.
4. `design_intake.py check <绑定表> --frontend <apps/miniprogram> --frontend <apps/admin>` — 100% action and page coverage, no prototype data or mock imports, no undesigned actions.

Changing a user-visible flow, permission rule or hierarchy goes back to Luz System first (INT-003).

## Launch: operations, qualifications, agreements

| Area | Read | Templates |
|---|---|---|
| Environments, deployment, HTTPS, monitoring, backups, rollback, mini-program release | `references/operations.md` | `assets/deploy/` (Compose, Nginx, env example, backup script, `LAUNCH-CHECKLIST.md`) |
| Qualifications, filings, platform category, payment merchant, graded protection, licences | `references/compliance.md` | `config/third-party-licenses.md` in the project |
| User terms, privacy policy, mini-program privacy guide, face consent, prepaid-card terms, consent records | `references/legal-documents.md` | `assets/legal/`; backend module `backend/27-legal` |

Qualifications and filings take time and are done by the merchant: list them in the plan in the first week. Agreement texts are drafted from templates and confirmed by the merchant (lawyer review recommended); Luz Dev implements versioning, consent records, re-consent and withdrawal, and does not make legal judgements for the merchant. Facts about regulations and platform rules must come from official pages read in this task's research.

## Standards

`references/standards.json` holds the rules (`MUST` / `SHOULD`, platforms, modules, requirement, why, do / don't, verification, sources) and feature baselines per project type. Each implementation or operations plan automatically receives every applicable rule as its checklist and the baselines for its project types. Domains: delivery, authentication, authorization and isolation, input and errors, money, orders and inventory, bookings, WeChat Pay, refunds and reconciliation, coupons, membership and prepaid, personal information, face data, files and content, secrets, external calls, rate limiting, logging and audit, jobs and cache, data and backups, mini-program, admin, UI, agents, frontend integration, qualifications and licences, agreements, release, operations.

To add or correct a rule: edit `references/standards.json` (source + check date for external facts), run `python -X utf8 scripts/build_standards.py`, then `tech_plan.py render NN --sync` for open plans.

## Routing

| Target | Read next | Reference code / baseline |
|---|---|---|
| Research for any plan | `references/research.md` + `references/research-sources.json` | Luz-Crawl collections under `D:\Codes\skill输出\Luz-Crawl` |
| Business logic | `references/patterns/README.md` → the module's pattern | `backend/07-inventory`, `08-order`, `10-after-sales`, `13-reservation`, `15-recharge`, `16-coupon`, `17-campaign`, `27-legal` |
| Payment, refund, reconciliation | `backend/09-wechat-pay/MODULE.md` (rules `PAY-*`, `RFD-*`) | `wechat_pay/apiv3.py` |
| Frontend from Luz System | `references/design-integration.md` | `scripts/design_intake.py` |
| WeChat mini-program implementation | `references/miniprogram.md` | Luz System session for the project |
| Merchant admin implementation | `references/admin.md` | Luz System session for the project |
| Mobile app implementation | `references/mobile-app.md` | Luz System session for the project |
| Backend, API, data | `references/backend.md` → `backend/SPEC.md` → selected `backend/NN-name/MODULE.md` | `backend/MODULES.md`, `backend/API.md` |
| Module catalog | Agents: `references/function-catalog.json`; humans: `references/function-catalog.html` | — |
| Result pages | `references/result-patterns.md` + catalog module `result-flow` | — |
| Operations and launch | `references/operations.md` | `assets/deploy/` |
| Qualifications and licences | `references/compliance.md` | — |
| Agreements and consent | `references/legal-documents.md` | `assets/legal/`, `backend/27-legal` |
| Intelligent agent | `references/agent.md` → `agent/README.md` → `agent/backend/SPEC.md` | `agent/shared/protocol.md` |
| Architecture decision | `references/implementation-architecture.md` | — |
| Quality / release | `references/quality.md` + `references/delivery.md` | — |
| Security and feature rules | Agents: `references/standards.json`; humans: `references/standards.html` | — |

Read only what the task needs.

## Start every task

1. Create the plan draft with project types, archetype and modules; search `experience/INDEX.md` for verified reusable logic.
2. Run the Luz-Crawl research and import it.
3. Register the Luz System session (or the reason there is none).
4. Read the pattern documents the plan lists and query `references/function-catalog.json`; for completion flows also read `references/result-patterns.md`.
5. Inspect the existing repository, conventions, tests, and current behavior.
6. Complete the plan including the launch section, work through the checklist, submit, and wait for the user's approval.
7. After `gate` passes: smallest vertical slice first; OpenAPI contract and design bindings before both sides change; tests for behavior, failure, concurrency and permissions.
8. Integrate the Luz System frontend and pass `design_intake.py check`.
9. Deploy through the release flow, complete the launch checklist with evidence, observe after release.
10. Mark checklist items done with evidence; record the experience.

Use `WORKFLOW.md` for gates and sequencing.

## Engineering rules

The full, verifiable list is `references/standards.json`. The rules that shape every design:

- Production pages show real backend data only; distinguish written, run, tested, and verified-in-production.
- The approved Luz System experience is the contract: preserve its IDs, flows and permissions; go back to Luz System before changing them.
- API contracts are versioned and explicit; frontend clients are generated from the same OpenAPI.
- Authenticate by default, check resource ownership on the server, validate input, rate-limit, and log without secrets or personal data.
- Money is integer minor units priced on the server; inventory, seats, balances and coupons change through conditional atomic updates with a release path and a ledger.
- State changes go through `luzback.StateMachine`; consequential writes are idempotent; side effects after a commit go through the outbox; transactions never call external services.
- Payment success comes only from a verified callback or an active query.
- Personal data needs a stated purpose and consent with records; face recognition is never the only option; agreements are versioned and re-consented after material changes.
- Production runs HTTPS only, with monitoring, backups that have been restored, and a rehearsed rollback.
- Secrets live in a secret store or restricted files, never in source, logs, screenshots or prompt-visible files.
- Done means verified in the target environment, including failure, repeat, concurrency, permission and dependency-outage behavior.

## Platform defaults

Use the project's approved stack when one exists. For greenfield Luz work:

- Mini-program: native WeChat Mini Program with TDesign Mini Program or Vant Weapp for behavior only; appearance from Luz Design tokens.
- Admin: Vue 3, Vite, TypeScript, Pinia, Vue Router, Element Plus, ECharts.
- Mobile app: Flutter; React Native when the team works in TypeScript; native when platform constraints require it.
- Backend: FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, Redis, OpenAPI.
- Agent: LangGraph and Pydantic with tool allowlists, confirmation, budgets and evaluation.
- Deployment: Docker Compose behind Nginx on a cloud server with an ICP-filed HTTPS domain (`assets/deploy/`).

## Project structure

```text
<project>/
  apps/
    miniprogram/        # from the approved Luz System session
    admin/
    mobile/
  services/
    backend/
    agent/
  packages/
    contracts/          # openapi.yaml, design-bindings.json, generated clients
    ui-tokens/          # from Luz Design
  infra/
    deploy/             # from assets/deploy/
    migrations/
  config/               # per-environment settings, where secrets live, third-party-licenses.md
  legal/                # agreement sources and published versions
  docs/
    IMPLEMENTATION.md
    LAUNCH-CHECKLIST.md
  README.md
```

Omit unused targets. Keep runtime secrets outside the repository.


## Skill registration

Luz Dev, Luz System, Luz Design, Luz Crawl and Luz Writing are registered for every agent on this machine as junctions in `D:\Codes\skills`, which `~/.claude/skills` and `~/.codex/skills` both point to: `luz-dev → D:\Codes\Luz-Dev`, `luz-system → D:\Codes\Luz-System`, `luz-design → D:\Codes\Luz-Design`, `luz-crawl → D:\Codes\Luz-Crawl`, `luz-writing → D:\Codes\Luz-Writing`. An agent calls them by name (Skill tool `luz-crawl`, `luz-system`, `luz-design`, `luz-writing`). Luz Dev uses Luz Crawl for research, Luz System for system design, product UI and prototypes, Luz Design for visual standards and every HTML page's layout, and Luz Writing for user agreements, privacy policies and launch copy; it reads their files and runs their scripts but never edits them. Check or restore them with `python -X utf8 scripts/register_skills.py --check` (report) or `python -X utf8 scripts/register_skills.py` (create missing junctions).

## Knowledge base

`knowledge/` keeps everything research found so the next project starts from it instead of searching again:

| File | Holds |
|---|---|
| `knowledge/sources.json` | every page read: normalized URL, title, tier, kind (法律法规 / 平台文档 / 开源实现 / 功能做法 / 工程实践 / 踩坑记录), modules, check date, Luz-Crawl evidence folder, excerpt |
| `knowledge/facts.json` | reusable conclusions distilled from sources, each citing its sources, with modules and the rules or documents that use it |
| `knowledge/queries.json` | searches already run and their outcome |
| `knowledge/index.html` | searchable page (`knowledge.py build`) |

Review periods: laws 365 days, platform documents 90, open-source 180, practice 365. Stale entries are re-read by the next research run (`knowledge.py stale`). Search before planning: `knowledge.py search 退款`, `knowledge.py for-modules payment refund`. Only public information belongs here — never merchant data, credentials or personal information. A fact that becomes a general rule is promoted into `references/standards.json` and lists that rule in `usedBy`.

## Experience and continuous learning

Search `experience/INDEX.md` and `knowledge/` before designing. Use verified records and assets when their applicability and tests match; treat candidates as observations.

Knowledge flows one way: **experience record (evidence) → verified → standard rule, pattern, or catalog entry.** A general lesson (platform limit, security requirement, failure mode, launch blocker) becomes a rule in `references/standards.json` with its source and check date; reusable module logic becomes a pattern with tested code and a catalog entry (`python -X utf8 scripts/build_function_catalog.py`).

At the end of every task, including a review, diagnosis or blocked task, record what was learned:

```powershell
python -X utf8 scripts/record_experience.py record --title "支付回调幂等处理" --task-mode implementation --plan "D:\Codes\skill输出\Luz-Dev\06-健身房下单小程序技术方案.html" --summary "使用持久化事件标识过滤重复回调。" --tag payment --tag idempotency
python -X utf8 scripts/record_experience.py promote EXP-0001 --evidence "test:test_payment_callback_replay"
```

Records begin as `candidate`; promote only with tests, production evidence, or explicit user confirmation; `deprecate` with a reason and replacement. Never promote credentials, merchant data, volatile prices, or unverified code.

## Validation

```powershell
python -X utf8 -m unittest -v tests.test_tech_plan tests.test_design_intake tests.test_knowledge tests.test_function_catalog tests.test_skill_automation tests.test_validate_handoff
python -X utf8 scripts/knowledge.py check
python -X utf8 scripts/build_standards.py
python -X utf8 scripts/build_function_catalog.py
python -X utf8 scripts/validate_skill_package.py
cd backend; python -m pytest -q
python -m pytest -q ../agent
```

Use the project's own test commands as primary evidence. External services are tested with isolated accounts and sandbox or small real transactions.
