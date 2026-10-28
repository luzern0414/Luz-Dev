# Luz Dev Function Logic Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans for inline execution. Steps use checkbox syntax for tracking.

**Goal:** Add a searchable, editable, offline HTML catalog that records concrete logic for Luz modules and serves as a shared reference for humans and future agents.

**Architecture:** Keep a canonical JSON dataset and a self-contained HTML workbench. A standard-library build script embeds the JSON into the HTML; a validator rejects malformed data and drift. The page supports search, module filtering, inline editing, add/delete operations, localStorage recovery, JSON import/export, and full HTML export.

**Tech Stack:** Python 3 standard library, unittest, vanilla HTML/CSS/JavaScript, embedded JSON.

## Global Constraints

- Primary human-facing file: references/function-catalog.html.
- Canonical structured source: references/function-catalog.json.
- No external scripts, styles, fonts, network calls, or third-party Python packages.
- Initial modules use backend/MODULES.md names and status terms.
- Each function is represented by a two-column table of concrete logic.
- Runtime rendering uses textContent; catalog content must never be interpreted as HTML.
- Candidate behavior and verified behavior must be visually distinguishable.
- Existing plan-first and experience lifecycle gates remain mandatory.

---

### Task 1: Canonical catalog and generator

**Files:**
- Create: references/function-catalog.json
- Create: assets/reference/function-catalog.template.html
- Create: scripts/build_function_catalog.py
- Create: tests/test_function_catalog.py

**Interfaces:**
- build_function_catalog.py --data references/function-catalog.json --output references/function-catalog.html
- Catalog schema: schemaVersion, updatedAt, title, modules[]; module fields id/name/backendModule/status/description/functions[]; function fields id/name/status/rows[]; row fields label/value.
- Required initial modules: reservation, order, payment, inventory, membership, coupon, delivery, lottery.

- [x] Write failing tests for generation, embedded JSON, required modules, and unique IDs.
- [x] Run the tests and confirm they fail because the generator and data do not exist.
- [x] Add the initial catalog data with concrete logic for reservations, orders, payment, inventory, membership, coupons, delivery, and lottery.
- [x] Add the HTML template and build script with safe JSON embedding.
- [x] Re-run tests until green.

### Task 2: Visual editing workbench

**Files:**
- Modify: assets/reference/function-catalog.template.html
- Test: tests/test_function_catalog.py

**Interfaces:**
- The page exposes search-input, module-filter, edit-toggle, add-module, import-data, export-json, export-html, and reset-local controls.
- Edit mode makes function names, status, and row label/value fields editable.
- Structural controls add/remove modules, functions, and rows.
- Data is saved to localStorage and can be imported/exported as JSON or HTML.

- [x] Write static contract tests for control IDs and safety statements.
- [x] Implement the responsive visual shell and per-function tables.
- [x] Implement render, filter, edit, add/delete, import/export, and localStorage behavior.
- [x] Verify text is rendered with textContent and not innerHTML.
- [x] Regenerate the HTML and run all tests.

### Task 3: Skill routing and validation

**Files:**
- Modify: SKILL.md
- Modify: WORKFLOW.md
- Modify: scripts/validate_skill_package.py
- Test: tests/test_function_catalog.py

**Interfaces:**
- SKILL routing table includes references/function-catalog.html and json.
- Stage 0 requires agents to search the catalog before module design.
- Package validation invokes catalog schema and HTML-embedding checks.

- [x] Add a failing package integration test for catalog presence and embedded-JSON parity.
- [x] Update Skill routing and workflow.
- [x] Extend package validation without duplicating parser logic.
- [x] Run package validation and all tests.

### Task 4: Evidence and experience

**Files:**
- Modify: D:\Codes\skill输出\Luz-Dev\02-Luz-Dev-功能参考模板与可视化维护.html
- Create: experience/records/EXP-0002-function-logic-catalog.md
- Modify: experience/INDEX.md

**Interfaces:**
- The plan records final data coverage, editing behavior, validation evidence, and limitations.
- The experience records reusable data shape and editor patterns; promotion follows evidence gates.

- [x] Run tests, validator, and static HTML checks.
- [x] Generate a temporary catalog and confirm deterministic output.
- [x] Record EXP-0002 with verification evidence.
- [x] Update the 02 plan status and revision log.
- [x] Run final checks after documentation changes.

## Verification Commands

- python -X utf8 -m unittest -v tests.test_function_catalog
- python -X utf8 -m unittest -v tests.test_skill_automation
- python -X utf8 scripts/build_function_catalog.py
- python -X utf8 scripts/validate_skill_package.py


## Completion Evidence

- [x] `tests.test_function_catalog`: 6 tests passed after generating the canonical HTML.
- [x] `tests.test_skill_automation`: 9 tests passed.
- [x] `scripts/validate_skill_package.py`: PASS with function-catalog schema, embedded-data parity, security, link, and Design contract checks.
- [x] Created verified `EXP-0002` and playbook `PB-0002`.
- [x] Completed the numbered technical plan with implementation and verification evidence.
