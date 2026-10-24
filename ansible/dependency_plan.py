#!/usr/bin/env python3
"""Canonical dependency-set key and installed-distribution verification."""
import hashlib
import json
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path

verify = len(sys.argv) > 1 and sys.argv[1] == "--verify"
args = sys.argv[2:] if verify else sys.argv[1:]
if len(args) != 2:
    raise SystemExit("usage: dependency_plan.py [--verify] PYPROJECT EXTRAS")
project = tomllib.loads(Path(args[0]).read_text())["project"]
extras = [x for x in args[1].strip("[]").split(",") if x]
requirements = list(project["dependencies"])
for extra in extras:
    requirements.extend(project["optional-dependencies"][extra])
requirements = sorted(set(requirements))
if verify:
    from pip._vendor.packaging.requirements import Requirement
    for item in requirements:
        req = Requirement(item)
        installed = version(req.name)
        if installed not in req.specifier:
            raise SystemExit(f"{req.name} {installed} does not satisfy {req.specifier}")
else:
    key = hashlib.sha256(json.dumps(requirements).encode()).hexdigest()[:16]
    print(json.dumps({"key": key, "requirements": requirements}))
