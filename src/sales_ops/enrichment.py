"""Company enrichment — a STUB with synthetic data (D-15). No external API is called.

A real provider is out of scope for P01 (UD-8). The README lists this as a known limitation.
"""

_SYNTHETIC_PROFILES: dict[str, dict[str, str]] = {
    "example.com": {"industry": "Software", "size": "51-200 employees", "region": "Japan"},
    "acme.example": {"industry": "Manufacturing", "size": "1000+ employees", "region": "Japan"},
    "globex.example": {"industry": "Logistics", "size": "201-1000 employees", "region": "APAC"},
}


def company_profile(email_domain: str) -> dict[str, str] | None:
    return _SYNTHETIC_PROFILES.get(email_domain.lower())
