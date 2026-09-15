# SLA 架构设计与绩效考核扩展说明

> 版本：2026-08-27（绩效考核扩展设计）
> 基于版本：v4.1（2026-08-24 四项改进后）

本文档分为两部分：
1. **现有 SLA 架构**：把当前系统怎么实现 SLA 说清楚
2. **绩效考核扩展**：基于现有数据如何设计绩效考核

---

## 第一部分：现有 SLA 架构

### 一、SLA 类型总览

系统有 **两类独立的 SLA**，分别解决不同问题：

| SLA 类型 | 解决什么问题 | 时间维度 | 责任对象 |
|---------|-------------|---------|---------|
| **响应 SLA** | 责任方响应及时性 | 分钟级（1h/4h） | 工程师/店长 |
| **时效 SLA** | 维修完成及时性 | 天级（1天/3天/7天） | 工程师 |

### 二、响应 SLA 详解

#### 2.1 业务规则

```
触发条件：工单进入等待状态（waiting_side 非空）
         ↓
    等待 1 小时
         ↓
    ┌─────────────────────────────────────┐
    │ 群内提醒责任方（工程师/店长）          │
    │ 每个等待周期只提醒一次                │
    └─────────────────────────────────────┘
         ↓
    等待 4 小时
         ↓
    ┌─────────────────────────────────────┐
    │ 群内升级提醒 + 单聊升级对象           │
    │ 每个等待周期只升级一次，不循环        │
    └─────────────────────────────────────┘
```

#### 2.2 关键数据结构

**tickets 表相关字段：**

```sql
waiting_side     TEXT    -- 'ENGINEER_SIDE' | 'MANAGER_SIDE' | 'NONE'
waiting_since    TEXT    -- 等待开始时间（ISO格式）
status           TEXT    -- 'ACTIVE' | 'ACTIVE_OVERDUE' | 'PENDING_CONFIRM'
created_at       TEXT    -- 建单时间（用于存量豁免判断）
```

**升级对象配置（config.py）：**

```python
# 工程师侧升级对象
RESPONSE_SLA_ENGINEER_ESCALATE_USER_ID  # 工程总监A userId
RESPONSE_SLA_ENGINEER_ESCALATE_NAME    # 工程总监A

# 店长侧升级对象（从 groups.json 动态获取）
# 按群路由：上海=区域经理A，杭/京/常/苏=区域经理B
```

#### 2.3 扫描逻辑（scheduler.scan_response_sla）

```python
# 伪代码
for ticket in 工单表:
    if ticket.created_at < "2026-08-26 15:00:00":
        continue  # 存量工单豁免
    
    if ticket.waiting_side == 'NONE':
        continue  # 未在等待
    
    elapsed = now - ticket.waiting_since
    
    if elapsed >= 1h:
        群内提醒责任方  # dedupe_key 包含 waiting_since，周期级去重
    
    if elapsed >= 4h:
        群内升级提醒责任方 + 升级对象
        单聊升级对象    # dedupe_key 同上，每周期一次
```

#### 2.4 关键配置参数

```python
RESPONSE_SLA_ENABLED = True           # 总开关（.env 热读）
RESPONSE_SLA_FIRST_HOURS = 1.0        # 一级提醒时间
RESPONSE_SLA_ESCALATE_HOURS = 4.0     # 二级升级时间
RESPONSE_SLA_EFFECTIVE_FROM = "2026-08-26 15:00:00"  # 存量分界点
```

### 三、时效 SLA 详解

#### 3.1 业务规则

```
建单时设置时效（1天/3天/7天/待商榷）
         ↓
    deadline = 建单时间 + 时效天数
         ↓
    ┌─────────────────────────────────────┐
    │ 临近到期前 6 小时 → 群提醒一次       │
    └─────────────────────────────────────┘
         ↓
    已超时
         ↓
    ┌─────────────────────────────────────┐
    │ 1. 状态推进 ACTIVE → ACTIVE_OVERDUE  │
    │ 2. 建立超时周期（timeout_cycles）     │
    │ 3. 群提醒一次                        │
    └─────────────────────────────────────┘
         ↓
    特殊情况：订单已签收
         ↓
    ┌─────────────────────────────────────┐
    │ 自签收次日起每日提醒一次，直到完成     │
    │ （签收当天只发「开始计时维修」通知）   │
    └─────────────────────────────────────┘
```

#### 3.2 关键数据结构

**tickets 表相关字段：**

```sql
sla_days               INTEGER  -- 1/3/7，0=待商榷
initial_deadline_at    TEXT     -- 初始截止时间
current_deadline_at    TEXT     -- 当前截止时间（可能因暂停顺延）
status                 TEXT     -- 'ACTIVE' | 'ACTIVE_OVERDUE'
```

**timeout_cycles 表：**

```sql
id                 INTEGER  -- 主键
ticket_id          INTEGER  -- 关联工单
cycle_no           INTEGER  -- 超时周期序号
status             TEXT     -- 'WAITING_REASON' | 'EXPLAINED'
old_deadline_at    TEXT     -- 超时前的截止时间
reminded_at        TEXT     -- 提醒时间
reason             TEXT     -- 工程师回复的超时原因
new_deadline_at    TEXT     -- 新的截止时间（当前未启用）
```

#### 3.3 扫描逻辑（scheduler.scan_sla_reminders）

```python
# 伪代码
for ticket in 活动工单表:
    if ticket.sla_days == 0:
        continue  # 待商榷不参与
    
    if ticket 有订单已签收:
        continue  # 走 scan_received_reminders 每日提醒
    
    if ticket 有特殊情况暂停中:
        continue  # 暂停期间豁免
    
    if now >= ticket.current_deadline_at:
        推进状态 → ACTIVE_OVERDUE
        建立超时周期
        群提醒
    elif now + 1h >= ticket.current_deadline_at:
        群提醒（临近到期）
```

#### 3.4 特殊情况暂停机制

```sql
-- ticket_special_cases 表
ticket_id            INTEGER  -- 关联工单
reason               TEXT     -- 原因（等待到货/等待上门/等待客户/等待第三方）
expected_resume_at   TEXT     -- 预计恢复时间
paused_deadline_at   TEXT     -- 暂停时的截止时间快照
submitted_at         TEXT     -- 提交时间
resumed_at           TEXT     -- 恢复时间（NULL=生效中）
```

**暂停期间：**
- 时效 SLA 提醒豁免
- 响应 SLA 提醒豁免
- 签收后每日催豁免
- 恢复后按实际暂停时长顺延 deadline

### 四、两套 SLA 的关系图

```
                    ┌─────────────────────────────────────────┐
                    │              工单 (tickets)              │
                    │                                         │
                    │  ┌───────────────────────────────────┐  │
                    │  │ 时效 SLA 相关字段                  │  │
                    │  │  - sla_days (1/3/7)               │  │
                    │  │  - initial_deadline_at            │  │
                    │  │  - current_deadline_at            │  │
                    │  └───────────────────────────────────┘  │
                    │                                         │
                    │  ┌───────────────────────────────────┐  │
                    │  │ 响应 SLA 相关字段                  │  │
                    │  │  - waiting_side (谁在等)           │  │
                    │  │  - waiting_since (从什么时候开始等) │  │
                    │  └───────────────────────────────────┘  │
                    └─────────────────────────────────────────┘
                                    │
            ┌───────────────────────┼───────────────────────┐
            │                       │                       │
            ▼                       ▼                       ▼
    ┌──────────────┐      ┌──────────────┐      ┌──────────────┐
    │ 时效 SLA 扫描 │      │ 响应 SLA 扫描 │      │ 特殊情况暂停 │
    │ (scheduler)  │      │ (scheduler)  │      │ (scanner)    │
    └──────────────┘      └──────────────┘      └──────────────┘
            │                       │                       │
            ▼                       ▼                       ▼
    ┌──────────────┐      ┌──────────────┐      ┌──────────────┐
    │ 群内提醒     │      │ 群内提醒     │      │ 暂停时效     │
    │ 状态推进     │      │ 升级+单聊    │      │ 顺延deadline │
    │ 超时周期     │      │              │      │              │
    └──────────────┘      └──────────────┘      └──────────────┘
```

---

## 第二部分：绩效考核扩展设计

### 一、从 SLA 数据中可以提取的绩效指标

#### 1.1 工程师维度

| 指标名称 | 数据来源 | 计算逻辑 |
|---------|---------|---------|
| **响应 SLA 达标率** | tickets.waiting_side + waiting_since | 工程师响应时间 ≤ 1h 的比例 |
| **响应平均时长** | tickets.waiting_since → 工程师首次回复 | 平均响应小时数 |
| **时效 SLA 达标率** | tickets.current_deadline_at → closed_at | 在截止前完成的比例 |
| **维修平均时长** | tickets.created_at → closed_at（扣除暂停） | 平均维修天数 |
| **被升级次数** | scheduler.scan_response_sla 触发记录 | 超 4h 被升级的比例 |
| **工单重开率** | tickets.reopen_count > 0 | 被重开的比例 |
| **超时次数** | tickets.status = 'ACTIVE_OVERDUE' | 超时效的次数 |

#### 1.2 店长维度

| 指标名称 | 数据来源 | 计算逻辑 |
|---------|---------|---------|
| **确认 SLA 达标率** | PENDING_CONFIRM → completed_confirm_at | 店长确认时间 ≤ 1h 的比例 |
| **确认平均时长** | tickets → completed_confirm_at | 平均确认小时数 |
| **建单准确率** | tickets → CANCELLED (reason) | 因信息不全取消的比例 |
| **被升级次数** | 升级对象 = 店长侧 | 被区域经理关注的次数 |

### 二、需要新增的数据表

#### 2.1 绩效记录表（performance_records）

```sql
CREATE TABLE IF NOT EXISTS performance_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    user_role TEXT NOT NULL,        -- 'ENGINEER' or 'MANAGER'
    period TEXT NOT NULL,           -- '2026-08' 月度
    
    -- 响应 SLA 相关
    response_sla_met INTEGER,      -- 1=达标, 0=未达标, NULL=不适用
    response_time_hours REAL,      -- 实际响应小时数
    
    -- 时效 SLA 相关
    time_sla_met INTEGER,          -- 1=达标, 0=未达标
    repair_time_hours REAL,        -- 实际维修时长（小时）
    
    -- 其他
    was_escalated INTEGER DEFAULT 0,  -- 是否被升级
    was_reopened INTEGER DEFAULT 0,   -- 是否被重开
    
    created_at TEXT NOT NULL,
    UNIQUE(ticket_id, user_id)
);
```

#### 2.2 绩效汇总表（performance_summary）

```sql
CREATE TABLE IF NOT EXISTS performance_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    user_role TEXT NOT NULL,
    period TEXT NOT NULL,           -- '2026-08' 或 '2026-Q3'
    
    total_tickets INTEGER,
    response_sla_compliance_rate REAL,   -- 响应 SLA 达标率
    time_sla_compliance_rate REAL,       -- 时效 SLA 达标率
    avg_response_time_hours REAL,        -- 平均响应时长
    avg_repair_time_hours REAL,          -- 平均维修时长
    escalation_count INTEGER,            -- 被升级次数
    reopen_count INTEGER,                -- 被重开次数
    
    performance_score REAL,              -- 综合评分 (0-100)
    performance_level TEXT,              -- 'A'/'B'/'C'/'D'
    
    calculated_at TEXT NOT NULL,
    UNIQUE(user_id, period)
);
```

### 三、数据采集时机

```
工单完成 (COMPLETED)
    │
    ├─→ 检查 waiting_since 到首次工程师回复的时间差
    │   └─→ 写入 performance_records.response_time_hours
    │
    ├─→ 检查 current_deadline_at 与 closed_at 的关系
    │   └─→ 写入 performance_records.time_sla_met
    │
    ├─→ 检查是否触发过升级 (notification_deliveries.dedupe_key LIKE 'resp_l2:%')
    │   └─→ 写入 performance_records.was_escalated
    │
    └─→ 检查 reopen_count
        └─→ 写入 performance_records.was_reopened
```

### 四、绩效评分算法建议

```python
def calculate_performance_score(
    response_sla_rate: float,   # 响应 SLA 达标率 (0-1)
    time_sla_rate: float,       # 时效 SLA 达标率 (0-1)
    avg_response_hours: float,  # 平均响应时长
    escalation_rate: float,     # 被升级率 (0-1)
    reopen_rate: float          # 重开率 (0-1)
) -> float:
    """计算综合绩效评分 (0-100)"""
    
    weights = {
        'response_sla': 0.25,    # 响应及时性
        'time_sla': 0.30,        # 完成及时性
        'response_time': 0.20,   # 响应速度
        'escalation': 0.15,      # 升级控制
        'reopen': 0.10           # 质量控制
    }
    
    scores = {
        'response_sla': response_sla_rate * 100,
        'time_sla': time_sla_rate * 100,
        'response_time': max(0, 100 - avg_response_hours * 10),  # 每小时扣10分
        'escalation': max(0, 100 - escalation_rate * 200),       # 每1%扣2分
        'reopen': max(0, 100 - reopen_rate * 150)               # 每1%扣1.5分
    }
    
    return sum(scores[k] * weights[k] for k in weights)

def score_to_level(score: float) -> str:
    if score >= 90: return 'A'
    if score >= 80: return 'B'
    if score >= 70: return 'C'
    return 'D'
```

### 五、实施建议

#### 阶段 1：数据采集（1-2 周）

1. 新增 `performance_records` 表
2. 在 `pipeline.py` 的 `_handle_complete` 方法中插入绩效记录采集逻辑
3. 修改 `_handle_shop_confirm` 方法，补充店长确认时间记录

#### 阶段 2：汇总计算（1 周）

1. 新增 `performance_summary` 表
2. 在 `scheduler.py` 中添加月度汇总任务（每月1号计算上月）
3. 实现绩效评分算法

#### 阶段 3：看板展示（1 周）

1. 在 AI 表格中添加「绩效」表
2. 实现排名查询接口
3. 添加绩效趋势图表

### 六、与现有系统的对接点

```
现有模块                           绩效扩展需要做的修改
─────────────────────────────────────────────────────────────
config.py                         新增绩效相关配置参数
db.py                             新增表定义 + 查询方法
workers/scheduler.py              新增 scan_performance 方法
pipeline.py                       在 _handle_complete 中采集数据
workers/aitable_sync.py           新增绩效表同步逻辑
```

### 七、绩效数据查询示例

```python
# 查询某工程师某月绩效
db.get_performance_summary(user_id='工程总监A的userId', period='2026-08')

# 查询团队排名
db.get_performance_ranking(user_role='ENGINEER', period='2026-08', 
                          order_by='performance_score DESC')

# 查询绩效趋势
db.get_performance_trend(user_id='xxx', months=6)
```

---

## 附录：关键配置参考

```bash
# .env 配置
RESPONSE_SLA_ENABLED=true
RESPONSE_SLA_FIRST_HOURS=1
RESPONSE_SLA_ESCALATE_HOURS=4
RESPONSE_SLA_ENGINEER_ESCALATE_USER_ID=900000000000000001
RESPONSE_SLA_EFFECTIVE_FROM=2026-08-26 15:00:00

# 时效 SLA 配置
SLA_REMIND_BEFORE_HOURS=6
SLA_SCAN_INTERVAL_SECONDS=60

# 群配置 (data/groups.json)
# 每个群包含：
# - group_id: 钉钉群 ID
# - store_name: 门店名称
# - manager_ids: 店长 userId 列表
# - engineer_ids: 工程师 userId 列表
# - engineering_leader_id: 工程负责人 userId
# - regional_manager_id: 区域经理 userId
```
