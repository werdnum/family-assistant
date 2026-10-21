"""A scripted async callable that records every call made to it.

Stands in for a single SDK coroutine method (patched in with
``patch.object(obj, "method", new=RecordingAsyncCall(...))``) when the test
needs to see what was sent and choose what comes back, without a mock that
accepts any attribute or signature.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RecordedCall:
    """The arguments of one call."""

    args: tuple[object, ...]
    kwargs: dict[str, object]


class RecordingAsyncCall[T]:
    """Return (or raise) the scripted outcomes in order, recording each call.

    A call beyond the script fails, so an unexpected extra call is visible.
    """

    def __init__(self, *outcomes: T | Exception) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[RecordedCall] = []

    async def __call__(self, *args: object, **kwargs: object) -> T:
        self.calls.append(RecordedCall(args=args, kwargs=kwargs))
        if not self._outcomes:
            raise AssertionError(
                f"Unscripted call #{len(self.calls)} with kwargs {sorted(kwargs)}"
            )
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def last_call(self) -> RecordedCall:
        assert self.calls, "No calls were made"
        return self.calls[-1]
