"""Read the active CSMS producer through its own installed Python environment.

Never import CSMS into the Reports environment or mutate CSMS storage.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def discover_active_csms() -> tuple[Path, str]:
    """Discover the running CSMS interpreter and its actual evidence directory."""
    unit = subprocess.run(
        ["systemctl", "show", "ocpp-csms.service", "--property=MainPID", "--value"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    pid = int(unit.stdout.strip())
    if pid <= 0:
        raise RuntimeError("Local ocpp-csms.service is not running; provide --source")
    argv = [part.decode() for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part]
    executable = Path(next((arg for arg in argv if arg.endswith("/ocpp-csms")), argv[0]))
    if not executable.is_absolute() or not executable.is_file():
        raise RuntimeError("Cannot resolve active CSMS executable")
    shebang = executable.open("rb").readline().decode().strip()
    if not shebang.startswith("#!/") or "python" not in shebang:
        raise RuntimeError("Active CSMS launcher does not identify a Python interpreter")
    interpreter = shebang[2:].split()[0]
    if "--data-dir" in argv:
        index = argv.index("--data-dir")
        if index + 1 >= len(argv):
            raise RuntimeError("CSMS --data-dir has no value")
        data_dir = Path(argv[index + 1])
    else:
        values = [arg.partition("=")[2] for arg in argv if arg.startswith("--data-dir=")]
        if values:
            data_dir = Path(values[0])
        else:
            # Ask the active CSMS installation for its own configured default.
            # This respects the service unit and avoids hardcoding a CSMS path.
            result = subprocess.run(
                [interpreter, "-c", "from ocpp_csms.cli import cli_data_dir; print(cli_data_dir())"],
                capture_output=True, text=True, check=True, timeout=10,
            )
            data_dir = Path(result.stdout.strip())
    if not data_dir.is_absolute() or not data_dir.is_dir() or data_dir.name != "8":
        raise RuntimeError(f"Unsupported or inaccessible CSMS schema-8 data directory: {data_dir}")
    return data_dir, interpreter


def active_csms_export(data_dir: Path, *, after: int, limit: int) -> dict:
    discovered_dir, interpreter = discover_active_csms()
    if data_dir.resolve() != discovered_dir.resolve():
        raise RuntimeError("Active CSMS data directory changed during synchronization")
    code = (
        "import json,sys;"
        "from pathlib import Path;"
        "from ocpp_csms.export_contract import export_contract;"
        "print(json.dumps(export_contract(Path(sys.argv[1]),after=int(sys.argv[2]),limit=int(sys.argv[3]))))"
    )
    result = subprocess.run(
        [interpreter, "-c", code, str(data_dir), str(after), str(limit)],
        capture_output=True, text=True, check=True, timeout=45,
    )
    return json.loads(result.stdout)
