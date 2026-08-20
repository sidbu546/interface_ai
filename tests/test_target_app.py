"""Smoke tests for the target application.

These are not tests of the automation system -- they pin down the *target's*
behaviour, which everything downstream depends on. Specifically they prove
that each business outcome in the replay contract has a real, distinguishable
screen behind it:

    account_not_found · access_denied · no_transactions
    login_rejected · validation error · session expiry

If one of these screens ever stops rendering the way the detectors expect,
the failure should surface here rather than as a mysterious replay result.
"""

from __future__ import annotations

import importlib
import re

import pytest

from targets.legacy_bank import app as app_module
from targets.legacy_bank import seed

USER, PASSWORD = "jsmith", "demo1234"


@pytest.fixture()
def client():
    importlib.reload(seed)  # restore balances mutated by transfer tests
    application = app_module.create_app()
    application.config.update(TESTING=True)
    with application.test_client() as c:
        yield c


def sign_in(client, tenant: str = "meridian"):
    response = client.post(
        f"/{tenant}/login.htm",
        data={"username": USER, "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 302, "valid credentials should redirect"
    return response


def body(response) -> str:
    """Rendered markup with HTML comments stripped.

    The templates carry explanatory comments about the anti-patterns they
    implement, and several of those comments mention the very tags the tests
    assert are absent. Assertions are about what the browser sees, so the
    comments have to go before matching.
    """
    return re.sub(r"<!--.*?-->", "", response.get_data(as_text=True), flags=re.S)


# --------------------------------------------------------------------- auth


def test_login_page_inputs_have_no_accessible_name(client):
    """The hostility that matters most: inputs are unlabelled.

    Locating them requires the table-anchor rung of the ladder, not a name.
    """
    page = body(client.get("/meridian/index.htm"))
    assert 'name="username"' in page
    assert "<label" not in page.lower()
    assert "aria-label" not in page.lower()
    assert "title=" not in page.lower()
    assert "Username:" in page  # the text exists, just not associated


def test_login_rejected_is_a_business_outcome(client):
    """A rejected sign-in is a 200 with a message, never a 5xx."""
    response = client.post(
        "/meridian/login.htm", data={"username": USER, "password": "wrong"}
    )
    assert response.status_code == 200
    assert "not correct" in body(response)


def test_meridian_says_which_half_of_the_credential_was_wrong(client):
    """Meridian is configured to be specific, so replay can be specific."""
    bad_password = body(
        client.post("/meridian/login.htm", data={"username": USER, "password": "wrong"})
    )
    assert "password entered for that username is not correct" in bad_password

    bad_username = body(
        client.post("/meridian/login.htm", data={"username": "nobody", "password": "x"})
    )
    assert "username was not recognised" in bad_username
    # Naming the wrong half would send someone to reset a credential that was fine.
    assert "password" not in bad_username.split("username was not recognised")[0][-200:]


def test_cascade_refuses_to_say_which_half_was_wrong(client):
    """The other tenant withholds it deliberately -- user enumeration.

    Replay must stay vague here rather than inventing a distinction the screen
    never made.
    """
    for username, password in ((USER, "wrong"), ("nobody", "x")):
        page = body(
            client.post("/cascade/login.htm", data={"username": username, "password": password})
        )
        assert "could not be verified" in page
        assert "not recognised" not in page


def test_login_success_reaches_the_frameset(client):
    sign_in(client)
    page = body(client.get("/meridian/main.htm"))
    assert "<frameset" in page
    assert 'name="contentframe"' in page
    assert 'name="navframe"' in page


def test_session_expiry_renders_login_with_explanation(client, monkeypatch):
    sign_in(client)
    monkeypatch.setattr(app_module, "IDLE_TIMEOUT_SECONDS", -1)
    page = body(client.get("/meridian/overview.htm"))
    assert "session has ended due to inactivity" in page


# ----------------------------------------------------------------- overview


def test_overview_lists_accounts_and_repeats_the_balance_label(client):
    sign_in(client)
    page = body(client.get("/meridian/overview.htm"))
    assert "13566" in page and "13344" in page and "13901" in page
    assert "19001" not in page, "another customer's account must not be listed"
    # The duplicate-label hazard the locator ladder has to disambiguate.
    assert page.count("Balance") >= 2


# ----------------------------------------------------- account detail + outcomes


def test_account_detail_exposes_balance_and_latest_transaction(client):
    sign_in(client)
    page = body(client.get("/meridian/account.htm?id=13566"))
    assert "$4820.55" in page
    assert "SAVINGS" in page
    # Newest first: the expected answer for the primary capability.
    assert page.index("2026-08-14") < page.index("2026-08-01")
    assert "$1200.00" in page


def test_unknown_account_is_account_not_found(client):
    sign_in(client)
    page = body(client.get("/meridian/account.htm?id=99999"))
    assert "Account Not Found" in page


def test_other_customers_account_is_access_denied(client):
    sign_in(client)
    page = body(client.get("/meridian/account.htm?id=19001"))
    assert "Access Denied" in page
    assert "15300" not in page, "denied screens must not leak the balance"


def test_empty_account_is_no_transactions_not_an_error(client):
    sign_in(client)
    page = body(client.get("/meridian/account.htm?id=13901"))
    assert "No transactions have posted" in page
    assert "Account Not Found" not in page


def test_business_outcomes_answer_http_200(client):
    """Nothing at the transport layer signals these -- detection must be visual."""
    sign_in(client)
    for path in ("account.htm?id=99999", "account.htm?id=19001", "account.htm?id=13901"):
        assert client.get(f"/meridian/{path}").status_code == 200


# ------------------------------------------------------------------ transfer


@pytest.mark.parametrize(
    "amount,expected",
    [
        ("abc", "valid amount"),
        ("0", "valid amount"),
        ("999999", "Insufficient available funds"),
    ],
)
def test_transfer_validation_errors(client, amount, expected):
    sign_in(client)
    page = body(
        client.post(
            "/meridian/transfer_confirm.htm",
            data={"fromAccount": "13566", "toAccount": "13344", "amount": amount},
        )
    )
    assert expected in page


def test_transfer_confirm_uses_a_div_not_a_button(client):
    """The irreversible control has no button role -- by design."""
    sign_in(client)
    page = body(
        client.post(
            "/meridian/transfer_confirm.htm",
            data={"fromAccount": "13566", "toAccount": "13344", "amount": "25"},
        )
    )
    assert "Submit Transfer" in page
    assert 'class="fakebtn"' in page
    assert "<button" not in page.lower()


def test_transfer_execution_actually_moves_money(client):
    sign_in(client)
    before_from = seed.ACCOUNTS["13566"].balance
    before_to = seed.ACCOUNTS["13344"].balance

    page = body(
        client.post(
            "/meridian/transfer_exec.htm",
            data={"fromAccount": "13566", "toAccount": "13344", "amount": "25.00"},
        )
    )
    assert "Transfer Complete" in page
    assert seed.ACCOUNTS["13566"].balance == pytest.approx(before_from - 25.00)
    assert seed.ACCOUNTS["13344"].balance == pytest.approx(before_to + 25.00)


# -------------------------------------------------------------- multi-tenant


def test_cascade_forces_an_interstitial_meridian_does_not(client):
    response = sign_in(client, "cascade")
    assert "notice.htm" in response.headers["Location"]
    # Reaching the shell without acknowledging bounces back to the notice.
    assert "notice.htm" in client.get("/cascade/main.htm").headers.get("Location", "")

    client.post("/cascade/notice.htm")
    assert "<frameset" in body(client.get("/cascade/main.htm"))


def test_cascade_relabels_the_same_controls(client):
    sign_in(client, "cascade")
    client.post("/cascade/notice.htm")
    page = body(client.get("/cascade/overview.htm"))
    assert "Member Account ID" in page  # Meridian calls this "Account #"
    assert "My Accounts" in page        # Meridian calls this "Accounts Overview"
    assert "13566" in page              # same underlying data


def test_cascade_uses_different_frame_names(client):
    sign_in(client, "cascade")
    client.post("/cascade/notice.htm")
    page = body(client.get("/cascade/main.htm"))
    assert 'name="body"' in page and 'name="menu"' in page
    assert 'name="contentframe"' not in page



def test_forced_expiry_uses_the_real_timeout_path(client):
    """The /_expire hook must produce the app's genuine inactivity screen.

    A hook that rendered a lookalike would let the replay tests pass against a
    condition the app never actually produces.
    """
    sign_in(client)
    assert "Accounts Overview" in body(client.get("/meridian/overview.htm"))

    client.post("/_expire?count=1")
    expired = body(client.get("/meridian/overview.htm"))
    assert "session has ended due to inactivity" in expired

    # One render only: the session is genuinely cleared, so what follows is an
    # ordinary signed-out state rather than a stuck flag.
    assert "Accounts Overview" not in body(client.get("/meridian/overview.htm"))
