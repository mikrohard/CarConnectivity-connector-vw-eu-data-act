"""Offline tests: dataset parsing and field -> attribute mapping.

These run without any network access. The mapping test builds a real
CarConnectivity garage and exercises the connector's ``_map_dataset`` against
the sample dataset shipped by the EU Data Act portal.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest

from carconnectivity.carconnectivity import CarConnectivity
from carconnectivity.charging import Charging
from carconnectivity.doors import Doors
from carconnectivity.observable import Observable
from carconnectivity.window_heating import WindowHeatings

import requests

from carconnectivity.units import Length, Speed

from carconnectivity_connectors.vw_eu_data_act.client import ApiError, EudaApiClient
from carconnectivity_connectors.vw_eu_data_act.connector import (
    Connector, _filename_timestamp, KNOWN_MAPPED_FIELDS,
    _charge_mode, _charge_mode_flat, VWEudaChargeMode,
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


def test_no_datasets_at_all_retries_soon(connector):
    """An empty listing (e.g. still provisioning) has no cadence to schedule
    from, so the connector falls back to the short retry interval."""
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

    assert connector.interval.value == timedelta(minutes=1)


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
