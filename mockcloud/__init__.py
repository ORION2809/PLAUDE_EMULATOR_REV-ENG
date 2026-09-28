"""plaud-harness Layer 4: a LOCAL mock of the Plaud partner cloud.

Everything this package issues is synthetic and labelled. It never proxies to,
fetches from, or otherwise contacts any real host: there is no HTTP client in
this package at all. tests/test_mockcloud_mock.py checks the source with an
AST scan for network imports and calls, and every mockcloud test runs with
outbound socket connects blocked; neither is a proof for code paths no test
exercises.

Evidence classes used in the code comments:

  DOC-EXACT        the path / field / status code is stated by the official
                   OpenAPI or docs page cited next to it
  BYTECODE_PROVEN  the path / field name is a literal in the shipped AAR
                   (build/evidence/javap/ALL.txt or jadx-out sources)
  OFFICIAL_SOURCE  read from the published template apps / demo backends
  INFERRED         follows from evidence but a detail is not literal
  HARNESS_POLICY   the mock's own choice where the evidence is silent
  UNKNOWN          the evidence is silent and the mock does not pretend

Package map:
  settings.py      MockSettings (every knob is HARNESS_POLICY, defaults cited)
  jwt.py           HS256 JWT with a fixed SYNTHETIC secret
  state.py         in-memory state, optional JSON persistence, request log
  oracle.py        meeting-dir ground truth -> documented transcription result
  worker.py        the background task walking Celery state names
  routers/         one module per surface (auth, binding, sdkinternal, files,
                   objectstore, transcription, mock)
  app.py           create_app()
  __main__.py      python -m mockcloud --port 8787 [--persist]
"""

from .app import create_app  # noqa: F401
from .settings import DEFAULT_PARTNER_APP, MOCK_JWT_SECRET, MockSettings, PartnerApp  # noqa: F401
from .state import MockState  # noqa: F401

__all__ = [
    "DEFAULT_PARTNER_APP",
    "MOCK_JWT_SECRET",
    "MockSettings",
    "MockState",
    "PartnerApp",
    "create_app",
]
