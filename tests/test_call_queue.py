import pytest

from app.call_queue import CallQueue
from app.errors import AppError


def test_calls_wait_in_fifo_order_and_can_leave_without_losing_place():
    queue = CallQueue(capacity=2)
    assert queue.join("first") == 0
    assert queue.join("second") == 1
    assert queue.join("third") == 2
    assert queue.join("second") == 1
    with pytest.raises(AppError) as error:
        queue.join("fourth")
    assert error.value.code == "call_queue_full"
    queue.release("second")
    assert queue.position("third") == 1
    assert queue.release("first") == "third"
    assert queue.position("third") == 0
