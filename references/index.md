# Luz Dev reference index

Start with `SKILL.md` (调用协议 and Lifecycle), then open only the references the task needs. This is the one routing table of the skill.

## Paired Design package

The Design package is resolved by `--design-root`, then `LUZ_DESIGN_ROOT`, then the default `../Luz-Design`. Luz Dev depends only on a stable contract, because Luz Design reorganises its internals between releases:

| Need | Where |
|---|---|
| The skill itself (visual standards and page layout) | `$LUZ_DESIGN_ROOT/SKILL.md` |
| Theme tokens for generated pages | `references/themes.json` inside the Design package (older layouts are found automatically) |
| Approved designs to implement | Luz System session folders in `D:\Codes\skill输出\Luz-System` (`NNN-项目/raw/99-delivery/MANIFEST.json` and `APPROVALS.json`) |
| How to consume a session | `references/design-integration.md` + `scripts/design_intake.py` |

## Dev routes

| Need | Read next |
|---|---|
| Every call starts here: the rule card for the task kind (preferences, lessons, rule text, tools, checks) | `python -X utf8 scripts/context.py list` → `context.py <kind>`; cards defined in `references/cards.json` |
| Technical plan lifecycle (create, research, submit, approve, gate) | `SKILL.md` → `scripts/tech_plan.py` |
| Research with Luz-Crawl | `references/research.md` + `references/research-sources.json`; evidence in the Luz-Crawl task folders under `D:\Codes\skill输出\Luz-Crawl` |
| Module detailed design: page inputs, APIs, result pages, data writes, tables and fields per module | `references/module-specs/README.md` |
| Business-logic patterns (orders, inventory and oversell, pricing, coupons, wallets, bookings, after-sales, flash sales, jobs, cache, multi-store, access control) | `references/patterns/README.md` → the module's pattern; reference code `backend/07-inventory`, `08-order`, `10-after-sales`, `13-reservation`, `15-recharge`, `16-coupon`, `17-campaign` |
| Security and feature standards | Agents: `references/standards.json`; humans: `references/standards.html` |
| WeChat Pay, refund, reconciliation | `backend/09-wechat-pay/MODULE.md` (rules `PAY-*`, `RFD-*`; code `wechat_pay/apiv3.py`) |
| Frontend from Luz System | `references/design-integration.md` + `scripts/design_intake.py` |
| Operations and launch | `references/operations.md` + `assets/deploy/` |
| Runtime config files and logging | `references/config-logging.md`; module spec `config`; skeleton `luzback/config.py`, `luzback/logging.py` |
| Front-end texts and display settings maintained in the admin | module spec `content` (`references/module-specs/content.json`) |
| Qualifications, filings, licences | `references/compliance.md` |
| Agreements and consent | `references/legal-documents.md` + `assets/legal/` + `backend/27-legal` |
| Mini-program implementation | `references/miniprogram.md` |
| Admin implementation | `references/admin.md` |
| Mobile app implementation | `references/mobile-app.md` |
| Backend, API, or data | `backend/SPEC.md` → `backend/MODULES.md` → selected `backend/NN-name/MODULE.md`; module, API and migration change rules in `references/implementation-architecture.md`; catalog `backend/API.md` |
| Module logic or reusable function design | Agents: `references/function-catalog.json`; humans: `references/function-catalog.html` |
| Completed-flow result pages | `references/result-patterns.md`; catalog module `result-flow` |
| Catalog maintenance | Canonical JSON -> `scripts/build_function_catalog.py` -> generated HTML |
| Standards maintenance | Canonical JSON -> `scripts/build_standards.py` -> generated HTML |
| Intelligent agent service (canonical spec: pipeline, contracts, limits, checklist) | `references/agent-foundation.md` → `agent/README.md` (code gaps) → `agent/backend/SPEC.md`; protocol `agent/shared/protocol.md` |
| Example map from mini-program modules to backend modules and APIs | `references/api-bindings.md`; the project's own map is `packages/contracts/design-bindings.json` |
| Architecture decisions | `references/implementation-architecture.md` |
| Quality and release | `references/quality.md` + `references/operations.md` (release order, rollback) |
| Handoff intake and deviations | `contracts/HANDOFF.md` |
| Implementation evidence | `contracts/IMPLEMENTATION-RECORD.md` |

## Resolver overrides

Luz-Crawl is resolved by `--crawl-root`, then `LUZ_CRAWL_ROOT`, then `../Luz-Crawl`.

```powershell
python -X utf8 scripts/validate_skill_package.py --design-root "D:\Codes\Luz-Design"
$env:LUZ_DESIGN_ROOT = "D:\Codes\Luz-Design"
python -X utf8 scripts/validate_skill_package.py
```
