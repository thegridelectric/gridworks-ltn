"""Degrees Fahrenheit from a channel's raw integer and the encoding the
channel declares: a data channel's telemetry name or a derived channel's
output unit."""

from gwltn.sema.enums import SpaceheatTelemetryName, Unit

TemperatureEncoding = SpaceheatTelemetryName | Unit


def convert_temp_to_f(raw: int, encoding: TemperatureEncoding) -> float:
    if encoding == Unit.FahrenheitX100:
        return raw / 100
    if encoding in (
        SpaceheatTelemetryName.WaterTempCTimes1000,
        SpaceheatTelemetryName.AirTempCTimes1000,
    ):
        return raw / 1000 * 9 / 5 + 32
    if encoding == SpaceheatTelemetryName.CelsiusTimes100:
        return raw / 100 * 9 / 5 + 32
    if encoding in (
        SpaceheatTelemetryName.WaterTempFTimes1000,
        SpaceheatTelemetryName.AirTempFTimes1000,
    ):
        return raw / 1000
    raise ValueError(f"Unknown temperature encoding: {encoding}")
