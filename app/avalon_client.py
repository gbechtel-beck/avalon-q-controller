"""
Avalon Q CGMiner-compatible API client.

Communicates with the Avalon Q miner over TCP port 4028 using the CGMiner
RPC protocol. All commands open a fresh connection, send a single command,
read the response, and close. This matches the protocol's TCP-short-connection
design (see Canaan Avalon10 API manual).

Workmode codes:
  0 = Eco
  1 = Standard
  2 = Super
  3 = Standby (softoff)

Query commands return JSON. Set commands (ascset|0,...) return STATUS=...
plain-text responses, which we wrap into a normalized dict.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_PORT = 4028
DEFAULT_TIMEOUT = 8.0

# Workmode integer codes per Avalon API manual
WORKMODE_ECO = 0
WORKMODE_STANDARD = 1
WORKMODE_SUPER = 2
WORKMODE_STANDBY = 3

WORKMODE_NAMES = {
    WORKMODE_ECO: "eco",
    WORKMODE_STANDARD: "standard",
    WORKMODE_SUPER: "super",
    WORKMODE_STANDBY: "standby",
}

WORKMODE_FROM_NAME = {v: k for k, v in WORKMODE_NAMES.items()}


@dataclass
class AvalonConfig:
    host: str
    port: int = DEFAULT_PORT
    password: str = "admin"
    username: str = "root"
    timeout: float = DEFAULT_TIMEOUT


class AvalonError(Exception):
    """Raised when an API call to the miner fails."""


class AvalonClient:
    """Synchronous client for the Avalon Q CGMiner API."""

    def __init__(self, config: AvalonConfig):
        self.config = config

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------
    def _send(self, payload: str) -> str:
        """Open a TCP connection, send the payload, read until the miner closes."""
        try:
            with socket.create_connection(
                (self.config.host, self.config.port), timeout=self.config.timeout
            ) as sock:
                sock.sendall(payload.encode("utf-8"))
                # CGMiner closes after each response; read until EOF.
                chunks = []
                while True:
                    data = sock.recv(8192)
                    if not data:
                        break
                    chunks.append(data)
                raw = b"".join(chunks).decode("utf-8", errors="replace")
                # CGMiner responses sometimes contain a trailing NUL.
                return raw.rstrip("\x00").strip()
        except (OSError, socket.timeout) as e:
            raise AvalonError(f"Failed to reach miner at {self.config.host}: {e}") from e

    # ------------------------------------------------------------------
    # query commands (JSON)
    # ------------------------------------------------------------------
    def _query(self, command: str, parameter: str = "") -> dict[str, Any]:
        if parameter:
            payload = json.dumps({"command": command, "parameter": parameter})
        else:
            payload = json.dumps({"command": command})
        raw = self._send(payload)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Some commands (multi-cmd) return a stream; raw text is still useful.
            log.debug("Non-JSON response for %s: %s", command, raw[:200])
            return {"_raw": raw}

    def summary(self) -> dict[str, Any]:
        return self._query("summary")

    def stats(self) -> dict[str, Any]:
        return self._query("stats")

    def litestats(self) -> dict[str, Any]:
        return self._query("litestats")

    def pools(self) -> dict[str, Any]:
        return self._query("pools")

    def devs(self) -> dict[str, Any]:
        return self._query("devs")

    def version(self) -> dict[str, Any]:
        return self._query("version")

    def config_query(self) -> dict[str, Any]:
        return self._query("config")

    # ------------------------------------------------------------------
    # ascset commands (control)
    # ------------------------------------------------------------------
    def _ascset(self, args: str) -> dict[str, Any]:
        """
        Send an ascset|0,<args> command. Used for workmode/softoff/setpool/etc.
        These return plain-text STATUS lines, not JSON.
        """
        payload = f"ascset|0,{args}"
        raw = self._send(payload)
        return {"raw": raw, "ok": _ascset_ok(raw)}

    # ---- power / sleep ------------------------------------------------
    def soft_off(self) -> dict[str, Any]:
        """Activate standby mode. softoff takes a delay arg: 1:<unix_ts+5>"""
        target_ts = int(time.time()) + 5
        return self._ascset(f"softoff,1:{target_ts}")

    def soft_on(self) -> dict[str, Any]:
        """Wake from standby; returns to last workmode."""
        target_ts = int(time.time()) + 5
        return self._ascset(f"softon,1:{target_ts}")

    def reboot(self) -> dict[str, Any]:
        return self._ascset("reboot,0")

    # ---- workmode -----------------------------------------------------
    def set_workmode(self, mode: int) -> dict[str, Any]:
        if mode not in WORKMODE_NAMES:
            raise ValueError(f"Invalid workmode: {mode}")
        # Standby is handled via soft_off(), not via workmode set.
        if mode == WORKMODE_STANDBY:
            return self.soft_off()
        # workmode requires the literal "set" keyword as the third arg
        # before the numeric mode (0=Eco, 1=Standard, 2=Super).
        return self._ascset(f"workmode,set,{mode}")

    # ---- LCD ----------------------------------------------------------
    def lcd_on(self) -> dict[str, Any]:
        return self._ascset("lcd,0:1")

    def lcd_off(self) -> dict[str, Any]:
        return self._ascset("lcd,0:0")

    # ---- pool management ---------------------------------------------
    def set_pool(
        self,
        url: str,
        worker: str,
        worker_password: str = "x",
    ) -> dict[str, Any]:
        """
        Set the primary pool. Note: takes effect after reboot per Canaan API.
        Wire format (verified): ascset|0,setpool,<url>,<worker>,<password>
        """
        return self._ascset(f"setpool,{url},{worker},{worker_password}")


def _ascset_ok(raw: str) -> bool:
    """
    ascset responses look like:
      STATUS=S,When=...,Code=119,Msg=ASC 0 set OK,Description=cgminer 4.11.1
    'STATUS=S' = success, 'STATUS=I' = info, 'STATUS=E' = error.
    """
    if not raw:
        return False
    head = raw.split(",", 1)[0].upper()
    return head.startswith("STATUS=S") or head.startswith("STATUS=I")


# ----------------------------------------------------------------------
# Convenience helpers used by the web layer
# ----------------------------------------------------------------------
def parse_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Pull the most useful fields from a summary response."""
    out: dict[str, Any] = {}
    try:
        s = (summary.get("SUMMARY") or [{}])[0]
        out["elapsed_s"] = s.get("Elapsed")
        out["mhs_av"] = s.get("MHS av")
        out["mhs_5s"] = s.get("MHS 5s")
        out["mhs_1m"] = s.get("MHS 1m")
        out["accepted"] = s.get("Accepted")
        out["rejected"] = s.get("Rejected")
        out["hardware_errors"] = s.get("Hardware Errors")
    except (AttributeError, IndexError, KeyError):
        pass
    return out


def parse_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """
    Pull workmode, temperature, fan, power, hashrate from a stats response.
    The Avalon Q embeds these as space-separated KEY[value] tokens inside
    a string field named "MM ID0:Summary" within the second STATS entry.
    """
    out: dict[str, Any] = {
        "workmode": None,
        "workmode_name": None,
        "temp_chassis": None,
        "temp_max": None,
        "temp_avg": None,
        "fan_pct": None,
        "load_w": None,
        "ths": None,
        "pool_url": None,
    }
    entries = stats.get("STATS") or []
    for entry in entries:
        for key, val in entry.items():
            if not isinstance(val, str):
                continue
            # The MM summary blob always contains WORKMODE — use that as marker.
            if "WORKMODE[" in val:
                _scan_mm_payload(val, out)
    if out["workmode"] is not None:
        out["workmode_name"] = WORKMODE_NAMES.get(out["workmode"])
    return out


def _scan_mm_payload(payload: str, out: dict[str, Any]) -> None:
    """
    The MM payload is KEY[value] tokens, but `value` may contain spaces
    (e.g. PS[0 1213 2455 64 1594 2456 1698]) and keys may contain spaces
    too (e.g. "Nonce Mask[25]"). Parse by walking bracket pairs instead
    of splitting on whitespace.
    """
    i = 0
    n = len(payload)
    while i < n:
        # Find next opening bracket
        ob = payload.find("[", i)
        if ob == -1:
            break
        cb = payload.find("]", ob + 1)
        if cb == -1:
            break
        # Key is the word(s) between the previous boundary and this open bracket.
        key_start = i
        # The key starts after the last whitespace before ob; trim leading spaces.
        raw_key = payload[key_start:ob].strip()
        # If the raw key contains a space, the actual key is the last
        # word(s) before the bracket — but Avalon uses "Nonce Mask" so
        # we keep the whole stripped string. Some sane normalization:
        # the parser's interest is matching key names exactly.
        key = raw_key
        value = payload[ob + 1:cb].strip()
        i = cb + 1

        if key == "WORKMODE":
            out["workmode"] = _safe_int(value)
        elif key == "ITemp":
            out["temp_chassis"] = _safe_float(value)
        elif key == "TMax":
            out["temp_max"] = _safe_float(value)
        elif key == "TAvg":
            out["temp_avg"] = _safe_float(value)
        elif key == "FanR":
            out["fan_pct"] = _safe_float(value.rstrip("%"))
        elif key == "GHSspd":
            ghs = _safe_float(value)
            if ghs is not None:
                out["ths"] = ghs / 1000.0
        elif key == "MPO":
            # Rated max power output — used as fallback if PS not present
            if out["load_w"] is None:
                out["load_w"] = _safe_float(value)
        elif key == "PS":
            # PS[a Vin Iin b Pin Pout c] — space-separated PSU values.
            # Index 4 is AC input wattage (the "wall draw" reading).
            parts = value.split()
            if len(parts) >= 5:
                pin = _safe_float(parts[4])
                if pin is not None and pin > 0:
                    out["load_w"] = pin


def _safe_int(s: str) -> int | None:
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _safe_float(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None
