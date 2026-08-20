"""Fault injection beneath the surface driver.

The interesting failures in this environment are not layout drift -- they are
runtime conditions: a slow load, a server error, an interstitial nobody
expected, a session that expired mid-flow. Those have to be *reproducible* to
be worth anything as evidence, and they cannot be produced on demand by asking
the application nicely.

So they are injected at the network boundary, below the driver. The replay
engine cannot tell an injected 500 from a real one, which is the only way a
demonstration proves anything about production behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Injected into a matching HTML response. A plain <button> so it shows up in the
# accessibility tree the way a real interstitial would, and can be dismissed by
# name rather than by selector.
DIALOG_HTML = """
<div style="position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.45);z-index:9999">
  <div style="background:#fff;max-width:420px;margin:80px auto;padding:22px;border:2px solid #96231a;font-family:Arial">
    <b style="font-size:15px;color:#96231a">Security Notice</b>
    <p style="font-size:13px;color:#333">
      Your institution requires periodic acknowledgement of the acceptable use policy.
    </p>
    <button onclick="this.closest('div').parentNode.remove()"
            style="font-family:Arial;font-size:12px;padding:4px 14px">Acknowledge</button>
  </div>
</div>
"""


@dataclass
class FaultProfile:
    """What to break, and where. Empty means inject nothing."""

    # Stall responses whose URL contains this, for this long.
    delay_match: str | None = None
    delay_ms: int = 0

    # Answer with this HTTP status instead of the real response.
    status_match: str | None = None
    http_status: int = 500

    # Splice an unexpected interstitial into a matching HTML response.
    dialog_match: str | None = None

    # Serve a different URL's response instead. This is how a permission denial
    # or a vanished record is reproduced: the request goes out as recorded, and
    # the application answers with its own denial page -- exactly what happens
    # when an operator's entitlements change between recording and replay.
    rewrite_match: str | None = None
    rewrite_to: str | None = None

    # Fire each fault only on its first matching request, so a retry can
    # succeed -- which is what makes a *recoverable* condition recoverable.
    once: bool = True
    _fired: set[str] = field(default_factory=set)

    @property
    def active(self) -> bool:
        return bool(
            self.delay_match
            or self.status_match
            or self.dialog_match
            or self.rewrite_match
        )

    def describe(self) -> str:
        parts = []
        if self.delay_match:
            parts.append(f"delay {self.delay_ms}ms on {self.delay_match!r}")
        if self.status_match:
            parts.append(f"HTTP {self.http_status} on {self.status_match!r}")
        if self.dialog_match:
            parts.append(f"interstitial on {self.dialog_match!r}")
        if self.rewrite_match:
            parts.append(f"rewrite {self.rewrite_match!r} -> {self.rewrite_to!r}")
        return "; ".join(parts) or "none"

    def _should_fire(self, key: str) -> bool:
        if self.once and key in self._fired:
            return False
        self._fired.add(key)
        return True


def install(page, profile: FaultProfile) -> None:
    """Attach the profile to a Playwright page via request interception."""
    if not profile.active:
        return

    def handler(route, request):
        url = request.url

        if profile.status_match and profile.status_match in url:
            if profile._should_fire(f"status:{url}"):
                route.fulfill(
                    status=profile.http_status,
                    content_type="text/html",
                    body=(
                        f"<html><body><h1>{profile.http_status} Internal Server Error</h1>"
                        f"<p>The application encountered an unexpected condition.</p>"
                        f"</body></html>"
                    ),
                )
                return

        if profile.rewrite_match and profile.rewrite_match in url:
            if profile._should_fire(f"rewrite:{url}"):
                response = route.fetch(url=profile.rewrite_to)
                route.fulfill(
                    status=response.status,
                    content_type="text/html",
                    body=response.text(),
                )
                return

        if profile.delay_match and profile.delay_match in url:
            if profile._should_fire(f"delay:{url}"):
                page.wait_for_timeout(profile.delay_ms)

        if profile.dialog_match and profile.dialog_match in url:
            if profile._should_fire(f"dialog:{url}"):
                response = route.fetch()
                body = response.text()
                if "</body>" in body:
                    body = body.replace("</body>", DIALOG_HTML + "</body>")
                    route.fulfill(
                        status=response.status,
                        content_type="text/html",
                        body=body,
                    )
                    return

        route.continue_()

    page.route("**/*", handler)
