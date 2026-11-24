"""Values read from the boiler, and how they are logged and charted."""

# name -> minimum seconds between reads (0 = every pass)
VALUES = {
    "Flame": 0,
    "Statenumber": 0,
    "FanSpeed": 0,
    "IonisationVoltageLevel": 0,
    "HwcWaterflow": 0,
    "FlowTemp": 0,
    "Gasvalve": 0,
    "PumpPower": 0,
    "Ignitor": 2,
    "ReturnTemp": 2,
    "HwcTemp": 2,
    "PositionValveSet": 2,
    "HwcDemand": 2,
    "StorageTemp": 10,
    "WaterPressure": 30,
    "Currenterror": 30,
    "DeactivationsTemplimiter": 30,
    "DeactivationsIFC": 30,
    "CounterStartattempts1": 30,
}

# Numeric values only appear in the change list once they have moved this far
# from the last listed value. Everything else (on/off, status numbers,
# counters, error codes) is listed on every change.
THRESHOLDS = {
    "FlowTemp": 1.0,
    "ReturnTemp": 1.0,
    "HwcTemp": 1.0,
    "StorageTemp": 1.0,
    "HwcWaterflow": 0.5,
    "FanSpeed": 200,
    "WaterPressure": 0.05,
    "IonisationVoltageLevel": 10,
    "PumpPower": 10,
}

# Values kept as a time series for the charts; switches are stored as 1/0.
SAMPLE_KEYS = [
    "FlowTemp",
    "ReturnTemp",
    "HwcTemp",
    "rise",
    "FanSpeed",
    "HwcWaterflow",
    "PumpPower",
    "WaterPressure",
    "IonisationVoltageLevel",
    "Flame",
    "Gasvalve",
    "HwcDemand",
    "Statenumber",
    "PositionValveSet",
]
SWITCHES = {"Flame", "Gasvalve", "HwcDemand"}

CHANGE_HISTORY = 500
# samples further apart than this are not joined (and give no rise value)
GAP_MS = 15000

# Long chart windows use per-bucket summaries (mean/min/max of each sample
# value), kept in memory and appended to <csv name>.<tier>.csv:
# (name, bucket ms, minutes kept)
TIERS = [
    ("1min", 60_000, 2 * 1440),
    ("15min", 15 * 60_000, 31 * 1440),
]
