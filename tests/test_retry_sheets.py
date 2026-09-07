"""Google Sheets 503s must be retried, not fatal.

On 2026-09-06 six of GTV's last thirty scheduled syncs died with
"APIError: [503]: The service is currently unavailable", and Arden,
OpenMaal and YRA the same way - usually on client.open(), before a single
row was read. with_retry was already wrapping that call, but gspread
raises its own APIError, which is neither TransientError nor a requests
exception, so nothing caught it. These pin both halves: a retryable
status is retried, and a real sheet error still fails immediately."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import retry_utils  # noqa: E402
from retry_utils import PermanentError, with_retry  # noqa: E402

APIError = pytest.importorskip("gspread.exceptions").APIError


class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.text = f"{status} from a stub"

    def json(self):
        return {"error": {"code": self.status_code, "message": "stub"}}


def _api_error(status):
    try:
        return APIError(_Resp(status))
    except TypeError:  # older/newer gspread signatures
        return APIError({"code": status, "message": "stub"})


def _flaky(errors, result="ok"):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= errors:
            raise _api_error(503)
        return result

    fn.calls = calls
    return fn


def test_503_is_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr(retry_utils.time, "sleep", lambda *_: None)
    fn = _flaky(2)
    assert with_retry(fn, what="sheet open", max_attempts=4) == "ok"
    assert fn.calls["n"] == 3


def test_503_every_time_still_raises_the_api_error(monkeypatch):
    monkeypatch.setattr(retry_utils.time, "sleep", lambda *_: None)
    fn = _flaky(99)
    with pytest.raises(APIError):
        with_retry(fn, what="sheet open", max_attempts=3)
    assert fn.calls["n"] == 3


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_every_retryable_sheet_status(monkeypatch, status):
    monkeypatch.setattr(retry_utils.time, "sleep", lambda *_: None)
    assert retry_utils._sheet_status(_api_error(status)) == status
    assert status in retry_utils._RETRYABLE_SHEET_STATUS


@pytest.mark.parametrize("status", [400, 403, 404])
def test_real_sheet_errors_fail_immediately(monkeypatch, status):
    """A missing sheet or a bad range is not going to fix itself - retrying
    it wastes the run's time and hides the cause."""
    monkeypatch.setattr(retry_utils.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _api_error(status)

    with pytest.raises(APIError):
        with_retry(fn, what="sheet open", max_attempts=4)
    assert calls["n"] == 1, "a permanent sheet error must not be retried"


def test_unrelated_exceptions_are_untouched(monkeypatch):
    monkeypatch.setattr(retry_utils.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ValueError("not a sheet problem")

    with pytest.raises(ValueError):
        with_retry(fn, what="whatever", max_attempts=4)
    assert calls["n"] == 1


def test_permanent_error_still_wins(monkeypatch):
    monkeypatch.setattr(retry_utils.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise PermanentError("bad credentials")

    with pytest.raises(PermanentError):
        with_retry(fn, what="auth", max_attempts=4)
    assert calls["n"] == 1
