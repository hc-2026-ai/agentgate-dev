"""Public AgentGate domain data models."""

from .artifact import Artifact, ArtifactProducer
from .base import (
    DomainModel,
    FrozenJsonObject,
    canonical_json,
    content_sha256,
    find_credential_path,
    freeze_json,
    normalize_utc,
    require_sha256,
    utcnow,
)
from .case import Case, CaseCategory, CaseDifficulty, CaseTurn
from .comparison_gate import (
    ComparisonConditionDecision,
    ComparisonConditionReason,
    ComparisonFact,
    ComparisonFactUnavailableReason,
    ComparisonGateDecision,
    ComparisonGateOutcome,
    ComparisonGateRule,
    ComparisonGateSpec,
    ComparisonMetricId,
    ComparisonOperator,
    ConditionStatus,
    FactAvailability,
)
from .credential import ApiKeyMetadata, ApiKeyScope
from .dataset import Dataset, DatasetVersion, DatasetVersionStatus
from .evaluator import (
    CombinationPolicy, Evaluator, EvaluatorDraft, EvaluatorKind, EvaluatorRef,
    EvaluatorSeverity, EvaluatorSource, EvaluatorSpec,
)
from .expectation import (
    Condition, Equals, Expectation, MatchesJsonSchema, MatchesPattern, MustBeMissing,
    OneOf, OutputExpectation, PolicyExpectation, SkillRouteExpectation,
    StateExpectation, ToolArgumentExpectation, ToolCallExpectation, WithinRange,
    WithinTolerance,
)
from .gate import (
    ReleaseGateDecision, ReleaseGateOutcome, ReleaseGateReason, ReleaseGateSpec,
    classify_release_gate,
)
from .metric import MetricLevel, MetricPlan, MetricSummary
from .optimization import (
    FailedResultEvidence,
    FailureCluster,
    ObservedRoute,
    ObservedRouteKind,
    OptimizationReport,
    OptimizationSuggestion,
    RootCauseHypothesis,
    RoutingConfusionCell,
    RoutingConfusionMatrix,
    RoutingExclusion,
    RoutingObservation,
    SuggestionPriority,
)
from .report import EvaluationReport
from .result import (
    CheckResult, EvaluationResult, EvaluatorErrorDetail, FailureStage,
    JudgeRecord, MethodRef, Outcome,
)
from .run import EvaluationRun, RunManifest, RunStatus, transition_run
from .skill_analysis import (
    FindingSeverity, ReviewDecision, SkillAnalysisFinding, SkillAnalysisReport,
    SkillAnalysisReview, SkillAnalysisStatus,
)
from .target import (
    SkillDescriptor, TargetDescriptor, TargetRef, TargetSnapshot, TargetType,
    ToolDescriptor,
)
from .trace import SpanStatus, Trace, TraceSpan

__all__ = [name for name in globals() if not name.startswith("_")]
