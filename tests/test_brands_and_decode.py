"""Offline tests for multi-brand support and the added decode helpers.

Pure tests: no network, no CarConnectivity garage. They cover the per-brand
OIDC client_id / manufacturer resolution and the deci-Kelvin / maintenance /
climatisation field decoding added to the connector.
"""
from datetime import datetime, timedelta, timezone

from carconnectivity_connectors.vw_eu_data_act.brands import BRANDS, resolve_brand
from carconnectivity_connectors.vw_eu_data_act.client import EudaApiClient
from carconnectivity_connectors.vw_eu_data_act.dataset import (
    Dataset, decikelvin_to_celsius,
)


def test_every_brand_has_distinct_identity():
    for key, brand in BRANDS.items():
        assert brand.key == key
        assert brand.client_id.endswith("@apps_vw-dilab_com")
        assert brand.manufacturer


def test_resolve_brand_keys_aliases_and_default():
    assert resolve_brand("CUPRA").manufacturer == "Cupra"
    assert resolve_brand("seat").manufacturer == "SEAT"           # case-insensitive
    assert resolve_brand("vw").key == "VOLKSWAGEN_PASSENGER_CARS"  # alias
    assert resolve_brand("audi").client_id.startswith("cc29b87a")
    assert resolve_brand(None).key == "VOLKSWAGEN_PASSENGER_CARS"  # default
    assert resolve_brand("nope").key == "VOLKSWAGEN_PASSENGER_CARS"  # unknown -> default


def test_brand_client_ids_match_expected():
    assert resolve_brand("SKODA").client_id.startswith("3ea88bf9")
    assert resolve_brand("BENTLEY").client_id.startswith("d38aac0f")
    # SEAT and Cupra share the portal default client_id.
    assert resolve_brand("SEAT").client_id == resolve_brand("CUPRA").client_id


def test_client_uses_per_brand_client_id():
    cupra = EudaApiClient(email="x", password="y", brand="CUPRA")
    assert cupra._client_id == resolve_brand("CUPRA").client_id
    assert cupra._state.endswith("__CUPRA")
    audi = EudaApiClient(email="x", password="y", brand="AUDI")
    assert audi._client_id.startswith("cc29b87a")


def test_decikelvin_to_celsius():
    assert decikelvin_to_celsius(3061) == 33.0
    assert decikelvin_to_celsius(3121) == 39.0
    assert decikelvin_to_celsius(None) is None
    assert decikelvin_to_celsius("oops") is None


def test_dataset_parses_maintenance_and_climate_fields():
    ds = Dataset.from_json({"vin": "V", "Data": [
        {"key": "a", "dataFieldName": "maintenance_interval__time_until_inspection", "value": "-127"},
        {"key": "b", "dataFieldName": "maintenance_interval_distance_until_inspection", "value": "-23500"},
        {"key": "c", "dataFieldName": "remaining_climatisation_time", "value": "10"},
        {"key": "d", "dataFieldName": "outside_temperature", "value": "3061"},
    ]})
    insp_days = ds.value_of("maintenance_interval__time_until_inspection")
    assert insp_days == -127
    # signed countdown -> due date (negative = remaining)
    captured = datetime(2026, 6, 26, 11, 0, tzinfo=timezone.utc)
    due_at = captured + timedelta(days=-insp_days)
    assert due_at.date().isoformat() == "2026-10-31"
    # distance: remaining km (abs of the signed value)
    assert abs(ds.value_of("maintenance_interval_distance_until_inspection")) == 23500
    # flat climatisation time is integer minutes
    assert ds.value_of("remaining_climatisation_time") == 10
    assert decikelvin_to_celsius(ds.value_of("outside_temperature")) == 33.0
