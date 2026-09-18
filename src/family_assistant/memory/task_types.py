"""The queue identities of the memory review work.

Kept apart from the handlers so that naming a task type costs no imports. The
task worker declares a handler budget for the review, and the review and sweep
handlers reach the tasks repository, which the worker is itself reached from --
a module that only names things breaks that loop.
"""

MEMORY_REVIEW_SWEEP_TASK_TYPE = "memory_review_sweep"
MEMORY_REVIEW_SWEEP_TASK_ID = "system_memory_review_sweep"
MEMORY_REVIEW_TASK_TYPE = "memory_review"


def memory_review_task_id(interface_type: str, conversation_id: str) -> str:
    """The one task id a review of this conversation may hold.

    Deterministic on purpose: the queue's uniqueness on it is what serialises
    reviews of a conversation.
    """
    return f"memory_review:{interface_type}:{conversation_id}"
