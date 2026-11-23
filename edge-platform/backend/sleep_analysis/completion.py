"""Completion conditions for explicit, caller-supplied sleep analysis tasks."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import ValidationError

from .contracts import ActivityOutput, InspectionOutput, VerificationOutput, WindowRequest
from .sleep_stage import STAGE_SKILL, SleepStageRequest, SleepStageOutput
from .depth_posture import DEPTH_SKILL, DepthPostureRequest, DepthPostureOutput

if TYPE_CHECKING:
    from .agent import TraceEntry


CompletionGoal = Literal['inspect_signals', 'verify_observed_activity', 'infer_sleep_stage', 'infer_depth_posture']


@dataclass(frozen=True)
class CompletionCheck:
    goal: CompletionGoal
    pending: str | None = None
    evidence_options: tuple[tuple[UUID, ...], ...] = ()
    terminal_failure: str | None = None


def check_completion(
    goal: CompletionGoal, target: WindowRequest, trace: tuple['TraceEntry', ...],
    stage_target: SleepStageRequest | None = None,
    depth_target: DepthPostureRequest | None = None,
) -> CompletionCheck:
    """Find scoped evidence sets that answer the goal or document a real limitation."""
    if goal == 'infer_depth_posture':
        for entry in reversed(trace):
            if (entry.call.name != DEPTH_SKILL or entry.call.call_id != entry.result.call_id
                    or depth_target is None or entry.call.arguments != depth_target.model_dump()):
                continue
            if entry.result.status == 'denied' and entry.result.error == 'depth_posture_not_authorized':
                return CompletionCheck(goal, evidence_options=((),), terminal_failure=entry.result.error)
            try:
                item = DepthPostureOutput.model_validate(entry.result.data)
            except ValidationError:
                continue
            if (item.execution_id != entry.call.call_id
                    or item.model_dump(include=set(DepthPostureRequest.model_fields)) != depth_target.model_dump()):
                continue
            if entry.result.status == 'succeeded' and item.status == 'success':
                return CompletionCheck(goal, evidence_options=((entry.call.call_id,),))
            if entry.result.status == 'failed' and item.status != 'success':
                return CompletionCheck(goal, evidence_options=((),), terminal_failure=item.reason)
        return CompletionCheck(goal, 'authorized_depth_sample_posture')
    if goal == 'infer_sleep_stage':
        options = []
        for entry in trace:
            if (entry.call.name != STAGE_SKILL or entry.result.status != 'succeeded'
                    or entry.call.call_id != entry.result.call_id or stage_target is None):
                continue
            item = SleepStageOutput.model_validate(entry.result.data)
            if item.model_dump(include=set(type(stage_target).model_fields)) == stage_target.model_dump():
                if (item.status == 'ok' and item.backend == 'CIX NOE NPU' and item.logits is not None
                        and item.predicted_stage is not None) or (item.status == 'unavailable' and item.reason):
                    options.append((entry.result.call_id,))
        return CompletionCheck(goal, None if options else 'authorized_epoch_sleep_stage', tuple(options))
    outputs = {
        'data_inspection': InspectionOutput,
        'sleep_activity_analysis': ActivityOutput,
        'evidence_verification': VerificationOutput,
    }
    results = []
    for entry in trace:
        model = outputs.get(entry.call.name)
        if model is None or entry.result.status != 'succeeded' or entry.result.call_id != entry.call.call_id:
            continue
        try:
            output = model.model_validate(entry.result.data)
        except ValidationError:
            continue
        if (output.dataset_id == target.dataset_id and output.session_id == target.session_id
                and target.window.start <= output.window.start < output.window.end <= target.window.end):
            results.append((entry.result.call_id, output))

    if goal == 'inspect_signals':
        options = tuple((call_id,) for call_id, item in results if isinstance(item, InspectionOutput)
                        and item.window == target.window and (item.status in ('ok', 'partial') or (
                            item.status == 'unavailable' and item.reason in (
                                'unknown_dataset', 'missing_databases', 'edge_ppg_dependency_unavailable', 'no_usable_window',
                            )
                        )))
        return CompletionCheck(goal, None if options else 'target_signal_availability', options)

    observed = [(call_id, event) for call_id, item in results if isinstance(item, ActivityOutput)
                and item.status == 'ok' for event in item.events or ()
                if event.source_ref.dataset_id == target.dataset_id and event.source_ref.session_id == target.session_id
                and item.window.start <= event.start_time < event.end_time <= item.window.end
                and event.source_ref in item.evidence]
    if not observed:
        options = tuple((call_id,) for call_id, item in results if isinstance(item, ActivityOutput)
                        and item.window == target.window and not item.events and (
                            item.status == 'ok' or (item.status == 'unavailable' and item.reason == 'not_implemented')
                        ))
        return CompletionCheck(goal, None if options else 'registered_activity_in_target', options)

    options = tuple((activity_id, verification_id)
                    for activity_id, event in observed
                    for verification_id, item in results if isinstance(item, VerificationOutput)
                    and item.event_id == event.event_id
                    and item.event_window == event.source_ref.window
                    and item.event_provenance == event.provenance
                    and item.event_source in (None, event.source_ref)
                    and event.source_ref in item.source_refs and event.source_ref in item.evidence
                    and item.window.start <= event.start_time < event.end_time <= item.window.end
                    and (item.status in ('ok', 'partial') or (
                        item.status == 'unavailable' and item.reason == 'insufficient_evidence'
                    )))
    return CompletionCheck(goal, None if options else 'wearable_evidence_for_observed_activity', options)
