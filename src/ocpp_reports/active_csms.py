"""Read the active CSMS producer through its own installed Python environment.

Never import CSMS into the Reports environment or mutate CSMS storage.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def active_csms_export(data_dir: Path, *, after: int, limit: int) -> dict:
    unit = subprocess.run(
        ["systemctl", "show", "ocpp-csms.service", "--property=MainPID", "--value"],
        capture_output=True, text=True, check=True,
    )
    pid = int(unit.stdout.strip())
    if pid <= 0:
        raise RuntimeError("CSMS service is not running")
    command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    argv = [arg.decode() for arg in command if arg]
    # CSMS launches its venv console script. Its shebang identifies the
    # interpreter that owns the matching installed package and dependencies.
    executable = Path(next((arg for arg in argv if arg.endswith("/ocpp-csms")), argv[0]))
    if not executable.is_absolute() or not executable.is_file():
        raise RuntimeError("Cannot resolve active CSMS executable")
    shebang = executable.open("rb").readline().decode().strip()
    if not shebang.startswith("#!/") or "python" not in shebang:
        raise RuntimeError("Active CSMS launcher does not identify a Python interpreter")
    interpreter = shebang[2:].split()[0]
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
