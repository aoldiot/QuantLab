import pytest
from fastapi import HTTPException

from app.models import ResearchStatus, RunStatus
from app.research import SPEC_FIELDS, _extract_text, _has_open_questions, _parse_json, _split_decisions
from app.runner import research_status_for_run


def test_extracts_text_from_hermes_responses_payload():
    payload = {
        "output": [
            {"type": "function_call", "name": "memory"},
            {"type": "message", "content": [{"type": "output_text", "text": "研究结论"}]},
        ]
    }
    assert _extract_text(payload) == "研究结论"


def test_parses_json_strategy_specification():
    content = {field: [] for field in SPEC_FIELDS}
    content.update(strategy_name="oi_reversal", title="OI反转", hypothesis="清算后反弹")
    value = _parse_json("说明文字\n```json\n" + __import__("json").dumps(content, ensure_ascii=False) + "\n```")
    assert value["strategy_name"] == "oi_reversal"


@pytest.mark.parametrize("value", ["not-json", '{"strategy_name":"Bad Name"}', "{}"])
def test_rejects_invalid_strategy_specification(value):
    with pytest.raises(HTTPException) as error:
        _parse_json(value)
    assert error.value.status_code == 422


def test_open_questions_block_specification_approval():
    assert _has_open_questions({"open_questions": ["退出条件尚未确定"]})
    assert not _has_open_questions({"open_questions": []})
    assert not _has_open_questions({"open_questions": ["  "]})


def test_accepts_open_question_that_user_can_decide():
    content = {field: [] for field in SPEC_FIELDS}
    content.update(
        strategy_name="kdj_filter",
        title="KDJ过滤",
        hypothesis="过滤震荡信号",
        open_questions=["是否加入 KDJ 过滤器（建议暂不加入，先建立基准模型）"],
    )
    value = _parse_json(__import__("json").dumps(content, ensure_ascii=False))
    assert value["open_questions"] == content["open_questions"]


def test_rejects_open_question_that_only_an_experiment_can_answer():
    content = {field: [] for field in SPEC_FIELDS}
    content.update(
        strategy_name="choppiness_filter",
        title="震荡过滤",
        hypothesis="过滤震荡信号",
        open_questions=["增加 Choppiness 过滤器能否有效提升胜率和盈亏比？"],
    )
    with pytest.raises(HTTPException) as error:
        _parse_json(__import__("json").dumps(content, ensure_ascii=False))
    assert "应由回测回答" in error.value.detail


DECISION_BLOCK = """判断：信号定义已足够，但退出方式尚未拍板。

## 建议
- 先固定入场信号

```quantlab-questions
[{"question": "是否加入 KDJ 过滤器", "options": ["加入", "暂不加入"], "recommendation": "暂不加入", "impact": "先保持基准模型简单"}]
```"""


def test_splits_decision_block_out_of_markdown_reply():
    content, decisions = _split_decisions(DECISION_BLOCK)
    assert "quantlab-questions" not in content
    assert content.endswith("先固定入场信号")
    assert len(decisions) == 1
    assert decisions[0] == {
        "question": "是否加入 KDJ 过滤器",
        "options": ["加入", "暂不加入"],
        "recommendation": "暂不加入",
        "impact": "先保持基准模型简单",
    }


def test_reply_without_block_raises_no_decision():
    """Producing decisions is optional: no block means nothing needs the user."""
    reply = "## 判断\n\n当前规则已经完整，没有需要你拍板的设计选项。"
    content, decisions = _split_decisions(reply)
    assert content == reply
    assert decisions == []


def test_malformed_decision_block_never_breaks_the_reply():
    content, decisions = _split_decisions("正文说明\n\n```quantlab-questions\n[{question: 坏 JSON,]\n```")
    assert content == "正文说明"
    assert decisions == []


def test_decision_block_drops_questions_only_an_experiment_can_answer():
    block = '```quantlab-questions\n[{"question": "加入 Choppiness 过滤器能否提升胜率", "options": ["是", "否"]}, {"question": "是否使用 4 小时周期", "options": ["是", "否"]}]\n```'
    _, decisions = _split_decisions(block)
    assert [item["question"] for item in decisions] == ["是否使用 4 小时周期"]


def test_decision_block_deduplicates_the_same_question():
    block = '```quantlab-questions\n[{"question": "是否加入 KDJ 过滤器？"}, {"question": "是否加入 KDJ  过滤器"}]\n```'
    _, decisions = _split_decisions(block)
    assert len(decisions) == 1
    assert decisions[0]["options"] == []
    assert decisions[0]["recommendation"] is None


@pytest.mark.parametrize(("run_status", "project_status"), [    (RunStatus.QUEUED, ResearchStatus.BACKTESTING),
    (RunStatus.RUNNING, ResearchStatus.BACKTESTING),
    (RunStatus.ANALYZING, ResearchStatus.BACKTESTING),
    (RunStatus.COMPLETED, ResearchStatus.READY_FOR_ANALYSIS),
    (RunStatus.FAILED, ResearchStatus.READY_FOR_BACKTEST),
    (RunStatus.CANCELED, ResearchStatus.READY_FOR_BACKTEST),
])
def test_research_status_tracks_backtest_lifecycle(run_status, project_status):
    assert research_status_for_run(run_status) == project_status
