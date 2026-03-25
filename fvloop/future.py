import typing


T = typing.TypeVar("T")


class Future[T]:
    _done: bool = False
    _value: T | None = None
    _exception: Exception | None = None

    _cb: typing.Callable[[Future[T]], None] | None = None

    def __init__(self): ...

    def _ondone(self):
        if self._cb:
            self._cb(self)

    def ok(self, value: T):
        self._done = True
        self._value = value
        self._ondone()

    def err(self, exception: Exception):
        self._done = True
        self._exception = exception
        self._ondone()
