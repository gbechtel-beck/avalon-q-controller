"""
Multi-miner controller.

Each registered miner gets its own poll loop. The controller orchestrates:
- Reading miner state via the CGMiner API
- Evaluating the schedule (or honoring active override / pause)
- Reconciling target action against current state, including pool switches
- Writing a time-series sample on every successful poll
- Logging every state change to the events table

Pool actions:
  When a rule's action is "pool:<id>":
    1. Look up the pool's url/worker/password.
    2. If miner is in standby, soft_on first (setpool may be rejected in standby).
    3. Send setpool command.
    4. If chain_workmode is set, send the workmode change.
    5. If reboot_after_pool_switch is True, send reboot.
    6. Update miner_state.last_pool_id.

  Idempotency: we only fire setpool when last_pool_id != target pool_id, OR
  when last_pool_id is None (never applied) AND the current pool URL doesn't
  match the target. This avoids needless reboots.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .avalon_client import (
    AvalonClient,
    AvalonConfig,
    AvalonError,
    WORKMODE_ECO,
    WORKMODE_FROM_NAME,
    WORKMODE_NAMES,
    WORKMODE_STANDARD,
    WORKMODE_STANDBY,
    WORKMODE_SUPER,
    parse_stats,
    parse_summary,
)
from .db import DB, Miner
from .scheduler import (
    BASE_ACTIONS,
    is_pool_action,
    is_valid_action,
    pool_id_from_action,
    schedule_from_dict,
)

log = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 30


class Controller:
    def __init__(self, db: DB):
        self.db = db
        self._tasks: dict[int, asyncio.Task] = {}
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # public: lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Start one poll loop per enabled miner."""
        for m in self.db.list_miners():
            if m.enabled:
                self._start_miner_loop(m.id)
        log.info("Controller started for %d miner(s)", len(self._tasks))

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    def _start_miner_loop(self, miner_id: int) -> None:
        if miner_id in self._tasks and not self._tasks[miner_id].done():
            return
        self._tasks[miner_id] = asyncio.create_task(
            self._miner_loop(miner_id),
            name=f"miner-{miner_id}",
        )

    def _stop_miner_loop(self, miner_id: int) -> None:
        task = self._tasks.pop(miner_id, None)
        if task and not task.done():
            task.cancel()

    def restart_miner(self, miner_id: int) -> None:
        self._stop_miner_loop(miner_id)
        m = self.db.get_miner(miner_id)
        if m and m.enabled:
            self._start_miner_loop(miner_id)

    # ------------------------------------------------------------------
    # public: one-shot operations
    # ------------------------------------------------------------------
    async def poll_once(self, miner_id: int) -> dict:
        """Force an immediate poll cycle for one miner (used by /api/refresh)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._poll_sync, miner_id)

    async def manual_command(self, miner_id: int, command: str) -> dict:
        """Run an immediate manual command bypassing schedule/override."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._manual_sync, miner_id, command)

    async def apply_pool(self, miner_id: int, pool_id: int, *, reboot: bool = False) -> dict:
        """Manual: apply a pool right now (used by 'Use this pool' button)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._apply_pool_sync, miner_id, pool_id, reboot
        )

    # ------------------------------------------------------------------
    # internal: poll loop
    # ------------------------------------------------------------------
    async def _miner_loop(self, miner_id: int) -> None:
        log.info("Poll loop starting for miner %d", miner_id)
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                await loop.run_in_executor(None, self._poll_sync, miner_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Unhandled error polling miner %d", miner_id)
            m = self.db.get_miner(miner_id)
            if m is None or not m.enabled:
                log.info("Miner %d gone or disabled; ending loop", miner_id)
                self._tasks.pop(miner_id, None)
                return
            await asyncio.sleep(m.poll_seconds or DEFAULT_POLL_SECONDS)

    # ------------------------------------------------------------------
    # internal: synchronous workers (run in executor)
    # ------------------------------------------------------------------
    def _client_for(self, m: Miner) -> AvalonClient:
        return AvalonClient(
            AvalonConfig(
                host=m.host,
                port=m.port,
                username=m.username,
                password=m.password,
            )
        )

    def _poll_sync(self, miner_id: int) -> dict:
        m = self.db.get_miner(miner_id)
        if m is None:
            return {"error": "Miner not found"}

        now_iso = datetime.now(ZoneInfo("UTC")).isoformat()
        status: dict[str, Any] = {
            "miner_id": miner_id,
            "online": False,
            "last_poll": now_iso,
            "summary": {},
            "stats": {},
            "scheduler": {"current_action": "none", "matched_rule": None},
            "applied_action": None,
            "error": None,
        }
        if not m.host:
            status["error"] = "Miner host not configured"
            self.db.set_last_status(miner_id, status)
            return status

        client = self._client_for(m)

        try:
            summary = client.summary()
            stats = client.stats()
            status["summary"] = parse_summary(summary)
            status["stats"] = parse_stats(stats)
            try:
                pools_data = client.pools()
                status["stats"]["pool_url"] = _extract_active_pool_url(pools_data)
            except AvalonError:
                pass
            status["online"] = True
        except AvalonError as e:
            status["error"] = str(e)
            self.db.set_last_status(miner_id, status)
            self._record_sample(miner_id, status)
            return status

        # Decide and reconcile
        target, src = self._decide_target(miner_id)
        status["scheduler"] = {"current_action": target, "matched_rule": src}

        if target != "none":
            applied = self._reconcile(client, miner_id, target, status["stats"])
            status["applied_action"] = applied

        # Pool-pending warning: if the active target is a pool:N and the
        # miner's currently-active pool URL doesn't match the target pool's
        # URL, surface that on the dashboard. The Avalon Q firmware doesn't
        # apply pool changes until the next reboot, so a "pending" state is
        # normal between setpool and reboot.
        if target.startswith("pool:"):
            try:
                pool_id = int(target.split(":", 1)[1])
                pool = self.db.get_pool(pool_id)
                cur_pool_url = (status["stats"].get("pool_url") or "").strip()
                if pool and cur_pool_url and cur_pool_url != pool.url:
                    status["pool_pending"] = {
                        "target_pool_id": pool_id,
                        "target_url": pool.url,
                        "active_url": cur_pool_url,
                        "hint": "Setpool sent. Avalon Q applies pool changes on next reboot.",
                    }
            except (ValueError, AttributeError):
                pass

        self.db.set_last_status(miner_id, status)
        self._record_sample(miner_id, status)
        return status

    def _record_sample(self, miner_id: int, status: dict) -> None:
        # Guard against the miner being deleted mid-poll: if it's gone,
        # silently skip — the FK constraint would fail otherwise.
        if self.db.get_miner(miner_id) is None:
            return
        stats = status.get("stats") or {}
        summ = status.get("summary") or {}
        sample = {
            "online": status.get("online"),
            "workmode": stats.get("workmode"),
            "ths": stats.get("ths") or _mhs_to_ths(summ.get("mhs_av")),
            "load_w": stats.get("load_w"),
            "temp_max": stats.get("temp_max"),
            "temp_chassis": stats.get("temp_chassis"),
            "fan_pct": stats.get("fan_pct"),
            "accepted": summ.get("accepted"),
            "rejected": summ.get("rejected"),
            "hw_errors": summ.get("hardware_errors"),
            "pool_url": stats.get("pool_url"),
        }
        try:
            self.db.insert_sample(miner_id, sample)
        except Exception:
            log.exception("Failed to record sample for miner %d", miner_id)

    def _decide_target(self, miner_id: int) -> tuple[str, dict | None]:
        state = self.db.get_state(miner_id)
        if state["paused"]:
            return ("none", {"source": "paused"})
        ov = state["override"]
        if ov:
            now_utc = datetime.now(ZoneInfo("UTC"))
            exp = ov.get("expires_at")
            active = True
            if exp:
                try:
                    active = now_utc < datetime.fromisoformat(exp)
                except ValueError:
                    active = False
            if active:
                return (ov["action"], {"source": "override", "expires_at": exp})
            self.db.set_override(miner_id, None)
        sched = schedule_from_dict(state["schedule"])
        action, rule = sched.evaluate()
        if rule is None:
            return (action, {"source": "default"})
        return (
            action,
            {
                "source": "rule",
                "rule_id": rule.id,
                "rule_name": rule.name,
                "reboot_after_pool_switch": rule.reboot_after_pool_switch,
                "chain_workmode": rule.chain_workmode,
            },
        )

    # ------------------------------------------------------------------
    # reconciliation
    # ------------------------------------------------------------------
    def _reconcile(
        self,
        client: AvalonClient,
        miner_id: int,
        target: str,
        current_stats: dict,
    ) -> str | None:
        try:
            if is_pool_action(target):
                return self._reconcile_pool(client, miner_id, target, current_stats)
            return self._reconcile_basic(client, miner_id, target, current_stats)
        except AvalonError as e:
            self.db.log_event(
                miner_id, "apply_error", {"target": target, "error": str(e)}
            )
            log.warning("Failed to apply %s on miner %d: %s", target, miner_id, e)
            return f"error: {e}"

    def _reconcile_basic(
        self,
        client: AvalonClient,
        miner_id: int,
        target: str,
        current_stats: dict,
    ) -> str | None:
        cur_mode = current_stats.get("workmode")
        cur_name = current_stats.get("workmode_name")
        # STATE field is the truth for soft-off: 1=working, 2=idle (standby), 0=init.
        # WORKMODE never goes to 3 — softoff is a separate state on this firmware.
        is_standby = current_stats.get("state") == 2

        if target == "off":
            if is_standby:
                return None
            client.soft_off()
            self.db.log_event(miner_id, "apply_soft_off", {"from": cur_name})
            return "soft_off"

        if target == "on":
            if is_standby:
                client.soft_on()
                self.db.log_event(miner_id, "apply_soft_on", {})
                return "soft_on"
            return None

        mode_map = {
            "eco": WORKMODE_ECO,
            "standard": WORKMODE_STANDARD,
            "super": WORKMODE_SUPER,
        }
        if target in mode_map:
            desired = mode_map[target]
            applied_parts = []
            if is_standby:
                client.soft_on()
                self.db.log_event(miner_id, "apply_soft_on", {"reason": "wake_for_workmode"})
                applied_parts.append("soft_on")
            # Always set the workmode if waking from standby (mode might be
            # the same numerically but the miner needs the explicit set after
            # waking) OR if the current mode differs from desired.
            if is_standby or cur_mode != desired:
                client.set_workmode(desired)
                self.db.log_event(
                    miner_id,
                    "apply_workmode",
                    {"from": cur_name, "to": WORKMODE_NAMES[desired]},
                )
                applied_parts.append(f"workmode_{target}")
            if not applied_parts:
                return None
            return "+".join(applied_parts)
        return None

    def _reconcile_pool(
        self,
        client: AvalonClient,
        miner_id: int,
        target: str,
        current_stats: dict,
    ) -> str | None:
        pool_id_str = pool_id_from_action(target)
        if not pool_id_str:
            return "error: invalid pool action"
        try:
            pool_id = int(pool_id_str)
        except ValueError:
            return "error: bad pool id"

        pool = self.db.get_pool(pool_id)
        if pool is None or pool.miner_id != miner_id:
            return f"error: pool {pool_id} not found for this miner"

        state = self.db.get_state(miner_id)
        last_pool_id = state.get("last_pool_id")
        cur_url = (current_stats.get("pool_url") or "").strip()

        # Idempotency: if we already recorded this pool as the target, we've
        # sent the setpool command. The Avalon Q firmware may not reflect the
        # change in stats until the next reboot, so we cannot rely on
        # cur_url == pool.url as the "applied" signal — that would cause us
        # to re-fire setpool every poll cycle. Trust last_pool_id instead.
        if last_pool_id == pool_id:
            return None

        # If the miner is in standby, wake it before changing pool.
        if current_stats.get("state") == 2:
            client.soft_on()
            self.db.log_event(miner_id, "apply_soft_on", {"reason": "wake_for_pool"})

        # Set the pool
        client.set_pool(pool.url, pool.worker, pool.worker_password)
        self.db.log_event(
            miner_id,
            "apply_pool",
            {"pool_id": pool_id, "name": pool.name, "url": pool.url},
        )
        self.db.set_last_pool(miner_id, pool_id)

        # Optional chained workmode change (rule-controlled)
        rule_info = self._current_rule_info(miner_id)
        chain = (rule_info or {}).get("chain_workmode")
        applied = "set_pool"
        if chain in ("eco", "standard", "super"):
            try:
                client.set_workmode(WORKMODE_FROM_NAME[chain])
                self.db.log_event(miner_id, "apply_workmode_chain", {"to": chain})
                applied += f"+workmode_{chain}"
            except AvalonError as e:
                self.db.log_event(
                    miner_id, "apply_workmode_chain_error", {"to": chain, "error": str(e)}
                )

        # Optional reboot (rule-controlled)
        if (rule_info or {}).get("reboot_after_pool_switch"):
            try:
                client.reboot()
                self.db.log_event(miner_id, "apply_reboot", {"reason": "pool_switch"})
                applied += "+reboot"
            except AvalonError as e:
                self.db.log_event(miner_id, "apply_reboot_error", {"error": str(e)})

        return applied

    def _current_rule_info(self, miner_id: int) -> dict | None:
        state = self.db.get_state(miner_id)
        sched = schedule_from_dict(state["schedule"])
        action, rule = sched.evaluate()
        if rule is None:
            return None
        return {
            "rule_id": rule.id,
            "rule_name": rule.name,
            "reboot_after_pool_switch": rule.reboot_after_pool_switch,
            "chain_workmode": rule.chain_workmode,
        }

    # ------------------------------------------------------------------
    # manual operations
    # ------------------------------------------------------------------
    def _manual_sync(self, miner_id: int, command: str) -> dict:
        m = self.db.get_miner(miner_id)
        if m is None:
            return {"ok": False, "error": "Miner not found"}
        client = self._client_for(m)
        try:
            if command == "reboot":
                resp = client.reboot()
            elif command == "lcd_on":
                resp = client.lcd_on()
            elif command == "lcd_off":
                resp = client.lcd_off()
            elif command == "soft_on":
                resp = client.soft_on()
            elif command == "soft_off":
                resp = client.soft_off()
            elif command in ("eco", "standard", "super"):
                resp = client.set_workmode(WORKMODE_FROM_NAME[command])
            else:
                return {"ok": False, "error": f"Unknown command: {command}"}
            self.db.log_event(
                miner_id,
                "manual_command",
                {"command": command, "ok": bool(resp.get("ok"))},
            )
            return {"ok": bool(resp.get("ok")), "raw": resp.get("raw", "")}
        except AvalonError as e:
            self.db.log_event(miner_id, "manual_command_error",
                              {"command": command, "error": str(e)})
            return {"ok": False, "error": str(e)}

    def _apply_pool_sync(self, miner_id: int, pool_id: int, reboot: bool) -> dict:
        m = self.db.get_miner(miner_id)
        if m is None:
            return {"ok": False, "error": "Miner not found"}
        pool = self.db.get_pool(pool_id)
        if pool is None or pool.miner_id != miner_id:
            return {"ok": False, "error": "Pool not found for this miner"}
        client = self._client_for(m)
        try:
            resp = client.set_pool(pool.url, pool.worker, pool.worker_password)
            self.db.log_event(
                miner_id,
                "manual_apply_pool",
                {"pool_id": pool_id, "name": pool.name},
            )
            self.db.set_last_pool(miner_id, pool_id)
            if reboot:
                client.reboot()
                self.db.log_event(miner_id, "apply_reboot", {"reason": "manual_pool_apply"})
            return {"ok": bool(resp.get("ok")), "raw": resp.get("raw", "")}
        except AvalonError as e:
            self.db.log_event(
                miner_id, "manual_apply_pool_error",
                {"pool_id": pool_id, "error": str(e)},
            )
            return {"ok": False, "error": str(e)}


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _extract_active_pool_url(pools_resp: dict) -> str | None:
    """Pick the active/alive pool URL from a CGMiner pools response."""
    if not pools_resp:
        return None
    pools = pools_resp.get("POOLS") or []
    # Prefer the one marked Status=Alive
    for p in pools:
        if str(p.get("Status", "")).lower() == "alive":
            return p.get("URL") or p.get("Stratum URL")
    if pools:
        return pools[0].get("URL") or pools[0].get("Stratum URL")
    return None


def _mhs_to_ths(mhs: float | None) -> float | None:
    if mhs is None:
        return None
    try:
        return float(mhs) / 1_000_000.0
    except (TypeError, ValueError):
        return None
