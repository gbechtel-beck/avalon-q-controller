"""
LAN discovery for Avalon Q miners.

Strategy:
  1. Determine the local /24 subnet (or a user-supplied CIDR).
  2. Probe each host's TCP port 4028 in parallel.
  3. For any host that responds, send a `version` query and check that the
     response identifies as an Avalon-class CGMiner build.

This is intentionally simple — no SSDP/mDNS/UPnP. Avalon firmwares don't
expose those, but they do expose the CGMiner API on port 4028 and respond
to a `version` query immediately.

The scan is asynchronous and bounded; default concurrency 64 keeps it from
flooding hobby-grade home routers.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
from dataclasses import dataclass

log = logging.getLogger(__name__)

DEFAULT_PORT = 4028
DEFAULT_CONCURRENCY = 64
DEFAULT_TIMEOUT = 1.5


@dataclass
class DiscoveredMiner:
    host: str
    port: int
    looks_like: str          # "avalon" | "cgminer" | "unknown"
    version_raw: str
    api_version: str | None
    miner_model: str | None


def local_subnet_guess() -> str | None:
    """
    Guess the local /24 by inspecting the address bound to a UDP socket
    aimed at a public IP. We never actually send anything.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("1.1.1.1", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        net = ipaddress.ip_interface(f"{ip}/24").network
        return str(net)
    except OSError:
        return None


async def scan(
    cidr: str | None = None,
    port: int = DEFAULT_PORT,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[DiscoveredMiner]:
    """Scan a CIDR (default: locally-detected /24) for miners on TCP port."""
    if not cidr:
        cidr = local_subnet_guess()
    if not cidr:
        raise ValueError("Could not determine local subnet; specify CIDR")

    network = ipaddress.ip_network(cidr, strict=False)
    if network.num_addresses > 1024:
        raise ValueError(
            f"Refusing to scan {network.num_addresses} addresses; "
            f"limit a /22 (1024) or smaller"
        )

    sem = asyncio.Semaphore(concurrency)
    targets = [str(h) for h in network.hosts()]

    async def probe_one(host: str) -> DiscoveredMiner | None:
        async with sem:
            return await _probe(host, port, timeout)

    results = await asyncio.gather(*[probe_one(h) for h in targets])
    return [r for r in results if r is not None]


async def _probe(host: str, port: int, timeout: float) -> DiscoveredMiner | None:
    """Open a connection, send `{"command":"version"}`, parse response."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except (OSError, asyncio.TimeoutError):
        return None

    try:
        writer.write(json.dumps({"command": "version"}).encode("utf-8"))
        await writer.drain()
        data = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        raw = data.decode("utf-8", errors="replace").rstrip("\x00").strip()
    except (OSError, asyncio.TimeoutError):
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        return None
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    return _classify(host, port, raw)


def _classify(host: str, port: int, raw: str) -> DiscoveredMiner | None:
    if not raw:
        return None
    looks_like = "unknown"
    api_version = None
    miner_model = None

    try:
        obj = json.loads(raw)
        ver = (obj.get("VERSION") or [{}])[0]
        api_version = ver.get("API")
        miner_model = ver.get("PROD") or ver.get("MODEL") or ver.get("HWTYPE")
        prod = (miner_model or "").lower()
        if "avalon" in prod or "ava" in prod:
            looks_like = "avalon"
        elif api_version:
            looks_like = "cgminer"
    except json.JSONDecodeError:
        # Some firmwares return plain-text VERSION lines.
        if "avalon" in raw.lower():
            looks_like = "avalon"
        elif "cgminer" in raw.lower():
            looks_like = "cgminer"

    if looks_like == "unknown":
        return None
    return DiscoveredMiner(
        host=host,
        port=port,
        looks_like=looks_like,
        version_raw=raw[:500],
        api_version=api_version,
        miner_model=miner_model,
    )
