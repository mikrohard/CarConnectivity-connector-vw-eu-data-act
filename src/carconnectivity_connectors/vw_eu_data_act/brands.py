"""Per-brand OIDC client_id and manufacturer label for the VW Group EU Data Act portal.

Every VW Group brand uses the same portal host and API paths, but a different
OIDC ``client_id`` and a different manufacturer name. The brand key also forms
part of the OIDC ``state`` (``country__language__BRAND``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class Brand:
    """A VW Group brand: its OIDC state key, client_id and manufacturer label."""
    key: str
    client_id: str
    manufacturer: str


# Shared default client_id (Volkswagen Passenger/Commercial). The SEAT/CUPRA
# value (f85e5b69) is the shared portal default observed in the wild; a
# SEAT-specific client_id may exist but is unverified, so the shared one is kept.
BRANDS: Dict[str, Brand] = {
    "VOLKSWAGEN_PASSENGER_CARS": Brand(
        "VOLKSWAGEN_PASSENGER_CARS",
        "9b58543e-1c15-4193-91d5-8a14145bebb0@apps_vw-dilab_com", "Volkswagen"),
    "VOLKSWAGEN_COMMERCIAL_VEHICLES": Brand(
        "VOLKSWAGEN_COMMERCIAL_VEHICLES",
        "9b58543e-1c15-4193-91d5-8a14145bebb0@apps_vw-dilab_com", "Volkswagen Commercial Vehicles"),
    "AUDI": Brand(
        "AUDI", "cc29b87a-5e9a-4362-aecf-5adea6b01bbb@apps_vw-dilab_com", "Audi"),
    "SKODA": Brand(
        "SKODA", "3ea88bf9-1d4e-4a68-b3ad-4098c1f1d246@apps_vw-dilab_com", "Skoda"),
    "SEAT": Brand(
        "SEAT", "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com", "SEAT"),
    "CUPRA": Brand(
        "CUPRA", "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com", "Cupra"),
    "BENTLEY": Brand(
        "BENTLEY", "d38aac0f-3d89-4a63-8538-b75b31322c7b@apps_vw-dilab_com", "Bentley"),
}

DEFAULT_BRAND_KEY = "VOLKSWAGEN_PASSENGER_CARS"

# Friendly aliases a user might type in config -> canonical brand key.
_ALIASES: Dict[str, str] = {
    "VW": "VOLKSWAGEN_PASSENGER_CARS",
    "VOLKSWAGEN": "VOLKSWAGEN_PASSENGER_CARS",
    "VWN": "VOLKSWAGEN_COMMERCIAL_VEHICLES",
    "VW_COMMERCIAL": "VOLKSWAGEN_COMMERCIAL_VEHICLES",
    "SKODA": "SKODA",
    "ŠKODA": "SKODA",
}


def resolve_brand(brand: Optional[str]) -> Brand:
    """Look up a :class:`Brand` by key or alias (case-insensitive).

    Falls back to Volkswagen Passenger Cars for unknown/empty input.
    """
    key = (brand or DEFAULT_BRAND_KEY).strip().upper()
    key = _ALIASES.get(key, key)
    return BRANDS.get(key, BRANDS[DEFAULT_BRAND_KEY])
