# Luz Dev Technical Plan and Learning Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans for inline execution. Steps use checkbox syntax for tracking.

**Goal:** Require every Luz Dev invocation to create a numbered technical-plan HTML before task changes, then capture every task's experience and promote only verified knowledge into reusable assets.

**Architecture:** A standard-library Python generator owns plan numbering and HTML rendering from one template. A second CLI owns experience records, index maintenance, verification promotion, and deprecation. SKILL.md and WORKFLOW.md make the plan the first execution gate; package validation detects broken files, invalid states, placeholders, and leaked secrets.

**Tech Stack:** Python 3 standard library, unittest, HTML/CSS, Markdown.

## Global Constraints

- Output root is exactly D:\Codes\skill输出\Luz-Dev.
- Plan filenames use at least two-digit sequence plus topic, such as 01-花店小程序后端技术方案.html.
- Every luz-dev invocation, including review, diagnosis, and analysis, creates a plan before task changes or external actions.
- No Python third-party dependency.
- Existing design handoff, security, test, and release gates remain mandatory.
- Candidate experience is never a default rule; promotion requires evidence.
- No secret values are stored in records, plans, indexes, templates, or reusable assets.

---

### Task 1: Plan generator contract

**Files:**
- Create: tests/test_skill_automation.py
- Create: scripts/create_tech_plan.py
- Create: assets/tech-plan/template.html

**Interfaces:**
- Produces: create_tech_plan.py TITLE --mode MODE --output-root PATH -> JSON with sequence, path, bytes.
- Modes: implementation, review, diagnosis, analysis, operations.
- Filename contract: ^(\d+)-[^/\\:*?"<>|]+\.html$.

- [x] Write tests for 01 and 02 numbering, required HTML sections, unsafe topic sanitization, and concurrent allocation.
- [x] Run the tests and confirm they fail because create_tech_plan.py is absent.
- [x] Add a self-contained HTML template with required section markers and escaped token rendering.
- [x] Implement exclusive locking, next-number scanning, no-overwrite writes, title validation, and CLI JSON output.
- [x] Re-run the tests and confirm all pass.

### Task 2: Experience lifecycle contract

**Files:**
- Create: assets/experience/RECORD-TEMPLATE.md
- Create: assets/experience/PLAYBOOK-TEMPLATE.md
- Create: scripts/record_experience.py
- Create: experience/README.md
- Create: experience/INDEX.md
- Create: experience/records/.gitkeep
- Create: experience/playbooks/.gitkeep
- Create: experience/reusable/README.md
- Create: experience/reusable/backend/.gitkeep
- Create: experience/reusable/frontend/.gitkeep
- Create: experience/reusable/scripts/.gitkeep
- Test: tests/test_skill_automation.py

**Interfaces:**
- Produces: record_experience.py record --title TEXT --task-mode MODE --plan PATH [--tag TAG] [--evidence EVIDENCE].
- Produces: record_experience.py promote EXP-NNNN --evidence EVIDENCE [--reusable-asset PATH].
- Produces: record_experience.py deprecate EXP-NNNN --reason TEXT [--superseded-by EXP-NNNN].
- Record IDs use EXP-NNNN and live in experience/records/.
- Status values are candidate, verified, deprecated.
- Verified status requires at least one evidence entry.

- [x] Write tests creating candidate experience, rejecting verification without evidence, promoting with evidence, and rejecting deprecation without a reason.
- [x] Run the tests and confirm they fail because record_experience.py is absent.
- [x] Add record and playbook templates plus repository guidance.
- [x] Implement exclusive record numbering, YAML-like front matter, index insertion/replacement, evidence enforcement, and status transitions.
- [x] Re-run the tests and confirm all pass.

### Task 3: Skill workflow gate

**Files:**
- Modify: SKILL.md
- Modify: WORKFLOW.md
- Test: scripts/validate_skill_package.py

**Interfaces:**
- Consumes: scripts/create_tech_plan.py, scripts/record_experience.py, experience/INDEX.md.
- Produces: mandatory pre-execution plan gate and mandatory post-task experience record.

- [x] Add an “Before any Luz Dev task” sequence that creates and completes the HTML plan before code, data, deployment, or external actions.
- [x] Add task modes and the numbered-output example.
- [x] Add verified-only reuse, post-task recording, and promotion rules.
- [x] Update workflow stage 0 so the plan and prior verified experience are inputs to intake.
- [x] Verify SKILL.md remains under 40,000 characters.

### Task 4: Package validation

**Files:**
- Modify: scripts/validate_skill_package.py
- Test: tests/test_skill_automation.py

**Interfaces:**
- Validates required automation files and experience directories.
- Validates record status, IDs, filenames, evidence, index references, HTML template markers, unresolved template tokens, and high-confidence secret patterns.

- [x] Add failing assertions for required automation files and experience structure.
- [x] Extend REQUIRED and package checks minimally to satisfy them.
- [x] Run the full automation test suite and validate_skill_package.py.

### Task 5: First-run evidence

**Files:**
- Create: experience/records/EXP-0001-luz-dev-plan-learning-loop.md
- Modify: experience/INDEX.md
- Modify: D:\Codes\skill输出\Luz-Dev\01-Luz-Dev技能技术方案增强.html

**Interfaces:**
- The first record links the 01 plan and records the verified implementation evidence.
- The plan implementation-status and revision log reflect delivered files.

- [x] Run the generator into a temporary directory to prove 01/02 allocation.
- [x] Run package validation, unit tests, handoff validator smoke test, and HTML static checks.
- [x] Record the implementation experience as verified with exact command evidence.
- [x] Update the 01 plan revision log and implementation status.
- [x] Re-run final checks after those documentation changes.

## Verification Commands

- python -X utf8 -m unittest -v tests.test_skill_automation
- python -X utf8 scripts/validate_skill_package.py
- python -X utf8 scripts/create_tech_plan.py "验证编号与渲染" --mode analysis --output-root <temporary-directory>
- python -X utf8 scripts/record_experience.py --help
