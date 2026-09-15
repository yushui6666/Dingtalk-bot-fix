# Pipeline Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复执行失败状态、可重试草稿、SHADOW 副作用、敏感编号防护、CSV 覆盖和协议源跳过六项审查问题。

**Architecture:** 保留现有 Pipeline、PendingActionService、Notifier 和 pytest 架构。将执行结果完成/失败通知收口到 Pipeline 内部辅助方法，SHADOW 在语义审计后立即返回，失败的补充建单通过新 WAITING Pending 保留合并字段。P2 改动使用确定性本地规则，不新增外部依赖。

**Tech Stack:** Python 3.14, pytest, pytest-asyncio, SQLite.

---

### Task 1: Pending 确认失败状态与通知

**Files:**
- Modify: `pipeline.py`
- Test: `tests/test_pipeline_integration.py`

- [ ] **Step 1: Write failing tests**

  注入返回 `CommandResult("INTERNAL_ERROR", ...)` 的执行器，分别覆盖建单 Pending 和有目标工单 Pending 确认，断言 `processed_result == "INTERNAL_ERROR"` 且群消息含“未完成”。

- [ ] **Step 2: Verify RED**

  Run: `.venv/bin/python -m pytest tests/test_pipeline_integration.py -k 'confirm_pending and executor_failure' -q`

  Expected: FAIL because `_confirm_pending()` stores `EXECUTED` and sends no failure message.

- [ ] **Step 3: Implement minimal result handling**

  Add a Pipeline helper that maps `OK` to `EXECUTED`, otherwise preserves `result.status`; flush success notifications and call `send_group_now()` for failures. Use it from both `_confirm_pending()` branches and the correction branch.

- [ ] **Step 4: Verify GREEN**

  Run the same focused pytest command and expect PASS.

### Task 2: 补充建单失败保留可重试草稿

**Files:**
- Modify: `pipeline.py`
- Test: `tests/test_pipeline_integration.py`

- [ ] **Step 1: Write failing test**

  创建缺 SLA 的 ticket.create Pending，补充 SLA 时让执行器返回 `INTERNAL_ERROR`，断言收件箱保留失败状态、群内收到失败通知，且新 WAITING Pending 保留合并后的 SLA 与原字段。

- [ ] **Step 2: Verify RED**

  Run: `.venv/bin/python -m pytest tests/test_pipeline_integration.py -k 'supplement_create and executor_failure' -q`

  Expected: FAIL because the Pending is confirmed before execution and no failure feedback is sent.

- [ ] **Step 3: Implement retry draft**

  Execute first. On `OK`, resolve the original Pending and flush; on failure, supersede it with a new draft containing the synthetic merged decision, preserve `result.status`, and send the common failure feedback.

- [ ] **Step 4: Verify GREEN**

  Run the focused test and expect PASS.

### Task 3: SHADOW semantic-audit-only gate

**Files:**
- Modify: `pipeline.py`
- Test: `tests/test_pipeline_integration.py`

- [ ] **Step 1: Write failing tests**

  Cover incomplete create and a message that replies to an existing Pending in SHADOW mode. Assert `processed_result == "SHADOW"`, a semantic decision exists, no new/changed Pending exists, and no group text is sent.

- [ ] **Step 2: Verify RED**

  Run: `.venv/bin/python -m pytest tests/test_pipeline_integration.py -k shadow -q`

  Expected: FAIL because validation and Pending reply handling currently run before the SHADOW gate.

- [ ] **Step 3: Move gate**

  Save the semantic decision immediately after model fallback handling, then return `SHADOW` before Pending resolution, clarify, routing, validation, Pending creation, execution, or notifications.

- [ ] **Step 4: Verify GREEN**

  Run the focused test and expect PASS.

### Task 4: Mobile and asset-number guard

**Files:**
- Modify: `semantics/classifier.py`
- Test: `tests/test_model_contract.py`

- [ ] **Step 1: Write parameterized failing test**

  Include phone numbers embedded in text or labeled with phone/contact aliases, and numeric/alphanumeric values labeled `资产号`/`资产编号`/`设备编号`. When the model returns `ticket.repair_plan.submit` using that protected value as `order_no`, assert `chat.ignore` and empty fields.

- [ ] **Step 2: Verify RED**

  Run: `.venv/bin/python -m pytest tests/test_model_contract.py -k 'protected_identifier' -q`

  Expected: FAIL because the current guard only full-matches a bare/labeled mobile number.

- [ ] **Step 3: Implement value-aware guard**

  Extract protected identifiers from the original message and reject only when the model-provided `order_no` equals one of them, preserving explicit real `订单号` submissions.

- [ ] **Step 4: Verify GREEN**

  Run the focused test and expect PASS.

### Task 5: Timestamped CSV default

**Files:**
- Modify: `scripts/export_tickets.py`
- Create: `tests/test_export_tickets.py`

- [ ] **Step 1: Write failing CLI default test**

  Freeze `datetime.now()` and capture the path passed to `export()`. Assert the default is `data/tickets_export_20260820_120000.csv`; also retain explicit `--output` behavior.

- [ ] **Step 2: Verify RED**

  Run: `.venv/bin/python -m pytest tests/test_export_tickets.py -q`

  Expected: FAIL because the current default is fixed `data/exports/tickets_export.csv`.

- [ ] **Step 3: Restore timestamped default**

  Import `datetime`, build the timestamped path only when `--output` is absent, create its parent, and update help/docstring text.

- [ ] **Step 4: Verify GREEN**

  Run the focused test and expect PASS.

### Task 6: Non-skipped protocol source consistency

**Files:**
- Create: `dashbord/维修工单_流程关键词.json`
- Modify: `tests/test_protocol_loader.py`
- Possibly regenerate: `protocols/ticket_semantics.v4.json`

- [ ] **Step 1: Make missing source fail, not skip**

  Point the test at the repository-owned `dashbord` path and assert it exists before compiling twice and comparing bytes with the committed runtime protocol.

- [ ] **Step 2: Verify RED**

  Run: `.venv/bin/python -m pytest tests/test_protocol_loader.py::test_compiler_is_reproducible_and_matches_committed_protocol -q`

  Expected: FAIL until a tracked source is present.

- [ ] **Step 3: Restore tracked business source**

  Add the source required by `compile_business_protocol()`, regenerate the runtime protocol if its source hash changes, and ensure the compiler output remains deterministic.

- [ ] **Step 4: Verify GREEN and full suite**

  Run the focused protocol test, then `.venv/bin/python -m pytest -q`. Expect no failures and no skipped protocol-consistency test.

