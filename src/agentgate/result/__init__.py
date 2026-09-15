"""Result reporting, analytics, comparison, and Gate calculations."""

from .comparison import (
    CaseDelta,
    ComparisonChange,
    EvaluationComparison,
    MetricDelta,
    compare_reports,
)
from .comparison_facts import (
    BadcaseComparison,
    RunComparisonAnalysis,
    compare_badcases,
    derive_comparison_facts,
)
from .comparison_gate import (
    decide_comparison_gate,
    default_comparison_gate_spec,
)
from .comparison_slices import (
    ComparisonSliceDelta,
    ComparisonSliceDimension,
    ComparisonSliceValue,
    compare_result_breakdowns,
)
from .metrics import calculate_metrics
from .gate import decide_release_gate
from .report import build_evaluation_report

__all__ = [
    "BadcaseComparison",
    "CaseDelta",
    "ComparisonChange",
    "ComparisonSliceDelta",
    "ComparisonSliceDimension",
    "ComparisonSliceValue",
    "EvaluationComparison",
    "MetricDelta",
    "RunComparisonAnalysis",
    "build_evaluation_report",
    "calculate_metrics",
    "compare_badcases",
    "compare_reports",
    "compare_result_breakdowns",
    "decide_comparison_gate",
    "decide_release_gate",
    "default_comparison_gate_spec",
    "derive_comparison_facts",
]
