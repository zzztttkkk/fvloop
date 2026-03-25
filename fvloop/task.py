import typing


from fvloop.errors import CanceledError
from fvloop.future import Future
from fvloop.loop import loop


T = typing.TypeVar("T")


class Task[T]:
    def __init__(self, coro: typing.Generator):
        self._coro = coro
        self._curfut: Future[typing.Any] | None = None
        self._canceled = False
        self._loop = loop()

        self._loop._calls.append(lambda: self._step())

    def _ondone(self):
        self._loop._tasks.remove(self)

    def _step(self, value: T | None = None, exc: Exception | None = None):
        if self._canceled:
            return

        try:
            fut: Future[typing.Any]
            if exc:
                fut = self._coro.throw(exc)
            else:
                fut = self._coro.send(typing.cast(None, value))

            if fut._done:
                self._loop._calls.append(lambda: self._wakeup(fut))
                return

            fut._cb = self._wakeup
            self._curfut = fut
        except StopIteration:
            self._ondone()

    def _wakeup(self, fut: Future[T]):
        if fut._exception:
            self._loop._calls.append(lambda: self._step(exc=fut._exception))
        else:
            self._loop._calls.append(lambda: self._step(value=fut._value))

    def cancel(self):
        self._canceled = True
        if self._curfut:
            self._curfut.err(CanceledError())

        self._ondone()


class Tasks:
    def __init__(self):
        self._tasks: set[Task] = set()

    def add(self, task: Task):
        self._tasks.add(task)

    def remove(self, task: Task):
        self._tasks.remove(task)
