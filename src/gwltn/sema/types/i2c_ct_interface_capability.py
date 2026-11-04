from typing import Literal
from gwltn.sema.base import SemaType
from gwltn.sema.enums import I2cAdcType
from gwltn.sema.property_format import NonNegativeInt
from gwltn.sema.property_format import PascalCase
from gwltn.sema.property_format import PositiveFloat
from gwltn.sema.property_format import PositiveInt


class I2cCtInterfaceCapability(SemaType):
    """Sema: https://schemas.electricity.works/types/i2c.ct.interface.capability/000"""

    name: PascalCase
    i2c_bus: PascalCase
    i2c_address: NonNegativeInt
    adc_type: I2cAdcType
    adc_reference_volts: PositiveFloat
    channels: PositiveInt
    type_name: Literal["i2c.ct.interface.capability"] = "i2c.ct.interface.capability"
    version: Literal["000"] = "000"
