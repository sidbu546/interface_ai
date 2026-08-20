"""Seed data for the target application.

All of it is fabricated. No real names, account numbers, balances or
credentials appear anywhere in this repository.

The data is shaped to produce each of the business outcomes the replay
contract has to distinguish, without any fault injection:

    13566  savings, has transactions      -> the happy path
    13344  checking, has transactions     -> transfer destination
    13901  money market, NO transactions  -> outcome: no_transactions
    19001  owned by a different customer  -> outcome: access_denied
    99999  does not exist                 -> outcome: account_not_found
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Customer:
    customer_id: str
    username: str
    password: str
    display_name: str


@dataclass(frozen=True)
class Account:
    account_id: str
    customer_id: str
    kind: str
    balance: float
    available: float


@dataclass(frozen=True)
class Transaction:
    txn_id: str
    account_id: str
    posted_on: str  # ISO date
    kind: str       # "Credit" | "Debit"
    amount: float
    description: str
    running_balance: float


CUSTOMERS: dict[str, Customer] = {
    "jsmith": Customer("C001", "jsmith", "demo1234", "Jordan Smith"),
    "rlopez": Customer("C002", "rlopez", "demo1234", "Robin Lopez"),
}

ACCOUNTS: dict[str, Account] = {
    "13566": Account("13566", "C001", "SAVINGS", 4820.55, 4820.55),
    "13344": Account("13344", "C001", "CHECKING", 1250.00, 1150.00),
    "13901": Account("13901", "C001", "MONEY MARKET", 0.00, 0.00),
    # Belongs to a different customer -> permission denial, not an error.
    "19001": Account("19001", "C002", "SAVINGS", 15300.10, 15300.10),
}

# Ordered oldest -> newest. The capability reads the most recent, so the
# expected answer for 13566 is deterministic: 2026-08-14, Credit, 1200.00.
TRANSACTIONS: list[Transaction] = [
    Transaction("T-40118", "13566", "2026-07-02", "Credit", 1200.00, "Payroll Deposit", 3105.55),
    Transaction("T-40219", "13566", "2026-07-18", "Debit", 285.00, "Transfer to Checking", 2820.55),
    Transaction("T-40330", "13566", "2026-08-01", "Credit", 800.00, "Branch Deposit", 3620.55),
    Transaction("T-40412", "13566", "2026-08-14", "Credit", 1200.00, "Payroll Deposit", 4820.55),
    Transaction("T-40120", "13344", "2026-07-05", "Debit", 62.40, "Card Purchase - Grocery", 1477.60),
    Transaction("T-40255", "13344", "2026-07-18", "Credit", 285.00, "Transfer from Savings", 1762.60),
    Transaction("T-40390", "13344", "2026-08-09", "Debit", 512.60, "Rent Payment", 1250.00),
    Transaction("T-40501", "19001", "2026-08-11", "Credit", 300.00, "Branch Deposit", 15300.10),
]


def authenticate(username: str, password: str) -> Customer | None:
    user = CUSTOMERS.get((username or "").strip().lower())
    if user is not None and password == user.password:
        return user
    return None


def accounts_for(customer_id: str) -> list[Account]:
    return [a for a in ACCOUNTS.values() if a.customer_id == customer_id]


def transactions_for(account_id: str) -> list[Transaction]:
    """Newest first -- the order the account detail screen renders."""
    rows = [t for t in TRANSACTIONS if t.account_id == account_id]
    return sorted(rows, key=lambda t: (t.posted_on, t.txn_id), reverse=True)


def total_balance(customer_id: str) -> float:
    return round(sum(a.balance for a in accounts_for(customer_id)), 2)


def apply_transfer(from_id: str, to_id: str, amount: float) -> None:
    """Mutate balances in place. Deliberately irreversible: there is no undo.

    This is what makes the transfer capability's final step genuinely
    ``irreversible`` rather than a contrived label -- pressing the button
    really does change state that nothing in this app can put back.
    """
    src, dst = ACCOUNTS[from_id], ACCOUNTS[to_id]
    ACCOUNTS[from_id] = replace(
        src, balance=round(src.balance - amount, 2), available=round(src.available - amount, 2)
    )
    ACCOUNTS[to_id] = replace(
        dst, balance=round(dst.balance + amount, 2), available=round(dst.available + amount, 2)
    )
