"""Emergency Stop CLI — authorized control of the global kill switch.

Usage::

    python emergency_stop.py activate --reason "Rate limit exceeded" --researcher "name"
    python emergency_stop.py status
    python emergency_stop.py clear --reason "Issue resolved" --researcher "name"

State is stored in the safeguard SQLite database and persists across
process restarts.  Workers must poll this state before and during visits.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from safeguard_audit import EventType, SafeguardAuditLogger
from safeguard_state import SafeguardState


def _activate(state: SafeguardState, audit: SafeguardAuditLogger, researcher: str, reason: str) -> None:
    state.activate_emergency_stop(researcher, reason)
    audit.log_event(
        EventType.EMERGENCY_STOP_ACTIVATED,
        safeguard="emergency_stop",
        action="activated",
        reason_code="EMERGENCY_STOP",
        extra={"researcher": researcher, "reason": reason},
    )
    print(f"[EMERGENCY STOP] Activated by {researcher}")
    print(f"  Reason: {reason}")
    print(f"  Time:   {datetime.now(timezone.utc).isoformat()}")
    print()
    print("All crawlers will stop accepting new work and active visits will be signalled to stop.")
    print("To resume, run: python emergency_stop.py clear --researcher <name> --reason <reason>")


def _clear(state: SafeguardState, audit: SafeguardAuditLogger, researcher: str, reason: str) -> None:
    if not state.is_emergency_stop_active():
        print("[INFO] Emergency stop is not currently active. Nothing to clear.")
        return

    state.clear_emergency_stop(researcher, reason)
    audit.log_event(
        EventType.EMERGENCY_STOP_CLEARED,
        safeguard="emergency_stop",
        action="cleared",
        extra={"researcher": researcher, "reason": reason},
    )
    print(f"[EMERGENCY STOP] Cleared by {researcher}")
    print(f"  Reason: {reason}")
    print(f"  Time:   {datetime.now(timezone.utc).isoformat()}")
    print()
    print("Crawlers may now resume normal operation.")


def _status(state: SafeguardState) -> None:
    info = state.get_emergency_stop_info()
    active = bool(info.get("is_active"))

    print("Emergency Stop Status")
    print("=" * 40)
    print(f"  Active:       {'YES — ALL CRAWLING STOPPED' if active else 'No (normal operation)'}")

    if active:
        print(f"  Activated by: {info.get('activated_by', 'unknown')}")
        print(f"  Activated at: {info.get('activated_at_utc', 'unknown')}")
        print(f"  Reason:       {info.get('reason', 'unknown')}")
    else:
        cleared_by = info.get("cleared_by")
        if cleared_by:
            print(f"  Last cleared by: {cleared_by}")
            print(f"  Cleared at:      {info.get('cleared_at_utc', 'unknown')}")
            print(f"  Clear reason:    {info.get('clear_reason', 'unknown')}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Emergency Stop control for the AdGraph crawler.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # activate
    activate_parser = subparsers.add_parser("activate", help="Activate the emergency stop.")
    activate_parser.add_argument("--researcher", required=True, help="Name of the authorized researcher.")
    activate_parser.add_argument("--reason", required=True, help="Reason for activating the emergency stop.")

    # clear
    clear_parser = subparsers.add_parser("clear", help="Clear the emergency stop to resume crawling.")
    clear_parser.add_argument("--researcher", required=True, help="Name of the authorized researcher.")
    clear_parser.add_argument("--reason", required=True, help="Reason for clearing the emergency stop.")

    # status
    subparsers.add_parser("status", help="Show current emergency stop status.")

    args = parser.parse_args()

    state = SafeguardState()
    audit = SafeguardAuditLogger()

    try:
        if args.command == "activate":
            _activate(state, audit, args.researcher, args.reason)
        elif args.command == "clear":
            _clear(state, audit, args.researcher, args.reason)
        elif args.command == "status":
            _status(state)
    finally:
        state.close()


if __name__ == "__main__":
    main()
