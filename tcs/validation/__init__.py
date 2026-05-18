"""
tcs.validation
==============

Calibration and mathematical validation utilities for the TCS runtime.

The :class:`CalibrationReport` consumes Trust Certificates from the
certificate store and answers seven calibration questions about whether
the governance system is properly tuned for the deployed workflow.
"""

from tcs.validation.calibration_report import CalibrationReport, CalibrationResult

__all__ = ["CalibrationReport", "CalibrationResult"]
