"""What stops a run, and what is worth trying again.
"""

from __future__ import annotations

import http.client
import re
import urllib.error


class Abort(RuntimeError):
    pass


class NothingWritten(Abort):
    """A step stopped before it wrote anything to the plug: a run of nothing
    else does not say what the plug is (past_the_commit)."""


class GoBack(Exception):
    """A step finds that an earlier one did not take on the plug - the image
    it sent was never installed: the run goes back and does that one again.
    `outcome`: what the stage line says of the step that found it."""

    def __init__(self, step: str, reason: str, outcome: str) -> None:
        super().__init__(reason)
        self.step, self.reason, self.outcome = step, reason, outcome


# A failure worth another go: the plug or the network not answering.  A refusal
# - the plug's checks, a password, the wrong model - would only say it again.
TRANSIENT_RE = re.compile(
    r"HTTP -1|timed out|timeout|sent nothing|did not answer|not answering|unreachable|"
    r"connection (refused|reset|aborted)|ConnectionError|IncompleteRead|URLError|"
    r"No route|stopped the \w+ without|did not complete within|Wi-Fi went down|"
    r"closed the connection|Broken pipe|connection to the (plug|device) was lost",
    re.IGNORECASE,
)


# The ways a request to the plug fails on the network, as the step's own
# code did not catch them: a connection refused, reset or cut off, its reply
# cut short or garbled, or no reply in time.
NETWORK_ERRORS = (ConnectionError, TimeoutError, urllib.error.URLError, http.client.HTTPException)
