"""Compatibility entry point for the installed preflight module."""
import runpy

if __name__ == "__main__":
    runpy.run_module("ocpp_reports.csms_export_preflight", run_name="__main__")
