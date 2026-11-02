from runtime.budget import ToolCallBudget


def test_budget_limits_total_and_each_tool() -> None:
    budget = ToolCallBudget(total_limit=2, per_tool_limit=1)

    assert budget.try_consume("Read") == (True, None)
    assert budget.try_consume("Read")[0] is False
    assert "Read" in (budget.try_consume("Read")[1] or "")
    assert budget.try_consume("Glob") == (True, None)
    allowed, message = budget.try_consume("Grep")
    assert allowed is False
    assert "总额度" in (message or "")


def test_budget_snapshot_round_trip_and_invalid_fallback() -> None:
    budget = ToolCallBudget(total_limit=5, per_tool_limit=2)
    budget.try_consume("Read")
    snapshot = budget.snapshot()

    restored = ToolCallBudget.from_snapshot(
        snapshot,
        default=ToolCallBudget(total_limit=1, per_tool_limit=1),
    )
    assert restored.total_limit == 5
    assert restored.per_tool_limit == 2
    assert restored.total_calls == 1
    assert restored.calls_by_tool == {"Read": 1}
    assert restored.remaining == 4

    fallback = ToolCallBudget(total_limit=3, per_tool_limit=1)
    assert ToolCallBudget.from_snapshot({"bad": object()}, default=fallback) is fallback
