"""项目根 .env 配置；不修改全局环境，不建立新的 Conda 环境。"""

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    ui_interval_ms: int = 100
    trace_interval_ms: int = 100
    preview_interval_ms: int = 250
    max_display_points: int = 20000
    default_profile: str = "own"
    conda_env: str = "rf-gnuradio"
    own_profile: str = "Data/device_profiles/own_device_20261008.json"
    teacher_profile: str = "local_device.json"

    @classmethod
    def from_env(cls, root=None):
        root = Path(root or ROOT).resolve()
        values = {**dotenv_values(root / ".env"), **os.environ}

        def integer(name, default, lower, upper):
            try:
                value = int(values.get(name, default))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} 必须为整数") from exc
            if not lower <= value <= upper:
                raise ValueError(f"{name} 必须在 {lower}–{upper} 之间")
            return value

        env_name = values.get("SSPP_CONDA_ENV", "rf-gnuradio")
        if env_name != "rf-gnuradio":
            raise ValueError("本项目已约定继续使用 rf-gnuradio Conda 环境")
        profile = values.get("SSPP_DEFAULT_PROFILE", "own")
        if profile not in {"own", "teacher"}:
            raise ValueError("SSPP_DEFAULT_PROFILE 应为 own 或 teacher")
        return cls(
            root=root,
            ui_interval_ms=integer("SSPP_UI_INTERVAL_MS", 100, 30, 1000),
            trace_interval_ms=integer("SSPP_TRACE_INTERVAL_MS", 100, 40, 1000),
            preview_interval_ms=integer("SSPP_PREVIEW_INTERVAL_MS", 250, 100, 3000),
            max_display_points=integer("SSPP_MAX_DISPLAY_POINTS", 20000, 1000, 100000),
            default_profile=profile,
            conda_env=env_name,
            own_profile=values.get("SSPP_OWN_PROFILE", cls.own_profile),
            teacher_profile=values.get("SSPP_TEACHER_PROFILE", cls.teacher_profile),
        )
