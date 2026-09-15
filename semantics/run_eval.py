"""离线评测脚本（计划书 Task 4 步骤 2）。

用法::

    # 先设置环境变量
    export LLM_API_KEY="sk-xxx"
    export LLM_BASE_URL="https://api.openai.com/v1"
    export LLM_MODEL="gpt-4o-mini"

    # 跑全量评测（标注集 + 自然语言集 + 盲测集）
    python -m semantics.run_eval

    # 只跑标注集
    python -m semantics.run_eval --labeled-only

    # 只跑自然语言集（模型路径核心评测）
    python -m semantics.run_eval --natural-only

    # 只跑盲测集
    python -m semantics.run_eval --blind-only

    # 显示每条用例的模型原文输出
    python -m semantics.run_eval --verbose

输出:
- 控制台表格：每条用例的 intent 对比
- 评测报告：intent_accuracy / field_accuracy / create_precision / create_recall / false_create_rate
- 逐条详情（--verbose）
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

# 确保项目根在 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from semantics.model_client import OpenAICompatibleModelClient
from semantics.classifier import SemanticClassifier
from semantics.protocol_loader import load_protocol
from semantics.evaluator import (
    LabeledCase,
    EvalPrediction,
    EvaluationReport,
    _make_live_candidates,
    evaluate,
)
from semantics.keyword_matcher import match_keyword
from models import NormalizedMessage

from logger import get_logger

logger = get_logger(__name__)

# ───────────────────────── 路径 ─────────────────────────

_FIXTURES_DIR = _PROJECT_ROOT / "tests" / "fixtures"
_LABELED_PATH = _FIXTURES_DIR / "semantic_cases.json"
_NATURAL_PATH = _FIXTURES_DIR / "semantic_cases_natural.json"
_BLIND_PATH = _FIXTURES_DIR / "semantic_cases.blind.json"
_PROTOCOL_PATH = _PROJECT_ROOT / "protocols" / "ticket_semantics.v4.json"


def _format_metric(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.1%}"


# ───────────────────────── 数据加载 ─────────────────────────


def _load_cases(path: Path) -> list[dict[str, Any]]:
    """加载 JSON 用例文件，跳过 _说明 行。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [c for c in raw if "id" in c]


def _to_labeled_case(c: dict[str, Any]) -> LabeledCase:
    """JSON dict → LabeledCase。"""
    return LabeledCase(
        id=c["id"],
        text=c["text"],
        expected_intent=c["expected_intent"],
        expected_fields=c.get("expected_fields", {}),
        expected_ticket_no=c.get("expected_ticket_no"),
    )


def _make_message(c: dict[str, Any]) -> NormalizedMessage:
    """JSON dict → NormalizedMessage。"""
    return NormalizedMessage(
        message_id=c["id"],
        group_id="eval-group",
        sender_id="eval-sender",
        sender_name="评测",
        content=c["text"],
        sent_at=datetime.now(),
        sender_role=c.get("sender_role", "MANAGER"),
    )


# ───────────────────────── 评测核心 ─────────────────────────


async def _run_classifier_on_cases(
    cases: list[dict[str, Any]],
    classifier: SemanticClassifier,
    *,
    verbose: bool = False,
) -> tuple[list[EvalPrediction], list[dict[str, Any]]]:
    """逐条调模型，返回 (predictions, detail_rows)。

    detail_rows 每条含: id / text / expected / predicted / confidence / source / raw_fields / error
    """
    predictions: list[EvalPrediction] = []
    details: list[dict[str, Any]] = []

    total = len(cases)
    for i, c in enumerate(cases, 1):
        msg = _make_message(c)
        expected = c["expected_intent"]

        print(f"  [{i}/{total}] {c['id']}: ", end="", flush=True)

        try:
            decision = await classifier.classify(
                msg,
                candidates=_make_live_candidates(c),
            )
            predicted_intent = decision.intent
            confidence = decision.intent_confidence
            fields = dict(decision.fields)
            error = None
        except Exception as exc:
            predicted_intent = "ERROR"
            confidence = 0.0
            fields = {}
            error = str(exc)

        mark = "✓" if predicted_intent == expected else "✗"
        print(f"{mark} {predicted_intent} (conf={confidence:.2f})")

        predictions.append(EvalPrediction(
            id=c["id"],
            predicted_intent=predicted_intent,
            predicted_fields=fields,
            predicted_ticket_no=decision.target_ticket_no if error is None else None,
        ))

        details.append({
            "id": c["id"],
            "text": c["text"],
            "expected": expected,
            "predicted": predicted_intent,
            "correct": predicted_intent == expected,
            "confidence": confidence,
            "fields": fields,
            "error": error,
        })

        if verbose:
            print(f"        原文: {c['text'][:60]}")
            print(f"        期望: {expected}")
            print(f"        预测: {predicted_intent} (conf={confidence:.2f})")
            if fields:
                print(f"        字段: {json.dumps(fields, ensure_ascii=False)[:120]}")
            if error:
                print(f"        错误: {error}")

    return predictions, details


async def _run_keyword_on_cases(
    cases: list[dict[str, Any]],
    protocol: Any,
) -> list[EvalPrediction]:
    """用关键词匹配器跑同一批数据，作为基线对照。"""
    predictions: list[EvalPrediction] = []
    for c in cases:
        msg_text = c["text"]
        decision = match_keyword(msg_text, protocol)
        if decision is not None:
            predicted = decision.intent
            fields = dict(decision.fields)
        else:
            predicted = "chat.ignore"
            fields = {}
        predictions.append(EvalPrediction(
            id=c["id"],
            predicted_intent=predicted,
            predicted_fields=fields,
            predicted_ticket_no=decision.target_ticket_no if decision is not None else None,
        ))
    return predictions


# ───────────────────────── 报告输出 ─────────────────────────


def _print_report(
    title: str,
    labeled: list[LabeledCase],
    pred: list[EvalPrediction],
    baseline_pred: list[EvalPrediction] | None = None,
) -> EvaluationReport:
    """打印评测报告，返回 EvaluationReport。"""
    report = evaluate(labeled, pred)

    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")
    print(f"  总用例数:     {report.total_cases}")
    print(f"  匹配预测数:   {report.matched_cases}")
    print(f"  意图准确率:   {report.intent_accuracy:.1%}")
    print(f"  字段准确率:   {_format_metric(report.field_accuracy)}")
    print(f"  字段精确率:   {_format_metric(report.field_precision)}")
    print(f"  字段召回率:   {_format_metric(report.field_recall)}")
    print(f"  字段 F1:      {_format_metric(report.field_f1)}")
    print(f"  字段标签覆盖: {report.field_coverage:.1%}")
    print(f"  建单精确率:   {report.create_precision:.1%}")
    print(f"  建单召回率:   {report.create_recall:.1%}")
    print(f"  误建单率:     {report.false_create_rate:.1%}")
    print(f"  路由精确率:   {_format_metric(report.routing_precision)}")
    print(f"  路由覆盖率:   {report.routing_coverage:.1%}")
    print(f"  歧义澄清率:   {report.ambiguity_clarification_rate:.1%}")

    if report.per_class:
        print(f"\n  分类别准确率:")
        for intent, stats in sorted(report.per_class.items()):
            acc = stats["correct"] / stats["total"] if stats["total"] > 0 else 0
            print(f"    {intent:30s}  {stats['correct']}/{stats['total']}  {acc:.0%}")

    if baseline_pred is not None:
        baseline_report = evaluate(labeled, baseline_pred)
        print(f"\n  [基线对照] 关键词匹配器:")
        print(f"    意图准确率:   {baseline_report.intent_accuracy:.1%}")
        print(f"    建单召回率:   {baseline_report.create_recall:.1%}")

    print()

    return report


def _print_detail_table(details: list[dict[str, Any]]) -> None:
    """打印逐条对比表。"""
    print(f"\n{'─' * 80}")
    print(f"  {'ID':<12} {'期望':<28} {'预测':<28} {'结果'}")
    print(f"{'─' * 80}")
    for detail in details:
        mark = "✓" if detail["correct"] else "✗"
        print(
            f"  {detail['id']:<12} {detail['expected']:<28} "
            f"{detail['predicted']:<28} {mark}"
        )
    print(f"{'─' * 80}")


def _print_summary(reports: list[tuple[str, EvaluationReport]]) -> None:
    """打印多数据集汇总，允许指标因无标签而不可用。"""
    print(f"\n{'=' * 60}")
    print("  汇总")
    print(f"{'=' * 60}")
    for name, report in reports:
        print(
            f"  {name:<12}  "
            f"intent_acc={report.intent_accuracy:.1%}  "
            f"field_acc={_format_metric(report.field_accuracy)}  "
            f"create_prec={report.create_precision:.1%}  "
            f"create_rec={report.create_recall:.1%}  "
            f"false_create={report.false_create_rate:.1%}"
        )
    print()


# ───────────────────────── 主入口 ─────────────────────────


async def main(
    *,
    labeled_only: bool = False,
    natural_only: bool = False,
    blind_only: bool = False,
    verbose: bool = False,
) -> None:
    """主评测流程。"""

    # 检查 API Key
    import os
    if not os.environ.get("LLM_API_KEY"):
        _env_file = _PROJECT_ROOT / ".env"
        print("⚠️  未设置 LLM_API_KEY，无法调用云端模型。")
        if not _env_file.exists():
            print(f"   请复制 .env.example 为 .env 并填入 API Key：")
            print(f"   cp {_env_file.name + '.example'} .env")
        else:
            print(f"   请编辑 {_env_file.name} 填入 LLM_API_KEY")
        sys.exit(1)

    # 加载协议
    print("加载协议...", end=" ")
    protocol = load_protocol(_PROTOCOL_PATH)
    print(f"v{protocol.protocol_version}, {len(protocol.actions)} 个动作")

    # 初始化模型客户端和分类器
    client = OpenAICompatibleModelClient()
    print(
        f"模型配置: base_url={client.base_url} model={client.model} "
        f"timeout={client.timeout_seconds}s configured={client.is_configured}"
    )
    classifier = SemanticClassifier(client=client, protocol=protocol)

    all_reports: list[tuple[str, EvaluationReport]] = []

    # ─── 标注集（含关键词用例）───
    if not natural_only and not blind_only:
        print(f"\n>>> 评测标注集 (keyword + natural)")
        cases = _load_cases(_LABELED_PATH)
        labeled = [_to_labeled_case(c) for c in cases]

        predictions, details = await _run_classifier_on_cases(
            cases, classifier, verbose=verbose,
        )
        baseline = await _run_keyword_on_cases(cases, protocol)

        _print_detail_table(details)
        report = _print_report("标注集 — 云端模型", labeled, predictions, baseline)
        all_reports.append(("标注集", report))

    # ─── 自然语言集（模型核心评测）───
    if not labeled_only and not blind_only:
        print(f"\n>>> 评测自然语言集 (模型核心 — 无关键词)")
        cases = _load_cases(_NATURAL_PATH)
        labeled = [_to_labeled_case(c) for c in cases]

        predictions, details = await _run_classifier_on_cases(
            cases, classifier, verbose=verbose,
        )
        baseline = await _run_keyword_on_cases(cases, protocol)

        _print_detail_table(details)
        report = _print_report("自然语言集 — 云端模型", labeled, predictions, baseline)
        all_reports.append(("自然语言集", report))

    # ─── 盲测集 ───
    if not labeled_only and not natural_only:
        print(f"\n>>> 评测盲测集 (blind)")
        cases = _load_cases(_BLIND_PATH)
        labeled = [_to_labeled_case(c) for c in cases]

        predictions, details = await _run_classifier_on_cases(
            cases, classifier, verbose=verbose,
        )
        baseline = await _run_keyword_on_cases(cases, protocol)

        _print_detail_table(details)
        report = _print_report("盲测集 — 云端模型", labeled, predictions, baseline)
        all_reports.append(("盲测集", report))

    # ─── 汇总 ───
    if all_reports:
        _print_summary(all_reports)


if __name__ == "__main__":
    import argparse

    # ── .env 自动加载（零依赖，纯标准库）──
    _ENV_PATH = _PROJECT_ROOT / ".env"
    if _ENV_PATH.exists():
        for _line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#"):
                continue
            if "=" not in _line:
                continue
            _key, _, _val = _line.partition("=")
            _key = _key.strip()
            _val = _val.strip().strip('"').strip("'")
            # 不覆盖已有的环境变量
            import os
            if _key and _key not in os.environ:
                os.environ[_key] = _val

    parser = argparse.ArgumentParser(description="云端模型离线评测")
    parser.add_argument("--labeled-only", action="store_true", help="只跑标注集")
    parser.add_argument("--natural-only", action="store_true", help="只跑自然语言集")
    parser.add_argument("--blind-only", action="store_true", help="只跑盲测集")
    parser.add_argument("--verbose", "-v", action="store_true", help="显示每条模型原文输出")
    args = parser.parse_args()

    asyncio.run(main(
        labeled_only=args.labeled_only,
        natural_only=args.natural_only,
        blind_only=args.blind_only,
        verbose=args.verbose,
    ))
