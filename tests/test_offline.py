"""Offline tests: dataset parsing and field -> attribute mapping.

These run without any network access. The mapping test builds a real
CarConnectivity garage and exercises the connector's ``_map_dataset`` against
the sample dataset shipped by the EU Data Act portal.
"""
import json
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
from carconnectivity_connectors.vw_eu_data_act.connector import Connector, _filename_timestamp, KNOWN_MAPPED_FIELDS
from carconnectivity_connectors.vw_eu_data_act.dataset import Dataset
from carconnectivity_connectors.vw_eu_data_act.vehicle import VWEudaElectricVehicle, VWEudaVehicle

SAMPLE = os.path.join(os.path.dirname(__file__), "sample_dataset.json")
VIN = "WVWZZZE1ZLP010257"


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


def test_unmapped_field_detection(caplog):
    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k1", "dataFieldName": "some_new_sensor", "value": "42"},
    ]})
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

    # First call: bootstrap + normal fetch = 2 downloads
    connector._update_vehicle(VIN)
    assert VIN in connector._bootstrapped
    assert fake.download_count == 2

    # Second call: only normal fetch = 1 download
    connector._update_vehicle(VIN)
    assert fake.download_count == 3


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


def test_unmapped_field_detection(caplog):
    ds = Dataset.from_json({"vin": VIN, "Data": [
        {"key": "k1", "dataFieldName": "some_new_sensor", "value": "42"},
    ]})
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

    # First call: bootstrap + normal fetch = 2 downloads
    connector._update_vehicle(VIN)
    assert VIN in connector._bootstrapped
    assert fake.download_count == 2

    # Second call: only normal fetch = 1 download
    connector._update_vehicle(VIN)
    assert fake.download_count == 3
