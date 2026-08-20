"""A deliberately hostile, server-rendered back-office banking app.

This is the *target* for the automation system, not part of it. It exists to
be driven through its UI, because it exposes no API -- which is the whole
premise of the project.

Every anti-pattern here is intentional and catalogued in README.md. The short
version: framesets, table layout, no test IDs, inputs with no label
association, repeated visible text, a div acting as a button, full page loads,
and an idle session timeout. If any of this looks like bad code, that is the
point; it is a reproduction of what the real environment looks like.

Run:  python -m targets.legacy_bank.app
"""

from __future__ import annotations

import os
import time

from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import seed
from .tenants import TENANTS, Tenant, get_tenant

# Short by design so a replay can plausibly hit it, and overridable so tests
# can force expiry without waiting.
IDLE_TIMEOUT_SECONDS = int(os.environ.get("BANK_IDLE_TIMEOUT", "90"))


def create_app() -> Flask:
    app = Flask(__name__)
    # Fixed dev key: this app holds no real data and the cookie is not a
    # security boundary for anything. Never do this in production.
    app.secret_key = os.environ.get("BANK_SECRET_KEY", "legacy-bank-demo-key")

    # Test-only levers, reachable through the /_ endpoints below. Kept in one
    # place so it stays obvious what is scaffolding and what is the app.
    control = {"force_idle": 0}

    # ---------------------------------------------------------------- helpers

    def tenant_or_404(slug: str) -> Tenant:
        tenant = get_tenant(slug)
        if tenant is None:
            abort(404)
        return tenant

    def current_customer(tenant: Tenant) -> seed.Customer | None:
        """Return the signed-in customer, or None if signed out or idle-expired."""
        if session.get("tenant") != tenant.slug:
            return None
        username = session.get("user")
        if not username:
            return None
        # A forced expiry takes the same path a real idle timeout does -- the
        # session is cleared and the caller gets the inactivity screen. Waiting
        # out BANK_IDLE_TIMEOUT would work too; this just makes the condition
        # reproducible instead of a ninety-second race.
        if control["force_idle"] > 0:
            control["force_idle"] -= 1
            session.clear()
            return None
        last_seen = session.get("seen", 0)
        if time.time() - last_seen > IDLE_TIMEOUT_SECONDS:
            session.clear()
            return None
        session["seen"] = time.time()
        return seed.CUSTOMERS.get(username)

    def render_expired(tenant: Tenant):
        # A real session timeout: the app answers with its own login screen
        # carrying an explanation. Replay must recognise this as recoverable,
        # not as "the click did nothing".
        return (
            render_template(
                "login.html",
                t=tenant,
                error="Your session has ended due to inactivity. Please sign in again.",
                expired=True,
            ),
            200,
        )

    # ------------------------------------------------------------------ index

    @app.get("/")
    def index():
        return render_template("index.html", tenants=list(TENANTS.values()))

    # ------------------------------------------------------------------- auth

    @app.get("/<slug>/index.htm")
    def login_form(slug: str):
        tenant = tenant_or_404(slug)
        session.clear()
        return render_template("login.html", t=tenant, error=None, expired=False)

    @app.post("/<slug>/login.htm")
    def login_submit(slug: str):
        tenant = tenant_or_404(slug)
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        customer = seed.authenticate(username, password)
        if customer is None or customer.username not in tenant.users:
            # Business outcome, not a crash: the caller needs to know the
            # credentials were rejected. Whether we say *which* half was wrong
            # is a per-tenant decision -- see Tenant.distinguish_login_failures.
            known = (username or "").strip().lower() in tenant.users
            if not tenant.distinguish_login_failures:
                error = "The username and password could not be verified."
            elif known:
                error = "The password entered for that username is not correct."
            else:
                error = "That username was not recognised."
            return render_template(
                "login.html", t=tenant, error=error, expired=False
            ), 200
        session.clear()
        session["tenant"] = tenant.slug
        session["user"] = customer.username
        session["seen"] = time.time()
        session["ack"] = not tenant.interstitial
        if tenant.interstitial:
            return redirect(url_for("notice", slug=tenant.slug))
        return redirect(url_for("main", slug=tenant.slug))

    @app.get("/<slug>/notice.htm")
    def notice(slug: str):
        tenant = tenant_or_404(slug)
        if current_customer(tenant) is None:
            return render_expired(tenant)
        return render_template("notice.html", t=tenant)

    @app.post("/<slug>/notice.htm")
    def notice_ack(slug: str):
        tenant = tenant_or_404(slug)
        if current_customer(tenant) is None:
            return render_expired(tenant)
        session["ack"] = True
        return redirect(url_for("main", slug=tenant.slug))

    @app.get("/<slug>/logout.htm")
    def logout(slug: str):
        tenant = tenant_or_404(slug)
        session.clear()
        return redirect(url_for("login_form", slug=tenant.slug))

    # ------------------------------------------------------------- app shell

    @app.get("/<slug>/main.htm")
    def main(slug: str):
        tenant = tenant_or_404(slug)
        if current_customer(tenant) is None:
            return render_expired(tenant)
        if not session.get("ack"):
            return redirect(url_for("notice", slug=tenant.slug))
        return render_template("shell.html", t=tenant)

    @app.get("/<slug>/nav.htm")
    def nav(slug: str):
        tenant = tenant_or_404(slug)
        if current_customer(tenant) is None:
            return render_expired(tenant)
        return render_template("nav.html", t=tenant)

    @app.get("/<slug>/status.htm")
    def status(slug: str):
        tenant = tenant_or_404(slug)
        customer = current_customer(tenant)
        if customer is None:
            return render_expired(tenant)
        return render_template("status.html", t=tenant, customer=customer)

    # ---------------------------------------------------------------- content

    @app.get("/<slug>/overview.htm")
    def overview(slug: str):
        tenant = tenant_or_404(slug)
        customer = current_customer(tenant)
        if customer is None:
            return render_expired(tenant)
        accounts = sorted(seed.accounts_for(customer.customer_id), key=lambda a: a.account_id)
        return render_template(
            "overview.html",
            t=tenant,
            accounts=accounts,
            total=seed.total_balance(customer.customer_id),
        )

    @app.get("/<slug>/account.htm")
    def account_detail(slug: str):
        tenant = tenant_or_404(slug)
        customer = current_customer(tenant)
        if customer is None:
            return render_expired(tenant)

        account_id = (request.args.get("id") or "").strip()
        account = seed.ACCOUNTS.get(account_id)

        if account is None:
            # Business outcome: account_not_found
            return render_template(
                "app_error.html",
                t=tenant,
                heading="Account Not Found",
                message=f"No account matching '{account_id}' could be located.",
            ), 200

        if account.customer_id != customer.customer_id:
            # Business outcome: access_denied
            return render_template(
                "app_error.html",
                t=tenant,
                heading="Access Denied",
                message="You are not authorized to view this account.",
            ), 200

        return render_template(
            "account_detail.html",
            t=tenant,
            account=account,
            transactions=seed.transactions_for(account_id),
        )

    # --------------------------------------------------------------- transfer

    @app.get("/<slug>/transfer.htm")
    def transfer_form(slug: str):
        tenant = tenant_or_404(slug)
        customer = current_customer(tenant)
        if customer is None:
            return render_expired(tenant)
        accounts = sorted(seed.accounts_for(customer.customer_id), key=lambda a: a.account_id)
        return render_template("transfer_form.html", t=tenant, accounts=accounts, error=None)

    @app.post("/<slug>/transfer_confirm.htm")
    def transfer_confirm(slug: str):
        tenant = tenant_or_404(slug)
        customer = current_customer(tenant)
        if customer is None:
            return render_expired(tenant)

        accounts = sorted(seed.accounts_for(customer.customer_id), key=lambda a: a.account_id)
        owned = {a.account_id for a in accounts}
        from_id = (request.form.get("fromAccount") or "").strip()
        to_id = (request.form.get("toAccount") or "").strip()
        raw_amount = (request.form.get("amount") or "").strip()

        def reject(message: str):
            # Validation error: the app re-renders its own form with a message.
            return render_template(
                "transfer_form.html", t=tenant, accounts=accounts, error=message
            ), 200

        try:
            amount = round(float(raw_amount), 2)
        except ValueError:
            return reject("Please enter a valid amount.")
        if amount <= 0:
            return reject("Please enter a valid amount.")
        if from_id not in owned or to_id not in owned:
            return reject("Please select valid accounts.")
        if from_id == to_id:
            return reject("The source and destination accounts must be different.")
        if amount > seed.ACCOUNTS[from_id].available:
            return reject("Insufficient available funds for this transfer.")

        return render_template(
            "transfer_confirm.html", t=tenant, from_id=from_id, to_id=to_id, amount=amount
        )

    @app.post("/<slug>/transfer_exec.htm")
    def transfer_exec(slug: str):
        """The irreversible step. Nothing in this app can undo it."""
        tenant = tenant_or_404(slug)
        customer = current_customer(tenant)
        if customer is None:
            return render_expired(tenant)

        owned = {a.account_id for a in seed.accounts_for(customer.customer_id)}
        from_id = (request.form.get("fromAccount") or "").strip()
        to_id = (request.form.get("toAccount") or "").strip()
        try:
            amount = round(float(request.form.get("amount") or ""), 2)
        except ValueError:
            abort(400)

        if from_id not in owned or to_id not in owned or amount <= 0:
            abort(400)
        if amount > seed.ACCOUNTS[from_id].available:
            abort(400)

        seed.apply_transfer(from_id, to_id, amount)
        return render_template(
            "transfer_done.html", t=tenant, from_id=from_id, to_id=to_id, amount=amount
        )

    # ------------------------------------------------------------ test control

    @app.post("/_expire")
    def expire():
        """Make the next page render behave as an idle timeout.

        Same test-only status as /_reset: outside the agent's allowlist, so the
        automation can never trigger it on itself.
        """
        control["force_idle"] = int(request.args.get("count", "1"))
        return {"status": "will expire", "renders": control["force_idle"]}, 200

    @app.post("/_reset")
    def reset():
        """Restore seeded balances. For test setup only.

        Not part of any flow and deliberately outside the agent's allowlist --
        the automation must never be able to reset the world it is operating on.
        """
        import importlib

        importlib.reload(seed)
        return {"status": "reset"}, 200

    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("BANK_PORT", "8099"))
    app.run(host="127.0.0.1", port=port, debug=False)
