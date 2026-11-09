"""Launch a simulated fleet.  Usage: python -m fleet.simulate --robots 8"""

from __future__ import annotations

import argparse
import logging
import random
import threading
import time

from fleet.outage_scheduler import OutageScheduler
from fleet.robot.robot import Robot
from fleet.robot.transport import get_bytes, open_command_connection, post_json

FIRMWARE_VERSIONS = ["1.0.0", "1.1.0", "1.2.0"]


def build_fleet(count: int, server_url: str, ws_url: str, interval_s: float,
                base_seed: int) -> list[Robot]:
    robots = []
    for i in range(count):
        robots.append(Robot(
            robot_id=f"arm-{i + 1:03d}",
            # Mixed versions so a staggered rollout has something to act on later.
            firmware_version=FIRMWARE_VERSIONS[i % len(FIRMWARE_VERSIONS)],
            heartbeat_url=f"{server_url}/api/heartbeats",
            heartbeat_interval_s=interval_s,
            sender=post_json,
            ws_url=ws_url,
            connect=open_command_connection,
            firmware_url=f"{server_url}/api/firmware",
            fetch=get_bytes,
            seed=base_seed + i,
        ))
    return robots


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate a fleet of robot arms.")
    parser.add_argument("--robots", type=int, default=8)
    parser.add_argument("--server-url", default="http://127.0.0.1:8000")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8001")
    parser.add_argument("--interval", type=float, default=2.0, help="heartbeat period (s)")
    parser.add_argument("--outage-every", type=float, default=30.0,
                        help="seconds of normal operation between outages (0 disables outages)")
    parser.add_argument("--outage-duration", type=float, default=20.0,
                        help="how long the downed robot stays offline (s)")
    parser.add_argument("--seed", type=int, default=None,
                        help="base random seed; omit for a different fleet behaviour every run")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Random by default so e.g. firmware trial outcomes differ between runs. The seed is
    # logged so an interesting run can be replayed with --seed.
    seed = args.seed if args.seed is not None else random.randrange(2**32)
    robots = build_fleet(args.robots, args.server_url, args.ws_url, args.interval, seed)
    for robot in robots:
        robot.start()
    logging.info("started %d robots -> %s (seed=%d, Ctrl-C to stop)",
                 len(robots), args.server_url, seed)

    stop = threading.Event()
    if args.outage_every > 0:
        scheduler = OutageScheduler(robots, args.outage_every, args.outage_duration,
                                    random.Random())
        threading.Thread(target=scheduler.run, args=(stop,), name="outages", daemon=True).start()
        logging.info("outages: one robot offline for %.0fs every %.0fs",
                     args.outage_duration, args.outage_every)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("stopping fleet")
        stop.set()
        for robot in robots:
            robot.stop()


if __name__ == "__main__":
    main()
