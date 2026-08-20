"""Tenant configuration.

Two institutions running the *same* vendor product, configured differently.
This is the stand-in for the real environment's "hundreds of tenants, many on
the same underlying vendor software" property.

Everything a tenant can customise lives here and nowhere else. If a difference
between Meridian and Cascade is not expressible as a field in this dataclass,
it is not a tenant difference -- it is a product difference, and it belongs in
the templates.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Tenant:
    """One institution's configuration of the vendor product."""

    slug: str
    institution: str
    product_version: str

    # --- branding -------------------------------------------------------
    accent: str
    wordmark: str

    # --- label overrides ------------------------------------------------
    # The same control, named differently per institution. This is exactly
    # the drift a capability artifact's TenantBinding has to absorb.
    label_username: str
    label_password: str
    label_signin: str
    title_overview: str
    col_account: str
    col_type: str
    col_balance: str
    col_available: str
    nav_overview: str
    nav_transfer: str

    # --- flow differences -----------------------------------------------
    # Cascade shows a compliance interstitial after login that Meridian does
    # not. An extra screen mid-flow is the most common real-world tenant
    # difference and the one that most often breaks naive replay.
    interstitial: bool = False
    interstitial_heading: str = ""
    interstitial_body: str = ""
    interstitial_button: str = ""

    # Frame names differ between installs, so a locator scoped to a frame by
    # name cannot be assumed portable.
    frame_nav: str = "navframe"
    frame_content: str = "contentframe"
    frame_status: str = "statusframe"

    # Whether a rejected sign-in says which half was wrong. Meridian does,
    # Cascade does not -- and that is a real split in the wild, because naming
    # the unknown username lets an attacker enumerate valid accounts. Both
    # behaviours exist here on purpose: replay must report precisely when the
    # application is precise, and stay vague when it is vague, rather than
    # inventing a distinction the screen never made.
    distinguish_login_failures: bool = False

    users: dict[str, str] = field(default_factory=dict)


MERIDIAN = Tenant(
    slug="meridian",
    institution="Meridian Credit Union",
    product_version="8.2.1",
    accent="#1c3f6e",
    wordmark="MERIDIAN CU",
    label_username="Username:",
    label_password="Password:",
    label_signin="Log In",
    title_overview="Accounts Overview",
    col_account="Account #",
    col_type="Type",
    col_balance="Balance",
    col_available="Available",
    nav_overview="Accounts Overview",
    nav_transfer="Transfer Funds",
    distinguish_login_failures=True,
    users={"jsmith": "demo1234"},
)

CASCADE = Tenant(
    slug="cascade",
    institution="Cascade Federal Credit Union",
    product_version="8.2.4",
    accent="#0f5a4a",
    wordmark="CASCADE FCU",
    # Same product, different words for the same controls.
    label_username="Member ID:",
    label_password="PIN / Password:",
    label_signin="Sign On",
    title_overview="My Accounts",
    col_account="Member Account ID",
    col_type="Product",
    col_balance="Current Balance",
    col_available="Available Funds",
    nav_overview="My Accounts",
    nav_transfer="Move Money",
    # ...and one extra screen in the middle of the flow.
    interstitial=True,
    interstitial_heading="Important Account Notice",
    interstitial_body=(
        "Scheduled maintenance will occur this weekend. Online services may be "
        "briefly unavailable. Please acknowledge this notice to continue."
    ),
    interstitial_button="I Acknowledge",
    frame_nav="menu",
    frame_content="body",
    frame_status="footer",
    users={"jsmith": "demo1234"},
)

TENANTS: dict[str, Tenant] = {t.slug: t for t in (MERIDIAN, CASCADE)}


def get_tenant(slug: str) -> Tenant | None:
    return TENANTS.get(slug)
