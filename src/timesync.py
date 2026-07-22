"""
Clock-drift correction and precise waiting.

Your OS clock can easily be 100ms-2s off, which is fatal for a sniper.
This module measures the offset between your local clock and a trusted
source, then gives you a high-precision "sleep until this exact instant".
"""

from __future__ import annotations

import socket
import struct
import time
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


# NTP epoch (1900) vs Unix epoch (1970) offset in seconds.
_NTP_UNIX_DELTA = 2_208_988_800


def _ntp_offset(server: str, timeout: float = 5.0) -> float:
    """
    Return (server_time - local_time) in seconds using SNTP.

    Positive means the local clock is BEHIND the true time, so we must fire
    that much earlier. Raises on any network/parse failure.
    """
    packet = b"\x1b" + 47 * b"\0"  # LI=0, VN=3, Mode=3 (client)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        t0 = time.time()
        sock.sendto(packet, (server, 123))
        data, _ = sock.recvfrom(48)
        t3 = time.time()
    finally:
        sock.close()

    # Transmit timestamp lives at bytes 40-47 (seconds, fraction).
    secs, frac = struct.unpack("!II", data[40:48])
    server_time = (secs - _NTP_UNIX_DELTA) + (frac / 2**32)
    # Approximate one-way delay by halving round trip.
    local_mid = t0 + (t3 - t0) / 2
    return server_time - local_mid


def _http_date_offset(url: str, timeout: float = 5.0) -> float:
    """
    Coarse fallback: read the server's HTTP `Date` header (1s resolution).
    Returns (server_time - local_time) in seconds.
    """
    req = urllib.request.Request(url, method="HEAD")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        t3 = time.time()
        date_hdr = resp.headers.get("Date")
    if not date_hdr:
        raise RuntimeError("No Date header on response")
    server_dt = parsedate_to_datetime(date_hdr)
    if server_dt.tzinfo is None:
        server_dt = server_dt.replace(tzinfo=timezone.utc)
    server_time = server_dt.timestamp()
    local_mid = t0 + (t3 - t0) / 2
    return server_time - local_mid


def measure_offset(method: str, ntp_server: str, http_url: str) -> float:
    """
    Measure clock offset in seconds using the configured method, with a
    graceful fallback chain: ntp -> http -> 0.0. Never raises.
    """
    if method == "ntp":
        try:
            return _ntp_offset(ntp_server)
        except Exception as exc:  # noqa: BLE001
            print(f"[timesync] NTP failed ({exc}); falling back to HTTP Date.")
            method = "http"
    if method == "http":
        try:
            return _http_date_offset(http_url)
        except Exception as exc:  # noqa: BLE001
            print(f"[timesync] HTTP Date failed ({exc}); trusting local clock.")
            return 0.0
    return 0.0


def true_now(offset: float) -> float:
    """Best estimate of true Unix time given a measured offset."""
    return time.time() + offset


def sleep_until(target_epoch: float, offset: float) -> None:
    """
    Block until the corrected clock reaches target_epoch.

    Sleeps coarsely until ~200ms out, then busy-waits for sub-millisecond
    landing. Busy-waiting the tail is deliberate: time.sleep() can overshoot
    by several ms, which is exactly what we're trying to avoid at T-0.
    """
    while True:
        remaining = target_epoch - true_now(offset)
        if remaining <= 0:
            return
        if remaining > 0.2:
            time.sleep(remaining - 0.2)
        else:
            # Tight spin for the final stretch.
            while target_epoch - true_now(offset) > 0:
                pass
            return


def describe_offset(offset: float) -> str:
    direction = "behind" if offset > 0 else "ahead"
    return (
        f"local clock is {abs(offset) * 1000:.0f}ms {direction} of true time "
        f"(true now: {datetime.fromtimestamp(true_now(offset), tz=timezone.utc).isoformat()})"
    )
