"""液体温度实验条件；参考温度为人工仪表示值，不是加热器设定值。"""
from __future__ import annotations

import math


def number(value, name, lower=-273.15, upper=1e6):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name}必须是有限数值")
    if not lower < value <= upper:
        raise ValueError(f"{name}超出记录范围")
    return float(value)


def validate_metadata(value):
    if not isinstance(value, dict):
        raise ValueError("液体温度条件必须是对象")
    result = {}
    for key in ("liquid", "sample_id", "container", "geometry", "thermometer", "repeat_id"):
        text = value.get(key)
        if not isinstance(text, str) or not text.strip() or len(text) > 200:
            raise ValueError(f"请填写液体实验条件 {key}（1–200 字）")
        result[key] = text.strip()
    for key in ("target_temperature_c", "temperature_c"):
        result[key] = number(value.get(key), key)
    result["volume_ml"] = number(value.get("volume_ml"), "液体体积 mL", 0)
    result["distance_mm"] = number(value.get("distance_mm"), "参考距离 mm", -1e-12)
    result["temperature_source"] = "operator_reference_thermometer"
    return result
