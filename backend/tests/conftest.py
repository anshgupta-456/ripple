"""Test environment: dev mode (ephemeral keys), data written to a temp dir, API clients pre-authenticated as an admin."""
import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="ripple-test-")
os.environ["RIPPLE_ENV"] = "dev"
os.environ["RIPPLE_DATA_DIR"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402

_orig_init = TestClient.__init__


def _init(self, *a, **k):
    _orig_init(self, *a, **k)
    from app.security.auth import issue_dev_token
    tok, _ = issue_dev_token("sam")
    self.headers["Authorization"] = f"Bearer {tok}"      # tests are authenticated as admin unless they say otherwise


TestClient.__init__ = _init


@pytest.fixture(autouse=True)
def _clean_security_state():
    from app.security.governance import KILL, LIMITER
    KILL.reset_for_tests()
    LIMITER.reset()
    yield
    KILL.reset_for_tests()
