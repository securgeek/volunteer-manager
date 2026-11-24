"""Test setup for the scheduler suite.

Two jobs, both of which exist because `backend/` and `portal/` are separate applications that
happen to contain same-named modules.

1. Environment first. `portal_sync` reads its configuration at module import time, so whichever
   test module imports it first freezes those values. Setting them here — conftest is imported
   before any test module in this directory — makes the result independent of alphabetical test
   order rather than accidentally dependent on it.

2. Put this directory on sys.path so test modules can import the app's own modules.

The cross-app collision check lives in the repository-root conftest.py, because it must run
after collection rather than at conftest import time.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# Values the sync client captures at import. Any value works; they only need to be consistent
# between portal_sync and the portal's security module within one run.
os.environ.setdefault("EMAIL_HMAC_KEY", "0" * 64)
os.environ.setdefault("SYNC_KEY_ID", "test-key")
os.environ.setdefault("SYNC_SECRET", "s3cret-for-tests")
os.environ.setdefault("PORTAL_SYNC_URL", "https://portal.test")

if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


import pytest  # noqa: E402  (after the environment setup above, deliberately)


@pytest.fixture(autouse=True)
def _fresh_login_limits():
    """Every test starts with no failed sign-ins counted. All test clients share one address,
    so without this, failures in one module would throttle sign-ins in the next. Only once
    `routers.auth` is loaded: importing it here would freeze portal_sync's settings early."""
    auth = sys.modules.get("routers.auth")
    if auth is not None:
        auth.reset_login_limits()
    alerts = sys.modules.get("alerts")
    if alerts is not None:
        alerts.reset()
    yield
