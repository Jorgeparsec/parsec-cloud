import attr
import trio
from uuid import uuid4
from async_generator import asynccontextmanager
from structlog import get_logger
from urllib.parse import urlparse

from parsec.types import DeviceID
from parsec.crypto import SigningKey
from parsec.core.backend_connection2.cmds import BackendCmds
from parsec.core.backend_connection2.exceptions import BackendNotAvailable
from parsec.api.transport import BaseTransport, TransportError, PatateTCPTransport
from parsec.api.protocole import ProtocoleError, AnonymousClientHandshake, ClientHandshake


logger = get_logger()


async def _transport_factory(log, addr: str) -> BaseTransport:
    # TODO: handle ssl and websocket here
    parsed_addr = urlparse(addr)
    try:
        stream = await trio.open_tcp_stream(parsed_addr.hostname, parsed_addr.port)
        return PatateTCPTransport(stream)

    except OSError as exc:
        log.debug("Impossible to connect to backend", reason=exc)
        raise BackendNotAvailable() from exc


async def _do_handshade(
    log, transport: BaseTransport, device_id: DeviceID = None, signing_key: SigningKey = None
):
    if device_id and not signing_key:
        raise ValueError("Signing key is mandatory for non anonymous authentication")

    try:
        if not device_id:
            ch = AnonymousClientHandshake()
        else:
            ch = ClientHandshake(device_id, signing_key)
        challenge_req = await transport.recv()
        answer_req = ch.process_challenge_req(challenge_req)
        await transport.send(answer_req)
        result_req = await transport.recv()
        ch.process_result_req(result_req)
        log.debug("Connected")

    except TransportError as exc:
        log.debug("Connection lost during handshake", reason=exc)
        await transport.aclose()
        raise BackendNotAvailable() from exc

    except ProtocoleError as exc:
        log.warning("Handshake failed", reason=exc)
        await transport.aclose()
        raise


async def backend_cmds_connect(
    addr, device_id: DeviceID = None, signing_key: SigningKey = None
) -> BackendCmds:
    log = logger.bind(addr=addr, auth=device_id or "<anonymous>", id=uuid4().hex)
    transport = await _transport_factory(addr, log)
    _do_handshade(transport, device_id, signing_key, log)
    return BackendCmds(transport, log)


class BackendCmdsPool:
    def __init__(self, addr, device_id, signing_key, max):
        self.addr = addr
        self.device_id = device_id
        self.signing_key = signing_key
        self.conns = []
        self.lock = trio.Semaphore(max)

    @asynccontextmanager
    async def acquire(self, fresh=False):
        async with self.lock.acquire():
            try:
                conn = self.conns.pop()
            except IndexError:
                conn = await backend_cmds_connect(self.addr, self.device_id, self.signing_key)

            try:
                yield conn

            except TransportError:
                await conn.aclose()
                raise

            else:
                self.conns.append(conn)


@asynccontextmanager
async def backend_cmds_create_pool(
    addr: str, device_id: DeviceID = None, signing_key: SigningKey = None, max=10
):
    pool = BackendCmdsPool(addr, device_id, signing_key)
    try:
        yield pool

    finally:
        for conn in pool.conns:
            try:
                await conn.aclose()
            except TransportError:
                pass
