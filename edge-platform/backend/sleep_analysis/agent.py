import asyncio
import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import ValidationError

from shared.interfaces import ToolCall, ToolExecutor, ToolResult
from shared.schemas.events import ContractModel

from .completion import CompletionCheck, CompletionGoal, check_completion
from .contracts import HistoryRequest, RegisteredActivity, TimeWindow, WindowRequest
from .skills import Skill, local_skills, video_pose_skill, sleep_stage_skill
from .video_pose import VIDEO_POSE, VideoAccessDenied, VideoArchive, VideoPoseRequest, VideoRange
from .sleep_stage import STAGE_SKILL, StageAccessDenied, AuthorizedEpoch, SleepStageRequest, SleepStageRuntime


class SkillRegistry:
    def __init__(self, skills: Iterable[Skill] = ()):
        self._skills: dict[str, Skill] = {}
        for skill in skills:
            self.register(skill)

    def register(self, skill: Skill) -> None:
        if not skill.name or skill.name in self._skills:
            raise ValueError('invalid or duplicate skill name')
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def skills(self) -> tuple[Skill, ...]:
        return tuple(self._skills.values())

    def discover(self) -> tuple[dict, ...]:
        return tuple({
            'name': skill.name,
            'description': skill.description,
            'input_schema': skill.input_model.model_json_schema(),
            'output_schema': skill.output_model.model_json_schema(),
        } for skill in self._skills.values())


_DETERMINISTIC_UNAVAILABLE = {
    'data_inspection': {'unknown_dataset'},
    'sleep_activity_analysis': {'not_implemented'},
    'evidence_verification': {'unknown_activity_event', 'event_source_mismatch',
                              'event_outside_analysis_window', 'control_window_overlaps_event'},
    'historical_comparison': {'no_comparable_history'},
}


def _contains(outer: TimeWindow, inner: TimeWindow) -> bool:
    return outer.start <= inner.start and inner.end <= outer.end


def _same_source(first: WindowRequest, second: WindowRequest) -> bool:
    return (first.dataset_id, first.session_id) == (second.dataset_id, second.session_id)


class LocalToolExecutor(ToolExecutor):
    def __init__(self, registry: SkillRegistry, scope: WindowRequest, timeout: float, *,
                 authorized_history: tuple[WindowRequest, ...] | None = None):
        self.registry = registry
        self.scope = scope
        self.timeout = timeout
        self.authorized_history = authorized_history
        # Each run owns one executor with a fixed scope and registered data sources.
        self._unavailable: dict[tuple[str, str], UUID] = {}

    async def execute(self, call: ToolCall) -> ToolResult:
        skill = self.registry.get(call.name)
        if skill is None:
            return ToolResult(call_id=call.call_id, status='denied', error='unknown_skill')
        try:
            request = skill.input_model.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(call_id=call.call_id, status='denied', error='invalid_arguments')
        if isinstance(request, WindowRequest):
            scope_fields = [field for field in ('dataset_id', 'session_id')
                            if getattr(request, field) != getattr(self.scope, field)]
            outside_window = any(not _contains(self.scope.window, window) for window in request.scoped_windows())
            if outside_window:
                scope_fields.extend(field for field in ('window', 'before_window', 'after_window')
                                    if (window := getattr(request, field, None)) is not None
                                    and not _contains(self.scope.window, window))
            if scope_fields or outside_window:
                return ToolResult(call_id=call.call_id, status='denied', error='out_of_scope',
                                  data={'scope_fields': scope_fields})
        elif not ((call.name == VIDEO_POSE and isinstance(request, VideoPoseRequest))
                  or (call.name == STAGE_SKILL and isinstance(request, SleepStageRequest))):
            return ToolResult(call_id=call.call_id, status='denied', error='invalid_scope_contract')
        if isinstance(request, HistoryRequest) and self.authorized_history is not None and not any(
            _same_source(request, grant) and _contains(grant.window, request.history_window)
            for grant in self.authorized_history
        ):
            return ToolResult(call_id=call.call_id, status='denied', error='history_out_of_scope',
                              data={'scope_fields': ['history_window']})
        key = (call.name, json.dumps(request.model_dump(mode='json'), sort_keys=True, separators=(',', ':')))
        original_id = self._unavailable.get(key)
        if original_id is not None:
            return ToolResult(call_id=call.call_id, status='denied', error='duplicate_unavailable_call',
                              data={'original_call_id': str(original_id)})
        try:
            output = await asyncio.wait_for(skill.handler(request), timeout=self.timeout)
            output = skill.output_model.model_validate(output.model_dump())
            request_fields = request.model_dump()
            if output.model_dump(include=set(request_fields)) != request_fields:
                raise ValueError('skill output does not match request')
            data = output.model_dump(mode='json')
            if data.get('status') == 'unavailable' and data.get('reason') in _DETERMINISTIC_UNAVAILABLE.get(call.name, ()):
                self._unavailable[key] = call.call_id
            return ToolResult(call_id=call.call_id, status='succeeded', data=data)
        except (VideoAccessDenied, StageAccessDenied) as exc:
            return ToolResult(call_id=call.call_id, status='denied', error=str(exc))
        except TimeoutError:
            return ToolResult(call_id=call.call_id, status='failed', error='skill_timeout')
        except Exception:
            return ToolResult(call_id=call.call_id, status='failed', error='skill_execution_failed')


class ReportFinding(ContractModel):
    skill: str
    result: ToolResult


class ReportLimitation(ContractModel):
    skill: str
    reason: str
    call_id: UUID


class GroundedReport(ContractModel):
    answer: str
    findings: list[ReportFinding]
    uncertainty_reasons: list[str]
    analysis_scope: WindowRequest | None = None
    limitations: list[ReportLimitation] = []


class Finish(ContractModel):
    reason: str
    report: GroundedReport | None = None


class TraceEntry(ContractModel):
    step: int
    started_at: datetime
    finished_at: datetime
    call: ToolCall
    result: ToolResult


class FinishRejection(ContractModel):
    round: int
    reason: str


class Planner(Protocol):
    async def plan(
        self, request: WindowRequest, skills: tuple[dict, ...], trace: tuple[TraceEntry, ...],
    ) -> ToolCall | Finish: ...


class IncompleteEvidence(ValueError):
    """A finish action omitted a required result from the trace."""


_REQUIRED_LIMITATIONS = {
    'data_inspection': {'unknown_dataset', 'missing_databases', 'edge_ppg_dependency_unavailable', 'no_usable_window'},
    'sleep_activity_analysis': {'not_implemented'},
}


def required_limitations(request: WindowRequest, trace: tuple[TraceEntry, ...]) -> tuple[ReportLimitation, ...]:
    required = {}
    usable = set()
    for entry in trace:
        skill = entry.call.name
        data = entry.result.data
        if (skill not in _REQUIRED_LIMITATIONS or entry.result.status != 'succeeded'
                or data.get('dataset_id') != request.dataset_id or data.get('session_id') != request.session_id
                or data.get('window') != request.window.model_dump(mode='json')):
            continue
        reason = data.get('reason')
        if data.get('status') in ('ok', 'partial'):
            usable.add(skill)
        if data.get('status') == 'unavailable' and reason in _REQUIRED_LIMITATIONS[skill]:
            required.setdefault(skill, ReportLimitation(skill=skill, reason=reason, call_id=entry.result.call_id))
    return tuple(item for skill, item in required.items() if skill not in usable)


class RunResult(ContractModel):
    run_id: UUID
    request: WindowRequest
    termination: Literal[
        'finished', 'call_limit', 'planner_timeout', 'planner_error', 'skill_timeout', 'duplicate_call_id',
        'run_timeout', 'target_out_of_scope', 'incomplete_evidence', 'task_incomplete',
    ]
    finish: Finish | None = None
    trace: tuple[TraceEntry, ...]
    finish_rejections: tuple[FinishRejection, ...] = ()
    completion_pending: str | None = None


class SleepAnalysisAgent:
    def __init__(
        self, registry: SkillRegistry, planner: Planner, *,
        max_calls: int = 8, planner_timeout: float = 10, skill_timeout: float = 10, run_timeout: float | None = None,
        video_archive: VideoArchive | None = None,
        stage_runtime: SleepStageRuntime | None = None,
    ):
        if max_calls < 1 or not all(isfinite(value) and value > 0 for value in (planner_timeout, skill_timeout)):
            raise ValueError('expected positive call limit and finite positive timeouts')
        if run_timeout is not None and (not isfinite(run_timeout) or run_timeout <= 0):
            raise ValueError('expected a finite positive run timeout')
        self.registry = registry
        self.planner = planner
        self.max_calls = max_calls
        self.planner_timeout = planner_timeout
        self.skill_timeout = skill_timeout
        self.run_timeout = run_timeout
        self.video_archive = video_archive
        self.stage_runtime = stage_runtime

    async def run(self, request: WindowRequest) -> RunResult:
        """Legacy trusted entry. The caller must authorize the target and any historical use."""
        return await self._run(request, None)

    async def run_authorized(self, *, target_window: WindowRequest, authorized_scope: WindowRequest,
                             authorized_history: Iterable[WindowRequest] = (),
                             completion_goal: CompletionGoal | None = None,
                             authorized_videos: Mapping[str, VideoRange] | None = None,
                             authorized_epochs: Iterable[AuthorizedEpoch] = ()) -> RunResult:
        """Use server-provided grants and an optional explicit goal, outside the planner's control."""
        epochs = tuple(authorized_epochs)
        if completion_goal == 'infer_sleep_stage' and (len(epochs) != 1 or self.stage_runtime is None):
            raise ValueError('sleep stage goal requires exactly one trusted epoch and an NPU runtime')
        if completion_goal not in (None, 'inspect_signals', 'verify_observed_activity', 'infer_sleep_stage'):
            raise ValueError('unknown completion goal')
        target = WindowRequest.model_validate(target_window.model_dump())
        grant = WindowRequest.model_validate(authorized_scope.model_dump())
        if not _same_source(target, grant) or not _contains(grant.window, target.window):
            return RunResult(run_id=uuid4(), request=target, termination='target_out_of_scope', trace=())
        history = tuple(WindowRequest.model_validate(item.model_dump()) for item in authorized_history)
        grants = (None if authorized_videos is None else
                  {video_id: VideoRange.model_validate(window.model_dump())
                   for video_id, window in authorized_videos.items()})
        return await self._run(target, history, completion_goal, grants, epochs)

    async def _run(self, request: WindowRequest, history: tuple[WindowRequest, ...] | None,
                   completion_goal: CompletionGoal | None = None,
                   video_grants: Mapping[str, VideoRange] | None = None,
                   stage_grants: tuple[AuthorizedEpoch, ...] = ()) -> RunResult:
        clock = asyncio.get_running_loop().time
        deadline = clock() + self.run_timeout if self.run_timeout is not None else float('inf')
        request = request.model_copy(deep=True)
        run_id = uuid4()
        trace: list[TraceEntry] = []
        seen: set[UUID] = set()
        registry = self.registry
        video_enabled = self.video_archive is not None and video_grants is not None
        if video_enabled:
            registry = SkillRegistry((*self.registry.skills(), video_pose_skill(self.video_archive, video_grants)))
        if self.stage_runtime is not None and stage_grants:
            registry = SkillRegistry((*registry.skills(), sleep_stage_skill(self.stage_runtime, stage_grants)))
        stage_target = stage_grants[0].request if completion_goal == 'infer_sleep_stage' else None
        executor = LocalToolExecutor(registry, request, self.skill_timeout, authorized_history=history)
        plan_with_budget = getattr(self.planner, 'plan_with_budget', None)
        budget_termination = 'task_incomplete' if completion_goal else 'call_limit'
        termination = budget_termination
        finish = None
        rejections: list[FinishRejection] = []
        completion: CompletionCheck | None = None
        for round_number in range(1, self.max_calls + 1 + int(plan_with_budget is not None or completion_goal is not None)):
            remaining = deadline - clock()
            if remaining <= 0:
                termination = 'run_timeout'
                break
            try:
                remaining_calls = self.max_calls - len(trace)
                if completion_goal is not None:
                    completion = check_completion(completion_goal, request, tuple(trace), stage_target)
                    if completion.pending and remaining_calls == 0:
                        break
                feedback = {} if completion is None else {
                    'completion': completion, 'finish_rejection': rejections[-1].reason if rejections else None,
                }
                inputs = (request.model_copy(deep=True), registry.discover(),
                          tuple(entry.model_copy(deep=True) for entry in trace))
                planning = (self.planner.plan(*inputs) if plan_with_budget is None else plan_with_budget(
                    *inputs, remaining_calls=remaining_calls, authorized_history=history, run_id=run_id, **feedback,
                ))
                action = await asyncio.wait_for(planning, timeout=min(self.planner_timeout, remaining))
                if isinstance(action, Finish):
                    candidate = Finish.model_validate(action.model_dump())
                    limitations = required_limitations(request, tuple(trace))
                    cited = set()
                    if candidate.report is not None:
                        originals = {entry.result.call_id: ReportFinding(skill=entry.call.name, result=entry.result)
                                     for entry in trace if entry.result.status == 'succeeded'}
                        findings = candidate.report.findings
                        cited = {item.result.call_id for item in findings}
                        if len(cited) != len(findings) or any(originals.get(item.result.call_id) != item for item in findings):
                            raise ValueError('report findings must match unique successful results from this run')
                    if completion is not None:
                        reason = completion.pending
                        if reason is None and not any(cited.issuperset(option) for option in completion.evidence_options):
                            reason = 'cite_completion_evidence'
                        if reason is not None:
                            rejections.append(FinishRejection(round=round_number, reason=reason))
                            continue
                    if candidate.report is not None:
                        if any(item.call_id not in cited for item in limitations):
                            raise IncompleteEvidence('finish omitted a required limitation result')
                        video_only = ((video_enabled or stage_grants)
                                      and all(item.skill in (VIDEO_POSE, STAGE_SKILL) for item in findings))
                        candidate.report.analysis_scope = None if video_only else request.model_copy(deep=True)
                        candidate.report.limitations = list(limitations)
                    finish = candidate
                    termination = 'finished'
                    break
                if not isinstance(action, ToolCall):
                    raise TypeError('planner must return ToolCall or Finish')
                call = ToolCall.model_validate(action.model_dump())
                if remaining_calls == 0:
                    break
            except IncompleteEvidence:
                termination = 'incomplete_evidence'
                break
            except TimeoutError:
                termination = 'run_timeout' if clock() >= deadline else 'planner_timeout'
                break
            except Exception:
                termination = 'planner_error'
                break

            started_at = datetime.now(timezone.utc)
            if call.call_id in seen:
                result = ToolResult(call_id=call.call_id, status='denied', error='duplicate_call_id')
                termination = 'duplicate_call_id'
            else:
                seen.add(call.call_id)
                try:
                    result = await asyncio.wait_for(
                        executor.execute(call), timeout=max(0, deadline - clock()) if self.run_timeout is not None else None,
                    )
                except TimeoutError:
                    result = ToolResult(call_id=call.call_id, status='failed', error='run_timeout')
                    termination = 'run_timeout'
                if result.error == 'skill_timeout':
                    termination = 'skill_timeout'
            trace.append(TraceEntry(step=len(trace) + 1, started_at=started_at, finished_at=datetime.now(timezone.utc),
                                    call=call, result=result))
            if termination != budget_termination:
                break
        if completion_goal is not None:
            completion = check_completion(completion_goal, request, tuple(trace), stage_target)
        return RunResult(run_id=run_id, request=request, termination=termination, finish=finish, trace=tuple(trace),
                         finish_rejections=tuple(rejections),
                         completion_pending=completion.pending if completion else None)


def create_agent(
    planner: Planner, datasets: Mapping[str, Path] | None = None, *,
    activities: Iterable[RegisteredActivity] = (), planner_timeout: float = 10, run_timeout: float | None = None,
    video_archive: VideoArchive | None = None,
    stage_runtime: SleepStageRuntime | None = None,
) -> SleepAnalysisAgent:
    return SleepAnalysisAgent(SkillRegistry(local_skills(datasets, activities=activities)), planner,
                              planner_timeout=planner_timeout, run_timeout=run_timeout, video_archive=video_archive,
                              stage_runtime=stage_runtime)
