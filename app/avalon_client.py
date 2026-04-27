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
        """Activate standby mode. Hashpower drops, network stays on."""
        # The Avalon Q exposes softoff via workmode=3.
        return self._ascset(
            f"softoff,{self.config.username},{self.config.password}"
        )

    def soft_on(self) -> dict[str, Any]:
        """Wake from standby; returns to last workmode."""
        return self._ascset(
            f"softon,{self.config.username},{self.config.password}"
        )

    def reboot(self) -> dict[str, Any]:
        return self._ascset(
            f"reboot,{self.config.username},{self.config.password},0"
        )

    # ---- workmode -----------------------------------------------------
    def set_workmode(self, mode: int) -> dict[str, Any]:
        if mode not in WORKMODE_NAMES:
            raise ValueError(f"Invalid workmode: {mode}")
        # Standby is handled via soft_off(), not workmode set.
        if mode == WORKMODE_STANDBY:
            return self.soft_off()
        return self._ascset(
            f"workmode,{self.config.username},{self.config.password},{mode}"
        )

    # ---- LCD ----------------------------------------------------------
    def lcd_on(self) -> dict[str, Any]:
        return self._ascset(
            f"led,{self.config.username},{self.config.password},1"
        )

    def lcd_off(self) -> dict[str, Any]:
        return self._ascset(
            f"led,{self.config.username},{self.config.password},0"
        )

    # ---- pool management ---------------------------------------------
    def set_pool(
        self,
        url: str,
        worker: str,
        worker_password: str = "x",
    ) -> dict[str, Any]:
        """
        Set the primary pool. Note: takes effect after reboot per Canaan API.
        """
        return self._ascset(
            f"setpool,{self.config.username},{self.config.password},"
            f"{url},{worker},{worker_password}"
        )


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
    Pull workmode, temperature, fan, power from a stats response.
    The Avalon Q embeds these in the second STATS entry as "MM ID" key/values.
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
        # Look for a key shaped like "MM ID0" carrying space-separated tokens.
        for key, val in entry.items():
            if not isinstance(val, str):
                continue
            if "WORKMODE" in val or "MTmax" in val or "Cur_Load" in val:
                _scan_mm_payload(val, out)
    if out["workmode"] is not None:
        out["workmode_name"] = WORKMODE_NAMES.get(out["workmode"])
    return out


def _scan_mm_payload(payload: str, out: dict[str, Any]) -> None:
    """The MM payload is space-separated KEY[value] tokens."""
    for token in payload.split():
        if "[" not in token or "]" not in token:
            continue
        key, _, rest = token.partition("[")
        value = rest.rstrip("]")
        # Some tokens carry comma-separated numeric arrays.
        if key == "WORKMODE":
            out["workmode"] = _safe_int(value)
        elif key == "ITemp":
            out["temp_chassis"] = _safe_float(value)
        elif key == "MTmax":
            # MTmax[a,b,c] - take the highest
            nums = [_safe_float(v) for v in value.split(",") if v]
            nums = [n for n in nums if n is not None]
            if nums:
                out["temp_max"] = max(nums)
        elif key == "MTavg":
            nums = [_safe_float(v) for v in value.split(",") if v]
            nums = [n for n in nums if n is not None]
            if nums:
                out["temp_avg"] = sum(nums) / len(nums)
        elif key == "FanR":
            out["fan_pct"] = _safe_float(value)
        elif key == "Cur_Load":
            out["load_w"] = _safe_float(value)
        elif key == "THSspd":
            out["ths"] = _safe_float(value)


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
