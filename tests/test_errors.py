"""Tests for the canonical error types."""

from sdk.adapter_api.errors import (
    AdapterError,
    AuthFailedError,
    SafetyBlockedError,
    TimeoutError,
    UnreachableError,
    VerifyFailedError,
)


class TestAdapterErrors:
    def test_base_error(self):
        err = AdapterError("test error")
        assert str(err) == "test error"
        assert err.code == "ADAPTER_ERROR"
        assert err.retryable is False

    def test_unreachable(self):
        err = UnreachableError("host down", native={"ip": "1.2.3.4"})
        assert err.code == "UNREACHABLE"
        assert err.retryable is True
        d = err.to_dict()
        assert d["code"] == "UNREACHABLE"
        assert d["native"]["ip"] == "1.2.3.4"

    def test_auth_failed(self):
        err = AuthFailedError("bad password")
        assert err.retryable is False

    def test_timeout(self):
        err = TimeoutError("5s exceeded")
        assert err.retryable is True

    def test_safety_blocked(self):
        err = SafetyBlockedError("S5 forbidden")
        assert err.code == "SAFETY_BLOCKED"

    def test_verify_failed(self):
        err = VerifyFailedError("state mismatch", retryable=True)
        assert err.retryable is True

    def test_retryable_override(self):
        err = UnreachableError("permanent", retryable=False)
        assert err.retryable is False
