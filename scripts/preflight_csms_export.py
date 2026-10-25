"""Preflight one actual read-only export as the Reports service identity."""
import sys
from pathlib import Path

from ocpp_reports.active_csms import active_csms_export


def main() -> None:
    requested = Path(sys.argv[1])
    result = active_csms_export(requested, after=0, limit=1)
    if result.get("schema") != "ocpp-csms/export/v1":
        raise RuntimeError("Active CSMS returned an unsupported export schema")
    print("Active CSMS export is readable by the Reports service identity")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Reports read-only CSMS export preflight failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
