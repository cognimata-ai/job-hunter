import uuid
from typing import NoReturn

import pytest
from jobhunter.worker.main import exit_unless_lock_held

from tests.support.logs import logged


class Exited(Exception):
    pass


def recording_exit(codes: list[int]):
    def exit(code: int) -> NoReturn:
        codes.append(code)
        raise Exited

    return exit


class Session:
    def __init__(self, outcome: bool | Exception) -> None:
        self.outcome = outcome
        self.calls: list[tuple[uuid.UUID, int]] = []

    def heartbeat(self, instance_id: uuid.UUID, epoch: int) -> bool:
        self.calls.append((instance_id, epoch))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.mark.parametrize(
    ("outcome", "error_class"), [(False, None), (OSError("detail that must not be logged"), "OSError")]
)
def test_a_process_that_cannot_prove_it_holds_the_lock_exits_70(caplog, outcome, error_class) -> None:
    me, codes = uuid.uuid4(), list[int]()
    session = Session(outcome)
    with pytest.raises(Exited):
        exit_unless_lock_held(session, me, 4, exit=recording_exit(codes))
    assert codes == [70] and session.calls == [(me, 4)]
    (event,) = logged(caplog, "worker_lock_lost")
    assert (event["epoch"], event["error_class"], event["exit_code"]) == (4, error_class, 70)
    assert "must not be logged" not in caplog.text


def test_the_lock_holder_goes_on_to_its_teardown(caplog) -> None:
    codes: list[int] = []
    exit_unless_lock_held(Session(True), uuid.uuid4(), 4, exit=recording_exit(codes))
    assert codes == [] and logged(caplog, "worker_lock_lost") == []
