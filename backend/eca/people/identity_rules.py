"""Pure identity rules (TECHNICAL_DESIGN.md §13.7): email normalization, aliases, org domains."""

from __future__ import annotations

import re

# Consumer mail domains never become organizations.
PUBLIC_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "mail.com",
        "fastmail.com",
        "zoho.com",
        "example.com",
        "example.org",
        "example.net",
        "mail.example",
        "home.example",
        "personal.example",
    }
)
_GMAIL = frozenset({"gmail.com", "googlemail.com"})
_SPACES = re.compile(r"\s+")


def normalize_email(address: str) -> str:
    """Lower-case; Gmail dot and plus normalization only for gmail.com (§13.7)."""
    addr = address.strip().lower()
    if "@" not in addr:
        raise ValueError(f"not an email address: {address!r}")
    local, domain = addr.rsplit("@", 1)
    if domain in _GMAIL:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def email_domain(address: str) -> str:
    return normalize_email(address).rsplit("@", 1)[1]


def is_public_domain(domain: str) -> bool:
    d = domain.lower()
    return any(d == p or d.endswith("." + p) for p in PUBLIC_DOMAINS)


def normalize_alias(name: str) -> str:
    return _SPACES.sub(" ", name.strip().strip('"').strip("'")).lower()


def organization_name(domain: str) -> str:
    """Readable default name from a domain: ``kestrelbank.example`` → ``Kestrelbank``."""
    label = domain.split(".")[0]
    return " ".join(part.capitalize() for part in re.split(r"[-_]", label) if part)


def first_name(alias: str) -> str:
    return alias.split(" ", 1)[0] if alias else ""
