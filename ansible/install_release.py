#!/usr/bin/env python3
"""Install Reports directly from staged source, without building a project wheel."""
import os
import pathlib
import subprocess
import sys
import tomllib

release = pathlib.Path(sys.argv[1]).resolve()
extra = sys.argv[2]
source = release / "source"
venv = release / "venv"
python = venv / "bin" / "python"
launcher = venv / "bin" / "ocpp-reports"
marker = release / ".installed"
if marker.exists() and launcher.is_file():
    raise SystemExit(0)
with (source / "pyproject.toml").open("rb") as stream:
    project = tomllib.load(stream)["project"]
requirements = list(project["dependencies"])
for name in extra.strip("[]").split(","):
    if name:
        requirements.extend(project["optional-dependencies"][name])
# Refuse source builds: installing Reports itself requires no build backend.
subprocess.run([str(python), "-m", "pip", "install", "--only-binary=:all:", *requirements], check=True, timeout=600)
launcher.write_text(
    "#!/bin/sh\n"
    f"export PYTHONPATH='{source / 'src'}'\"\u0024{{PYTHONPATH:+:\u0024PYTHONPATH}}\"\n"
    f"exec '{python}' -c 'from ocpp_reports.cli import app; app()' \"\u0024@\"\n"
)
launcher.chmod(0o755)
marker.write_text("installed\n")
