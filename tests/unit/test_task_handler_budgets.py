"""The handler-timeout override and the parking set come from one declaration.

A handler that parks on another queued task needs both a longer timeout and an
exclusion from the reserved workers, and the two must never drift apart: a
parking handler with the timeout but not the exclusion would occupy the very
capacity that has to run the task releasing it.
"""

from family_assistant.task_worker import (
    DEFAULT_TASK_HANDLER_BUDGETS,
    DEFAULT_TASK_HANDLER_TIMEOUT_OVERRIDES,
    PARKING_TASK_TYPES,
    TaskHandlerBudget,
    handler_timeouts_from_budgets,
    parking_task_types_from_budgets,
)


def test_shipped_constants_are_derived_from_the_budget_table() -> None:
    """The shipped timeout overrides and parking set read the same table."""
    assert (
        handler_timeouts_from_budgets(DEFAULT_TASK_HANDLER_BUDGETS)
        == DEFAULT_TASK_HANDLER_TIMEOUT_OVERRIDES
    )
    assert (
        parking_task_types_from_budgets(DEFAULT_TASK_HANDLER_BUDGETS)
        == PARKING_TASK_TYPES
    )


def test_declaring_a_parking_type_gives_it_both_a_timeout_and_an_exclusion() -> None:
    """One entry is all a future parking handler needs."""
    budgets = {
        **DEFAULT_TASK_HANDLER_BUDGETS,
        "waits_for_a_sibling": TaskHandlerBudget(
            timeout=900, parks_on_queued_work=True
        ),
    }

    assert handler_timeouts_from_budgets(budgets)["waits_for_a_sibling"] == 900
    assert "waits_for_a_sibling" in parking_task_types_from_budgets(budgets)


def test_a_longer_budget_alone_does_not_park() -> None:
    """A task type that is merely slow still runs on a reserved worker."""
    budgets = {"just_slow": TaskHandlerBudget(timeout=900)}

    assert handler_timeouts_from_budgets(budgets) == {"just_slow": 900}
    assert parking_task_types_from_budgets(budgets) == frozenset()


def test_the_delegated_run_is_declared_as_parking() -> None:
    """The confirmation-gated delegated run is the parking handler we ship."""
    assert "delegated_profile_run" in PARKING_TASK_TYPES
    assert DEFAULT_TASK_HANDLER_TIMEOUT_OVERRIDES["delegated_profile_run"] == 600
