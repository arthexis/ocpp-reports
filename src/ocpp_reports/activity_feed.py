"""Aggregate fault transitions; never export charger or customer identity."""
import hashlib
import os
import subprocess

def enqueue(kind, title, severity, stamp, *, repository, category, run=subprocess.run):
    """Opt-in local queue only. No remote or primary operation retry."""
    if os.environ.get("GWAY_ACTIVITY_FEED_EXTERNAL") != "1":
        return False
    try:
        identity = hashlib.sha256((repository + ":" + kind + ":" + stamp).encode()).hexdigest()
        result = run(["/usr/local/bin/gway-remote", "feed", title, "--type", kind,
                      "--category", category, "--severity", severity, "--repo", repository,
                      "--id", identity, "--time", stamp], timeout=1.5, check=False,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0
    except Exception:
        return False

FAULTS = frozenset({"Faulted", "Unavailable", "EmergencyStop"})
HEALTHY = frozenset({"Available", "Preparing", "Charging", "SuspendedEV", "SuspendedEVSE", "Finishing", "Complete", "Reserved"})

def changes(before, after):
    if before is None:
        return ()
    entered = cleared = False
    for key, row in after.items():
        old = before.get(key)
        if old is None:
            continue
        entered |= old.get("state") in HEALTHY and row.get("state") in FAULTS
        cleared |= old.get("state") in FAULTS and row.get("state") in HEALTHY
    return tuple(kind for kind, yes in (("fault_observed", entered), ("fault_cleared", cleared)) if yes)

def publish(before, after, now):
    for kind in changes(before, after):
        enqueue("reports." + kind,
                "Charger fault observed" if kind == "fault_observed" else "Charger fault cleared",
                "warning" if kind == "fault_observed" else "info",
                now.isoformat(), repository="arthexis/ocpp-reports", category="ocpp")
