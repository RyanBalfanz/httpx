import collections
import itertools
import typing

import pytest

import httpx
from httpx.config import TimeoutTypes
from httpx.dispatch.base import AsyncDispatcher


def test_retries_config() -> None:
    client = httpx.AsyncClient()
    assert client.retries == httpx.Retries(0)
    assert client.retries.limit == 0

    client = httpx.AsyncClient(retries=3)
    assert client.retries.limit == 3
    assert client.retries.backoff_factor == 0.2

    client = httpx.AsyncClient(retries=httpx.Retries(2, backoff_factor=0.1))
    assert client.retries == httpx.Retries(2, backoff_factor=0.1)
    assert client.retries.limit == 2
    assert client.retries.backoff_factor == 0.1


@pytest.mark.parametrize(
    "retries, delays",
    [
        (httpx.Retries(3), [0, 0.2, 0.4, 0.8, 1.6]),
        (httpx.Retries(3, backoff_factor=0.1), [0, 0.1, 0.2, 0.4, 0.8]),
    ],
)
def test_retries_delays_sequence(
    retries: httpx.Retries, delays: typing.List[int]
) -> None:
    sample_delays = list(itertools.islice(retries.get_delays(), 5))
    assert sample_delays == delays


class MockDispatch(AsyncDispatcher):
    _ENDPOINTS: typing.Dict[str, typing.Tuple[typing.Type[httpx.HTTPError], bool]] = {
        "/connect_timeout": (httpx.ConnectTimeout, True),
        "/pool_timeout": (httpx.PoolTimeout, True),
        "/network_error": (httpx.NetworkError, True),
        "/read_timeout": (httpx.ReadTimeout, False),
    }

    def __init__(self, succeed_after: int) -> None:
        self.succeed_after = succeed_after
        self.attempts: typing.DefaultDict[str, int] = collections.defaultdict(int)

    async def send(
        self, request: httpx.Request, timeout: TimeoutTypes = None
    ) -> httpx.Response:
        if request.url.path not in self._ENDPOINTS:
            return httpx.Response(httpx.codes.OK, request=request)

        exc_cls, is_retryable = self._ENDPOINTS[request.url.path]

        if not is_retryable:
            raise exc_cls(request=request)

        if self.attempts[request.url.path] < self.succeed_after:
            self.attempts[request.url.path] += 1
            raise exc_cls(request=request)

        return httpx.Response(httpx.codes.OK, request=request)


@pytest.mark.usefixtures("async_environment")
async def test_no_retries() -> None:
    client = httpx.AsyncClient(dispatch=MockDispatch(succeed_after=1))

    response = await client.get("https://example.com")
    assert response.status_code == 200

    with pytest.raises(httpx.ConnectTimeout):
        await client.get("https://example.com/connect_timeout")

    with pytest.raises(httpx.PoolTimeout):
        await client.get("https://example.com/pool_timeout")

    with pytest.raises(httpx.NetworkError):
        await client.get("https://example.com/network_error")


@pytest.mark.usefixtures("async_environment")
async def test_client_retries() -> None:
    client = httpx.AsyncClient(dispatch=MockDispatch(succeed_after=3), retries=3)

    response = await client.get("https://example.com")
    assert response.status_code == 200

    response = await client.get("https://example.com/connect_timeout")
    assert response.status_code == 200

    response = await client.get("https://example.com/pool_timeout")
    assert response.status_code == 200

    response = await client.get("https://example.com/network_error")
    assert response.status_code == 200


@pytest.mark.usefixtures("async_environment")
async def test_request_retries() -> None:
    client = httpx.AsyncClient(dispatch=MockDispatch(succeed_after=3))
    response = await client.get("https://example.com", retries=3)
    assert response.status_code == 200

    client = httpx.AsyncClient(dispatch=MockDispatch(succeed_after=3), retries=3)
    with pytest.raises(httpx.ConnectTimeout):
        await client.get("https://example.com/connect_timeout", retries=None)


@pytest.mark.usefixtures("async_environment")
async def test_too_many_retries() -> None:
    client = httpx.AsyncClient(dispatch=MockDispatch(succeed_after=2), retries=1)

    response = await client.get("https://example.com")
    assert response.status_code == 200

    with pytest.raises(httpx.TooManyRetries):
        await client.get("https://example.com/connect_timeout")

    with pytest.raises(httpx.TooManyRetries):
        await client.get("https://example.com/pool_timeout")

    with pytest.raises(httpx.TooManyRetries):
        await client.get("https://example.com/network_error")


@pytest.mark.usefixtures("async_environment")
async def test_exception_not_retryable() -> None:
    # If ReadTimeout was retryable, the call would succeed after one retry, but
    # it shouldn't.
    client = httpx.AsyncClient(dispatch=MockDispatch(succeed_after=1), retries=1)
    with pytest.raises(httpx.ReadTimeout):
        await client.get("https://example.com/read_timeout")

    # If ReadTimeout was retryable, this call would fail with 'TooManyRetries',
    # but it shouldn't.
    client = httpx.AsyncClient(dispatch=MockDispatch(succeed_after=2), retries=1)
    with pytest.raises(httpx.ReadTimeout):
        await client.get("https://example.com/read_timeout")


@pytest.mark.usefixtures("async_environment")
@pytest.mark.parametrize("method", ["HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE"])
async def test_retries_idempotent_methods(method: str) -> None:
    client = httpx.AsyncClient(dispatch=MockDispatch(succeed_after=1), retries=3)
    response = await client.request(method, "https://example.com/connect_timeout")
    assert response.status_code == 200


@pytest.mark.usefixtures("async_environment")
async def test_non_idempotent_methods_not_retryable() -> None:
    client = httpx.AsyncClient(dispatch=MockDispatch(succeed_after=1), retries=3)

    # These would succeed if POST or PATCH triggered retries, but they shouldn't.
    with pytest.raises(httpx.ConnectTimeout):
        await client.post("https://example.com/connect_timeout")
    with pytest.raises(httpx.PoolTimeout):
        await client.patch("https://example.com/pool_timeout")


@pytest.mark.usefixtures("async_environment")
@pytest.mark.parametrize(
    "retries, elapsed",
    [
        (3, pytest.approx(0 + 0.2 + 0.4, rel=0.1)),
        (httpx.Retries(3, backoff_factor=0.1), pytest.approx(0 + 0.1 + 0.2, rel=0.2)),
    ],
)
async def test_retries_backoff(retries: httpx.Retries, elapsed: float) -> None:
    client = httpx.AsyncClient(dispatch=MockDispatch(succeed_after=3), retries=retries)
    response = await client.get("https://example.com/connect_timeout")
    assert response.status_code == 200
    assert response.elapsed.total_seconds() == elapsed
