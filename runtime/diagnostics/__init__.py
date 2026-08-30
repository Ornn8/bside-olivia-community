"""Bounded, privacy-safe diagnostics exported by the local client only."""

from .support_bundle import (
    DIAGNOSTIC_BUNDLE_MEMBERS,
    DiagnosticBundleError,
    build_diagnostic_bundle,
)

__all__ = [
    "DIAGNOSTIC_BUNDLE_MEMBERS",
    "DiagnosticBundleError",
    "build_diagnostic_bundle",
]
