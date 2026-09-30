"""Coverage measurement: query AST features x plan operators x data corners."""

from coverage.features import extract_cells
from coverage.store import CoverageStore

__all__ = ["extract_cells", "CoverageStore"]
