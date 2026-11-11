"""Entry point: `python3 -m halcyon`."""

from __future__ import annotations

import logging
import sys

from .api import build_server
from .brain import build_brain
from .concierge import Concierge
from .config import ConfigError, Settings
from .store import Store
from .venues import VenueService


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    log = logging.getLogger("halcyon")

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.data_dir / "halcyon.db")
    venues = VenueService(
        db_path=settings.data_dir / "venues.db",
        latency_s=settings.venue_latency_s,
    )
    brain = build_brain(settings)
    concierge = Concierge(store=store, venues=venues, brain=brain, settings=settings)
    concierge.recover_unanswered()

    server = build_server(settings, concierge)
    log.info("Halcyon concierge listening on %s:%s using %s", settings.host, settings.port, brain.name)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        concierge.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
