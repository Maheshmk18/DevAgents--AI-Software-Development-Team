"""Errors shared by the external integrations (GitHub, Gmail).

Before this module existed the clients silently returned fabricated "demo"
payloads whenever a token was missing. That made the agents *look* connected to
GitHub and Gmail while they were actually inventing data, which is exactly the
kind of static placeholder this project must not ship. Tools now raise instead,
so a missing or expired credential shows up honestly in the UI and the run log.
"""

from __future__ import annotations


class IntegrationError(RuntimeError):
    """A tool integration failed: missing configuration, auth, or transport."""


class IntegrationNotConfigured(IntegrationError):
    """The integration has no usable credentials, so the tool cannot run."""


class IntegrationAuthError(IntegrationError):
    """The provider rejected the configured credentials."""
