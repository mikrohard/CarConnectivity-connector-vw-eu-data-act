"""Vehicle classes for the VW EU Data Act connector."""
from __future__ import annotations

from typing import TYPE_CHECKING

from carconnectivity.vehicle import GenericVehicle, ElectricVehicle

if TYPE_CHECKING:
    from typing import Optional, Dict
    from carconnectivity.garage import Garage
    from carconnectivity_connectors.base.connector import BaseConnector


class VWEudaVehicle(GenericVehicle):
    """A vehicle sourced from the Volkswagen EU Data Act portal."""

    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None,
                 managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[VWEudaVehicle] = None, initialization: Optional[Dict] = None) -> None:
        if origin is not None:
            # Promotion path (e.g. to the electric subclass): keep the manufacturer
            # already resolved from the brand on the origin vehicle; do not clobber it.
            super().__init__(garage=garage, origin=origin, initialization=initialization)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector, initialization=initialization)
            # Default label; the connector overrides it from the configured brand.
            self.manufacturer._set_value(value='Volkswagen')  # pylint: disable=protected-access


class VWEudaElectricVehicle(ElectricVehicle, VWEudaVehicle):
    """A Volkswagen electric vehicle sourced from the EU Data Act portal."""

    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None,
                 managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[VWEudaVehicle] = None, initialization: Optional[Dict] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin, initialization=initialization)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector, initialization=initialization)
