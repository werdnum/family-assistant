"""Fixture: reads that filter on raw visibility grants are flagged."""

from family_assistant.tools.types import ToolExecutionContext


def admit_a_row_on_grants_alone(
    exec_context: ToolExecutionContext, labels: list[str]
) -> bool:
    """A subset check against grants admits every unlabelled row."""
    if exec_context.visibility_grants is None:
        return True
    return set(labels) <= exec_context.visibility_grants


def hoist_the_grants_into_a_local(exec_context: ToolExecutionContext) -> set[str]:
    """Naming them first does not make them a policy."""
    grants = exec_context.visibility_grants
    return grants or set()


def pass_the_grants_to_a_query_layer(
    exec_context: ToolExecutionContext,
) -> dict[str, object]:
    """Handing grants to a search layer confines half of what a policy would."""
    return {"visibility_grants": exec_context.visibility_grants}
