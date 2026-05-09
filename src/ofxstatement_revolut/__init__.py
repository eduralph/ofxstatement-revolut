import re
from importlib.metadata import PackageNotFoundError, version as _pkg_version


def plugin_version() -> str:
    """Installed package version, or ``"unknown"`` when unavailable.

    Logged as the first INFO line of each parser's ``parse()`` so a user
    reading the convert output can confirm which install actually ran,
    without having to drop out and ``pip show ofxstatement-revolut``.
    Useful when multiple checkouts or a mix of pip / pipx / system
    installs are in play.

    Resolved at runtime via ``importlib.metadata`` so editable installs
    pick up whatever the last reinstall pinned. Falls back to
    ``"unknown"`` when the distribution metadata is absent (running
    tests directly out of a source tree without ``pip install -e``).
    """
    try:
        return _pkg_version("ofxstatement-revolut")
    except PackageNotFoundError:
        return "unknown"


# ── Internal pocket-transfer detection ───────────────────────────────────────
#
# Revolut renders transfers between an account and one of its own savings
# pockets ("To EUR Savings", "From EUR Family Savings", etc.) inside the
# main account section of the PDF. They balance against entries in the
# corresponding "Deposit transactions" section. If a user imports both
# OFX exports into accounting software they double-count, so the parsers
# expose an `exclude_internal_pocket_transfers` toggle that drops them.
#
# Three description shapes are recognised:
#   - "(To|From) <CCY> <pocket name>"        — main statement form
#   - "To pocket <CCY> <name> from <CCY>"    — sub-account-side form
#   - "Pocket Withdrawal"                    — sub-account withdrawal
#
# Matching the 3-letter currency code positively (against ISO 4217 codes
# Revolut actually supports) avoids false positives from real merchants
# whose name happens to be three uppercase letters (e.g. "BMW Group").

_REVOLUT_CURRENCY_CODES = (
    "EUR",
    "USD",
    "GBP",
    "JPY",
    "INR",
    "TRY",
    "CHF",
    "SEK",
    "NOK",
    "DKK",
    "PLN",
    "CZK",
    "HUF",
    "RON",
    "BGN",
    "AUD",
    "CAD",
    "NZD",
    "SGD",
    "HKD",
    "CNY",
    "AED",
    "ZAR",
    "MXN",
    "ILS",
    "THB",
)

_INTERNAL_POCKET_RE = re.compile(
    r"^(?:"
    r"(?:To|From) (?:" + "|".join(_REVOLUT_CURRENCY_CODES) + r")\b"
    r"|To pocket (?:" + "|".join(_REVOLUT_CURRENCY_CODES) + r")\b"
    r"|Pocket Withdrawal\b"
    r")"
)


def is_internal_pocket_transfer(description: str) -> bool:
    """True if *description* looks like a transfer to/from an internal pocket.

    Used by both the PDF and CSV parsers to optionally drop these rows;
    see the module-level comment above for why and the patterns matched.
    """
    return bool(_INTERNAL_POCKET_RE.match(description.strip()))
