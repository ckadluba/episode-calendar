import httpx
import pytest

from episode_calendar.importer.providers.http import is_resource_gone


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://player.example/summary/big-brother")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"Server error '{status_code}'", request=request, response=response
    )


def test_recognizes_a_direct_gone_response() -> None:
    assert is_resource_gone(_status_error(410)) is True


def test_recognizes_a_wrapped_gone_response() -> None:
    class ProviderHTTPError(RuntimeError):
        pass

    def wrapped() -> ProviderHTTPError:
        try:
            raise _status_error(410)
        except httpx.HTTPStatusError as exc:
            raise ProviderHTTPError("STV HTTP 410") from exc

    with pytest.raises(ProviderHTTPError) as captured:
        wrapped()

    assert is_resource_gone(captured.value) is True


def test_recognizes_a_provider_error_carrying_the_status_code() -> None:
    class ProviderHTTPError(RuntimeError):
        def __init__(self) -> None:
            super().__init__("Joyn HTTP 410")
            self.status_code = 410

    assert is_resource_gone(ProviderHTTPError()) is True


def test_ignores_other_failures() -> None:
    assert is_resource_gone(RuntimeError("provider unavailable")) is False
    assert is_resource_gone(_status_error(500)) is False
    assert is_resource_gone(_status_error(404)) is False
