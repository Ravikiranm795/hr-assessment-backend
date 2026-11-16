"""Fixtures for the core-layer unit tests.

The root ``tests/conftest.py`` imports the app (and so builds the ``settings`` singleton)
before this file loads, so test secrets are installed by mutating that singleton.
``configure_test_secrets`` only fills values that are empty, so it is idempotent and safe
to call from the root conftest as well.
"""

from app.core.config import settings
from app.core.testing import configure_test_secrets

configure_test_secrets(settings)
