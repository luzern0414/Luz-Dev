# Luz Dev reference index

Start with `SKILL.md`, then open only the platform and engineering references needed by the task.

## Paired Design package

The Design package is resolved by `--design-root`, then `LUZ_DESIGN_ROOT`, then the default `../Luz-Design`. Luz Dev depends only on a stable contract, because Luz Design reorganises its internals between releases:

| Need | Where |
|---|---|
| The skill itself (frontend experience owner) | `$LUZ_DESIGN_ROOT/SKILL.md` |
| Theme tokens for generated pages | `references/themes.json` or `design/shared/themes/themes.json` inside the Design package (found automatically) |
| Approved designs to implement | Luz Design session folders (`NNN-项目/` with `99-delivery/MANIFEST.json` and `APPROVALS.json`) |
| How to consume a session | `references/design-integration.md` + `scripts/design_intake.py` |

## Dev routes

| Need | Read next |
|---|---|
| Technical plan lifecycle (create, research, submit, approve, gate) | `SKILL.md` → `scripts/tech_plan.py` |
| Research with Luz-Crawl | `references/research.md` + `references/research-sources.json` |
| Module detailed design: page inputs, APIs, result pages, data writes, tables and fields per module | `references/module-specs/README.md` |
| Business-logic patterns (orders, inventory and oversell, pricing, coupons, wallets, bookings, after-sales, flash sales, jobs, cache, multi-store) | `references/patterns/README.md` |
| Security and feature standards | Agents: `references/standards.json`; humans: `references/standards.html` |
| WeChat Pay, refund, reconciliation | `backend/09-wechat-pay/MODULE.md` (rules `PAY-*`, `RFD-*`) |
| Frontend from Luz Design | `references/design-integration.md` |
| Operations and launch | `references/operations.md` + `assets/deploy/` |
| Qualifications, filings, licences | `references/compliance.md` |
| Agreements and consent | `references/legal-documents.md` + `assets/legal/` + `backend/27-legal` |
| Mini-program implementation | `references/miniprogram.md` |
| Admin implementation | `references/admin.md` |
| Mobile app implementation | `references/mobile-app.md` |
| Backend, API, or data | `references/backend.md` -> `backend/SPEC.md` |
| Module logic or reusable function design | Agents: `references/function-catalog.json`; humans: `references/function-catalog.html` |
| Completed-flow result pages | `references/result-patterns.md`; catalog module `result-flow` |
| Catalog maintenance | Canonical JSON -> `scripts/build_function_catalog.py` -> generated HTML |
| Standards maintenance | Canonical JSON -> `scripts/build_standards.py` -> generated HTML |
| Intelligent agent service | `references/agent.md` -> `agent/README.md` |
| Frontend/backend API bindings | `references/api-bindings.md` |
| Architecture decisions | `references/implementation-architecture.md` |
| Quality and release | `references/quality.md` + `references/delivery.md` |
| Handoff intake and deviations | `contracts/HANDOFF.md` |
| Implementation evidence | `contracts/IMPLEMENTATION-RECORD.md` |

## Resolver overrides

Luz-Crawl is resolved by `--crawl-root`, then `LUZ_CRAWL_ROOT`, then `../Luz-Crawl`.

```powershell
python -X utf8 scripts/validate_skill_package.py --design-root "D:\Codes\Luz-Design"
$env:LUZ_DESIGN_ROOT = "D:\Codes\Luz-Design"
python -X utf8 scripts/validate_skill_package.py
```
