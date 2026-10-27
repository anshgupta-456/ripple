"""Test environment: dev mode (ephemeral keys), data written to a temp dir, API clients pre-authenticated as an admin."""
import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="ripple-test-")
os.environ["RIPPLE_ENV"] = "dev"
os.environ["RIPPLE_DATA_DIR"] = _TMP
os.environ["RIPPLE_LLM_LOCAL"] = "off"      # tests never talk to a model server that happens to run on this laptop

from fastapi.testclient import TestClient  # noqa: E402

_orig_init = TestClient.__init__


def _init(self, *a, **k):
    _orig_init(self, *a, **k)
    from app.security.auth import AuthError, issue_dev_token
    try:
        tok, _ = issue_dev_token("sam")
        self.headers["Authorization"] = f"Bearer {tok}"      # tests are authenticated as admin unless they say otherwise
    except AuthError:                                        # a test switched to prod mode: no dev tokens exist there
        pass


TestClient.__init__ = _init


@pytest.fixture(autouse=True)
def _clean_security_state():
    from app.security.governance import KILL, LIMITER
    from app import main
    main.SESSIONS.clear()
    main.ACTIVE.clear()
    KILL.reset_for_tests()
    LIMITER.reset()
    from app.llm import client as llm_client
    llm_client.reset_state()                       # the provider circuit breaker is process-wide
    yield
    KILL.reset_for_tests()
