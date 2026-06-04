"""Offline tests: dataset parsing and field -> attribute mapping.

These run without any network access. The mapping test builds a real
CarConnectivity garage and exercises the connector's ``_map_dataset`` against
the sample dataset shipped by the EU Data Act portal.
"""
import json
import os

import pytest

from carconnectivity.carconnectivity import CarConnectivity
from carconnectivity.charging import Charging
from carconnectivity.doors import Doors
from carconnectivity.observable import Observable
from carconnectivity.window_heating import WindowHeatings

import requests

from carconnectivity.units import Length

from carconnectivity_connectors.vw_eu_data_act.client import ApiError, EudaApiClient
from carconnectivity_connectors.vw_eu_data_act.connector import Connector, _filename_timestamp
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


# ---------------------------------------------------------------------------
# eGolf / flat-format tests
# ---------------------------------------------------------------------------

def test_extract_flat_fields_detects_egolf_format(connector):
    """A payload with a Data array and no dotted field names is identified as
    flat (eGolf) format and returns a populated dict."""
    fields = connector._extract_flat_fields(EGOLF_PAYLOAD)  # pylint: disable=protected-access
    assert fields == {
        "state_of_charge": "26",
        "cruising_range_primary_engine": "67",
        "charging_state": "charging",
        "mileage": "100571",
        "lock_state": "locked",
        "outside_temperature": "2956",
    }


def test_extract_flat_fields_rejects_idx_format(connector):
    """A payload with dotted field names (ID.x nested format) must return {}
    so the flat path is never triggered for ID.x vehicles."""
    idxpayload = {"vin": VIN, "Data": [
        {"key": "k1", "dataFieldName": "battery_state_report.soc", "value": "69"},
        {"key": "k2", "dataFieldName": "charging_state_report.current_charge_state",
         "value": "CHARGE_STATE_NOT_READY_FOR_CHARGING"},
    ]}
    assert connector._extract_flat_fields(idxpayload) == {}  # pylint: disable=protected-access


def test_extract_flat_fields_rejects_none(connector):
    """None and non-dict inputs must return {} without raising."""
    assert connector._extract_flat_fields(None) == {}   # pylint: disable=protected-access
    assert connector._extract_flat_fields({}) == {}     # pylint: disable=protected-access


def test_egolf_ev_promotion(connector):
    """A plain VWEudaVehicle must be promoted to VWEudaElectricVehicle when
    the flat mapper runs, just as the ID.x path promotes on seeing EV fields."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=EGOLF_VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(EGOLF_VIN, vehicle)

    connector._map_dataset(EGOLF_VIN, Dataset.from_json(EGOLF_PAYLOAD),  # pylint: disable=protected-access
                           payload=EGOLF_PAYLOAD)

    assert isinstance(garage.get_vehicle(EGOLF_VIN), VWEudaElectricVehicle)


def test_egolf_state_of_charge(connector):
    """state_of_charge maps onto drive.level (%)."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=EGOLF_VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(EGOLF_VIN, vehicle)

    connector._map_dataset(EGOLF_VIN, Dataset.from_json(EGOLF_PAYLOAD),  # pylint: disable=protected-access
                           payload=EGOLF_PAYLOAD)

    drive = garage.get_vehicle(EGOLF_VIN).get_electric_drive()
    assert drive is not None
    assert drive.level.value == 26


def test_egolf_cruising_range(connector):
    """cruising_range_primary_engine maps onto drive.range (km)."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=EGOLF_VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(EGOLF_VIN, vehicle)

    connector._map_dataset(EGOLF_VIN, Dataset.from_json(EGOLF_PAYLOAD),  # pylint: disable=protected-access
                           payload=EGOLF_PAYLOAD)

    drive = garage.get_vehicle(EGOLF_VIN).get_electric_drive()
    assert drive.range.value == 67


def test_egolf_charging_state_mapping(connector):
    """charging_state values map correctly via FLAT_CHARGE_STATE_MAPPING."""
    garage = connector.car_connectivity.garage

    for flat_value, expected in [
        ("charging",     Charging.ChargingState.CHARGING),
        ("not_charging", Charging.ChargingState.OFF),
        ("complete",     Charging.ChargingState.CONSERVATION),
        ("unknown_val",  Charging.ChargingState.UNKNOWN),
    ]:
        vin = EGOLF_VIN + flat_value  # unique VIN per iteration
        vehicle = VWEudaVehicle(vin=vin, garage=garage, managing_connector=connector)
        garage.add_vehicle(vin, vehicle)
        payload = {"vin": vin, "Data": [
            {"key": "k1", "dataFieldName": "state_of_charge", "value": "50"},
            {"key": "k2", "dataFieldName": "charging_state", "value": flat_value},
        ]}
        connector._map_dataset(vin, Dataset.from_json(payload),  # pylint: disable=protected-access
                               payload=payload)
        assert garage.get_vehicle(vin).charging.state.value == expected, \
            f"charging_state '{flat_value}' should map to {expected}"


def test_egolf_mileage(connector):
    """mileage maps onto vehicle.odometer in km."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=EGOLF_VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(EGOLF_VIN, vehicle)

    connector._map_dataset(EGOLF_VIN, Dataset.from_json(EGOLF_PAYLOAD),  # pylint: disable=protected-access
                           payload=EGOLF_PAYLOAD)

    v = garage.get_vehicle(EGOLF_VIN)
    assert v.odometer.value == 100571
    assert v.odometer.unit == Length.KM


def test_egolf_lock_state(connector):
    """lock_state 'locked' maps to Doors.LockState.LOCKED."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=EGOLF_VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(EGOLF_VIN, vehicle)

    connector._map_dataset(EGOLF_VIN, Dataset.from_json(EGOLF_PAYLOAD),  # pylint: disable=protected-access
                           payload=EGOLF_PAYLOAD)

    assert garage.get_vehicle(EGOLF_VIN).doors.lock_state.value == Doors.LockState.LOCKED


def test_egolf_idxpayload_not_hijacked(connector):
    """An ID.x payload must never be routed through the flat mapper — the
    detection heuristic (dotted field names -> ID.x) must hold firm."""
    garage = connector.car_connectivity.garage
    vehicle = VWEudaVehicle(vin=VIN, garage=garage, managing_connector=connector)
    garage.add_vehicle(VIN, vehicle)

    idxpayload = json.load(open(SAMPLE, "r", encoding="utf-8"))
    connector._map_dataset(VIN, Dataset.from_json(idxpayload),  # pylint: disable=protected-access
                           payload=idxpayload)

    v = garage.get_vehicle(VIN)
    # Must have been mapped via the ID.x path, not the flat path
    assert isinstance(v, VWEudaElectricVehicle)
    assert v.odometer.value == 116803
    drive = v.get_electric_drive()
    assert drive.level.value == 69  # from battery_state_report.soc, not state_of_charge
