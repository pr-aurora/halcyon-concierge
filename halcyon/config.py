"""Runtime settings, read from the environment once at start-up."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "claude-haiku-5-5"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    token: str
    data_dir: Path
    model: str
    anthropic_api_key: str | None
    claude_bin: str
    venue_latency_s: float
    turn_budget_s: float
    max_steps: int
    workers: int

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)

        token = env.get("AGENT_TOKEN", "").strip()
        if not token:
            raise ConfigError("AGENT_TOKEN must be set")

        try:
            port = int(env.get("PORT", "8411"))
        except ValueError as exc:
            raise ConfigError("PORT must be an integer") from exc

        api_key = env.get("ANTHROPIC_API_KEY", "").strip() or None

        return cls(
            host=env.get("HALCYON_HOST", "0.0.0.0"),
            port=port,
            token=token,
            data_dir=Path(env.get("HALCYON_DATA_DIR", str(ROOT / "data"))),
            model=env.get("HALCYON_MODEL", DEFAULT_MODEL),
            anthropic_api_key=api_key,
            claude_bin=env.get("CLAUDE_BIN", "claude"),
            venue_latency_s=float(env.get("HALCYON_VENUE_LATENCY_S", "0.15")),
            # The member gives up after 180 s of silence. Keep a margin below that.
            turn_budget_s=float(env.get("HALCYON_TURN_BUDGET_S", "120")),
            max_steps=int(env.get("HALCYON_MAX_STEPS", "4")),
            workers=int(env.get("HALCYON_WORKERS", "8")),
        )
