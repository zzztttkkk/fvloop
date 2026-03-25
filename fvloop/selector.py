from abc import ABCMeta, abstractmethod
import os
import selectors
import socket
import typing
from collections import deque

from fvloop.errors import ClosedError
from fvloop.future import Future


class SendBuf:
    buf: memoryview
    _future: Future[None]

    def __init__(self, buf: memoryview, future: Future[None]):
        self.buf = buf
        self._future = future


class Selector:
    def __init__(self):
        self._selector = selectors.DefaultSelector()
        self._sendbufs = set()

    def close(self):
        self._selector.close()

    def empty(self) -> bool:
        return len(self._selector.get_map()) == 0

    def register(self, fd, event, data):
        return self._selector.register(fd, event, data)

    def unregister(self, fd):
        return self._selector.unregister(fd)

    def modify(self, fd, event, data):
        return self._selector.modify(fd, event, data)

    def tcpserver(self, addr: tuple[str, int], protocol: AbsProtocol):
        sock = socket.create_server(addr)
        sock.setblocking(False)
        protocol._sock = sock
        self.register(
            sock.fileno(),
            selectors.EVENT_READ,
            protocol.onaccept,
        )

    def accept(self, protocol: AbsProtocol):
        conn, addr = protocol.sock.accept()
        conn.setblocking(False)

        cli = Conn()
        cli._addr = addr
        cli._sock = conn
        cli._selector = self
        cli._protocol = protocol
        if not protocol.onconn(cli):
            conn.close()
            return

        self.register(
            conn.fileno(),
            selectors.EVENT_READ,
            cli._dispatch,
        )

    def select(self, timeout: float | None = None):
        items = self._selector.select(timeout)
        for key, mask in items:
            data = key.data
            data(key, mask)


class ChainBuffer:
    def __init__(self):
        self._bytesize = 0
        self._bufs: deque[bytearray] = deque()

    def append(self, buf: bytearray):
        if not buf:
            return

        self._bufs.append(buf)
        self._bytesize += len(buf)

    def appendleft(self, buf: bytearray):
        if not buf:
            return

        self._bufs.appendleft(buf)
        self._bytesize += len(buf)

    def pop(self) -> bytearray:
        buf = self._bufs.popleft()
        self._bytesize -= len(buf)
        return buf

    def queuesize(self) -> int:
        return len(self._bufs)

    def __len__(self) -> int:
        return self._bytesize

    @property
    def bytesize(self) -> int:
        return self._bytesize

    def read(self, size: int):
        assert size > 0, "size must be greater than 0"
        buf = bytearray()
        while self.queuesize():
            required = size - len(buf)
            ele = self.pop()
            if len(ele) >= required:
                buf.extend(ele[:required])
                self.appendleft(ele[required:])
                return buf
            else:
                buf.extend(ele)

        return buf

    def peek(self, size: int) -> bytearray:
        assert size > 0, "size must be greater than 0"
        buf = bytearray()
        for ele in self._bufs:
            required = size - len(buf)
            if len(ele) >= required:
                buf.extend(ele[:required])
                return buf
            else:
                buf.extend(ele)

        return buf

    def writeto(self, buf: bytearray):
        for ele in self._bufs:
            buf.extend(ele)

        self._bufs.clear()
        self._bytesize = 0


class Conn:
    _addr: tuple[str, int]
    _sock: socket.socket
    _wbufs: deque[SendBuf]
    _selector: Selector
    _protocol: AbsProtocol
    _closed: bool
    _rfut: Future[None] | None
    _rbufs: ChainBuffer

    def __init__(self):
        self._wbufs = deque()
        self._closed = False
        self._rbufs = ChainBuffer()

    def _send(self):
        if not self._wbufs:
            return

        buf: SendBuf | None = None
        try:
            while self._wbufs:
                buf = self._wbufs[0]
                sl = self._sock.send(buf.buf)
                if sl == len(buf.buf):
                    self._wbufs.popleft()
                    buf._future.ok(None)
                    continue
                else:
                    buf.buf = buf.buf[sl:]
                    break

            self._selector.modify(
                self._sock.fileno(),
                selectors.EVENT_READ,
                self._dispatch,
            )
        except Exception as e:
            if isinstance(e, (BlockingIOError, InterruptedError)):
                return
            if buf:
                buf._future.err(e)
                self.close(e)

    def _dispatch(self, key: selectors.SelectorKey, mask: int):
        if mask & selectors.EVENT_READ:
            if self._rfut:
                self._rfut.ok(None)
                self._rfut = None
            self._protocol.ondata(self)
        if mask & selectors.EVENT_WRITE:
            self._send()

    def close(self, err: Exception | None = None):
        if self._closed:
            return
        self._closed = True

        for buf in self._wbufs:
            buf._future.err(err or Exception("close"))
        self._wbufs.clear()

        self._sock.close()
        self._selector.unregister(self._sock.fileno())

    def write(self, data: bytes) -> Future[None]:
        if self._closed:
            raise Exception("closed")

        data = memoryview(data)

        fut = Future[None]()
        self._wbufs.append(SendBuf(buf=data, future=fut))
        if len(self._wbufs) == 1:
            self._selector.modify(
                self._sock.fileno(),
                selectors.EVENT_READ | selectors.EVENT_WRITE,
                self._dispatch,
            )
        return fut

    def _doread(self):
        assert self._rfut is None, "read() must be called in order"

        fut = Future[None]()
        self._rfut = fut
        yield fut
        if fut._exception:
            raise fut._exception

        rbs = self._sock.recv(4096)
        if not rbs:
            raise ClosedError()
        self._rbufs.append(bytearray(rbs))

    def read(
        self, maxlen: int = 2048
    ) -> typing.Generator[Future[None], None, bytearray]:
        assert maxlen > 0, "maxlen must be greater than 0"

        buf = bytearray()
        while True:
            if self._rbufs:
                rl = len(buf)
                required = maxlen - rl
                if self._rbufs.bytesize >= required:
                    buf.extend(self._rbufs.read(required))
                    return buf
                self._rbufs.writeto(buf)
                return buf

            yield from self._doread()

    def readuntil(
        self, target: bytes, maxlen: int = 2048
    ) -> typing.Generator[Future[None], None, tuple[bytearray, bool]]:
        maxrl = max(maxlen, 1024)

        linebuf = bytearray()
        while True:
            buf = yield from self.read(maxrl)
            idx = buf.find(target)
            if idx > -1:
                end = idx + len(target)
                linebuf.extend(buf[:end])
                remain = buf[end:]
                self._rbufs.appendleft(remain)
                return linebuf, True

            linebuf.extend(buf)

            if maxlen > 0 and linebuf.__len__() >= maxlen:
                return linebuf, False

    def readexact(self, n: int) -> typing.Generator[Future[None], None, bytearray]:
        assert n > 0, "n must be greater than 0"
        buf = bytearray()
        while True:
            required = n - len(buf)
            rbuf = yield from self.read(4096)
            if rbuf.__len__() < required:
                buf.extend(rbuf)
                continue

            buf.extend(rbuf[:required])
            self._rbufs.appendleft(rbuf[required:])
            return buf

    def peek(self, n: int) -> typing.Generator[Future[None], None, bytearray]:
        assert n > 0, "n must be greater than 0"
        buf = bytearray()
        while True:
            required = n - len(buf)
            if self._rbufs.bytesize >= required:
                buf.extend(self._rbufs.peek(required))
                return buf

            yield from self._doread()


class AbsProtocol(metaclass=ABCMeta):
    _sock: socket.socket | None = None

    @property
    def sock(self) -> socket.socket:
        return typing.cast(socket.socket, self._sock)

    @abstractmethod
    def onaccept(self, key: selectors.SelectorKey, mask: int): ...

    @abstractmethod
    def onconn(self, cli: Conn) -> bool: ...

    @abstractmethod
    def ondata(self, cli: Conn): ...


if __name__ == "__main__":
    print("start", os.getpid())

    selector = Selector()

    class TestProtocol(AbsProtocol):
        def onaccept(self, key: selectors.SelectorKey, mask: int):
            selector.accept(self)

        def onconn(self, cli: Conn):
            return True

        def ondata(self, cli: Conn):
            data = cli._sock.recv(1024)
            print(data)

    selector.tcpserver(("127.0.0.1", 8080), TestProtocol())

    while True:
        selector.select()
