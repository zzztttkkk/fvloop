class LoopError(Exception):
    pass


class CanceledError(LoopError):
    pass


class ClosedError(LoopError):
    pass
