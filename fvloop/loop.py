import threading
import time
import typing

from fvloop.selector import Selector
from fvloop.timer import TimerManager
from fvloop.task import Tasks, Task


class Loop:
    def __init__(self):
        self._selector = Selector()
        self._timers: TimerManager = TimerManager()
        self._tasks: Tasks = Tasks()
        self._calls: list[typing.Callable[[], None]] = []

        self._isrunning = False

    def empty(self) -> bool:
        return self._selector.empty() and self._timers.empty()

    def once(self):
        if self.empty():
            return

        timeout: float | None = None

        if self._calls.__len__() > 0:
            timeout = 0.0
        else:
            nextexpire = None
            if not self._timers.empty():
                nextexpire = self._timers._timers[0]._expire
                timeout = max(0, nextexpire - time.monotonic_ns()) / 1e9

        self._selector.select(timeout)

        now = time.monotonic_ns()

        self._timers.once(now)

        calls = self._calls
        self._calls = []
        for call in calls:
            call()

    def until(self):
        if self._isrunning:
            return

        self._isrunning = True

        while not self.empty():
            self.once()

    def forever(self):
        if self._isrunning:
            return
        self._isrunning = True

        while self._isrunning:
            self.once()
        self._isrunning = False

    def close(self):
        self._isrunning = False
        self._selector.close()

    def spawn(self, coro: typing.Generator):
        self._tasks.add(Task(coro))


class _Local(threading.local):
    def __init__(self):
        self.loop: Loop | None = None


__g = _Local()


def loop() -> Loop:
    if __g.loop:
        return __g.loop
    __g.loop = Loop()
    return __g.loop
