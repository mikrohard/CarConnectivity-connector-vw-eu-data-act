"""Offline tests: dataset parsing and field -> attribute mapping.

These run without any network access. The mapping test builds a real
CarConnectivity garage and exercises the connector's ``_map_dataset`` against
the sample dataset shipped by the EU Data Act portal.
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest

from carconnectivity.carconnectivity import CarConnectivity
from carconnectivity.charging import Charging
from carconnectivity.doors import Doors
from carconnectivity.errors import TooManyRequestsError
from carconnectivity.observable import Observable
from carconnectivity.window_heating import WindowHeatings

import requests

from carconnectivity.units import Length, Speed

from carconnectivity_connectors.vw_eu_data_act.client import ApiError, AuthError, EudaApiClient
from carconnectivity_connectors.vw_eu_data_act.connector import (
    Connector, _filename_timestamp, KNOWN_MAPPED_FIELDS,
    _charge_mode, _charge_mode_flat, VWEudaChargeMode,
    KEY_PRIMARY_RANGE, KEY_PRIMARY_RANGE_UNIT,
)
from carconnectivity_connectors.vw_eu_data_act.dataset import Dataset
from carconnectivity_connectors.vw_eu_data_act.vehicle import VWEudaElectricVehicle, VWEudaVehicle

SAMPLE = os.path.join(os.path.dirname(__file__), "sample_dataset.json")
VIN = "WVWZZZE1ZLP010257"

# A minimal eGolf flat-format payload (no dotted field names).
EGOLF_VIN = "WVWZZZE1ZLP000001"
EGOLF_PAYLOAD = {
    "vin": EGOLF_VIN,
    "user_id": "173ba297-16cd-4da6-bdd7-de433cd4fdf2",
    "Data": [
        {"key": "ae0294b4-1286-3e98-a818-1485b8d88430", "dataFieldName": "state_of_charge",
         "value": "26", "timestampUtc": "2026-05-31T14:11:43.000Z"},
        {"key": "55e0d40b-38ed-3cb5-9dcd-6193df6fc493", "dataFieldName": "cruising_range_primary_engine",
         "value": "67", "timestampUtc": "2026-05-31T14:11:43.000Z"},
        {"key": "9da735bb-c5d5-39f8-bf53-0fa2a367aa8f", "dataFieldName": "charging_state",
         "value": "charging", "timestampUtc": "2026-05-31T14:11:43.000Z"},
        {"key": "41c0805c-43e5-313e-9dfb-356cb8d20f7c", "dataFieldName": "mileage",
         "value": "100571", "timestampUtc": "2026-05-31T14:11:15.000Z"},
        {"key": "60bc0937-f5a7-3809-9535-9a7942e5dd94", "dataFieldName": "lock_state",
         "value": "locked", "timestampUtc": "2026-05-31T14:11:43.000Z"},
        {"key": "6810b781-e54a-35e8-af98-fcdefb54bac6", "dataFieldName": "outside_temperature",
         "value": "2956", "timestampUtc": "2026-05-31T14:11:15.000Z"},
    ]
}


def _load() -> Dataset:
    with open(SAMPLE, "r", encoding="utf-8") as fh:
        return Dataset.from_json(json.load(fh))


def test_dataset_parsing():
    ds = _load()
    assert ds.vin == VIN
    assert ds.value_of("mileage.value") == 116803
    assert ds.value_of("battery_state_report.soc") == 69
    assert ds.value_of("battery_state_report.charge_power") == 0.0
    assert ds.value_of("settings.target_soc") == 80
    assert ds.value_of("min_temperature") == 19.5
    assert ds.value_of("max_temperature") == 20.0
    assert ds.value_of("locked") is True
    assert ds.value_of("window_heating_state") == "WINDOW_HEATING_STATE_OFF"
    assert ds.value_of("charging_state_report.current_charge_state") == "CHARGE_STATE_NOT_READY_FOR_CHARGING"
    # captured_at is the max of the car_captured_time points
    assert ds.captured_at is not None
    assert ds.captured_at.isoformat().startswith("2026-05-29T22:59:28")


@pytest.fixture()
def connector():
    cc = CarConnectivity(config={"carConnectivity": {"connectors": []}})
    conn = Connector(connector_id="test", car_connectivity=cc,
                     config={"username": "user@example.com", "password": "secret"})
    return conn


def test_flat_data_basic_field_mapping(connector):
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({
        "vin": VIN,
        "Data": [
            {"key": "k1", "dataFieldName": "mileage.value", "value": "12345"},
        ]
    })

    connector._map_dataset(VIN, ds)

    assert garage.get_vehicle(VIN).odometer.value == 12345


def test_flat_data_multiple_fields_mapped(connector):
    garage = connector.car_connectivity.garage
    vehicle = VWEudaElectricVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({
        "vin": VIN,
        "Data": [
            {"key": "k1", "dataFieldName": "mileage.value", "value": "100"},
            {"key": "k2", "dataFieldName": "locked", "value": "true"},
            {"key": "k3", "dataFieldName": "settings.target_soc", "value": "80"},
        ]
    })

    connector._map_dataset(VIN, ds)

    v = garage.get_vehicle(VIN)
    assert v.odometer.value == 100
    assert v.doors.lock_state.value == Doors.LockState.LOCKED

    assert v.charging.settings.target_level.value == 80


def test_flat_data_enum_mapping(connector):
    garage = connector.car_connectivity.garage
    vehicle = VWEudaElectricVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({
        "vin": VIN,
        "Data": [
            {"key": "k1", "dataFieldName": "charging_state_report.current_charge_state",
             "value": "CHARGE_STATE_CHARGING"},
        ]
    })

    connector._map_dataset(VIN, ds)

    assert garage.get_vehicle(VIN).charging.state.value == Charging.ChargingState.CHARGING


def test_flat_data_enum_index_mapping(connector):
    garage = connector.car_connectivity.garage
    vehicle = VWEudaElectricVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({
        "vin": VIN,
        "Data": [
            {"key": "k1", "dataFieldName": "charging_state_report.current_charge_state",
             "value": "2"},
        ]
    })

    connector._map_dataset(VIN, ds)

    assert garage.get_vehicle(VIN).charging.state.value == Charging.ChargingState.CHARGING


def test_flat_data_unit_resolution(connector):
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({
        "vin": VIN,
        "Data": [
            {"key": "k1", "dataFieldName": "mileage.value", "value": "100"},
            {"key": "k2", "dataFieldName": "mileage.unit", "value": "MILES"},
        ]
    })

    connector._map_dataset(VIN, ds)

    assert garage.get_vehicle(VIN).odometer.unit == Length.MI


def test_flat_data_unknown_field_ignored(connector):
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({
        "vin": VIN,
        "Data": [
            {"key": "k1", "dataFieldName": "this_field_does_not_exist", "value": "42"},
            {"key": "k2", "dataFieldName": "mileage.value", "value": "100"},
        ]
    })

    connector._map_dataset(VIN, ds)

    assert garage.get_vehicle(VIN).odometer.value == 100


def test_mapping(connector):
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    connector._map_dataset(VIN, _load())  # pylint: disable=protected-access

    vehicle = garage.get_vehicle(VIN)
    # EV promotion happened
    assert isinstance(vehicle, VWEudaElectricVehicle)

    assert vehicle.odometer.value == 116803
    assert vehicle.odometer.unit.value == "km"
    assert vehicle.doors.lock_state.value == Doors.LockState.LOCKED
    assert vehicle.window_heatings.heating_state.value == WindowHeatings.HeatingState.OFF

    drive = vehicle.get_electric_drive()
    assert drive is not None
    assert drive.level.value == 69
    assert drive.battery.temperature_min.value == 19.5
    assert drive.battery.temperature_max.value == 20.0

    assert vehicle.charging.power.value == 0.0
    assert vehicle.charging.state.value == Charging.ChargingState.OFF
    assert vehicle.charging.settings.target_level.value == 80
    # estimated range is absent from this dataset -> stays unset
    assert drive.range.value is None


def test_update_vehicles_flushes_transaction(connector):
    """update_vehicles() must call transaction_end() so the mqtt_homeassistant
    plugin's on_transaction_end discovery observer fires. Without it, HA entities
    stay 'unavailable'. This guards against regressing that fix."""
    cc = connector.car_connectivity
    garage = cc.garage
    vehicle = VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    # An on_transaction_end ENABLED observer (same registration the HA plugin uses).
    fired = []
    cc.add_observer(lambda element, flags: fired.append(flags),
                    Observable.ObserverEvent.ENABLED,
                    on_transaction_end=True)

    # Stub the API client so update_vehicles() runs fully offline.
    payload = json.load(open(SAMPLE, "r", encoding="utf-8"))

    class _FakeClient:
        def get_metadata(self, vin):
            return {"Identifier": "ident"}

        def list_datasets(self, vin, identifier):
            return [{"name": "20260530104136_%s.zip" % vin,
                     "createdOn": "2026-05-30T10:41:36Z"}]

        def download_dataset(self, vin, identifier, name):
            return payload

    connector.client = _FakeClient()

    connector.update_vehicles()

    # The on_transaction_end observer fired (discovery would be (re)published).
    assert fired, "transaction_end() was not called; HA discovery would not refresh"
    assert garage.get_vehicle(VIN).odometer.value == 116803


def test_by_field_picks_smallest_uuid_deterministically():
    """A curated field can appear several times under different UUIDs with
    conflicting values; the portal does not order the array. by_field() must
    return the entry with the smallest UUID so the mapped attribute tracks the
    same data point across refreshes instead of flip-flopping."""
    data = [
        {"key": "cccc", "dataFieldName": "charging_state_report.current_charge_state",
         "value": "CHARGE_STATE_CHARGING"},
        {"key": "aaaa", "dataFieldName": "charging_state_report.current_charge_state",
         "value": "CHARGE_STATE_OFF"},
        {"key": "bbbb", "dataFieldName": "charging_state_report.current_charge_state",
         "value": "CHARGE_STATE_ERROR"},
    ]
    # smallest UUID ("aaaa") wins regardless of array order
    assert Dataset.from_json({"vin": VIN, "Data": data}).value_of(
        "charging_state_report.current_charge_state") == "CHARGE_STATE_OFF"
    assert Dataset.from_json({"vin": VIN, "Data": list(reversed(data))}).value_of(
        "charging_state_report.current_charge_state") == "CHARGE_STATE_OFF"


def test_by_field_prefers_freshest_timestamp():
    """When a field appears several times, the reading with the latest
    timestampUtc wins, even if a staler reading has a smaller UUID (which the
    old smallest-UUID rule would have picked)."""
    data = [
        {"key": "aaaa", "dataFieldName": "oil_level_actual_level",
         "value": "100.0", "timestampUtc": "2026-06-23T15:14:12.000Z"},
        {"key": "zzzz", "dataFieldName": "oil_level_actual_level",
         "value": "87.5", "timestampUtc": "2026-06-25T10:37:14.000Z"},
    ]
    assert Dataset.from_json({"vin": VIN, "Data": data}).value_of("oil_level_actual_level") == 87.5


def test_merge_prefers_freshest_timestamp_regardless_of_list_order():
    """A stale reading in a later-listed dataset must not override a fresher one.
    Reproduces the oil-level bug: oil 100.0 measured 2026-06-23 must lose to oil
    87.5 measured 2026-06-25 whatever the merge order."""
    fresh = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k1", "dataFieldName": "oil_level_actual_level",
         "value": "87.5", "timestampUtc": "2026-06-25T10:37:14.000Z"},
    ]})
    stale = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k2", "dataFieldName": "oil_level_actual_level",
         "value": "100.0", "timestampUtc": "2026-06-23T15:14:12.000Z"},
    ]})
    assert Dataset.merge([fresh, stale]).value_of("oil_level_actual_level") == 87.5
    assert Dataset.merge([stale, fresh]).value_of("oil_level_actual_level") == 87.5


def test_merge_timestampless_field_keeps_list_order():
    """Fields without timestampUtc keep the previous behaviour: the later dataset
    in list order wins (no regression for timestamp-less fields)."""
    first = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k1", "dataFieldName": "charging_state", "value": "off"},
    ]})
    second = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k2", "dataFieldName": "charging_state", "value": "charging"},
    ]})
    assert Dataset.merge([first, second]).value_of("charging_state") == "charging"


def test_filename_timestamp_both_layouts():
    """createdOn-less listings fall back to the filename timestamp; both
    "TIMESTAMP_VIN.zip" and "VIN_TIMESTAMP.zip" layouts must parse, else the
    newest-dataset sort collapses and the wrong dataset can be selected."""
    ts_first = _filename_timestamp("20260530104136_%s.zip" % VIN)
    ts_last = _filename_timestamp("%s_20260530104136.zip" % VIN)
    assert ts_first is not None and ts_last is not None
    assert ts_first == ts_last
    assert ts_first.isoformat().startswith("2026-05-30T10:41:36")
    # no parseable segment -> None (sort falls back to datetime.min)
    assert _filename_timestamp("no_content_found.zip") is None


def test_mileage_unit_resolved_from_companion_field(connector):
    """Vehicles reporting in miles expose a mileage.unit enum; the odometer unit
    must follow it instead of being hardcoded to km."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k1", "dataFieldName": "mileage.value", "value": "72580"},
        {"key": "k2", "dataFieldName": "mileage.unit", "value": "MILES"},
    ]})
    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    vehicle = garage.get_vehicle(VIN)
    assert vehicle.odometer.value == 72580
    assert vehicle.odometer.unit == Length.MI


def test_mileage_unit_defaults_to_km_when_absent(connector):
    """Without a mileage.unit companion field, the odometer stays in km."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k1", "dataFieldName": "mileage.value", "value": "116803"},
    ]})
    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    assert garage.get_vehicle(VIN).odometer.unit == Length.KM


def test_enum_integer_index_resolves_to_label():
    """Enum fields occasionally arrive as the raw protobuf integer index instead
    of the label; value_of() must resolve it back to the documented label."""
    ds = Dataset.from_json({"vin": VIN, "Data": [
        # index 2 of current_charge_state -> CHARGE_STATE_CHARGING_HV_BATTERY
        {"key": "k1", "dataFieldName": "charging_state_report.current_charge_state", "value": "2"},
        # index 1 of window_heating_state -> WINDOW_HEATING_STATE_ON
        {"key": "k2", "dataFieldName": "window_heating_state", "value": "1"},
    ]})
    assert ds.value_of("charging_state_report.current_charge_state") == "CHARGE_STATE_CHARGING_HV_BATTERY"
    assert ds.value_of("window_heating_state") == "WINDOW_HEATING_STATE_ON"
    # string labels still pass through untouched
    ds2 = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k1", "dataFieldName": "charging_state_report.current_charge_state",
         "value": "CHARGE_STATE_READY_FOR_CHARGING"},
    ]})
    assert ds2.value_of("charging_state_report.current_charge_state") == "CHARGE_STATE_READY_FOR_CHARGING"
    # out-of-range index and non-enum integer fields are left as-is
    ds3 = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k1", "dataFieldName": "charging_state_report.current_charge_state", "value": "99"},
        {"key": "k2", "dataFieldName": "mileage.value", "value": "116803"},
    ]})
    assert ds3.value_of("charging_state_report.current_charge_state") == 99
    assert ds3.value_of("mileage.value") == 116803


def test_enum_integer_index_maps_to_charging_state(connector):
    """An integer charge-state index resolves to its label and then maps onto the
    CarConnectivity charging enum (index 2 -> CHARGING)."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaElectricVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k0", "dataFieldName": "battery_state_report.soc", "value": "55"},
        {"key": "k1", "dataFieldName": "charging_state_report.current_charge_state", "value": "2"},
        {"key": "k2", "dataFieldName": "window_heating_state", "value": "1"},
    ]})
    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    vehicle = garage.get_vehicle(VIN)
    assert vehicle.charging.state.value == Charging.ChargingState.CHARGING
    assert vehicle.window_heatings.heating_state.value == WindowHeatings.HeatingState.ON


def test_connector_accepts_initialization_kwarg():
    """Newer carconnectivity cores pass an ``initialization`` kwarg when loading
    connectors. The connector must accept it without raising (issue #1):
    previously it forwarded the kwarg to a base __init__ that rejects it,
    raising "TypeError: ...__init__() got an unexpected keyword argument
    'initialization'"."""
    cc = CarConnectivity(config={"carConnectivity": {"connectors": []}})
    conn = Connector(connector_id="test-init", car_connectivity=cc,
                     config={"username": "user@example.com", "password": "secret"},
                     initialization={})
    assert conn is not None


def test_network_errors_become_apierror():
    """Transient requests failures must surface as ApiError (which the background
    loop retries), not raw ConnectionError (which crashed the worker thread)."""
    client = EudaApiClient(email="u", password="p")
    client._logged_in = True  # skip login for this unit test

    def _boom(*args, **kwargs):
        raise requests.exceptions.ConnectionError(
            "('Connection aborted.', RemoteDisconnected(...))")

    client._session.get = _boom

    with pytest.raises(ApiError):
        client.list_datasets("WVWZZZE1ZLP010257", "ident")
    with pytest.raises(ApiError):
        client.download_dataset("WVWZZZE1ZLP010257", "ident", "x.zip")


def _http_response(status_code, headers=None):
    response = requests.Response()
    response.status_code = status_code
    response.headers.update(headers or {})
    return response


def test_json_get_raises_too_many_requests_for_http_429(monkeypatch):
    """Real 429s were seen on the listing endpoint (PR #43): they must reach the
    back-off instead of the generic one-minute retry."""
    client = EudaApiClient(email="user@example.com", password="secret")
    monkeypatch.setattr(client, "_session_get", lambda *_args, **_kwargs: _http_response(429))

    with pytest.raises(TooManyRequestsError) as err:
        client._get_json("https://example.invalid/data", _retry=False)  # pylint: disable=protected-access
    assert err.value.retry_after is None


def test_download_raises_too_many_requests_for_http_429(monkeypatch):
    client = EudaApiClient(email="user@example.com", password="secret")
    monkeypatch.setattr(client, "ensure_login", lambda: None)
    monkeypatch.setattr(client, "_session_get",
                        lambda *_args, **_kwargs: _http_response(429, {"Retry-After": "120"}))

    with pytest.raises(TooManyRequestsError) as err:
        client.download_dataset(VIN, "identifier", "dataset.zip")
    assert err.value.retry_after == 120


def test_retry_after_http_date_is_converted_to_seconds(monkeypatch):
    client = EudaApiClient(email="user@example.com", password="secret")
    when = (datetime.now(tz=timezone.utc) + timedelta(minutes=5)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    monkeypatch.setattr(client, "_session_get",
                        lambda *_args, **_kwargs: _http_response(429, {"Retry-After": when}))

    with pytest.raises(TooManyRequestsError) as err:
        client._get_json("https://example.invalid/data", _retry=False)  # pylint: disable=protected-access
    assert 240 <= err.value.retry_after <= 300


def test_json_get_keeps_other_http_errors_generic(monkeypatch):
    client = EudaApiClient(email="user@example.com", password="secret")
    monkeypatch.setattr(client, "_session_get", lambda *_args, **_kwargs: _http_response(500))

    with pytest.raises(ApiError):
        client._get_json("https://example.invalid/data", _retry=False)  # pylint: disable=protected-access


def test_historical_recon_propagates_rate_limits(connector):
    """The optional historical path must not swallow account rate limits."""
    connector.active_config["historical"] = True

    class _RateLimitedClient:
        def get_metadata(self, vin, request_type="partial"):
            raise TooManyRequestsError("HTTP 429")

    connector.client = _RateLimitedClient()

    with pytest.raises(TooManyRequestsError):
        connector._historical_recon(VIN)  # pylint: disable=protected-access


def test_bootstrap_resumes_after_rate_limit(connector):
    """A 429 mid-bootstrap must not throw away what was already downloaded: the
    next attempt only fetches the files it does not have yet (PR #43)."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))
    payload = json.load(open(SAMPLE, "r", encoding="utf-8"))
    names = ["2026053010%02d00_%s.zip" % (minute, VIN) for minute in (0, 15, 30)]

    class _FakeClient:
        def __init__(self):
            self.listing = names
            self.downloads = []
            self.limit_on = names[1]

        def list_datasets(self, vin, identifier):
            return [{"name": n, "createdOn": "2026-05-30T10:%s:00Z" % n[10:12]} for n in self.listing]

        def download_dataset(self, vin, identifier, name):
            if name == self.limit_on:
                self.limit_on = None
                raise TooManyRequestsError("Download -> HTTP 429")
            self.downloads.append(name)
            return payload

    fake = _FakeClient()
    connector.client = fake
    connector._identifiers[VIN] = "ident"  # pylint: disable=protected-access

    with pytest.raises(TooManyRequestsError):
        connector._update_vehicle(VIN)  # pylint: disable=protected-access
    assert fake.downloads == [names[0]]
    assert VIN not in connector._bootstrapped  # pylint: disable=protected-access

    # A new file arrives before the retry; the first one must not be fetched again.
    fake.listing = names + ["20260530104500_%s.zip" % VIN]
    connector._update_vehicle(VIN)  # pylint: disable=protected-access

    assert fake.downloads == [names[0], names[1], names[2], fake.listing[-1]]
    assert VIN in connector._bootstrapped  # pylint: disable=protected-access
    assert connector._bootstrap_partial == {}  # pylint: disable=protected-access


def test_bootstrap_resume_drops_files_that_left_the_portal_window(connector):
    """Files downloaded before a 429 that are no longer listed on the retry are
    not merged back in: the bootstrap follows the current listing."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))
    payload = json.load(open(SAMPLE, "r", encoding="utf-8"))
    gone = "20260530100000_%s.zip" % VIN
    kept = "20260530101500_%s.zip" % VIN
    merged_inputs = []
    original_merge = Dataset.merge

    class _FakeClient:
        def __init__(self):
            self.listing = [gone, kept]
            self.limited = True

        def list_datasets(self, vin, identifier):
            return [{"name": n, "createdOn": "2026-05-30T10:%s:00Z" % n[10:12]} for n in self.listing]

        def download_dataset(self, vin, identifier, name):
            if name == kept and self.limited:
                self.limited = False
                raise TooManyRequestsError("Download -> HTTP 429")
            return payload

    fake = _FakeClient()
    connector.client = fake
    connector._identifiers[VIN] = "ident"  # pylint: disable=protected-access
    with pytest.raises(TooManyRequestsError):
        connector._update_vehicle(VIN)  # pylint: disable=protected-access
    assert gone in connector._bootstrap_partial[VIN]  # pylint: disable=protected-access

    fake.listing = [kept]
    Dataset.merge = staticmethod(lambda datasets: merged_inputs.append(len(datasets)) or original_merge(datasets))
    try:
        connector._update_vehicle(VIN)  # pylint: disable=protected-access
    finally:
        Dataset.merge = original_merge
    assert merged_inputs == [1]


def test_background_loop_backs_off_on_rate_limit(connector):
    """The loop honours Retry-After (with a one-minute floor) and falls back to
    15 minutes when the portal sends none."""
    waits = []

    class _Stop:
        def __init__(self, rounds):
            self.rounds = rounds

        def clear(self):
            pass

        def is_set(self):
            return self.rounds <= 0

        def set(self):
            self.rounds = 0

        def wait(self, seconds):
            waits.append(seconds)
            self.rounds -= 1

    for retry_after, expected in ((None, 900), (120, 120), (5, 60)):
        def _limited(retry_after=retry_after):
            raise TooManyRequestsError("HTTP 429", retry_after=retry_after)
        connector.fetch_all = _limited
        connector._stop_event = _Stop(1)  # pylint: disable=protected-access
        connector._background_loop()  # pylint: disable=protected-access
        assert waits[-1] == expected


def test_background_loop_backs_off_on_login_failures(connector):
    """#49: a login failure does not heal by itself, so consecutive failures
    back off exponentially (1 min doubling, capped at 1 h) instead of rerunning
    the login every minute; a successful update resets the back-off."""
    waits = []
    outcomes = iter([False] * 8 + [True, False])

    class _Stop:
        def __init__(self, rounds):
            self.rounds = rounds

        def clear(self):
            pass

        def is_set(self):
            return self.rounds <= 0

        def set(self):
            self.rounds = 0

        def wait(self, seconds):
            waits.append(seconds)
            self.rounds -= 1

    def _attempt():
        if not next(outcomes):
            raise AuthError("Login failed - check email and password")
    connector.fetch_all = _attempt
    connector.update_vehicles = _attempt
    connector._stop_event = _Stop(10)  # pylint: disable=protected-access
    connector._background_loop()  # pylint: disable=protected-access
    assert waits[:8] == [60, 120, 240, 480, 960, 1920, 3600, 3600]
    assert waits[9] == 60


def test_charge_type_rate_and_remaining_time_mapped(connector):
    """The curated charging fields ported from the HA integration (charge type,
    charge rate, remaining time) map onto native CarConnectivity attributes."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaElectricVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k0", "dataFieldName": "battery_state_report.soc", "value": "55"},
        {"key": "kc", "dataFieldName": "car_captured_time", "value": "2026-05-29T22:59:28Z"},
        {"key": "k1", "dataFieldName": "charging_state_report.charge_type", "value": "CHARGE_TYPE_DC"},
        {"key": "k2", "dataFieldName": "battery_state_report.charge_rate", "value": "120"},
        {"key": "k3", "dataFieldName": "battery_state_report.charge_rate_unit",
         "value": "CHARGE_RATE_UNIT_KM_PER_H"},
        {"key": "k4", "dataFieldName": "battery_state_report.remaining_charging_time_complete",
         "value": "1800s"},
    ]})
    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    vehicle = garage.get_vehicle(VIN)
    assert vehicle.charging.type.value == Charging.ChargingType.DC
    assert vehicle.charging.rate.value == 120
    assert vehicle.charging.rate.unit == Speed.KMH
    # remaining 1800s after the 22:59:28 capture -> 23:29:28
    assert vehicle.charging.estimated_date_reached.value.isoformat().startswith("2026-05-29T23:29:28")


def test_charge_rate_per_minute_and_miles_normalised(connector):
    """A per-minute / miles charge rate is converted to a per-hour mph speed."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaElectricVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k0", "dataFieldName": "battery_state_report.soc", "value": "55"},
        {"key": "k1", "dataFieldName": "battery_state_report.charge_rate", "value": "2"},
        {"key": "k2", "dataFieldName": "battery_state_report.charge_rate_unit",
         "value": "CHARGE_RATE_UNIT_MILES_PER_MIN"},
    ]})
    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    vehicle = garage.get_vehicle(VIN)
    assert vehicle.charging.rate.value == 120  # 2 mi/min * 60
    assert vehicle.charging.rate.unit == Speed.MPH


def test_charge_type_integer_index_resolves(connector):
    """charge_type delivered as a raw protobuf index resolves to its label and
    maps onto the charging-type enum (index 2 -> AC)."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaElectricVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k0", "dataFieldName": "battery_state_report.soc", "value": "55"},
        {"key": "k1", "dataFieldName": "charging_state_report.charge_type", "value": "2"},
    ]})
    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    assert garage.get_vehicle(VIN).charging.type.value == Charging.ChargingType.AC


class _RefreshFakeClient:
    """Fake client whose list endpoint heals once the identifier is refreshed."""

    def __init__(self, good_id, behaviour):
        self.good_id = good_id
        self.behaviour = behaviour  # "error" or "empty" for the stale identifier
        self.metadata_calls = 0
        self.payload = json.load(open(SAMPLE, "r", encoding="utf-8"))

    def get_metadata(self, vin):
        self.metadata_calls += 1
        return {"Identifier": self.good_id}

    def list_datasets(self, vin, identifier):
        if identifier == self.good_id:
            return [{"name": "20260530104136_%s.zip" % vin,
                     "createdOn": "2026-05-30T10:41:36Z"}]
        if self.behaviour == "error":
            raise ApiError("GET list -> HTTP 500")
        return []  # stale identifier returns an empty listing

    def download_dataset(self, vin, identifier, name):
        assert identifier == self.good_id, "download must use the refreshed identifier"
        return self.payload


@pytest.mark.parametrize("behaviour", ["error", "empty"])
def test_self_heals_stale_identifier(connector, behaviour):
    """A recreated portal subscription assigns a new identifier; the stored one
    goes stale and the listing errors or returns empty. The connector must
    re-fetch the identifier and retry once, recovering without a reload (#13)."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    connector.client = _RefreshFakeClient(good_id="new-ident", behaviour=behaviour)
    connector._identifiers[VIN] = "stale-ident"  # pylint: disable=protected-access

    created = connector._update_vehicle(VIN)  # pylint: disable=protected-access

    assert created is not None
    assert connector._identifiers[VIN] == "new-ident"  # pylint: disable=protected-access
    assert connector.client.metadata_calls == 1
    assert garage.get_vehicle(VIN).odometer.value == 116803


def test_no_content_latest_interval_reschedules_to_next_interval(connector):
    """A "no content" zip for the latest interval means there is simply no data
    this interval - not that the dataset is overdue. The next poll must be ~15
    min after that zip, not the ~1-min retry, and the unchanged older content
    must not be re-downloaded."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    now = datetime.now(tz=timezone.utc)
    older_name = "20260101000000_%s.zip" % VIN
    # Pretend the older content dataset was already mapped on a previous cycle.
    connector._identifiers[VIN] = "ident"  # pylint: disable=protected-access
    connector._last_dataset[VIN] = older_name  # pylint: disable=protected-access
    connector._bootstrapped.add(VIN)  # pylint: disable=protected-access

    class _FakeClient:
        def list_datasets(self, vin, identifier):
            return [
                {"name": older_name, "createdOn": (now - timedelta(minutes=30)).isoformat()},
                {"name": "%s_no_content_found.zip" % vin,
                 "createdOn": (now - timedelta(minutes=2)).isoformat()},
            ]

        def download_dataset(self, vin, identifier, name):
            raise AssertionError("must not re-download the unchanged content dataset")

    connector.client = _FakeClient()
    connector.update_vehicles()

    # newest entry was ~2 min ago -> next due ~13 min out, well above the 1-min retry.
    assert connector.interval.value > timedelta(minutes=10)


def test_no_datasets_at_all_uses_configured_interval(connector):
    """An empty listing (e.g. still provisioning) has no cadence to schedule
    from, so the connector falls back to its configured polling interval rather
    than polling every minute indefinitely (PR #43)."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)
    connector._identifiers[VIN] = "ident"  # pylint: disable=protected-access

    class _FakeClient:
        def get_metadata(self, vin):
            return {"Identifier": "ident"}  # refresh finds no new identifier

        def list_datasets(self, vin, identifier):
            return []

    connector.client = _FakeClient()
    connector.update_vehicles()

    assert connector.interval.value == timedelta(seconds=connector.active_config["interval"])


def test_overdue_dataset_keeps_short_retry(connector):
    """A late delivery has a cadence to catch up with: keep the one-minute retry
    so it is picked up as soon as it lands."""
    connector._reschedule([datetime.now(tz=timezone.utc) - timedelta(minutes=1)])  # pylint: disable=protected-access

    assert connector.interval.value == timedelta(minutes=1)


def test_future_dataset_target_keeps_cadence_schedule(connector):
    """A future delivery target still takes precedence over any fallback."""
    connector._reschedule([datetime.now(tz=timezone.utc) + timedelta(minutes=5)])  # pylint: disable=protected-access

    assert timedelta(minutes=4, seconds=59) < connector.interval.value <= timedelta(minutes=5)


def test_dataset_merge_latest_per_field():
    ds1 = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k1", "dataFieldName": "mileage.value", "value": "100"},
        {"key": "k2", "dataFieldName": "locked", "value": "true"},
    ]})
    ds2 = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k3", "dataFieldName": "mileage.value", "value": "200"},
        {"key": "k4", "dataFieldName": "battery_state_report.soc", "value": "80"},
    ]})
    merged = Dataset.merge([ds1, ds2])
    assert merged.vin == VIN
    assert merged.value_of("mileage.value") == 200
    assert merged.value_of("locked") is True
    assert merged.value_of("battery_state_report.soc") == 80


def test_dataset_merge_rejects_empty():
    with pytest.raises(ValueError, match="Cannot merge empty"):
        Dataset.merge([])


def test_dataset_field_names():
    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k1", "dataFieldName": "mileage.value", "value": "100"},
        {"key": "k2", "dataFieldName": "locked", "value": "true"},
    ]})
    assert ds.field_names == {"mileage.value", "locked"}


def test_known_mapped_fields_contains_mapped_fields():
    assert "mileage.value" in KNOWN_MAPPED_FIELDS
    assert "locked" in KNOWN_MAPPED_FIELDS
    assert "battery_state_report.soc" in KNOWN_MAPPED_FIELDS
    assert "charging_state_report.current_charge_state" in KNOWN_MAPPED_FIELDS
    assert "window_heating_state" in KNOWN_MAPPED_FIELDS
    assert "settings.target_soc" in KNOWN_MAPPED_FIELDS
    assert "min_temperature" in KNOWN_MAPPED_FIELDS
    assert "max_temperature" in KNOWN_MAPPED_FIELDS
    assert "battery_state_report.charge_power" in KNOWN_MAPPED_FIELDS
    assert "range" in KNOWN_MAPPED_FIELDS
    # Curated charging fields and flat-format (eGolf) fields are mapped too, so
    # they must not be reported as "new unmapped sensor".
    assert "charging_state_report.charge_type" in KNOWN_MAPPED_FIELDS
    assert "battery_state_report.charge_rate" in KNOWN_MAPPED_FIELDS
    assert "battery_state_report.charge_rate_unit" in KNOWN_MAPPED_FIELDS
    assert "battery_state_report.remaining_charging_time_complete" in KNOWN_MAPPED_FIELDS
    assert "state_of_charge" in KNOWN_MAPPED_FIELDS
    assert "cruising_range_primary_engine" in KNOWN_MAPPED_FIELDS
    assert "mileage" in KNOWN_MAPPED_FIELDS
    assert "battery_level_HV.value" in KNOWN_MAPPED_FIELDS
    assert "battery_level_HV.state" in KNOWN_MAPPED_FIELDS


def test_egolf_flat_fields_not_flagged_unmapped(caplog):
    """The eGolf flat-format payload's mapped fields must not trigger the
    'new unmapped sensor' notice."""
    with caplog.at_level(logging.INFO, logger="carconnectivity.connectors.vw_eu_data_act"):
        Connector._detect_unmapped_fields(EGOLF_VIN, Dataset.from_json(EGOLF_PAYLOAD))  # pylint: disable=protected-access
    assert "state_of_charge" not in caplog.text
    assert "cruising_range_primary_engine" not in caplog.text


def test_unmapped_field_detection(caplog):
    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k1", "dataFieldName": "some_new_sensor", "value": "42"},
    ]})
    with caplog.at_level(logging.INFO, logger="carconnectivity.connectors.vw_eu_data_act"):
        Connector._detect_unmapped_fields(VIN, ds)
    assert "New unmapped sensor for" in caplog.text
    assert "some_new_sensor" in caplog.text


def test_bootstrap_skips_already_bootstrapped(connector):
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    payload = json.load(open(SAMPLE, "r", encoding="utf-8"))

    class _FakeClient:
        def __init__(self):
            self.download_count = 0
        def get_metadata(self, vin):
            return {"Identifier": "ident"}
        def list_datasets(self, vin, identifier):
            return [{"name": "20260530104136_%s.zip" % vin,
                     "createdOn": "2026-05-30T10:41:36Z"}]
        def download_dataset(self, vin, identifier, name):
            self.download_count += 1
            return payload

    fake = _FakeClient()
    connector.client = fake

    # First call: bootstrap downloads everything, skips redundant normal fetch
    connector._update_vehicle(VIN)
    assert VIN in connector._bootstrapped
    assert fake.download_count == 1

    # Second call: newest dataset already in _last_dataset, no download
    connector._update_vehicle(VIN)
    assert fake.download_count == 1


def test_flat_data_egolf_payload_promotes_and_maps(connector):
    """A real eGolf flat-format payload (no dotted field names) promotes the
    vehicle to electric and maps the flat fields: state_of_charge -> drive level,
    cruising_range_primary_engine -> drive range, and the flat 'mileage' field
    -> odometer (since there is no 'mileage.value')."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=EGOLF_VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(EGOLF_VIN, vehicle)

    connector._map_dataset(EGOLF_VIN, Dataset.from_json(EGOLF_PAYLOAD))  # pylint: disable=protected-access

    vehicle = garage.get_vehicle(EGOLF_VIN)
    assert isinstance(vehicle, VWEudaElectricVehicle)
    assert vehicle.odometer.value == 100571

    drive = vehicle.get_electric_drive()
    assert drive is not None
    assert drive.level.value == 26
    assert drive.range.value == 67
    assert drive.range.unit == Length.KM


def test_phev_promotes_to_hybrid_and_maps_both_drives(connector):
    """A dataset with both battery and fuel fields -> HybridVehicle with two drives."""
    from carconnectivity.vehicle import HybridVehicle
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))
    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "a", "dataFieldName": "state_of_charge", "value": "25"},
        {"key": "b", "dataFieldName": "cruising_range_secondary_engine", "value": "11"},
        {"key": "c", "dataFieldName": "long_term_data_average_electr_engine_consumption", "value": "160"},
        {"key": "d", "dataFieldName": "fuel_level_current_level", "value": "37"},
        {"key": "e", "dataFieldName": "cruising_range_primary_engine", "value": "210"},
        {"key": "f", "dataFieldName": "long_term_data_average_fuel_consumption", "value": "14"},
    ]})
    connector._map_dataset(VIN, ds)
    v = garage.get_vehicle(VIN)
    assert isinstance(v, HybridVehicle)
    electric = v.get_electric_drive()
    combustion = v.get_combustion_drive()
    assert electric.level.value == 25 and electric.range.value == 11
    assert electric.consumption.value == 16.0   # 160 kWh/1000km -> 16.0 kWh/100km
    assert combustion.level.value == 37 and combustion.range.value == 210
    assert combustion.consumption.value == 1.4   # 14 L/1000km -> 1.4 L/100km


def test_pure_ev_stays_electric_not_hybrid(connector):
    """A battery-only dataset stays a pure electric vehicle (no combustion drive)."""
    from carconnectivity.vehicle import CombustionVehicle
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))
    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "a", "dataFieldName": "battery_state_report.soc", "value": "69"},
        {"key": "b", "dataFieldName": "range", "value": "312"},
    ]})
    connector._map_dataset(VIN, ds)
    v = garage.get_vehicle(VIN)
    assert not isinstance(v, CombustionVehicle)
    assert v.get_electric_drive().level.value == 69


def test_diesel_creates_dieseldrive_and_maps_adblue(connector):
    """A diesel dataset (numeric scr_range) must create a DieselDrive, because
    adblue_range exists only there. Writing it on a plain CombustionDrive (the
    previous behaviour) raised AttributeError on real diesels."""
    from carconnectivity.drive import DieselDrive
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))
    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "a", "dataFieldName": "fuel_level_current_level", "value": "60"},
        {"key": "b", "dataFieldName": "cruising_range_primary_engine", "value": "700"},
        {"key": "c", "dataFieldName": "scr_range", "value": "9000"},
    ]})
    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    drive = garage.get_vehicle(VIN).get_combustion_drive()
    assert isinstance(drive, DieselDrive)
    assert str(drive.type.value) == "Type.DIESEL"
    assert drive.adblue_range.value == 9000
    assert drive.adblue_range.unit == Length.KM


def test_petrol_empty_scr_range_stays_combustion_no_crash(connector):
    """Petrol/PHEV cars also carry the scr_range field but report it as an empty
    string. That must NOT be read as diesel (no DieselDrive) and must not raise."""
    from carconnectivity.drive import CombustionDrive, DieselDrive
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))
    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "a", "dataFieldName": "fuel_level_current_level", "value": "37"},
        {"key": "b", "dataFieldName": "cruising_range_primary_engine", "value": "210"},
        {"key": "c", "dataFieldName": "scr_range", "value": ""},
    ]})
    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    drive = garage.get_vehicle(VIN).get_combustion_drive()
    assert isinstance(drive, CombustionDrive)
    assert not isinstance(drive, DieselDrive)
    assert str(drive.type.value) == "Type.GASOLINE"


def test_oil_level_mapped_when_core_supports_it(connector):
    """oil_level_actual_level maps onto the combustion drive's oil_level. The
    attribute only exists in carconnectivity cores that shipped it, so the mapping
    is hasattr-guarded; the test skips (rather than fails) on an older core."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))
    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "a", "dataFieldName": "fuel_level_current_level", "value": "60"},
        {"key": "b", "dataFieldName": "oil_level_actual_level", "value": "87.5"},
    ]})
    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    drive = garage.get_vehicle(VIN).get_combustion_drive()
    if not hasattr(drive, "oil_level"):
        pytest.skip("installed carconnectivity core has no oil_level attribute yet")
    assert drive.oil_level.value == 87.5


def test_parking_brake_mapped_when_core_supports_it(connector):
    """parking_brake 1/0 maps to vehicle.parking_brake True/False, when the core
    exposes the attribute (hasattr-guarded; skips on an older core)."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))
    connector._map_dataset(VIN, Dataset.from_json({"vin": VIN, "Data": [  # pylint: disable=protected-access
        {"key": "k1", "dataFieldName": "parking_brake", "value": "1"},
    ]}))
    v = garage.get_vehicle(VIN)
    if not hasattr(v, "parking_brake"):
        pytest.skip("installed carconnectivity core has no parking_brake attribute yet")
    assert v.parking_brake.value is True

    connector._map_dataset(VIN, Dataset.from_json({"vin": VIN, "Data": [  # pylint: disable=protected-access
        {"key": "k1", "dataFieldName": "parking_brake", "value": "0"},
    ]}))
    assert garage.get_vehicle(VIN).parking_brake.value is False


def test_parking_brake_does_not_crash_on_older_core(connector):
    """Mapping parking_brake must not raise even without the core attribute."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))
    connector._map_dataset(VIN, Dataset.from_json({"vin": VIN, "Data": [  # pylint: disable=protected-access
        {"key": "k1", "dataFieldName": "parking_brake", "value": "1"},
    ]}))  # must not raise


def test_oil_level_does_not_crash_on_older_core(connector):
    """Even without oil_level in the core, mapping an oil dataset must not raise."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))
    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "a", "dataFieldName": "fuel_level_current_level", "value": "60"},
        {"key": "b", "dataFieldName": "oil_level_actual_level", "value": "87.5"},
    ]})
    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access  # must not raise


PHEV_SAMPLE = os.path.join(os.path.dirname(__file__), "phev_sample_dataset.json")


def test_phev_real_world_sample(connector):
    """Full mapping against an anonymised real flat SEAT Leon PHEV dataset."""
    from carconnectivity.vehicle import HybridVehicle
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))
    payload = json.load(open(PHEV_SAMPLE, "r", encoding="utf-8"))
    payload["vin"] = VIN
    connector._map_dataset(VIN, Dataset.from_json(payload))
    v = garage.get_vehicle(VIN)

    assert isinstance(v, HybridVehicle)
    # primary slot = petrol (combustion), secondary = electric (seatcupra convention)
    primary = v.drives.drives["primary"]
    secondary = v.drives.drives["secondary"]
    assert str(primary.type.value) == "Type.GASOLINE"
    assert str(secondary.type.value) == "Type.ELECTRIC"
    assert primary.range.value == 210 and primary.level.value == 37        # petrol
    assert secondary.range.value == 11 and secondary.level.value == 25     # electric
    assert primary.consumption.value == 1.4                               # L/100km
    assert secondary.consumption.value == 16.0                            # kWh/100km
    # vehicle-level
    assert v.odometer.value == 40208
    assert v.outside_temperature.value == 39.0
    # status objects populated
    assert len(v.doors.doors) == 6
    assert v.doors.doors["front_right"].open_state.value.value == "open"
    assert len(v.windows.windows) == 5
    assert v.lights.lights["parking"].light_state.value.value == "off"
    # maintenance distance preserved
    assert v.maintenance.inspection_due_after.value == 23500


def test_charge_mode_normalisation():
    """_charge_mode maps the data-dictionary tokens (with or without the
    CHARGE_MODE_SELECTION_ prefix) to the enum, None stays None, unknown -> UNKNOWN."""
    assert _charge_mode(None) is None
    assert _charge_mode("") is None
    assert _charge_mode("CHARGE_MODE_SELECTION_TIMERCHARGING") == VWEudaChargeMode.TIMER
    assert _charge_mode("CHARGE_MODE_SELECTION_TIMER_CHARGING_CLIMATIZATION") \
        == VWEudaChargeMode.TIMER_CHARGING_WITH_CLIMATISATION
    assert _charge_mode("CHARGE_MODE_SELECTION_PREFERRED_CHARGING_TIMES") \
        == VWEudaChargeMode.PREFERRED_CHARGING_TIMES
    assert _charge_mode("manual") == VWEudaChargeMode.MANUAL
    assert _charge_mode("timer") == VWEudaChargeMode.TIMER
    assert _charge_mode("something_new") == VWEudaChargeMode.UNKNOWN


def test_charge_mode_dotted_mapping(connector):
    garage = connector.car_connectivity.garage
    vehicle = VWEudaElectricVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({
        "vin": VIN,
        "Data": [
            {"key": "k1", "dataFieldName": "settings.charge_mode_selection",
             "value": "CHARGE_MODE_SELECTION_PREFERRED_CHARGING_TIMES"},
        ]
    })

    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    v = garage.get_vehicle(VIN)
    assert v.charging.settings.charge_mode.value == VWEudaChargeMode.PREFERRED_CHARGING_TIMES


def test_charge_mode_flat_options_mapping(connector):
    """Flat/continuous format: the active per-option boolean wins."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaElectricVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({
        "vin": VIN,
        "Data": [
            {"key": "k1", "dataFieldName": "charge_mode_selection_options.manual", "value": "false"},
            {"key": "k2", "dataFieldName": "charge_mode_selection_options.timer_charging", "value": "true"},
        ]
    })

    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    v = garage.get_vehicle(VIN)
    assert v.charging.settings.charge_mode.value == VWEudaChargeMode.TIMER


def test_charge_mode_absent_leaves_attribute_unset(connector):
    """A vehicle that does not report the field gets no charge_mode attribute
    (no regression on brands/platforms without it, e.g. many VWs)."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaElectricVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({
        "vin": VIN,
        "Data": [
            {"key": "k1", "dataFieldName": "mileage.value", "value": "100"},
        ]
    })

    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    v = garage.get_vehicle(VIN)
    assert getattr(v.charging.settings, "charge_mode", None) is None


def test_flat_charge_power_and_rate_deci_scaling(connector):
    """Flat-format charge power/rate are deci integers (99 -> 9.9)."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaElectricVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({
        "vin": VIN,
        "Data": [
            {"key": "k1", "dataFieldName": "battery_state_report.soc", "value": "50"},
            {"key": "k2", "dataFieldName": "charging_power", "value": "99"},
            {"key": "k3", "dataFieldName": "actual_charge_rate", "value": "99"},
        ]
    })

    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    v = garage.get_vehicle(VIN)
    assert v.charging.power.value == 9.9   # 99 / 10 kW
    assert v.charging.rate.value == 9.9    # 99 / 10 (km/h default)


def test_dotted_charge_power_takes_precedence_over_flat(connector):
    """The dotted battery_state_report.charge_power is already kW: when present,
    the flat deci-kW field must be ignored (no mis-scaling)."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaElectricVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({
        "vin": VIN,
        "Data": [
            {"key": "k1", "dataFieldName": "battery_state_report.soc", "value": "50"},
            {"key": "k2", "dataFieldName": "battery_state_report.charge_power", "value": "7.4"},
            {"key": "k3", "dataFieldName": "charging_power", "value": "99"},
        ]
    })

    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    assert garage.get_vehicle(VIN).charging.power.value == 7.4   # dotted wins, no /10


def test_freshest_numeric_by_prefix_skips_enum_companion():
    """The prefix lookup returns the numeric physicalValue and skips the
    companion value_type enum; unknown prefixes return None."""
    ds = Dataset.from_json({
        "vin": VIN,
        "Data": [
            {"key": "k1", "dataFieldName": "energy_contents.maximal_energy_content.value_type",
             "value": "SOME_ENUM"},
            {"key": "k2", "dataFieldName": "energy_contents.maximal_energy_content.physicalValue",
             "value": "128"},
        ]
    })
    assert ds.freshest_numeric_by_prefix("energy_contents.maximal_energy_content") == 128
    assert ds.freshest_numeric_by_prefix("energy_contents.current_energy_content") is None


def test_battery_available_capacity_mapping(connector):
    """maximal_energy_content maps to battery.available_capacity in kWh (value/10)."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaElectricVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({
        "vin": VIN,
        "Data": [
            {"key": "k1", "dataFieldName": "battery_state_report.soc", "value": "50"},
            {"key": "k2", "dataFieldName": "energy_contents.maximal_energy_content.physicalValue",
             "value": "128"},
        ]
    })

    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    drive = garage.get_vehicle(VIN).get_electric_drive()
    assert drive is not None
    assert drive.battery.available_capacity.value == 12.8  # 128 / 10 kWh


def test_request_type_threads_through_client(monkeypatch):
    """client request_type defaults to 'partial' (unchanged behaviour) and, when
    set to 'all', changes both the metadata path segment and the type header."""
    from carconnectivity_connectors.vw_eu_data_act.client import EudaApiClient
    client = EudaApiClient("user@example.com", "secret")
    monkeypatch.setattr(client, "ensure_login", lambda: None)

    seen = {}

    def fake_get_json(url, *, headers=None, _retry=True):
        seen["url"] = url
        seen["headers"] = headers
        return {"Identifier": "id"} if "metadata" in url else []
    monkeypatch.setattr(client, "_get_json", fake_get_json)

    client.get_metadata("VIN1")
    assert seen["url"].endswith("/metadata/partial")
    client.get_metadata("VIN1", "all")
    assert seen["url"].endswith("/metadata/all")

    client.list_datasets("VIN1", "id", "all")
    assert seen["headers"] == {"type": "all"}
    client.list_datasets("VIN1", "id")
    assert seen["headers"] == {"type": "partial"}

    captured = {}

    class _FakeResp:
        status_code = 200
        content = b""

    def fake_session_get(url, *, headers=None):
        captured["headers"] = headers
        return _FakeResp()
    monkeypatch.setattr(client, "_session_get", fake_session_get)
    monkeypatch.setattr(client, "_unzip_json", lambda content, name: {})

    client.download_dataset("VIN1", "id", "file.zip", "all")
    assert captured["headers"] == {"filename": "file.zip", "type": "all"}
    client.download_dataset("VIN1", "id", "file.zip")
    assert captured["headers"] == {"filename": "file.zip", "type": "partial"}


def test_freshest_max_value_prefers_highest_equal_freshness():
    """Two equally-fresh mileage slots: by_field takes the stable smallest-UUID
    (which can be the lower reading); freshest_max_value_of prefers the highest."""
    ds = Dataset.from_json({
        "vin": VIN,
        "Data": [
            {"key": "aaaaaaaa-0000-0000-0000-000000000000", "dataFieldName": "mileage.value",
             "value": "70876", "timestampUtc": "2026-05-31T14:11:43.000Z"},
            {"key": "bbbbbbbb-0000-0000-0000-000000000000", "dataFieldName": "mileage.value",
             "value": "70908", "timestampUtc": "2026-05-31T14:11:43.000Z"},
        ]
    })
    assert ds.by_field("mileage.value").value == 70876          # arbitrary stable choice
    assert ds.freshest_max_value_of("mileage.value") == 70908   # highest slot wins


def test_freshest_max_value_single_and_absent():
    ds = Dataset.from_json({
        "vin": VIN,
        "Data": [{"key": "k1", "dataFieldName": "mileage.value", "value": "12345"}],
    })
    assert ds.freshest_max_value_of("mileage.value") == 12345
    assert ds.freshest_max_value_of("does.not.exist") is None


def test_odometer_prefers_highest_slot(connector):
    """The mapped odometer never reads low when a dataset carries several
    equally-fresh mileage slots."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    ds = Dataset.from_json({
        "vin": VIN,
        "Data": [
            {"key": "aaaaaaaa-0000-0000-0000-000000000000", "dataFieldName": "mileage.value",
             "value": "70876", "timestampUtc": "2026-05-31T14:11:43.000Z"},
            {"key": "bbbbbbbb-0000-0000-0000-000000000000", "dataFieldName": "mileage.value",
             "value": "70908", "timestampUtc": "2026-05-31T14:11:43.000Z"},
        ]
    })

    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    assert garage.get_vehicle(VIN).odometer.value == 70908


# --- login verdict (issues #29, #49 and VW's October 2026 login changes) ------

PORTAL = "https://eu-data-act.drivesomethinggreater.com"
IDENTITY = "https://identity.vwgroup.io"
CALLBACK_URL = PORTAL + "/services/callbacklogin?state=ch__en__VOLKSWAGEN_PASSENGER_CARS&code=x"
USER_PAGE = PORTAL + "/content/euda/ch/en/user.html"
MARKETING_PAGE = IDENTITY + "/signin-service/v1/consent/marketing/user-1234/xxx@apps_vw-dilab_com/0"


class _FakeResp:
    """Minimal stand-in for requests.Response in login tests."""

    def __init__(self, url, status_code=200, text="", headers=None, cookies=None):
        self.url = url
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.cookies = cookies or {}


def _redirect(url, location, cookies=None):
    return _FakeResp(url, status_code=302, headers={"Location": location}, cookies=cookies)


def _callback_resp(cookies=None):
    """The portal callback: sets access_token and redirects to the CMS page."""
    return _redirect(CALLBACK_URL, USER_PAGE,
                     cookies={"access_token": "tok"} if cookies is None else cookies)


def _login_client(routes=None, accept=False):
    """Client whose GETs are answered from ``routes`` (url without query ->
    response) and recorded, so tests can assert what was (not) fetched."""
    client = EudaApiClient(email="u", password="p", accept_terms_on_login=accept)
    client.gets = []

    def _get(url, **kwargs):
        client.gets.append((url, kwargs))
        return (routes or {})[url.split("?", 1)[0]]

    client._session.get = _get
    return client


def test_login_callback_with_access_token_succeeds():
    """The callback response setting access_token is the success verdict. Its
    redirect to the localized CMS page (missing for e.g. "ch", issue #29) is
    never followed, and no session probe is made afterwards."""
    client = _login_client()
    client._finish_login(_callback_resp())  # pylint: disable=protected-access
    assert not client.gets


def test_login_callback_without_access_token_fails():
    client = _login_client()
    with pytest.raises(AuthError, match="access_token cookie missing"):
        client._finish_login(_callback_resp(cookies={}))  # pylint: disable=protected-access


def test_login_callback_error_fails():
    client = _login_client()
    with pytest.raises(AuthError, match=r"callback rejected \(HTTP 401\)"):
        client._finish_login(_FakeResp(CALLBACK_URL, status_code=401))  # pylint: disable=protected-access


def test_login_4xx_without_callback_still_fails():
    """A 4xx at the end of a chain that never reached the portal callback is a
    real login failure and must keep raising AuthError."""
    client = _login_client()
    resp = _FakeResp(IDENTITY + "/signin-service/v1/x/login/authenticate", status_code=400)
    with pytest.raises(AuthError, match="Login rejected"):
        client._finish_login(resp)  # pylint: disable=protected-access


@pytest.mark.parametrize("url", [IDENTITY + "/signin-service/v1/x/login/authenticate", CALLBACK_URL])
@pytest.mark.parametrize("status", [404, 500, 503])
def test_login_identity_outage_is_not_a_credentials_failure(url, status):
    """5xx/404 at the end of the chain is VW failing, not the password: still an
    AuthError (keeps the login back-off) but saying so."""
    client = _login_client()
    with pytest.raises(AuthError, match=rf"identity service unavailable .*\(HTTP {status}\)") as excinfo:
        client._finish_login(_FakeResp(url, status_code=status))  # pylint: disable=protected-access
    assert "check email and password" not in str(excinfo.value)


def test_login_rate_limit_honours_retry_after():
    client = _login_client()
    resp = _FakeResp(CALLBACK_URL, status_code=429, headers={"Retry-After": "120"})
    with pytest.raises(TooManyRequestsError) as excinfo:
        client._finish_login(resp)  # pylint: disable=protected-access
    assert excinfo.value.retry_after == 120


@pytest.mark.parametrize("status, error", [(503, AuthError), (404, AuthError), (429, TooManyRequestsError)])
def test_login_sign_in_page_outage_is_reported_as_such(status, error):
    """A failing sign-in page used to surface as "could not parse the sign-in
    form" and never reached the identifier POST."""
    client = _login_client({IDENTITY + "/oidc/v1/authorize": _FakeResp(IDENTITY + "/signin-service/v1/x/login",
                                                                        status_code=status)})
    client._session.post = lambda *a, **k: pytest.fail("identifier POST after a failed sign-in page")
    with pytest.raises(error) as excinfo:
        client.login()
    assert "parse the sign-in form" not in str(excinfo.value)


def test_login_bad_credentials_still_detected():
    """Bad credentials re-render the identity sign-in page (HTTP 200, URL still
    on signin-service): detection must be unchanged."""
    client = _login_client()
    resp = _FakeResp(IDENTITY + "/signin-service/v1/x/login/authenticate", status_code=200)
    with pytest.raises(AuthError, match="check email and password"):
        client._finish_login(resp)  # pylint: disable=protected-access


def test_login_stopping_on_portal_page_is_not_success():
    """Only the callback proves the login: any other portal page is not enough,
    and the message keeps the query (codes, ids) out."""
    client = _login_client()
    with pytest.raises(AuthError, match=r"did not complete \(ended at https://eu-data-act[^?]*/login\)"):
        client._finish_login(_FakeResp(PORTAL + "/login?code=secret"))  # pylint: disable=protected-access


def test_login_terms_interstitial_gets_specific_message():
    """The terms-and-conditions interstitial (issue #15) is a real login stop,
    but the generic "check email and password" message is misleading there: a
    dedicated message must point at the terms acceptance instead."""
    client = _login_client()
    resp = _FakeResp(IDENTITY + "/signin-service/v1/x/terms-and-conditions"
                     "?relayState=y&updated=dataprivacy", status_code=200)
    with pytest.raises(AuthError, match="terms and conditions"):
        client._finish_login(resp)  # pylint: disable=protected-access


def test_login_consent_interstitial_gets_specific_message():
    """A consent page the chain could not skip (issue #49) is not a credentials
    problem: the message must say so, name the consent kind and keep the user
    id out of the log."""
    client = _login_client()
    with pytest.raises(AuthError, match=r"consent question \(marketing\)") as excinfo:
        client._finish_login(_FakeResp(MARKETING_PAGE, status_code=200))  # pylint: disable=protected-access
    assert "user-1234" not in str(excinfo.value)
    assert "check email and password" not in str(excinfo.value)


def test_login_redirects_stop_at_callback():
    """Redirects are followed by hand (no automatic redirects) and stop at the
    callback response: the localized CMS page is never fetched."""
    callback = _callback_resp()
    identity_callback = IDENTITY + "/oidc/v1/oauth/client/callback"
    client = _login_client({
        identity_callback: _redirect(identity_callback, CALLBACK_URL),
        PORTAL + "/services/callbacklogin": callback,
    })
    start = _redirect(IDENTITY + "/signin-service/v1/x/login/authenticate",
                      "/oidc/v1/oauth/client/callback?code=y")
    assert client._follow_login_redirects(start) is callback  # pylint: disable=protected-access
    assert [url.split("?", 1)[0] for url, _ in client.gets] == [
        identity_callback, PORTAL + "/services/callbacklogin"]
    assert all(kwargs["allow_redirects"] is False for _, kwargs in client.gets)
    assert not any("user.html" in url for url, _ in client.gets)


def test_login_skips_marketing_consent_without_consenting():
    """The optional marketing consent page (issue #49) is skipped the way VW's
    evcc update does it: its callback is followed, the page itself is never
    fetched, and the callback query is re-encoded (raw spaces in scopes)."""
    identity_callback = IDENTITY + "/oidc/v1/oauth/client/callback"
    client = _login_client({
        identity_callback: _redirect(identity_callback, CALLBACK_URL),
        PORTAL + "/services/callbacklogin": _callback_resp(),
    })
    start = _redirect(IDENTITY + "/signin-service/v1/x/login/authenticate",
                      MARKETING_PAGE + "?" + urlencode({"callback": identity_callback + "?scopes=openid cars&x=1"}))
    client._finish_login(client._follow_login_redirects(start))  # pylint: disable=protected-access
    fetched = [url for url, _ in client.gets]
    assert fetched[0] == identity_callback + "?scopes=openid+cars&x=1"
    assert not any("/consent/marketing/" in url for url in fetched)


@pytest.mark.parametrize("callback", [
    "http://identity.vwgroup.io/oidc/v1/oauth/client/callback",
    "https://example.org/oidc/v1/oauth/client/callback",
    "https://identity.vwgroup.io/unexpected/callback",
    "",
])
def test_login_marketing_consent_with_unusable_callback_is_reported(callback):
    """A marketing callback that is not https on the identity host with a known
    path is never followed: the page is reported as a consent question."""
    client = _login_client({MARKETING_PAGE: _FakeResp(MARKETING_PAGE + "?callback=z")})
    query = "?" + urlencode({"callback": callback}) if callback else ""
    start = _redirect(IDENTITY + "/signin-service/v1/x/login/authenticate", MARKETING_PAGE + query)
    with pytest.raises(AuthError, match=r"consent question \(marketing\)"):
        client._finish_login(client._follow_login_redirects(start))  # pylint: disable=protected-access
    assert [url.split("?", 1)[0] for url, _ in client.gets] == [MARKETING_PAGE]


@pytest.mark.parametrize("location", [
    "https://attacker.example/content/euda/de/de/user.html",
    "http://eu-data-act.drivesomethinggreater.com/services/callbacklogin",
    "weconnect://callback?code=x",
])
def test_login_redirect_to_unexpected_location_fails(location):
    client = _login_client()
    start = _redirect(IDENTITY + "/signin-service/v1/x/login/authenticate", location)
    with pytest.raises(AuthError, match="unexpected location"):
        client._follow_login_redirects(start)  # pylint: disable=protected-access
    assert not client.gets


def test_login_redirect_loop_is_bounded():
    loop = IDENTITY + "/loop"
    client = _login_client({loop: _redirect(loop, loop)})
    with pytest.raises(AuthError, match="exceeded 10 hops"):
        client._follow_login_redirects(_redirect(loop, loop))  # pylint: disable=protected-access
    assert len(client.gets) == 10


SIGNIN_HTML = """<form action="/signin-service/v1/x/login/identifier">
<input name="_csrf" value="c1"><input name="relayState" value="r1"><input name="hmac" value="h1"></form>"""
AUTHENTICATE_HTML = """<script>window._IDK = { templateModel: {"hmac": "h2", "relayState": "r2"},
csrf_token: 'c2' };</script>
<form action="/signin-service/v1/x/login/authenticate"></form>"""


def test_full_login_flow_follows_vw_requirements():
    """End to end: no priming GET of the portal home page, credentials POSTed
    without automatic redirects, and the login ends on the callback without
    fetching the CMS page or probing the API."""
    client = _login_client({
        IDENTITY + "/oidc/v1/authorize": _FakeResp(IDENTITY + "/signin-service/v1/x/login", text=SIGNIN_HTML),
        PORTAL + "/services/callbacklogin": _callback_resp(),
    })
    posts = []

    def _post(url, data=None, **kwargs):
        posts.append((url, data, kwargs))
        if url.endswith("/identifier"):
            return _FakeResp(IDENTITY + "/signin-service/v1/x/login/authenticate?relayState=r2",
                             text=AUTHENTICATE_HTML)
        return _redirect(url, CALLBACK_URL)

    client._session.post = _post
    client.login()

    fetched = [url.split("?", 1)[0] for url, _ in client.gets]
    assert fetched == [IDENTITY + "/oidc/v1/authorize", PORTAL + "/services/callbacklogin"]
    assert posts[1][0] == IDENTITY + "/signin-service/v1/x/login/authenticate"
    assert posts[1][1]["password"] == "p"
    assert posts[1][2]["allow_redirects"] is False
    assert client._logged_in  # pylint: disable=protected-access


def test_user_agent_is_rfc9110_product_token():
    """VW asked community clients for a product token, not a browser UA."""
    ua = EudaApiClient(email="u", password="p")._session.headers["User-Agent"]
    assert re.fullmatch(r"carconnectivity-connector-vw-eu-data-act/[!#$%&'*+\-.^_`|~0-9A-Za-z]+", ua)
    assert "Mozilla" not in ua


# --- datapoints identified by key only (issues #12 / #33) --------------------

def test_dataset_lookup_by_key():
    """Datapoints whose dataFieldName carries no meaning must be reachable by
    their portal key."""
    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": KEY_PRIMARY_RANGE, "dataFieldName": "value", "value": "386"},
        {"key": "57b433c8-3dcc-3fbf-9b38-74f8f6480682", "dataFieldName": "value", "value": "7"},
    ]})

    assert ds.value_by_key(KEY_PRIMARY_RANGE) == 386
    assert ds.by_key(KEY_PRIMARY_RANGE).field_name == "value"
    assert ds.value_by_key("no-such-key") is None


def test_range_by_key_when_no_named_field(connector):
    """An ID.4 reporting no cruising_range_* field at all delivers the range as
    a bare 'value' datapoint identified only by its key (issue #33): it must
    still reach drive.range."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaElectricVehicle(vin=VIN, garage=garage, managing_connector=connector))

    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "ae0294b4-1286-3e98-a818-1485b8d88430", "dataFieldName": "state_of_charge",
         "value": "80"},
        {"key": KEY_PRIMARY_RANGE, "dataFieldName": "value", "value": "386"},
    ]})
    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    drive = garage.get_vehicle(VIN).get_electric_drive()
    assert drive.range.value == 386
    assert drive.range.unit == Length.KM


def test_range_by_key_honours_companion_unit(connector):
    """The bare range has a companion 'unit' datapoint, also key-identified."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaElectricVehicle(vin=VIN, garage=garage, managing_connector=connector))

    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "ae0294b4-1286-3e98-a818-1485b8d88430", "dataFieldName": "state_of_charge",
         "value": "80"},
        {"key": KEY_PRIMARY_RANGE, "dataFieldName": "value", "value": "240"},
        {"key": KEY_PRIMARY_RANGE_UNIT, "dataFieldName": "unit", "value": "MILES"},
    ]})
    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    assert garage.get_vehicle(VIN).get_electric_drive().range.unit == Length.MI


def test_named_range_field_wins_over_key(connector):
    """The by-key lookup is a fallback: a vehicle sending the named field keeps
    using it."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaElectricVehicle(vin=VIN, garage=garage, managing_connector=connector))

    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "ae0294b4-1286-3e98-a818-1485b8d88430", "dataFieldName": "state_of_charge",
         "value": "80"},
        {"key": "55e0d40b-38ed-3cb5-9dcd-6193df6fc493",
         "dataFieldName": "cruising_range_primary_engine", "value": "300"},
        {"key": KEY_PRIMARY_RANGE, "dataFieldName": "value", "value": "999"},
    ]})
    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access

    assert garage.get_vehicle(VIN).get_electric_drive().range.value == 300


def test_range_key_ignored_on_phev(connector):
    """The key holds the *primary* range, which is the combustion one as soon
    as there is an engine: it must never be pushed onto the electric drive of a
    hybrid, including on a snapshot that carries no fuel field of its own."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaElectricVehicle(vin=VIN, garage=garage, managing_connector=connector))

    # First snapshot establishes the hybrid drivetrain (electric + fuel).
    connector._map_dataset(VIN, Dataset.from_json({"vin": VIN, "Data": [  # pylint: disable=protected-access
        {"key": "ae0294b4-1286-3e98-a818-1485b8d88430", "dataFieldName": "state_of_charge",
         "value": "80"},
        {"key": "f1000000-0000-0000-0000-000000000001",
         "dataFieldName": "fuel_level_current_level", "value": "33"},
    ]}))

    # A later snapshot without any named range must NOT publish the primary
    # (fuel) range as the electric one.
    connector._map_dataset(VIN, Dataset.from_json({"vin": VIN, "Data": [  # pylint: disable=protected-access
        {"key": "ae0294b4-1286-3e98-a818-1485b8d88430", "dataFieldName": "state_of_charge",
         "value": "80"},
        {"key": KEY_PRIMARY_RANGE, "dataFieldName": "value", "value": "999"},
    ]}))

    assert garage.get_vehicle(VIN).get_electric_drive().range.value is None


def test_bare_value_not_reported_as_unmapped():
    """Bare 'value'/'unit' leaves are handled by key, so they must not show up
    in the unmapped-sensor diagnostics."""
    assert 'value' in KNOWN_MAPPED_FIELDS
    assert 'unit' in KNOWN_MAPPED_FIELDS


# --- terms-and-conditions auto-acceptance (issue #15) ------------------------

TERMS_URL = ("https://identity.vwgroup.io/signin-service/v1/xxx@apps_vw-dilab_com/"
             "terms-and-conditions?relayState=rs1&updated=dataprivacy")

# Shape observed in issue #15: the acceptance form lives in the JS
# templateModel, with the pending document under legalDocuments[0].
TERMS_HTML = """
<script>
window._IDK = {
  templateModel: {"relayState": "rs1", "hmac": "h1", "countryOfResidence": "ch",
    "loginUrl": "/signin-service/v1/xxx@apps_vw-dilab_com/terms-and-conditions",
    "legalDocuments": [{"name": "dataPrivacy", "textId": "t16",
      "majorVersion": 16, "minorVersion": 1,
      "skippable": false, "declinable": true, "saveLink": "/save",
      "skipLink": "/skip", "declineLink": "/decline", "changeSummary": "upd"}]},
  csrf_token: 'tok123'
};
</script>
"""


def _terms_client(accept, post_result):
    """Client whose POSTs are captured."""
    client = EudaApiClient(email="u", password="p", accept_terms_on_login=accept)
    calls = []

    def _post(url, data=None, **kwargs):
        calls.append((url, data))
        return post_result

    client._session.post = _post
    return client, calls


def test_terms_interstitial_accepted_when_opted_in():
    """With accept_terms_on_login, the acceptance form embedded in the
    interstitial's templateModel is POSTed (issue #15 recipe: countryOfResidence
    upper-cased, legalDocuments[0].* flattened with yes/no booleans, link and
    version keys excluded, _csrf from csrf_token) and the login completes."""
    client, calls = _terms_client(True, _callback_resp())
    client._finish_login(_FakeResp(TERMS_URL, status_code=200, text=TERMS_HTML))  # pylint: disable=protected-access

    assert len(calls) == 1
    url, data = calls[0]
    assert url == "https://identity.vwgroup.io/signin-service/v1/xxx@apps_vw-dilab_com/terms-and-conditions"
    assert data["relayState"] == "rs1"
    assert data["hmac"] == "h1"
    assert data["_csrf"] == "tok123"
    assert data["countryOfResidence"] == "CH"
    assert data["legalDocuments[0].skippable"] == "no"
    assert data["legalDocuments[0].declinable"] == "yes"
    assert data["legalDocuments[0].saveLink"] == "/save"
    assert data["legalDocuments[0].name"] == "dataPrivacy"
    for excluded in ("majorVersion", "minorVersion", "skipLink", "declineLink", "changeSummary"):
        assert f"legalDocuments[0].{excluded}" not in data


def test_terms_interstitial_still_raises_without_opt_in():
    """Without the opt-in, the interstitial keeps raising the dedicated
    AuthError and nothing is POSTed on the user's behalf."""
    client, calls = _terms_client(False, None)
    with pytest.raises(AuthError, match="terms and conditions"):
        client._finish_login(_FakeResp(TERMS_URL, status_code=200, text=TERMS_HTML))  # pylint: disable=protected-access
    assert not calls


def test_terms_acceptance_does_not_loop():
    """If the IdP serves the interstitial again after the acceptance POST, the
    login fails instead of retrying forever."""
    still_terms = _FakeResp(TERMS_URL, status_code=200, text=TERMS_HTML)
    client, calls = _terms_client(True, still_terms)
    with pytest.raises(AuthError, match="terms and conditions"):
        client._finish_login(_FakeResp(TERMS_URL, status_code=200, text=TERMS_HTML))  # pylint: disable=protected-access
    assert len(calls) == 1


# --- session headers (issue #15) ---------------------------------------------

def test_session_sends_locale_headers():
    """The session must send a browser-like header set: the IdP routes
    header-less clients to legal interstitials a browser never sees (issue
    #15). Accept-Language follows the configured country/language."""
    client = EudaApiClient(email="u", password="p", country="ch", language="fr")
    assert client._session.headers["Accept-Language"] == "fr-CH,fr;q=0.9,en;q=0.8"
    assert "text/html" in client._session.headers["Accept"]
    assert "*/*" in client._session.headers["Accept"]


def test_session_accept_language_default_and_english():
    """Default locale (si/sl) and an English locale (no duplicate 'en' tail)."""
    default = EudaApiClient(email="u", password="p")
    assert default._session.headers["Accept-Language"] == "sl-SI,sl;q=0.9,en;q=0.8"
    english = EudaApiClient(email="u", password="p", country="ie", language="en")
    assert english._session.headers["Accept-Language"] == "en-IE,en;q=0.9"


# --- real ID.4 export (issue #33) --------------------------------------------

ID4_SAMPLE = os.path.join(os.path.dirname(__file__), "id4_sample_dataset.json")


def _load_id4() -> Dataset:
    with open(ID4_SAMPLE, "r", encoding="utf-8-sig") as fh:
        return Dataset.from_json(json.load(fh))


def test_id4_export_has_no_named_range_field():
    """Premise of issue #33, locked against future edits of the fixture: this
    vehicle reports no cruising_range_* field whatsoever, only the bare
    key-identified 'value' entry."""
    ds = _load_id4()
    for name in ("range", "cruising_range_primary_engine",
                 "cruising_range_secondary_engine", "cruising_range_combined"):
        assert ds.by_field(name) is None
    assert ds.value_by_key(KEY_PRIMARY_RANGE) == 386


def test_id4_real_export_maps_range_and_total(connector):
    """End-to-end on the real anonymised export contributed in issue #33: the
    key-identified range reaches drive.range, and the vehicle's total range
    equals it since this EV has a single drive."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))

    connector._map_dataset(VIN, _load_id4())  # pylint: disable=protected-access

    v = garage.get_vehicle(VIN)
    assert isinstance(v, VWEudaElectricVehicle)
    drive = v.get_electric_drive()
    assert drive.range.value == 386
    assert drive.range.unit == Length.KM
    assert drive.level.value == 80
    assert v.drives.total_range.value == 386


def test_total_range_key_fallback_skipped_on_hybrid(connector):
    """The single-drive fallback must not fire once the car has an engine: the
    key holds the primary (combustion) range there."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))

    connector._map_dataset(VIN, Dataset.from_json({"vin": VIN, "Data": [  # pylint: disable=protected-access
        {"key": "h1", "dataFieldName": "fuel_level_current_level", "value": "50"},
        {"key": "h2", "dataFieldName": "state_of_charge", "value": "80"},
        {"key": KEY_PRIMARY_RANGE, "dataFieldName": "value", "value": "999"},
    ]}))

    assert garage.get_vehicle(VIN).drives.total_range.value is None


# --- combined range reconstruction -------------------------------------------

def _range_dataset(entries):
    return Dataset.from_json({"vin": VIN, "Data": [
        {"key": f"r{i}", "dataFieldName": name, "value": value}
        for i, (name, value) in enumerate(entries)
    ]})


def test_combined_range_reconstructed_from_engines(connector):
    """A PHEV reporting both per-engine ranges but an empty combined slot must
    still get a total range, summed from the components."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))

    connector._map_dataset(VIN, _range_dataset([  # pylint: disable=protected-access
        ("cruising_range_primary_engine", "630"),
        ("cruising_range_secondary_engine", "51"),
        ("cruising_range_combined", ""),
        ("fuel_level_current_level", "100"),
        ("state_of_charge", "88"),
    ]))

    assert garage.get_vehicle(VIN).drives.total_range.value == 681


def test_portal_combined_range_wins_over_sum(connector):
    """A value actually reported by the portal takes precedence over the sum."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))

    connector._map_dataset(VIN, _range_dataset([  # pylint: disable=protected-access
        ("cruising_range_primary_engine", "630"),
        ("cruising_range_secondary_engine", "51"),
        ("cruising_range_combined", "700"),
        ("fuel_level_current_level", "100"),
        ("state_of_charge", "88"),
    ]))

    assert garage.get_vehicle(VIN).drives.total_range.value == 700


def test_combined_range_single_engine(connector):
    """With a single per-engine range the total equals it, and a dataset with
    no per-engine range at all leaves the total unset."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))
    connector._map_dataset(VIN, _range_dataset([  # pylint: disable=protected-access
        ("cruising_range_primary_engine", "420"),
    ]))
    assert garage.get_vehicle(VIN).drives.total_range.value == 420

    other = "WVWZZZE1ZLP000999"
    garage.add_vehicle(other, VWEudaVehicle(vin=other, garage=garage, managing_connector=connector))
    connector._map_dataset(other, Dataset.from_json({"vin": other, "Data": [  # pylint: disable=protected-access
        {"key": "x1", "dataFieldName": "mileage.value", "value": "1000"},
    ]}))
    assert garage.get_vehicle(other).drives.total_range.value is None


# --- maintenance countdown sign ----------------------------------------------

def _maintenance_dataset(insp_dist, oil_dist):
    return Dataset.from_json({"vin": VIN, "Data": [
        {"key": "m1", "dataFieldName": "maintenance_interval_distance_until_inspection",
         "value": str(insp_dist)},
        {"key": "m2", "dataFieldName": "maintenance_interval_distance_until_oil_change",
         "value": str(oil_dist)},
    ]})


def test_maintenance_distance_remaining_is_positive(connector):
    """The portal counts down toward zero, so a negative reading means "still
    remaining" and must surface as a positive distance."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))

    connector._map_dataset(VIN, _maintenance_dataset(-23500, -12000))  # pylint: disable=protected-access

    v = garage.get_vehicle(VIN)
    assert v.maintenance.inspection_due_after.value == 23500
    assert v.maintenance.oil_service_due_after.value == 12000


def test_maintenance_distance_overdue_stays_negative(connector):
    """A positive portal reading means the service is overdue. Taking the
    absolute value would render "overdue by 500 km" exactly like "500 km
    remaining"; negating keeps the two states apart."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))

    connector._map_dataset(VIN, _maintenance_dataset(500, 1200))  # pylint: disable=protected-access

    v = garage.get_vehicle(VIN)
    assert v.maintenance.inspection_due_after.value == -500
    assert v.maintenance.oil_service_due_after.value == -1200


def test_maintenance_distance_due_now_is_zero(connector):
    """Zero is the due point and must stay zero."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))

    connector._map_dataset(VIN, _maintenance_dataset(0, 0))  # pylint: disable=protected-access

    v = garage.get_vehicle(VIN)
    assert v.maintenance.inspection_due_after.value == 0
    assert v.maintenance.oil_service_due_after.value == 0


# --- Born battery_level_HV SoC (issue #38) -----------------------------------

BORN_REDUCED_SAMPLE = os.path.join(os.path.dirname(__file__), "born_reduced_sample_dataset.json")
BORN_FULL_SAMPLE = os.path.join(os.path.dirname(__file__), "born_full_sample_dataset.json")


def _load_born(path) -> Dataset:
    with open(path, "r", encoding="utf-8-sig") as fh:
        return Dataset.from_json(json.load(fh))


def test_born_reduced_export_premise():
    """Premise of issue #38, locked against future edits of the fixture: the
    reduced delivery shape (car parked and asleep) carries no named SoC field
    at all; the state of charge only arrives as battery_level_HV."""
    ds = _load_born(BORN_REDUCED_SAMPLE)
    assert ds.by_field("battery_state_report.soc") is None
    assert ds.by_field("state_of_charge") is None
    assert ds.value_of("battery_level_HV.value") == 67.0
    assert ds.value_of("battery_level_HV.state") == "VALID"


def test_born_reduced_maps_soc_from_battery_level_hv(connector):
    """End-to-end on the real Born export contributed in issue #38:
    battery_level_HV.value reaches drive.level, which in turn unblocks the
    attributes the core derives from it (range_estimated_full, consumption)."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))

    connector._map_dataset(VIN, _load_born(BORN_REDUCED_SAMPLE))  # pylint: disable=protected-access
    # The fetch loop flushes notifications after mapping; the core's derived
    # attributes are computed by on_transaction_end observers.
    connector.car_connectivity.transaction_end()

    v = garage.get_vehicle(VIN)
    assert isinstance(v, VWEudaElectricVehicle)
    drive = v.get_electric_drive()
    assert drive.level.value == 67.0
    assert drive.range.value == 325
    assert v.drives.total_range.value == 325
    # Derived by the core once level exists: 325 km at 67 % -> ~485 km full,
    # and 77.25 kWh usable over that projected range -> ~15.9 kWh/100km.
    assert drive.range_estimated_full.value == pytest.approx(485.07, abs=0.01)
    assert drive.battery.available_capacity.value == pytest.approx(77.25)
    assert drive.consumption.value == pytest.approx(15.93, abs=0.01)


def test_born_full_maps_soc_from_named_report(connector):
    """The full delivery shape has both SoC sources agreeing; mapping stays on
    the named report and the rest of the dataset maps as usual."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))

    connector._map_dataset(VIN, _load_born(BORN_FULL_SAMPLE))  # pylint: disable=protected-access

    v = garage.get_vehicle(VIN)
    assert isinstance(v, VWEudaElectricVehicle)
    assert v.get_electric_drive().level.value == 72
    assert v.get_electric_drive().range.value == 394
    assert v.odometer.value == 10256


def test_battery_level_hv_yields_to_named_soc(connector):
    """Where both fields are present the named report wins. They agree on every
    real export seen so far; this locks the priority in case they diverge."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))

    connector._map_dataset(VIN, Dataset.from_json({"vin": VIN, "Data": [  # pylint: disable=protected-access
        {"key": "s1", "dataFieldName": "battery_state_report.soc", "value": "50"},
        {"key": "s2", "dataFieldName": "battery_level_HV.value", "value": "60.0"},
    ]}))

    assert garage.get_vehicle(VIN).get_electric_drive().level.value == 50


def test_battery_level_hv_alone_promotes_electric(connector):
    """battery_level_HV is a traction-battery field, so its presence alone must
    promote the vehicle to electric and fill the drive level."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))

    connector._map_dataset(VIN, Dataset.from_json({"vin": VIN, "Data": [  # pylint: disable=protected-access
        {"key": "s1", "dataFieldName": "battery_level_HV.value", "value": "55.0"},
    ]}))

    v = garage.get_vehicle(VIN)
    assert isinstance(v, VWEudaElectricVehicle)
    assert v.get_electric_drive().level.value == 55.0


def test_battery_level_hv_not_flagged_unmapped(caplog):
    """Once mapped, battery_level_HV must no longer be reported as a new
    unmapped sensor (the reduced Born export is full of fields that still are;
    only this family is asserted here)."""
    with caplog.at_level(logging.INFO, logger="carconnectivity.connectors.vw_eu_data_act"):
        Connector._detect_unmapped_fields(VIN, _load_born(BORN_REDUCED_SAMPLE))  # pylint: disable=protected-access
    assert "battery_level_HV" not in caplog.text


# --- flat format: per-field timestamps (premise lock) ------------------------

FLAT_TS_SAMPLE = os.path.join(os.path.dirname(__file__), "flat_timestamped_sample_dataset.json")


def test_flat_export_premise_per_field_timestamps():
    """Premise lock on a real anonymised flat-format export: every entry carries
    its own timestampUtc and there is no report-level car_captured_time entry,
    the mirror image of the dotted MEB shape (see the Born fixtures). The four
    distinct stamp groups span 13 hours, which is why fields of one dataset must
    not be read as simultaneous (see README, dataset-semantics notes)."""
    with open(FLAT_TS_SAMPLE, "r", encoding="utf-8-sig") as fh:
        payload = json.load(fh)
    entries = payload["Data"]
    assert all(e.get("timestampUtc") for e in entries)
    assert not any(e.get("dataFieldName") == "car_captured_time" for e in entries)
    stamps = {e["timestampUtc"] for e in entries}
    assert len(stamps) == 4

    ds = Dataset.from_json(payload)
    # Nothing is named car_captured_time on this format: captured_at is derived
    # from the newest per-field timestampUtc, so values are stamped with a
    # measurement time instead of the delivery slot time.
    assert ds.captured_at == datetime(2026, 7, 31, 10, 40, 28, tzinfo=timezone.utc)


MEB44 = os.path.join(os.path.dirname(__file__), "meb_issue44_sample_dataset.json")


def _meb44_payload() -> dict:
    with open(MEB44, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _with(payload: dict, **overrides) -> dict:
    """Copy of a payload with every entry of the named fields set to the given value."""
    data = [dict(item) for item in payload["Data"]]
    for item in data:
        if item["dataFieldName"] in overrides:
            item["value"] = overrides[item["dataFieldName"]]
    return {**payload, "Data": data}


def test_repeated_capture_time_never_becomes_a_clock_stamp(connector):
    """mikrohard#44: core swaps last_updated for now() when it is handed the same
    measured time twice; the connector must never trigger that, or every later
    real capture (always older than the clock) is refused as 'Value from the past'."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)
    payload = _meb44_payload()
    captured = datetime(2026, 9, 7, 6, 31, 49, tzinfo=timezone.utc)

    ds = Dataset.from_json(payload)
    assert ds.captured_at == captured
    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access
    vehicle = garage.get_vehicle(VIN)
    assert vehicle.odometer.value == 53533
    assert vehicle.odometer.last_updated == captured
    assert vehicle.doors.lock_state.last_updated == captured

    # Quiet delivery: same capture time, same values -> no write, no clock stamp.
    connector._map_dataset(VIN, Dataset.from_json(payload))  # pylint: disable=protected-access
    assert vehicle.odometer.last_updated == captured
    assert vehicle.doors.lock_state.last_updated == captured
    assert vehicle.window_heatings.heating_state.last_updated == captured

    # Changed value under the same capture time: taken, and the measurement time
    # stays a measurement time (nudged, not replaced by the clock).
    connector._map_dataset(VIN, Dataset.from_json(_with(payload, **{"mileage.value": "53540"})))  # pylint: disable=protected-access
    assert vehicle.odometer.value == 53540
    assert timedelta(0) < vehicle.odometer.last_updated - captured < timedelta(seconds=1)

    # A genuinely newer capture, delivered later, is accepted: the reporter's
    # 06:31:49Z was refused behind a 06:58:25 clock stamp on 0.3.0.
    newer = _with(payload, **{"mileage.value": "53560", "car_captured_time": "2026-09-07T06:58:49Z"})
    connector._map_dataset(VIN, Dataset.from_json(newer))  # pylint: disable=protected-access
    assert vehicle.odometer.value == 53560
    assert vehicle.odometer.last_updated == datetime(2026, 9, 7, 6, 58, 49, tzinfo=timezone.utc)


def test_nudged_stamp_still_counts_as_the_same_capture(connector):
    """mikrohard#44 follow-up (reporter's log, 2026-09-23 18:03 -> 18:47 -> 19:01):
    values changed under an unchanged capture time are stamped one microsecond
    past it; the next delivery under that same capture time must be compared to
    the capture, not refused as 'Value from the past' behind our own nudge."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)
    payload = _meb44_payload()
    captured = datetime(2026, 9, 7, 6, 31, 49, tzinfo=timezone.utc)
    connector._map_dataset(VIN, Dataset.from_json(payload))  # pylint: disable=protected-access
    vehicle = garage.get_vehicle(VIN)

    # 18:47: changed under the same capture time -> nudged.
    connector._map_dataset(VIN, Dataset.from_json(_with(payload, **{"mileage.value": "53540"})))  # pylint: disable=protected-access
    nudged = vehicle.odometer.last_updated
    assert nudged == captured + timedelta(microseconds=1)

    # 19:01, same values: nothing to write, the stamp stays put.
    connector._map_dataset(VIN, Dataset.from_json(_with(payload, **{"mileage.value": "53540"})))  # pylint: disable=protected-access
    assert vehicle.odometer.value == 53540
    assert vehicle.odometer.last_updated == nudged

    # 19:01, changed again: taken, still a measurement time, not the clock.
    connector._map_dataset(VIN, Dataset.from_json(_with(payload, **{"mileage.value": "53545"})))  # pylint: disable=protected-access
    assert vehicle.odometer.value == 53545
    assert vehicle.odometer.last_updated == nudged + timedelta(microseconds=1)

    # A genuinely older capture is still refused.
    older = _with(payload, **{"mileage.value": "53500", "car_captured_time": "2026-09-07T06:31:48Z"})
    connector._map_dataset(VIN, Dataset.from_json(older))  # pylint: disable=protected-access
    assert vehicle.odometer.value == 53545


COPIES = os.path.join(os.path.dirname(__file__), "meb_charging_report_copies_dataset.json")
SOC_LIVE_KEY = "506cb83e-f99f-3af3-bbeb-0429b69a78d9"
CHARGE_STATE_LIVE_KEY = "a08cca2b-ed42-37bc-b160-d015ce205d3d"


def _copies_payload(*drop_keys: str) -> dict:
    """The #44 reporter's 2026-09-18 ID.4 delivery, taken mid charging session,
    optionally without the entries carrying ``drop_keys``."""
    with open(COPIES, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return {**payload, "Data": [e for e in payload["Data"] if e["key"] not in drop_keys]}


def test_report_copies_premise():
    """Premise lock: the delivery carries the live readings and the charging-job
    copies side by side, with conflicting values."""
    by_key = {e["key"]: e["value"] for e in _copies_payload()["Data"]}
    assert by_key[SOC_LIVE_KEY] == "76"
    assert by_key["7bddd5e7-43a4-3878-bd63-9502782f77a5"] == "34"  # SoC when charging started
    assert by_key[CHARGE_STATE_LIVE_KEY] == "CHARGE_STATE_NOT_READY_FOR_CHARGING"
    assert by_key["3581f1e0-580b-3f18-aa73-6fbf037b841e"] == "CHARGE_STATE_READY_FOR_CHARGING"


def test_live_report_reading_wins_over_job_copies():
    """#44 follow-up: the smallest-UUID tie-break picked the READY copy of
    current_charge_state over the live NOT_READY reading."""
    ds = Dataset.from_json(_copies_payload())
    assert ds.value_of("battery_state_report.soc") == 76
    assert ds.value_of("charging_state_report.current_charge_state") == "CHARGE_STATE_NOT_READY_FOR_CHARGING"


def test_soc_copy_never_stands_in_for_the_live_soc(connector):
    """A delivery holding only the 'SoC when charging is started' copy must not
    become the vehicle SoC (the reporter's history sat at 34 % for hours): the
    connector falls back to battery_level_HV.value instead."""
    ds = Dataset.from_json(_copies_payload(SOC_LIVE_KEY))
    assert ds.value_of("battery_state_report.soc") is None

    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))
    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access
    assert garage.get_vehicle(VIN).get_electric_drive().level.value == 76


def test_job_copy_is_a_fallback_for_same_meaning_fields():
    """Copies that carry the same meaning still stand in when the live reading
    is missing, so no value is lost."""
    ds = Dataset.from_json(_copies_payload(CHARGE_STATE_LIVE_KEY))
    assert ds.value_of("charging_state_report.current_charge_state") == "CHARGE_STATE_READY_FOR_CHARGING"


def test_merge_keeps_live_reading_over_a_later_job_copy():
    """Bootstrap merge: a later dataset carrying only a job copy does not
    replace the live reading from an earlier one."""
    live = Dataset.from_json(_copies_payload())
    copy_only = Dataset.from_json(_copies_payload(CHARGE_STATE_LIVE_KEY))
    merged = Dataset.merge([live, copy_only])
    assert merged.value_of("charging_state_report.current_charge_state") == "CHARGE_STATE_NOT_READY_FOR_CHARGING"


def test_remaining_climate_time_is_not_a_countdown(connector):
    """#44 follow-up: the dotted remaining_climate_time is the AC duration once
    started, not a countdown; mapped as capture time + 1800 s it moved the
    climatisation end date forward on every delivery, AC running or not."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))
    connector._map_dataset(VIN, Dataset.from_json(_copies_payload()))  # pylint: disable=protected-access
    assert garage.get_vehicle(VIN).climatization.estimated_date_reached.value is None


def test_remaining_climatisation_time_still_maps(connector):
    """The flat format's remaining_climatisation_time is a real countdown in
    minutes and keeps feeding the climatisation end date."""
    garage = connector.car_connectivity.garage
    garage.add_vehicle(VIN, VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector))
    captured = datetime(2026, 9, 23, 14, 51, 7, tzinfo=timezone.utc)
    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k1", "dataFieldName": "remaining_climatisation_time", "value": "10",
         "timestampUtc": "2026-09-23T14:51:07Z"},
    ]})
    connector._map_dataset(VIN, ds)  # pylint: disable=protected-access
    assert garage.get_vehicle(VIN).climatization.estimated_date_reached.value == captured + timedelta(minutes=10)


def test_measured_falls_back_to_created_on_never_none(connector):
    """A dataset with no capture time at all must be stamped with the delivery's
    createdOn, not left to core, which would store the wall clock and then refuse
    every later real capture."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)
    created = datetime(2026, 9, 7, 10, 30, 0, tzinfo=timezone.utc)
    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k1", "dataFieldName": "mileage.value", "value": "100"},
    ]})
    assert ds.captured_at is None
    connector._map_dataset(VIN, ds, created)  # pylint: disable=protected-access
    vehicle = garage.get_vehicle(VIN)
    assert vehicle.odometer.last_updated == created
