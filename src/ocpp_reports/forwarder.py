"""Compatibility module alias: monkeypatching legacy globals must still work."""
import sys
from .replication import forwarder as _implementation

sys.modules[__name__] = _implementation
