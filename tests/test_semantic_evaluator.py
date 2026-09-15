"""离线评测器测试（计划书 Task 3）。

TDD 驱动：先写会失败的指标计算测试，再实现 evaluator.py。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


_BLIND_LABEL_SHA256 = "17fa6902a0374111a587aaee13a5dc529cc1fa2d6d75a54266805f2516c8416b"


# ───────────────────────── 辅助 ─────────────────────────


def _load_dataset(name: str = "semantic_cases.json") -> list[dict[str, Any]]:
    import json
    p = Path(__file__).resolve().parent / "fixtures" / name
    return json.loads(p.read_text(encoding="utf-8"))


def _load_protocol() -> Any:
    p = Path(__file__).resolve().parent.parent / "protocols" / "ticket_semantics.v4.json"
    from semantics.protocol_loader import load_protocol
    return load_protocol(p)


# ───────────────────────── 失败测试 ─────────────────────────


def test_evaluator_counts_wrong_intent_as_error():
    """意图预测错误被正确统计。"""
    from semantics.evaluator import evaluate, LabeledCase, EvalPrediction

    cases = [LabeledCase(id="c1", text="#报修 主题：门", expected_intent="ticket.create")]
    preds = [EvalPrediction(id="c1", predicted_intent="ticket.complete", predicted_fields={})]
    report = evaluate(cases, preds)
    assert report.intent_accuracy < 1.0


def test_evaluator_counts_correct_intent():
    """意图预测正确得分 1.0。"""
    from semantics.evaluator import evaluate, LabeledCase, EvalPrediction

    cases = [LabeledCase(id="c1", text="#报修 主题：门", expected_intent="ticket.create")]
    preds = [EvalPrediction(id="c1", predicted_intent="ticket.create", predicted_fields={})]
    report = evaluate(cases, preds)
    assert report.intent_accuracy == 1.0


def test_evaluator_counts_field_accuracy():
    """字段抽取正确率被计算。"""
    from semantics.evaluator import evaluate, LabeledCase, EvalPrediction

    cases = [
        LabeledCase(id="c1", text="msg", expected_intent="ticket.create",
                     expected_fields={"subject": "门", "sla": "3天"}),
    ]
    preds = [
        EvalPrediction(id="c1", predicted_intent="ticket.create",
                       predicted_fields={"subject": "门", "sla": "1天"}),
    ]
    report = evaluate(cases, preds)
    # 主题正确，时效错误 → field_accuracy < 1.0
    assert 0.0 < report.field_accuracy < 1.0


def test_evaluator_penalizes_hallucinated_fields():
    """预测额外字段应降低字段准确率。"""
    from semantics.evaluator import evaluate, LabeledCase, EvalPrediction

    cases = [
        LabeledCase(
            id="c1",
            text="msg",
            expected_intent="ticket.create",
            expected_fields={"subject": "门"},
        )
    ]
    preds = [
        EvalPrediction(
            id="c1",
            predicted_intent="ticket.create",
            predicted_fields={"subject": "门", "location": "大厅"},
        )
    ]
    assert evaluate(cases, preds).field_accuracy == 0.5


def test_wrong_intent_does_not_receive_field_credit():
    """意图错误时，即使字段文本碰巧一致也不计字段正确。"""
    from semantics.evaluator import evaluate, LabeledCase, EvalPrediction

    cases = [
        LabeledCase(
            id="c1",
            text="msg",
            expected_intent="ticket.create",
            expected_fields={"subject": "门"},
        )
    ]
    preds = [
        EvalPrediction(
            id="c1",
            predicted_intent="ticket.complete",
            predicted_fields={"subject": "门"},
        )
    ]
    assert evaluate(cases, preds).field_accuracy == 0.0


def test_evaluator_counts_wrong_auto_route_as_error():
    """意图正确但自动路由到错误工单时路由精确率为零。"""
    from semantics.evaluator import evaluate, LabeledCase, EvalPrediction

    cases = [
        LabeledCase(
            id="c1",
            text="msg",
            expected_intent="ticket.complete",
            expected_ticket_no="T1",
        )
    ]
    preds = [
        EvalPrediction(
            id="c1",
            predicted_intent="ticket.complete",
            predicted_ticket_no="T2",
        )
    ]
    assert evaluate(cases, preds).routing_precision == 0.0


def test_evaluator_handles_empty_dataset():
    """空数据集不崩溃。"""
    from semantics.evaluator import evaluate, LabeledCase, EvalPrediction
    report = evaluate([], [])
    assert report.intent_accuracy == 0.0
    assert report.field_accuracy is None
    assert report.routing_precision is None


def test_evaluator_reports_field_precision_recall_and_coverage():
    """字段指标只统计有字段标签的用例，并区分精确率与召回率。"""
    from semantics.evaluator import evaluate, LabeledCase, EvalPrediction

    cases = [
        LabeledCase(
            id="c1",
            text="msg",
            expected_intent="ticket.create",
            expected_fields={"subject": "门", "sla": "3天"},
        ),
        LabeledCase(id="c2", text="msg", expected_intent="chat.ignore"),
    ]
    predictions = [
        EvalPrediction(
            id="c1",
            predicted_intent="ticket.create",
            predicted_fields={"subject": "门", "location": "大厅"},
        ),
        EvalPrediction(
            id="c2",
            predicted_intent="chat.ignore",
            predicted_fields={"subject": "不应参与字段评分"},
        ),
    ]

    report = evaluate(cases, predictions)
    assert report.field_accuracy == pytest.approx(1 / 3)
    assert report.field_precision == 0.5
    assert report.field_recall == 0.5
    assert report.field_f1 == 0.5
    assert report.field_coverage == 0.5


def test_evaluator_unlabeled_fields_are_not_counted_as_wrong():
    """无字段标签的数据集不应把模型输出字段误判为错误。"""
    from semantics.evaluator import evaluate, LabeledCase, EvalPrediction

    cases = [LabeledCase(id="c1", text="msg", expected_intent="chat.ignore")]
    predictions = [
        EvalPrediction(
            id="c1",
            predicted_intent="chat.ignore",
            predicted_fields={"subject": "门"},
        )
    ]

    report = evaluate(cases, predictions)
    assert report.field_accuracy is None
    assert report.field_precision is None
    assert report.field_recall is None
    assert report.field_f1 is None
    assert report.field_coverage == 0.0


def test_evaluator_reports_routing_coverage():
    """路由覆盖率与已路由样本的精确率分别统计。"""
    from semantics.evaluator import evaluate, LabeledCase, EvalPrediction

    cases = [
        LabeledCase(
            id="c1",
            text="msg",
            expected_intent="ticket.complete",
            expected_ticket_no="T1",
        ),
        LabeledCase(
            id="c2",
            text="msg",
            expected_intent="ticket.complete",
            expected_ticket_no="T2",
        ),
    ]
    predictions = [
        EvalPrediction(
            id="c1",
            predicted_intent="ticket.complete",
            predicted_ticket_no="T1",
        ),
        EvalPrediction(id="c2", predicted_intent="ticket.complete"),
    ]

    report = evaluate(cases, predictions)
    assert report.routing_precision == 1.0
    assert report.routing_coverage == 0.5


def test_evaluator_rejects_id_mismatch():
    """ID 不匹配的预测应被拒绝。"""
    from semantics.evaluator import evaluate, LabeledCase, EvalPrediction

    cases = [LabeledCase(id="c1", text="msg", expected_intent="ticket.create")]
    preds = [EvalPrediction(id="c2", predicted_intent="ticket.complete", predicted_fields={})]
    with pytest.raises(ValueError, match="未知预测 ID"):
        evaluate(cases, preds)


def test_missing_prediction_counts_in_per_class_and_clarification():
    """漏预测必须计入分类别和歧义澄清指标分母。"""
    from semantics.evaluator import evaluate, LabeledCase, EvalPrediction

    cases = [
        LabeledCase(id="c1", text="msg", expected_intent="system.clarify"),
        LabeledCase(id="c2", text="msg", expected_intent="system.clarify"),
    ]
    preds = [EvalPrediction(id="c1", predicted_intent="system.clarify")]
    report = evaluate(cases, preds)
    assert report.ambiguity_clarification_rate == 0.5
    assert report.per_class["system.clarify"] == {"total": 2, "correct": 1}


def test_duplicate_prediction_ids_rejected():
    """重复预测 ID 不得静默覆盖。"""
    from semantics.evaluator import evaluate, LabeledCase, EvalPrediction

    cases = [LabeledCase(id="c1", text="msg", expected_intent="chat.ignore")]
    predictions = [
        EvalPrediction(id="c1", predicted_intent="chat.ignore"),
        EvalPrediction(id="c1", predicted_intent="ticket.create"),
    ]
    with pytest.raises(ValueError, match="重复预测 ID"):
        evaluate(cases, predictions)


def test_unexpected_prediction_ids_rejected():
    """数据集外的预测 ID 应被拒绝。"""
    from semantics.evaluator import evaluate, LabeledCase, EvalPrediction

    cases = [LabeledCase(id="c1", text="msg", expected_intent="chat.ignore")]
    predictions = [
        EvalPrediction(id="c1", predicted_intent="chat.ignore"),
        EvalPrediction(id="extra", predicted_intent="ticket.create"),
    ]
    with pytest.raises(ValueError, match="未知预测 ID"):
        evaluate(cases, predictions)


# ───────────────────────── 数据集集成测试 ─────────────────────────


def test_dataset_all_cases_have_required_fields():
    """所有标注用例都有必填字段。"""
    cases = _load_dataset("semantic_cases.json")
    assert len(cases) >= 10, f"至少 10 个标注用例，当前 {len(cases)}"
    for c in cases:
        assert "id" in c, f"缺少 id: {c}"
        assert "text" in c, f"缺少 text: {c}"
        assert "expected_intent" in c, f"缺少 expected_intent: {c}"


def test_dataset_covers_all_intent_types():
    """数据集覆盖 10 个工单动作及 5 个系统/闲聊动作。"""
    cases = _load_dataset("semantic_cases.json")
    intents = {c["expected_intent"] for c in cases}
    required = {
        "ticket.add_detail", "ticket.cancel", "ticket.complete", "ticket.create",
        "ticket.diagnosis.submit", "ticket.query", "ticket.reopen",
        "ticket.repair_plan.submit", "ticket.select", "ticket.timeout_reason.submit",
        "system.clarify", "system.confirm_pending_action",
        "system.correct_pending_action", "system.reject_pending_action", "chat.ignore",
    }
    missing = required - intents
    assert not missing, f"缺少意图类型: {missing}"


def test_dataset_covers_roles_and_multi_ticket_routing():
    """标注集包含越权角色、多候选和目标工单标签。"""
    cases = _load_dataset("semantic_cases.json")
    assert any(c.get("sender_role") == "OTHER" for c in cases)
    assert any(c.get("expected_validation_status") == "VALIDATION_REJECTED" for c in cases)
    assert any(len(c.get("candidates", [])) >= 2 for c in cases)
    assert any(c.get("expected_ticket_no") for c in cases)


def test_blind_dataset_structure():
    """盲测集结构与训练集一致。"""
    blind = _load_dataset("semantic_cases.blind.json")
    assert len(blind) >= 3, f"至少 3 个盲测用例，当前 {len(blind)}"
    for c in blind:
        assert "id" in c
        assert "text" in c
        assert "expected_intent" in c


def test_blind_role_restricted_actions_have_allowed_role():
    """盲测中的角色受限动作必须提供与协议一致的发送人角色。"""
    blind = _load_dataset("semantic_cases.blind.json")
    diagnosis = next(
        case for case in blind
        if case["expected_intent"] == "ticket.diagnosis.submit"
    )
    assert diagnosis.get("sender_role") == "ENGINEER"


def test_blind_dataset_labels_are_frozen():
    """盲测文本和标签摘要固定，修改时必须显式更新冻结基线。"""
    import hashlib
    import json

    blind = _load_dataset("semantic_cases.blind.json")
    frozen = [
        {
            "id": case["id"],
            "text": case["text"],
            "expected_intent": case["expected_intent"],
            "expected_fields": case.get("expected_fields", {}),
            "expected_ticket_no": case.get("expected_ticket_no"),
        }
        for case in blind
    ]
    payload = json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(payload.encode("utf-8")).hexdigest() == _BLIND_LABEL_SHA256


def test_evaluator_module_cli_outputs_report():
    """计划书规定的模块命令应真实执行离线评测。"""
    import subprocess
    import sys

    dataset = Path(__file__).resolve().parent / "fixtures" / "semantic_cases.json"
    completed = subprocess.run(
        [sys.executable, "-m", "semantics.evaluator", "--dataset", str(dataset)],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "intent_accuracy" in completed.stdout
    assert "routing_precision" in completed.stdout


def test_evaluator_module_cli_accepts_live_model_option():
    """Task 4 规定的真实模型 CLI 参数应被识别。"""
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-m", "semantics.evaluator", "--help"],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "--live-model" in completed.stdout


@pytest.mark.asyncio
async def test_live_model_evaluator_uses_classifier_predictions():
    """真实模型模式把 classifier 决策转换为统一评测报告。"""
    from semantics.evaluator import _run_live_model
    from semantics.types import SemanticDecision

    class FakeClassifier:
        async def classify(self, message, candidates, history=None):
            assert message.sender_role == "ENGINEER"
            assert [candidate.ticket_no for candidate in candidates] == ["T001"]
            return SemanticDecision(
                protocol_version="4.0.0",
                source="SEMANTIC_MODEL",
                intent="ticket.complete",
                target_ticket_no="T001",
                intent_confidence=0.95,
                fields={},
            )

    dataset = [{
        "id": "live-1",
        "text": "已经修好了",
        "sender_role": "ENGINEER",
        "expected_intent": "ticket.complete",
        "expected_fields": {},
        "expected_ticket_no": "T001",
        "candidates": [{
            "ticket_no": "T001",
            "subject": "门下沉",
            "location": "大厅",
            "problem_summary": "门体下沉",
            "status": "ACTIVE",
            "version": 3,
        }],
    }]
    report = await _run_live_model(dataset, classifier=FakeClassifier())
    assert report.intent_accuracy == 1.0
    assert report.routing_precision == 1.0


@pytest.mark.asyncio
async def test_run_eval_passes_dataset_candidates_to_classifier():
    """完整评测入口必须传递数据集中的候选快照。"""
    from semantics.run_eval import _run_classifier_on_cases
    from semantics.types import SemanticDecision

    class FakeClassifier:
        async def classify(self, message, candidates, history=None):
            assert message.sender_role == "MANAGER"
            assert [candidate.ticket_no for candidate in candidates] == ["T001", "T002"]
            return SemanticDecision(
                protocol_version="4.0.0",
                source="SEMANTIC_MODEL",
                intent="ticket.select",
                target_ticket_no="T002",
                intent_confidence=0.95,
                fields={},
            )

    cases = [{
        "id": "route-1",
        "text": "我选 T002",
        "sender_role": "MANAGER",
        "expected_intent": "ticket.select",
        "expected_ticket_no": "T002",
        "candidates": [
            {"ticket_no": "T001", "subject": "门", "status": "ACTIVE"},
            {"ticket_no": "T002", "subject": "空调", "status": "ACTIVE"},
        ],
    }]
    predictions, _ = await _run_classifier_on_cases(cases, FakeClassifier())
    assert predictions[0].predicted_ticket_no == "T002"


def test_run_eval_summary_formats_unavailable_metrics(capsys):
    """最终汇总遇到无字段标签指标时应输出 N/A 而不是崩溃。"""
    from semantics.evaluator import EvaluationReport
    from semantics.run_eval import _print_summary

    report = EvaluationReport(
        total_cases=1,
        matched_cases=1,
        intent_accuracy=1.0,
        field_accuracy=None,
        field_precision=None,
        field_recall=None,
        field_f1=None,
        field_coverage=0.0,
        create_precision=1.0,
        create_recall=1.0,
        false_create_rate=0.0,
        routing_precision=None,
        routing_coverage=0.0,
        ambiguity_clarification_rate=0.0,
    )

    _print_summary([("自然语言集", report)])

    assert "field_acc=N/A" in capsys.readouterr().out


# ───────────────────────── 关键词匹配器评测 ─────────────────────────


def test_keyword_matcher_on_labeled_dataset():
    """关键词匹配器在标注数据集上的表现可测量。"""
    from semantics.keyword_matcher import match_keyword
    from semantics.evaluator import evaluate, LabeledCase, EvalPrediction

    protocol = _load_protocol()
    raw_cases = _load_dataset("semantic_cases.json")

    cases: list[LabeledCase] = []
    preds: list[EvalPrediction] = []

    for c in raw_cases:
        cases.append(LabeledCase(
            id=c["id"],
            text=c["text"],
            expected_intent=c["expected_intent"],
            expected_fields=c.get("expected_fields", {}),
        ))
        result = match_keyword(c["text"], protocol)
        if result is not None:
            preds.append(EvalPrediction(
                id=c["id"],
                predicted_intent=result.intent,
                predicted_fields=result.fields,
            ))
        else:
            preds.append(EvalPrediction(
                id=c["id"],
                predicted_intent="chat.ignore",
                predicted_fields={},
            ))

    report = evaluate(cases, preds)
    # 关键词匹配器对显式关键词样本应达到 100% 意图准确率
    keyword_cases = [c for c in raw_cases if c.get("has_keyword")]
    if keyword_cases:
        for kc in keyword_cases:
            assert any(
                p.id == kc["id"] and p.predicted_intent == kc["expected_intent"]
                for p in preds
            ), f"关键词用例 {kc['id']} 应命中: {kc['expected_intent']}"
    # 至少不应崩溃
    assert report.intent_accuracy >= 0.0
