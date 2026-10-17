"""An injected monotonic clock for tests: it moves only when the test moves it."""


class FakeClock:
    """A `Callable[[], float]` clock reading `now`; the test sets or advances `now` itself."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now
