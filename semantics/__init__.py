"""语义协议与路由模块（v4.0）。

子模块：
- types：共享接口契约类型（计划书 15.1）
- protocol_loader：运行时协议加载与校验（Task 1）
- protocol_compiler：业务源 JSON → 运行时协议（Task 1）
- keyword_matcher：确定性关键词匹配（Task 2）。**不在运行时决策链路中**——
  关键词快路径已永久停用（2026-09-16），本模块仅供离线评测与单测使用。
- validator：语义决策校验（Task 2）
- evaluator：离线评测器（Task 3）
"""
