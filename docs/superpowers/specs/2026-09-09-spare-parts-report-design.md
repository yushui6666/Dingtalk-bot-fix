# 报修备件登记设计（2026-09-09）

> 状态：设计已确认（含数据决策「全部取并集」），待进入 writing-plans / TDD 实现。
> 目标：当工程师报修消息提到「更换/采购/用了某备件」时，AI 在**该工单本身（店×主题房）**的备件权威表里匹配到具体备件，把**规范备件名**记进工单的一项新字段，供排障与归档。**不做动态库存增减，只做记录**；匹配不到时**静默不记**（不报错、不催填）。

## 1. 背景与目标

- 现状：每张机建房/店有报修工单，但「这次换了/买了什么备件」没有结构化落库，维修方式(repair_method)是自由文本，备件信息混在正文里无法统计与回溯。
- 用户已确认口径：
  - **不做动态增减**——本次修复不维护「库存进出」，只在工单上记录「本次使用备件」。
  - **匹配范围**：仅在该单所在「店 × 主题房」的备件表 + 该店「工具」共享池里匹配；命中才记录，未命中**静默跳过**。
  - **记的是规范名**：只记备件权威表里的 `name`（不是工程师口头说法），多个用顿号连接。
  - **数据源**：以每店人工维护的备件权威表为准（`dingtalk_script/data/stores/*.json`），草稿已自动生成、可复现。
- 草稿已生成并核验：**28 家门店**，按「全部取并集」合并多份源文件，逐场景核验**零遗漏**（源件数==合并后件数）。

## 2. 数据源：门店备件权威表草稿

- 位置：`dingtalk_script/data/stores/<门店>.json`（28 个）。
- 结构：
  ```json
  {
    "store": "1.北京商场15店",
    "scenes": {
      "博物馆": {
        "parts": [
          { "name": "继电器DC12V", "aliases": ["继电器"], "spec": "NY4N-J 14脚", "qty": "2个", "check_time": "46268", "check_qty": "2" }
        ]
      },
      "工具": { "parts": [ ... ] }        // 每店共享工具备件池
    },
    "_review": [ {"scene","files","contents_identical"} ]   // 草稿期提示，不作为运行时数据
  }
  ```
- 生成脚本：`/Users/example/Desktop/钉钉消息/build_store_tables.py`（读 `备件汇总/备件明细.jsonl`，`OVERRIDE` 文件名→规范主题映射、`norm()` 去前缀/日期/括号、按 `name` 去重取并集、`fuzzy_key` 聚合别名）。草稿人工审阅后即为运行时数据源。
- 规范主题 key 与工单 `subject`（场景）的对应：读取工单既有的 `store_name` / `subject`，用规范主题 key 查该店文件；无该主题时的兜底见 §7。

## 3. 语义落地：`parts` 作为 `ticket.repair_plan.submit` 的可选字段

> 实现期接地修正（2026-09-09）：**不新增独立 `ticket.part.report` 意图**，而是把 `parts` 作为既有 `ticket.repair_plan.submit`（维修方式）的**可选增强字段**。原因：
> - 现有 `_find_business_action_cues` 对「同一消息多个业务动作」会返回 `system.clarify`；若新增独立意图并给 `更换/采购` 等 cue，则「更换继电器」会同时命中 repair_plan 与 part.report 的 cue → 误触发 `system.clarify`，打断主流程。
> - 而 `更换/采购` 本来就路由到 `repair_plan`，把备件并进去恰好与用户口径一致（维修方式=自由文本操作；parts=具体备件）。
> - 角色/确认/状态约束全部复用 repair_plan，无新高危面（risk 低、无需确认、成功静默）。

- 语义：工程师报修/维修消息说「更换继电器 / 买了干簧管 / 换个电磁锁」→ 仍判 `ticket.repair_plan.submit`：
  - `repair_method` = 操作原文（自由文本，原有不变）。
  - `parts` = 从消息中识别出的**具体备件规范名**（数组），匹配到即填。
- 若消息只报备件不构成完整维修方式（如纯「继电器坏了」），repair_method 兜底回填逻辑（naive: 消息原文）已覆盖，备件照常登记。
- 关键约束：**未匹配到任何规范备件名 → parts 留空（或全被丢弃），静默不记，不生成回执/不催填/不报错**。「用了某物但该店该主题表里没有」是常态（备件表不全），绝不能因此打断工单主流程。

## 4. 存储：`part_versions` 表（镜像 `repair_method_versions`）

```sql
CREATE TABLE IF NOT EXISTS part_versions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id         INTEGER NOT NULL,
    source_message_id TEXT NOT NULL UNIQUE,
    parts             TEXT NOT NULL,          -- 规范备件名，多个用顿号连接，如 "继电器DC12V、干簧管"
    raw_text          TEXT,                   -- 工程师原文（便于核对该次登记依据）
    engineer_id       TEXT NOT NULL,
    submitted_at      TEXT NOT NULL,
    is_current        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_part_ticket ON part_versions(ticket_id);
```

- 复刻 `db.add_repair_method_version` 的写法：写前 `UPDATE part_versions SET is_current=0 WHERE ticket_id=? AND is_current=1`，再 `INSERT ... is_current=1`（每次登记覆盖当前备件）。
- 新增 `db.add_part_version(...)`；读侧 `db.get_current_part_version(ticket_id)` 与 `db.list_part_versions(ticket_id)`（镜像 repair_method_versions 的读法）。

## 5. 分类器 / 校验 / 协议

- `semantics/classifier.py`：
  - `_KNOWN_FIELDS` 追加 `"parts"`。
  - 既有映射 `"ticket.repair_plan.submit": frozenset({"repair_method", "order_no"})` 追加 `"parts"`。
  - 输出 Schema 的 `parts` 为 `array<string>`（规范备件名）。
  - **候选词注入**：system prompt 增加一段「本店可用备件清单（id）：name1/别名1, name2/别名2, …」，取自该「店×主题房」的 `parts[].name`+"aliases" + 该店「工具」池的 `name`；并写明「parts 只能从清单里选规范名；找不到任何清单内备件就返回空数组 []，不要编造」。
- `semantics/validator.py`：
  - `parts` 为字符串数组；每个元素必须能匹配到注入候选（规范名或别名→规范名）。
  - **候选外/无法确定 → 丢弃该项**（不报错）。全部丢弃则 `parts` 视为空。
  - `parts` 可选：消息没提备件时模型返回 `[]`，不 lead 到任何必填错误。
- 协议 `protocols/ticket_semantics.v4.json`：`ticket.repair_plan.submit` 的 `optional_fields` 追加 `parts`；`risk` 不变（备件登记低危、无需确认、成功静默）。

## 6. 执行器 / 归档 / 展示

- `pipeline.py`：在不改 repair_plan 主流程的前提下，把 `repair_plan` 决策中的 `parts`（顿号连接）写 `part_versions`（镜像 `_handle_repair_plan` 的数据写入路径）。
  - 成功登记**静默**（与订单登记成功一致：不回复）。备件登记不需确认。
  - `parts` 为空/未命中 → 静默跳过，不写表、不回复、不排队。
- `scripts/export_tickets_md.py`：在「当前维修方式」区块**下方**新增「本次使用备件」行，取值 `part_versions` 当前(real)记录；历史区追加「备件历史」列表（镜像维修方式历史）。

## 7. 匹配范围与兜底

- 候选池 = 该「店 × 主题房」`scenes[subject].parts` ＋ 该店 `scenes["工具"].parts`（工具池固定并入，因公共五金/电子件常借自工具池）。
- **多文件并集（2026-09-09 用户决策）**：一个工单店名可对应多份备件表（如「上海商场08店」= 商场08品牌C + 商场08品牌B、「上海商场03店」= 商场03品牌C + 商场03品牌B，均为一店含两个品牌），注册表 `stores[店名]` 用**列表**存多份文件名；`build_pool`/`build_vocab` 把每份文件的主题场景 + 各自工具池**并集**合并。主题按 subject 各自命中（同名主题如「特工」会同时命中两表的该场景并合并）；工具池跨品牌共用。
- **权威过滤在执行期**（可信环保守规则）：executor 拿到已解析的 `ticket_id` 后，读该工单 `store_name` + `subject` → `store_parts` 解析对应池，把模型给出的 `parts` 逐项 `canonicalize`（规范名/别名→规范名），**只保留命中该池的项**并归一为规范名；未命中全丢弃（即静默不记）。
- 兜底：
  - 主题 key 未命中（subject 与表内 key 对不上）→ 仅用该店「工具」池；仍无 → 不记。
  - 门店未见（该店无表/未映射）→ 不记。
  - **绝不**跨店/跨场景匹配（并集店内跨品牌主题同样不匹配，如商场08店「博物馆」主题不会命中品牌C主题件）。
- 提示词注入是**辅助**（帮模型先产出贴合的备件词），权威约束以执行期过滤为准——即便模型返回表外词，执行期也会丢弃，保证只记本店本主题表内规范名。

## 8. 测试 / 上线

- TDD `tests/test_part_report.py`：
  1. repair_plan 决策含 `parts=["继电器DC12V"]` 命中店内规范名 → 写入 `part_versions` 且 `is_current=1`，覆盖前版本置 0。
  2. 别名命中（消息「换继电器」，aliases 含「继电器」→ 规范名「继电器DC12V」）→ 记录规范名（非口头词）。
  3. 用「工具」池里的备件 → 命中。
  4. 消息提到备件但店内表无此件（parts 含候选外值）→ **静默**：丢弃、不写表、无回执、无催填错误，repair_method 仍正常登记。
  5. 消息未提备件 → parts 空，不触发备件登记、不报错。
  6. 多个备件 → 顿号连接。
  7. 跨店/跨主题不匹配（B 店件在 A 店单里，注入的是 A 店清单 → 不记）。
  8. 归档 md 在维修方式下方出现「本次使用备件」。
- 全量回归需保持 `>346 passed`。
- 上线：拖入 `data/stores/*.json` 后重启服务（classifier 需重载候选注入）。

## 9. 非目标

- 不做库存动态增减/数量扣减（用户明确）。
- 不一致时的错误回执/补录流程（用户明确静默）。
- 不解析文件名实时定位（以人工维护表为准）。
- 不跨店自动匹配。
