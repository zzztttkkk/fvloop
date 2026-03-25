import heapq

from fvloop.loop import loop
from fvloop.future import Future
from fvloop.errors import CanceledError


class Timer:
    def __init__(self, fut: Future[None], expire: int):
        self._task = fut
        self._expire = expire
        self._canceled = False

        loop()._timers.add(self)

    def __lt__(self, other):
        return self._expire < other._expire

    def cancel(self):
        self._task.err(CanceledError())
        self._canceled = True


class TimerManager:
    def __init__(self):
        self._timers: list[Timer] = []

    def add(self, timer: Timer):
        heapq.heappush(self._timers, timer)

    def empty(self) -> bool:
        return len(self._timers) == 0

    def once(self, now: int):
        while self._timers and self._timers[0]._expire <= now:
            timer = heapq.heappop(self._timers)
            if timer._canceled:
                continue
            timer._task.ok(None)
