"""Classification of SQL runtime errors for oracle soundness gating.

A *value-dependent* error (numeric overflow, division by zero, invalid cast
input, out-of-range, ...) depends on which rows an expression is physically
evaluated over. SQL leaves expression evaluation order unspecified, so an
engine may legally raise such an error in one query form while a logically
related form — whose filter would exclude the offending row — succeeds.
Differences of this shape are NOT bug evidence; reporting them is the classic
NoREC/TLP expected-error false positive.
"""

from __future__ import annotations

import re

_VALUE_DEPENDENT = re.compile(
    r"("
    r"out of range|overflow|underflow|"
    r"division by zero|divide by zero|"
    r"invalid input syntax|invalid argument|invalid value|"
    r"cannot cast|could not convert|invalid cast|"
    r"date/time field value out of range|timestamp.*out of range|"
    r"negative substring|non-positive|domain error|"
    r"invalid regular expression|invalid escape|"
    r"exponent out of range|result overflows|"
    r"value too long|numeric field overflow|"
    r"integer out of range|bigint out of range|"
    r"could not parse|conversion error|"
    r"not a valid|malformed|"
    r"cannot be represented|must be between"
    r")",
    re.IGNORECASE,
)


def is_value_dependent_error(error: str | None) -> bool:
    """True when the error text indicates a row-value-dependent failure."""
    if not error:
        return False
    return bool(_VALUE_DEPENDENT.search(error))


# Forced executor paths legitimately refuse some inputs. When a debug flag
# routes a query to an executor whose domain is narrower than SQL's (IEJoin
# needs numeric keys for its infinity sentinels), a guard-rail error is the
# *intended* refusal — not a bug.
_GUARD_RAIL = re.compile(
    r"("
    r"requires numeric|requires a numeric|"
    r"infinity requires|"
    r"not supported for|not implemented for|unsupported.*type"
    r")",
    re.IGNORECASE,
)

# Variants that force a narrower-domain executor path. Guard-rail errors
# under these labels are expected refusals, not divergence.
FORCING_VARIANTS = frozenset({
    "asof_iejoin",
})


def is_guard_rail_error(error: str | None, variant_label: str = "") -> bool:
    """True for intended refusals of forced executor paths."""
    if not error or variant_label not in FORCING_VARIANTS:
        return False
    return bool(_GUARD_RAIL.search(error))
