# 待商榷独立状态设计（2026-08-28）

> 状态：设计已确认，待进入 writing-plans。关联事故：2026-08-28 007号单错挂005（`PENDING_NEGOTIATION` 前身为 `sla=待商榷/ deadline=NULL` 的 `ACTIVE` 展示）。

## 1. 背景与目标

- 现行 `待商榷` 仅是 `sla_days=0 / current_deadline_at=NULL` 的 `ACTIVE` 工单展示，仍被 `list_active_tickets` 计入活动，调度器虽不催时效但仍受响应催、看板仍在“进行中”列。
- 用户诉求（2026-08-28）：`待商榷` 升级为**独立状态**，与 `ACTIVE / ACTIVE_OVERDUE / PENDING_CONFIRM / STOPPED / CANCELLED / COMPLETED` 并列，看板单独一列，期间**计时/催办完全暂停**；**C 方案**：DB 真新增状态，历史 `ACTIVE+deadline IS NULL` 兼容为该状态，新单 `时效：待商榷` 直接进新状态；**很多单是后续从确定时效转待商榷**，需支持 `ACTIVE/OVERDUE → 待商榷` 的显式转换（需确认）；退出**直接走完成**，不需改回时效。

## 2. 状态机

```
ACTIVE ──#待商榷(确认)──┐
ACTIVE_OVERDUE ─────────┤
                         └─> PENDING_NEGOTIATION ──完成/取消/停止──> 终态
PENDING_NEGOTIATION ──完成──> COMPLETED
```

- 新增：`models.TICKET_NEGOTIATING = "PENDING_NEGOTIATION"`，`TICKET_STATUS_LABELS["PENDING_NEGOTIATION"]="待商榷"`。
- 不提供 `PENDING_NEGOTIATION → ACTIVE` 改回时效路径（用户明确）。
- `STOPPED / CANCELLED` 从待商榷可达，避免死单（与看板“终态”语义一致）。

## 3. DB 与看板协调

- `db.py`：`tickets.status TEXT` 无需新增列；`list_active_tickets` 保持 `IN ('ACTIVE','ACTIVE_OVERDUE')` —— 待商榷**不算活动**；`scan_response_sla / scan_deadline / scan_pending_confirm` 的 `WHERE status IN (...)` 已天然排除，无需改调度器。
- 历史兼容：看板查询层视图兼容 `CASE WHEN status='ACTIVE' AND current_deadline_at IS NULL THEN 'PENDING_NEGOTIATION'`，并提供可选离线迁移：`UPDATE tickets SET status='PENDING_NEGOTIATION', version=version+1 WHERE status='ACTIVE' AND current_deadline_at IS NULL AND sla_days=0`（执行前备份）。
- 看板：`tickets/repository.py` / `dashbord` 分组由 `GROUP BY status` 显式六列，新增“待商榷”列，排序 `ACTIVE → OVERDUE → 待商榷 → PENDING_CONFIRM → 终态`；`list_group_tickets` 仍全量返回。
- 计时：切入待商榷时冻结 `waiting_since`（不推进），`current_deadline_at` 置 `NULL`；待商榷期间不计入响应/时效；完成时直接终态，无需 `close_active_special_case` 式的停表平移（长期暂停不回溯）。

## 4. 协议 / 执行 / 校验 / 语义

- 协议 `protocols/ticket_semantics.v4.json` 新增 `ticket.negotiate.submit`：`display:"设为待商榷"`, `allowed_roles:["MANAGER","ENGINEER"]`, `allowed_states:["ACTIVE","ACTIVE_OVERDUE"]`, `required:["negotiate_reason"]`, `optional:["ticket_no"]`, `risk:MEDIUM`, `confirmation: REQUIRED (SEMANTIC_MODEL+EXPLICIT_KEYWORD)`。
- `ticket.complete / ticket.cancel / ticket.stop` 的 `allowed_states` 追加 `PENDING_NEGOTIATION`。
- 执行器 `tickets/executor.py:_execute_negotiate`：`WHERE id=? AND version=?` 乐观锁，`UPDATE SET status='PENDING_NEGOTIATION', current_deadline_at=NULL, sla_days=0, version+1`，记录 `ticket_messages`。
- 校验 `semantics/validator.py`：`negotiate_reason` 必填 `1..500` 字。
- 语义 `semantics/classifier.py`：关键词 `["#待商榷","#改待商榷"]` → `ticket.negotiate.submit`；提示词追加“‘改成待商榷/暂时不定/先待商榷吧/时效待定’→该意图”。

## 5. 权限 / 并发 / 异常

- 权限：`MANAGER/ENGINEER`（沿用超集），`OTHER` 不可。
- 确认：`MEDIUM` → 双路径均需 `PendingActionDraft`（`expected_ticket_versions`, `expires_at` 5min）。
- 并发：`version` 乐观锁，冲突回“状态已更新，请重试”。
- 幂等：`processed_events.message_id` 去重。
- 边界：已是待商榷再 `#待商榷` → `status not in allowed` → “已是待商榷”；短编号解析复用 `_resolve_ticket_reference` 尾缀逻辑与非活动解释文案（2026-08-28 守卫）。

## 6. 测试 / 迁移 / 上线

- TDD `tests/test_negotiating_status.py`：建单落待商榷、ACTIVE→待商榷需确认、待商榷直接完成、调度器跳过、active 计数排除、尾缀解释复用；全量 `>340 passed`。
- 迁移：上线前 `data/tickets.db` 备份；可选离线 `UPDATE`；不执行则视图兼容兜底。
- 上线：合并后重启服务（`pipeline/classifier/executor` 需重载）。

## 7. 非目标

- 不提供待商榷改回确定时效；不改特殊情况真停表逻辑；不改 `PENDING_CONFIRM` 窗口归档。
