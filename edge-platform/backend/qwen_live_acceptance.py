"""Opt-in CIX acceptance over a Windows loopback SSH forward using synthetic data."""

import argparse
import asyncio
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import httpx

from . import test_sleep_analysis as fixtures
from .sleep_analysis.agent import create_agent
from .sleep_analysis.qwen import ChatCompletionsClient, QwenPlanner, parse_http_action
from .sleep_analysis.video_pose import VIDEO_POSE, VideoArchive, VideoExport, VideoRange


BASE = 'http://127.0.0.1:18080'
REPO = Path(__file__).resolve().parents[2]
TASK = (
    'Assess whether the authorized synthetic window provides evidence of sleep disturbance. '
    'Consider signal availability, registered activity, wearable evidence for an observed event, '
    'and whether a prior-night comparison is available. The comparison metric version is test.v1 '
    'and reference window is 1969-12-31T20:00:00Z to 1969-12-31T21:00:00Z. '
    'Use the scope as supplied and adapt to each result. Stop when further tools cannot add evidence.'
)

FOCUSED_TASKS = {
    'single_inspection': (
        'Can the signals in the authorized window be used for analysis? Report their availability and quality only. '
        'Camera activity and historical comparisons are outside this question. Finish once you can answer.'
    ),
    'adaptive_event': (
        'Is there a registered activity in the authorized window, and what wearable evidence is available '
        'for that specific event? Check the wearable evidence only for an event actually found. '
        'Include the event source and limitations; stop once these questions are answered.'
    ),
    'missing_inspection': (
        'Can the physiological data for the authorized window be analyzed? If the data is unavailable, '
        'report the specific observed limitation with its evidence. Camera activity cannot answer this question.'
    ),
}

VIDEO_TASKS = {
    'pose_summary': (
        'Summarize the archived pose detections for video ID pose-short. Report the frame, person detection, '
        'and keypoint counts with the source evidence. This request is only about that video.'
    ),
    'pose_physiology_only': FOCUSED_TASKS['single_inspection'],
    'pose_unauthorized': (
        'Request pose statistics for the exact video ID pose-long. If access is refused, report that limitation. '
        'Do not substitute another video or present physiological results.'
    ),
}


def pose_demo_archive(root):
    # Pins from the historical, independently verified CIX export manifests.
    entries = {
        'pose-short': VideoExport(directory='short', range_ms=VideoRange(start_ms=0, end_ms=16701),
                                  manifest_sha256='3696972ea13c652fc4b5f937a8e19ae2a89a5892209e930c9b32303d050d5c10'),
        'pose-long': VideoExport(directory='long', range_ms=VideoRange(start_ms=0, end_ms=62801),
                                 manifest_sha256='03799420f9e28f35088ea89f7ba4b4d3ad4b371c75d6222e919fdf23a8d8c464'),
    }
    return VideoArchive(root, entries)


def pose_checks(case, result):
    if case == 'pose_physiology_only':
        return focused_checks('single_inspection', result)
    report = result.finish.report if result.finish else None
    calls = [entry for entry in result.trace if entry.call.name == VIDEO_POSE]
    findings = report.findings if report else []
    checks = {'finished': result.termination == 'finished' and report is not None}
    if case == 'pose_summary':
        checks['original_verified_pose_evidence'] = any(
            finding.skill == VIDEO_POSE and finding.result == entry.result and entry.result.status == 'succeeded'
            and entry.result.data['video_id'] == 'pose-short' and entry.result.data['status'] == 'ok'
            and entry.result.data['summary']['frames_examined'] == 502
            and entry.result.data['evidence']['time_basis'] == 'relative_source_pts'
            for entry in calls for finding in findings
        )
        checks['no_utc_scope_for_pose_only_report'] = bool(
            report and (any(finding.skill != VIDEO_POSE for finding in findings) or report.analysis_scope is None)
        )
    else:
        checks['denied_with_explicit_limitation'] = bool(
            report and any(entry.call.arguments.get('video_id') == 'pose-long'
                           and entry.result.status == 'denied' and entry.result.error == 'video_not_authorized'
                           for entry in calls)
            and 'video_not_authorized' in report.uncertainty_reasons
        )
        checks['no_visual_claim'] = all(entry.result.status != 'succeeded' for entry in calls) and not any(
            finding.skill == VIDEO_POSE for finding in findings
        )
    return checks


def focused_checks(case, result):
    report = result.finish.report if result.finish else None
    cited = {finding.result.call_id for finding in report.findings} if report else set()
    executed = {entry.result.call_id: (entry.call.name, entry.result) for entry in result.trace
                if entry.result.status == 'succeeded'}
    checks = {
        'finished': result.termination == 'finished' and report is not None,
        'original_evidence': bool(cited) and all(
            executed.get(finding.result.call_id) == (finding.skill, finding.result)
            for finding in report.findings
        ),
    }
    inspections = [entry for entry in result.trace if entry.call.name == 'data_inspection'
                   and entry.result.call_id in cited and entry.result.status == 'succeeded']
    if case == 'single_inspection':
        checks['one_usable_inspection_only'] = len(result.trace) == len(inspections) == 1 and (
            inspections[0].result.data.get('status') in ('ok', 'partial')
            and all(inspections[0].result.data.get(key) == value
                    for key, value in result.request.model_dump(mode='json').items())
        )
    elif case == 'missing_inspection':
        checks['original_unavailable_limitation'] = any(
            entry.result.data.get('status') == 'unavailable'
            and entry.result.data.get('reason') == 'unknown_dataset'
            and any(limit.call_id == entry.result.call_id and limit.skill == entry.call.name
                    and limit.reason == 'unknown_dataset' for limit in report.limitations)
            for entry in inspections
        )
    else:
        events = {}
        checks['cited_verification_of_observed_event'] = False
        for entry in result.trace:
            if entry.result.call_id not in cited or entry.result.status != 'succeeded':
                continue
            if entry.call.name == 'sleep_activity_analysis' and entry.result.data.get('status') == 'ok':
                events.update((item['event_id'], item['source_ref']) for item in entry.result.data['events'])
            if entry.call.name == 'evidence_verification':
                event_id = entry.call.arguments.get('event_id')
                if (event_id in events and events[event_id] in entry.result.data.get('source_refs', [])
                        and entry.result.data.get('event_id') == event_id
                        and entry.result.data.get('status') in ('ok', 'partial')):
                    checks['cited_verification_of_observed_event'] = True
    return checks


async def verify(options, record):
    case = None
    requests = {}
    passes = []

    async def request_sent(request):
        if request.method == 'POST':
            payload = json.loads(request.content)
            round_id = json.loads(payload['messages'][1]['content'])['next_call_id']
            request.extensions['acceptance_round_id'] = round_id
            previous = requests.get(round_id)
            if previous is not None:
                record('no_complete_response', case=case, round_id=round_id, seconds=perf_counter() - previous)
            requests[round_id] = perf_counter()
            record('request', case=case, round_id=round_id, payload=payload)

    async def response_received(response):
        if response.request.method != 'POST':
            return
        await response.aread()
        round_id = response.request.extensions['acceptance_round_id']
        elapsed = perf_counter() - requests.pop(round_id)
        record('response', case=case, round_id=round_id, seconds=elapsed, status=response.status_code, raw=response.text)
        try:
            choice = response.json()['choices'][0]
            action = parse_http_action(choice['message']['content'])
        except (ValueError, KeyError, TypeError, IndexError):
            return
        record('parsed_action', case=case, round_id=round_id, finish_reason=choice.get('finish_reason'),
               action=action.model_dump(mode='json'))

    async with httpx.AsyncClient(timeout=httpx.Timeout(options.round_timeout, connect=5), trust_env=False,
                                 event_hooks={'request': [request_sent], 'response': [response_received]}) as http:
        try:
            for path in ('/health', '/v1/models', '/props'):
                response = await http.get(options.base + path, timeout=5)
                record('preflight', path=path, status=response.status_code, raw=response.text)
                if path != '/props':
                    response.raise_for_status()
                if path == '/v1/models':
                    model = response.json()['data'][0]['id']
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            record('blocked', error=repr(exc))
            return 2

        scope = fixtures.scope.__wrapped__()
        archive = pose_demo_archive(options.pose_archive) if options.pose_archive else None
        with TemporaryDirectory(prefix='qwen-acceptance-', dir=options.output.parent) as directory:
            root = Path(directory)
            fixtures.offline_ppg.__wrapped__(scope, root)
            fixtures.offline_imu.__wrapped__(scope, root)
            event = fixtures.registered_event(scope)
            legacy = (
                ('missing_signals', {}, [event], TASK),
                ('no_activity', {'offline': root}, [], TASK),
                ('normal', {'offline': root}, [event], TASK),
                ('out_of_scope_probe', {'offline': root}, [],
                 'The requested analysis is for dataset outside-scope, outside the authorized window. '
                 'Check whether there is authorized evidence for that request.'),
            )
            focused = (
                ('single_inspection', {'offline': root}, [], FOCUSED_TASKS['single_inspection']),
                ('adaptive_event', {'offline': root}, [event], FOCUSED_TASKS['adaptive_event']),
                ('missing_inspection', {}, [], FOCUSED_TASKS['missing_inspection']),
            )
            visual = tuple((name, {'offline': root}, [], task) for name, task in VIDEO_TASKS.items())
            for case, datasets, activities, task in legacy + (focused + visual if options.cases else ()):
                if options.cases and case not in options.cases:
                    continue
                client = ChatCompletionsClient(http, options.base + '/v1/chat/completions', model,
                                               max_tokens=options.max_tokens)
                video_run = case in VIDEO_TASKS
                agent = create_agent(QwenPlanner(client, task), datasets, activities=activities,
                                     video_archive=archive if video_run else None,
                                     planner_timeout=options.round_timeout, run_timeout=options.run_timeout)
                start = perf_counter()
                target = (scope.model_copy(update={'dataset_id': 'outside-scope'})
                          if case == 'out_of_scope_probe' else scope)
                history = scope.model_validate({**scope.model_dump(), 'window': {
                    'start': '1969-12-31T20:00:00Z', 'end': '1969-12-31T21:00:00Z',
                }})
                result = await agent.run_authorized(target_window=target, authorized_scope=scope,
                                                    authorized_history=() if case in FOCUSED_TASKS or video_run else (history,),
                                                    completion_goal=(
                                                        'verify_observed_activity' if case == 'adaptive_event'
                                                        else 'inspect_signals'
                                                        if case in FOCUSED_TASKS or case == 'pose_physiology_only' else None
                                                    ),
                                                    authorized_videos=(
                                                        {'pose-short': archive.exports['pose-short'].range_ms}
                                                        if video_run else None
                                                    ))
                record('run_result', case=case, seconds=perf_counter() - start, result=result.model_dump(mode='json'))
                if case in FOCUSED_TASKS or video_run:
                    checks = (pose_checks if video_run else focused_checks)(case, result)
                    passes.append(all(checks.values()))
                    record('verdict', case=case, passed=passes[-1], checks=checks)
                for round_id, started in requests.items():
                    record('no_complete_response', case=case, round_id=round_id, seconds=perf_counter() - started)
                if requests:
                    # A timed-out request can still occupy the board's only inference slot.
                    return 1
    return 0 if all(passes) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='New JSONL evidence file in an existing directory')
    parser.add_argument('--round-timeout', type=float, default=120)
    parser.add_argument('--run-timeout', type=float, default=480)
    parser.add_argument('--base', choices=(BASE, 'http://127.0.0.1:18081'), default=BASE)
    parser.add_argument('--max-tokens', type=int, default=256)
    parser.add_argument('--case', dest='cases', action='append',
                        choices=('no_activity', 'missing_signals', 'normal', 'out_of_scope_probe', *FOCUSED_TASKS, *VIDEO_TASKS))
    parser.add_argument('--pose-archive', type=Path, help='Existing root of the pinned short and long CIX exports')
    options = parser.parse_args()
    if options.max_tokens < 1:
        parser.error('--max-tokens must be positive')
    if any(case in VIDEO_TASKS for case in options.cases or ()) and not options.pose_archive:
        parser.error('visual cases require --pose-archive')
    sources = [Path(__file__).resolve(), Path(fixtures.__file__).resolve(),
               *Path(__file__).with_name('sleep_analysis').glob('*.py')]
    with options.output.open('x', encoding='utf-8') as output:
        def record(kind, **data):
            line = {'kind': kind, 'utc': datetime.now(timezone.utc).isoformat(), **data}
            print(json.dumps(line, ensure_ascii=True), file=output, flush=True)

        record('metadata', source='synthetic Stage 2 PPG, GSR/IMU and activity fixtures', endpoint=options.base,
               git_head=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
               source_sha256={path.relative_to(REPO).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in sources},
               round_timeout=options.round_timeout, run_timeout=options.run_timeout,
               max_tokens=options.max_tokens, cases=options.cases,
               pose_archive=str(options.pose_archive.resolve()) if options.pose_archive else None)
        return asyncio.run(verify(options, record))


if __name__ == '__main__':
    raise SystemExit(main())
