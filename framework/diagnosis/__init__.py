"""Diagnosis layer: intervention-basis fault attribution + signatures."""

from .bisect import (
    all_off_prelude,
    compute_fix_set,
    interventions_for,
    plan_op_names,
)
from .signature import SignatureEngine

__all__ = [
    "all_off_prelude",
    "compute_fix_set",
    "interventions_for",
    "plan_op_names",
    "SignatureEngine",
]
