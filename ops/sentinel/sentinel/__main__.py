"""Run the sentinel: `python -m sentinel` (every SENTINEL interval_s, default 60 s)."""

import logging
import signal
import time

from . import api, config
from .core import Sentinel

log = logging.getLogger("sentinel")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = config.load()
    if not cfg["token"]:
        log.warning("SENTINEL_TOKEN is empty: PersonalOS will refuse events and the API answers 401")
    sen = Sentinel(cfg)
    api.serve(sen, cfg["listen"])
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    log.info("sentinel %s started (instance %s, warm-up %s min)", sen.instance, sen.instance, cfg["thresholds"]["warmup_min"])
    while not stop["now"]:
        started = time.monotonic()
        try:
            out = sen.tick()
            if any(out.values()):
                log.info("tick: %s", out)
        except Exception:  # noqa: BLE001 - keep watching
            log.exception("tick failed")
        wait = cfg["interval_s"] - (time.monotonic() - started)
        while wait > 0 and not stop["now"]:
            time.sleep(min(wait, 1.0))
            wait -= 1.0


if __name__ == "__main__":
    main()
