import asyncio
import json
import math
import sqlite3
import struct
from contextlib import closing
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import httpx
from pydantic import ValidationError

from shared.interfaces import ToolCall

from .sleep_analysis.agent import Finish, RunResult, SkillRegistry, SleepAnalysisAgent, create_agent
from .sleep_analysis.contracts import ActivityEvent, RegisteredActivity, SynchronizationQuality, WindowRequest
from .sleep_analysis.qwen import ChatCompletionsClient, QwenPlanner, _calls_without_new_usable_data, _evidence_feedback
from .sleep_analysis.skills import Skill, local_skills


@pytest.fixture
def scope():
    start = datetime.fromtimestamp(1002, timezone.utc)
    return WindowRequest(dataset_id='offline', session_id='session', window={
        'start': start, 'end': start + timedelta(seconds=10),
    })


def call(scope, name, **extra):
    return ToolCall(call_id=uuid4(), name=name, arguments={**scope.model_dump(mode='json'), **extra})


class ScriptedTestPlanner:
    """Test-only planner. Each script decides what to do with the observed trace."""

    def __init__(self, script):
        self.script = script

    async def plan(self, request, skills, trace):
        return self.script(request, trace)


def run(scope, script, registry=None, **limits):
    agent = SleepAnalysisAgent(registry or SkillRegistry(local_skills()), ScriptedTestPlanner(script), **limits)
    return asyncio.run(agent.run(scope))


def test_discovery_and_duplicate_registration():
    skills = local_skills()
    registry = SkillRegistry(skills)
    specs = {spec['name']: spec for spec in registry.discover()}
    assert set(specs) == {'data_inspection', 'sleep_activity_analysis', 'evidence_verification', 'historical_comparison'}
    assert 'event_id' in specs['evidence_verification']['input_schema']['required']
    assert 'history_window' in specs['historical_comparison']['output_schema']['required']
    assert 'sources' in specs['data_inspection']['output_schema']['properties']
    with pytest.raises(ValueError, match='duplicate'):
        registry.register(skills[0])


@pytest.mark.parametrize('mismatch', [None, 'window', 'dataset_id', 'session_id'])
def test_authorized_target_checked_before_planner(scope, mismatch):
    target = scope.model_dump()
    if mismatch == 'window':
        target['window']['end'] += timedelta(seconds=1)
    elif mismatch:
        target[mismatch] = 'other'
    planned = []

    def plan(request, _):
        planned.append(request)
        return Finish(reason='done')

    agent = create_agent(ScriptedTestPlanner(plan))
    result = asyncio.run(agent.run_authorized(target_window=WindowRequest.model_validate(target), authorized_scope=scope))
    assert result.termination == ('target_out_of_scope' if mismatch else 'finished')
    assert planned == ([] if mismatch else [scope])
    assert not result.trace


@pytest.mark.parametrize('grant_kind', ['none', 'exact', 'partial', 'other_session'])
def test_authorized_history_requires_separate_grant(scope, grant_kind):
    reference = {'start': '1969-12-31T20:00:00Z', 'end': '1969-12-31T21:00:00Z'}
    grant = scope.model_dump()
    grant['window'] = reference.copy()
    if grant_kind == 'partial':
        grant['window']['end'] = '1969-12-31T20:59:00Z'
    if grant_kind == 'other_session':
        grant['session_id'] = 'other'
    history = () if grant_kind == 'none' else (WindowRequest.model_validate(grant),)

    def plan(request, trace):
        return (Finish(reason='done') if trace else
                call(request, 'historical_comparison', metric_version='v1', history_window=reference))

    agent = create_agent(ScriptedTestPlanner(plan))
    result = asyncio.run(agent.run_authorized(target_window=scope, authorized_scope=scope, authorized_history=history))
    tool = result.trace[0].result
    assert result.termination == 'finished'
    if grant_kind == 'exact':
        assert tool.status == 'succeeded' and tool.data['reason'] == 'no_comparable_history'
    else:
        assert tool.status == 'denied' and tool.error == 'history_out_of_scope'


def test_validation_whitelist_scope_and_placeholders(scope):
    original = scope.model_dump(mode='json')
    calls = [
        call(scope, 'shell', command='anything'),
        call(scope, 'evidence_verification'),
        call(scope, 'data_inspection', path='arbitrary.sqlite3'),
        call(scope, 'data_inspection', window={**original['window'], 'end': '1970-01-01T00:16:50Z'}),
        call(scope, 'data_inspection', window={**original['window'], 'start': '1970-01-01T00:16:42'}),
        call(scope, 'data_inspection', dataset_id='other'),
        call(scope, 'sleep_activity_analysis'),
        call(scope, 'evidence_verification', event_id='event-1'),
        call(scope, 'historical_comparison', metric_version='v1', history_window={
            'start': '1969-12-31T20:00:00Z', 'end': '1969-12-31T21:00:00Z',
        }),
    ]
    result = run(scope, lambda _, trace: calls[len(trace)] if len(trace) < len(calls) else Finish(reason='done'),
                 max_calls=10)
    assert result.termination == 'finished'
    assert [(entry.result.status, entry.result.error) for entry in result.trace[:6]] == [
        ('denied', 'unknown_skill'), ('denied', 'invalid_arguments'), ('denied', 'invalid_arguments'),
        ('denied', 'invalid_arguments'), ('denied', 'invalid_arguments'), ('denied', 'out_of_scope'),
    ]
    for entry, reason in zip(result.trace[6:], ('not_implemented', 'unknown_activity_event', 'no_comparable_history')):
        assert entry.result.status == 'succeeded'
        assert entry.result.data['status'] == 'unavailable'
        assert entry.result.data['reason'] == reason
        assert entry.result.data['evidence'] == []
    assert result.trace[6].result.data['events'] is None
    assert result.trace[7].result.data['verification_status'] == 'insufficient_evidence'
    assert result.trace[8].result.data['comparable_nights'] is None


def test_inspection_missing_databases_and_no_file_creation(scope, tmp_path):
    registry = SkillRegistry(local_skills({'offline': tmp_path}))
    result = run(scope, lambda req, _: call(req, 'data_inspection'), registry, max_calls=1)
    data = result.trace[0].result.data
    assert data['status'] == 'unavailable'
    assert data['reason'] == 'missing_databases'
    assert {source['status'] for source in data['sources']} == {'missing'}
    assert data['evidence'] == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('first_result', ['unavailable', 'transient_failure', 'recoverable_unavailable'])
def test_only_deterministic_unavailable_calls_are_deduplicated(scope, first_result):
    skill = next(skill for skill in local_skills() if skill.name == 'historical_comparison')
    executed = []

    async def handler(request):
        executed.append(request)
        if len(executed) == 1:
            if first_result == 'transient_failure':
                raise OSError('temporary fixture failure')
            if first_result == 'recoverable_unavailable':
                return skill.output_model(**request.model_dump(), status='unavailable', reason='temporary_failure')
        return await skill.handler(request)

    registry = SkillRegistry([Skill(skill.name, skill.description, skill.input_model, skill.output_model, handler)])
    history = {'start': '1969-12-31T20:00:00Z', 'end': '1969-12-31T21:00:00Z'}
    equivalent = {'end': '1970-01-01T05:00:00+08:00', 'start': '1970-01-01T04:00:00+08:00'}
    calls = [call(scope, skill.name, metric_version=version, history_window=window)
             for version, window in [('v1', history), ('v1', equivalent), ('v2', history), ('v1', history)]]

    def plan(_, trace):
        return calls[len(trace)] if len(trace) < len(calls) else Finish(reason='done')

    result = run(scope, plan, registry)
    assert result.termination == 'finished'
    original = result.trace[0 if first_result == 'unavailable' else 1].result
    assert original.status == 'succeeded' and original.data['reason'] == 'no_comparable_history'
    assert [request.metric_version for request in executed] == (
        ['v1', 'v2'] if first_result == 'unavailable' else ['v1', 'v1', 'v2'])
    for index in ([1, 3] if first_result == 'unavailable' else [3]):
        duplicate = result.trace[index].result
        assert duplicate.call_id == calls[index].call_id
        assert duplicate.status == 'denied' and duplicate.error == 'duplicate_unavailable_call'
        assert duplicate.data == {'original_call_id': str(original.call_id)}
    assert result.trace[2].result.status == 'succeeded'
    assert run(scope, lambda req, _: call(req, skill.name, metric_version='v1', history_window=history),
               registry, max_calls=1).trace[0].result.status == 'succeeded'


def test_optional_reader_dependency(scope, tmp_path):
    (tmp_path / 'ppg.sqlite3').touch()

    def missing_reader(_):
        raise ModuleNotFoundError('inference')

    registry = SkillRegistry(local_skills({'offline': tmp_path}, open_reader=missing_reader))
    result = run(scope, lambda req, _: call(req, 'data_inspection'), registry, max_calls=1)
    assert result.trace[0].result.data['reason'] == 'edge_ppg_dependency_unavailable'


@pytest.fixture
def offline_ppg(scope, tmp_path):
    pytest.importorskip('inference.adapter')
    path = tmp_path / 'ppg.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.executescript('''
            CREATE TABLE sessions(id TEXT, started_utc REAL, config TEXT);
            CREATE TABLE events(id INTEGER PRIMARY KEY, session TEXT, recorded_utc REAL, kind TEXT, detail TEXT);
            CREATE TABLE metrics(session TEXT, recorded_utc REAL, payload TEXT);
            CREATE TABLE raw_chunks(id INTEGER PRIMARY KEY, session TEXT, segment INTEGER,
                                    received_utc REAL, elapsed_s REAL, data BLOB);
        ''')
        db.execute('INSERT INTO sessions VALUES(?,?,?)', (scope.session_id, 1000, '{"fs":500}'))
        for index in range(150):
            signal = (int(20000 + 500 * math.sin(sample * math.tau / 500))
                      for sample in range(index * 50, (index + 1) * 50))
            payload = b''.join(struct.pack('<HH', value, 0) for value in signal)
            frame = bytes.fromhex('a5ca11db') + payload + bytes.fromhex('5a')
            elapsed = (index + 1) / 10
            db.execute('INSERT INTO raw_chunks VALUES(?,?,?,?,?,?)',
                       (index + 1, scope.session_id, 1, 1000 + elapsed, elapsed, frame))
        db.executemany('INSERT INTO metrics VALUES(?,?,?)', [
            (scope.session_id, stamp, '{"quality":"usable","measurement_enabled":true,"heart_rate_bpm":60,"segment":1}')
            for stamp in range(1002, 1012)
        ])
        db.commit()
    return path


def test_adaptive_plan_with_real_read_only_adapter(scope, tmp_path, offline_ppg):
    path = offline_ppg
    before = path.read_bytes()

    def plan(request, trace):
        if not trace:
            return call(request, 'data_inspection')
        if len(trace) == 1 and trace[-1].result.data['status'] == 'partial':
            return call(request, 'sleep_activity_analysis')
        return Finish(reason='test_finished')

    present = run(scope, plan, SkillRegistry(local_skills({'offline': tmp_path})))
    missing = run(scope, plan)
    assert present.termination == missing.termination == 'finished'
    assert [entry.call.name for entry in present.trace] == ['data_inspection', 'sleep_activity_analysis']
    assert [entry.call.name for entry in missing.trace] == ['data_inspection']
    data = present.trace[0].result.data
    assert data['sources'][0]['status'] == 'usable'
    assert data['sources'][0]['raw_points'] > 0
    assert data['sources'][1]['status'] == 'missing'
    assert data['cross_device_synchronization_verified'] is False
    assert data['evidence'][0]['session_id'] == scope.session_id
    assert data['evidence'][0]['window'] == scope.window.model_dump(mode='json')
    assert path.read_bytes() == before
    assert not (tmp_path / 'gsr.sqlite3').exists()


def test_failed_skill_is_visible_and_planner_can_recover(scope):
    original = local_skills()[0]

    async def fail(_):
        raise RuntimeError('private details')

    registry = SkillRegistry([Skill(original.name, original.description, original.input_model,
                                    original.output_model, fail)])

    def plan(request, trace):
        if not trace:
            return call(request, original.name)
        assert trace[-1].result.error == 'skill_execution_failed'
        trace[-1].result.error = 'attempt to rewrite trace'
        return Finish(reason='handled failure')

    result = run(scope, plan, registry)
    entry = result.trace[0]
    assert result.termination == 'finished'
    assert entry.result.status == 'failed'
    assert entry.result.error == 'skill_execution_failed'
    assert entry.call.call_id == entry.result.call_id
    assert entry.step == 1
    assert entry.started_at <= entry.finished_at
    assert RunResult.model_validate_json(result.model_dump_json()) == result


def test_call_limit_and_duplicate_id(scope):
    limited = run(scope, lambda req, _: call(req, 'data_inspection'), max_calls=2)
    assert limited.termination == 'call_limit'
    assert len(limited.trace) == 2
    first = call(scope, 'data_inspection')
    duplicate = run(scope, lambda *_: first)
    assert duplicate.termination == 'duplicate_call_id'
    assert duplicate.trace[1].result.error == 'duplicate_call_id'


def test_planner_error_and_timeouts(scope):
    class SlowTestPlanner:
        async def plan(self, *_):
            await asyncio.sleep(60)

    async def slow_skill(_):
        await asyncio.sleep(60)

    invalid = run(scope, lambda *_: {'name': 'data_inspection'})
    assert invalid.termination == 'planner_error'
    assert invalid.trace == ()
    timed_out = asyncio.run(SleepAnalysisAgent(SkillRegistry(), SlowTestPlanner(), planner_timeout=0.01).run(scope))
    assert timed_out.termination == 'planner_timeout'
    original = local_skills()[0]
    registry = SkillRegistry([Skill(original.name, original.description, original.input_model,
                                    original.output_model, slow_skill)])
    result = run(scope, lambda req, _: call(req, original.name), registry, skill_timeout=0.01)
    assert result.termination == 'skill_timeout'
    assert result.trace[0].result.error == 'skill_timeout'


@pytest.fixture
def offline_imu(scope, tmp_path):
    path = tmp_path / 'gsr.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.executescript('''
            CREATE TABLE sessions(id TEXT, started_utc REAL, config TEXT);
            CREATE TABLE events(id INTEGER PRIMARY KEY, session TEXT, recorded_utc REAL, kind TEXT, detail TEXT);
            CREATE TABLE samples(id INTEGER PRIMARY KEY, session TEXT, segment INTEGER,
                                 received_utc REAL, elapsed_s REAL, frame_index INTEGER,
                                 adc INTEGER, ax INTEGER, ay INTEGER, az INTEGER, gx INTEGER, gy INTEGER, gz INTEGER);
        ''')
        db.execute('INSERT INTO sessions VALUES(?,?,?)', (scope.session_id, 1000, '{"nominal_fs":50}'))
        for index in range(755):
            elapsed = (index // 2 * 2) / 50.32 + 0.02
            db.execute('INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (index + 1, scope.session_id, 1, 1000 + elapsed, elapsed, index, 100 + index % 9,
                        index % 11, index % 13, 16000 + index % 17, index % 5, index % 7, index % 3))
        db.commit()
    return path


def registered_event(scope, modality='depth', *, aligned=True):
    window = {'start': scope.window.start + timedelta(seconds=2), 'end': scope.window.end - timedelta(seconds=2)}
    return RegisteredActivity(
        event=ActivityEvent(
            event_id='activity-1', start_time=window['start'], end_time=window['end'],
            event_type='movement', activity_metrics={'fixture_score': {
                'value': 2.5, 'unit': 'fixture_unit', 'definition': 'depth or thermal fixture score',
            }},
            source_modality=modality, source_ref={
                'dataset_id': scope.dataset_id, 'session_id': scope.session_id,
                'source': modality, 'record_id': 'fixture-record', 'window': window,
            },
            quality_status='usable', algorithm_version='fixture.v1', uncertainty_reasons=[], provenance='test',
        ),
        synchronization=SynchronizationQuality(status='verified', alignment_ref='fixture-clock', error_bound_s=0.1)
        if aligned else SynchronizationQuality(),
    )


@pytest.mark.parametrize('modality', ['depth', 'thermal'])
def test_camera_contract(scope, modality):
    activity = registered_event(scope, modality)
    assert ActivityEvent.model_validate_json(activity.event.model_dump_json()) == activity.event
    payload = activity.event.model_dump()
    with pytest.raises(ValidationError):
        ActivityEvent.model_validate({**payload, 'start_time': activity.event.start_time.replace(tzinfo=None)})
    with pytest.raises(ValidationError):
        ActivityEvent.model_validate({**payload, 'source_modality': 'thermal' if modality == 'depth' else 'depth'})


@pytest.mark.parametrize('condition', ['normal', 'missing_ppg', 'missing_imu', 'quality', 'unsynchronized'])
def test_evidence_verification(scope, tmp_path, offline_ppg, offline_imu, condition):
    if condition == 'missing_ppg':
        offline_ppg.unlink()
    if condition == 'missing_imu':
        offline_imu.unlink()
    if condition == 'quality':
        with closing(sqlite3.connect(offline_imu)) as db:
            db.execute('DELETE FROM samples WHERE elapsed_s BETWEEN 6 AND 7')
            db.commit()
    paths = {path: path.read_bytes() for path in (offline_ppg, offline_imu) if path.exists()}
    activity = registered_event(scope, aligned=condition != 'unsynchronized')
    registry = SkillRegistry(local_skills({'offline': tmp_path}, activities=[activity]))
    result = run(scope, lambda req, _: call(req, 'evidence_verification', event_id='activity-1'),
                 registry, max_calls=1)
    assert result.trace[0].result.status == 'succeeded'
    data = result.trace[0].result.data
    assert data['verification_status'] == 'insufficient_evidence'
    assert data['event_provenance'] == 'test'
    assert data['source_refs'][0]['record_id'] == 'fixture-record'
    assert data['analysis_window'] == scope.window.model_dump(mode='json')
    assert data['hypothesis'] == 'camera_activity_coincides_with_observable_wearable_motion'
    assert 'no_validated_motion_criterion' in data['uncertainty_reasons']
    if condition == 'missing_ppg':
        assert data['physiological_features'] is None
        assert data['signal_quality'][0]['status'] == 'missing'
    else:
        assert data['physiological_features']['pulse_rate_median_bpm'] == 60
        assert data['signal_quality'][0]['estimated_span_coverage'] == pytest.approx(1.0)
    if condition in ('missing_imu', 'quality'):
        assert data['motion_features'] is None
        assert data['signal_quality'][1]['status'] == ('missing' if condition == 'missing_imu' else 'rejected')
    else:
        assert data['motion_features']['accel_centered_rms_raw_count'] > 0
    if condition == 'unsynchronized':
        assert data['synchronization_quality']['status'] == 'unverified'
        assert 'camera_wearable_alignment_unverified' in data['uncertainty_reasons']
    assert all(path.read_bytes() == content for path, content in paths.items())


def test_control_window_is_scoped(scope):
    activity = registered_event(scope)
    before = {'start': '1970-01-01T00:16:32Z', 'end': '1970-01-01T00:16:42Z'}
    plan = lambda req, _: call(req, 'evidence_verification', event_id='activity-1', before_window=before)
    registry = SkillRegistry(local_skills(activities=[activity]))
    result = run(scope, plan, registry, max_calls=1)
    assert result.trace[0].result.error == 'out_of_scope'
    earlier_scope = WindowRequest.model_validate({**scope.model_dump(), 'window': {
        'start': before['start'], 'end': scope.window.end,
    }})
    result = run(earlier_scope, lambda *_: plan(scope, ()), registry, max_calls=1)
    assert result.trace[0].result.data['controls'][0]['role'] == 'before'
    assert result.trace[0].result.data['controls'][0]['window'] == before


def model_call(prompt, name, **extra):
    return {'action': 'call', 'call': {
        'call_id': prompt['next_call_id'], 'name': name, 'arguments': {**prompt['scope'], **extra},
    }}


def model_finish(prompt):
    if 'run_ref' in prompt:
        return {'action': 'finish', 'run_ref': prompt['run_ref'], 'evidence': {
            item['result']['ref']: True for item in prompt['trace'] if 'ref' in item['result']
        }}
    return {'action': 'finish', 'evidence_call_ids': [
        item['result']['call_id'] for item in prompt['trace'] if item['result']['status'] == 'succeeded'
    ]}


@pytest.mark.parametrize('cite', ['original', 'omit', 'denied'])
def test_required_limitation_uses_original_id_on_final_round(scope, cite):
    def respond(prompt):
        if prompt['remaining_calls']:
            return model_call(prompt, 'data_inspection')
        first, duplicate = prompt['trace']
        assert prompt['remaining_calls'] == 0 and not prompt['skills']
        assert prompt['recent_calls_without_usable_data'] == 2
        assert duplicate['result']['error'] == 'duplicate_unavailable_call'
        first_id = first['call']['call_id']
        assert prompt['required_evidence'] == [dict(skill='data_inspection', reason='unknown_dataset', call_id=first_id)]
        ids = {'original': [first_id], 'omit': [], 'denied': [duplicate['call']['call_id']]}[cite]
        return {'action': 'finish', 'evidence_call_ids': ids}

    client = TestModelClient(respond)
    agent = SleepAnalysisAgent(SkillRegistry(local_skills()), QwenPlanner(client, 'offline test'), max_calls=2)
    result = asyncio.run(agent.run_authorized(target_window=scope, authorized_scope=scope))
    assert len(result.trace) == 2 and len(client.prompts) == 3
    assert result.termination == {'original': 'finished', 'omit': 'incomplete_evidence', 'denied': 'planner_error'}[cite]
    if cite == 'original':
        report = result.finish.report
        assert report.analysis_scope == scope
        assert report.limitations[0].call_id == result.trace[0].result.call_id
        assert report.findings[0].result == result.trace[0].result
    else:
        assert result.finish is None


class TestModelClient:
    """Test double for strict JSON planning, with no network or model inference."""

    __test__ = False

    def __init__(self, script):
        self.script = script
        self.prompts = []

    async def complete(self, *, system, prompt):
        data = json.loads(prompt)
        self.prompts.append(data)
        result = self.script(data)
        return result if isinstance(result, str) else json.dumps(result)


def test_qwen_chat_protocol_multi_round_and_grounded_report(scope, tmp_path, offline_ppg, offline_imu):
    prompts = []

    async def respond(request):
        assert str(request.url) == 'https://model.invalid/v1/chat/completions'
        assert request.headers['Authorization'] == 'Bearer fixture-token'
        payload = json.loads(request.content)
        assert payload['model'] == 'test-qwen' and payload['stream'] is False
        assert payload['max_tokens'] == 256
        assert payload['messages'][0]['role'] == 'system'
        prompt = json.loads(payload['messages'][1]['content'])
        prompts.append(prompt)
        assert prompt['task'] == '帮我分析昨晚为什么睡得不安稳'
        trace = prompt['trace']
        observed_events = [event['event_id'] for item in trace if item['call']['name'] == 'sleep_activity_analysis'
                           for event in item['result']['data'].get('events', [])]
        available = [skill for skill in local_skills() if skill.name != 'evidence_verification' or observed_events]
        assert [spec['name'] for spec in prompt['skills']] == [skill.name for skill in available]
        format_ = payload['response_format']
        assert format_['type'] == 'json_schema' and format_['json_schema']['strict'] is True
        schema = format_['json_schema']['schema']
        finish_schema, *call_schemas = schema['anyOf']
        evidence = finish_schema['properties']['evidence']
        assert set(evidence['properties']) == set(model_finish(prompt)['evidence'])
        assert evidence['required'] == list(prompt['required_evidence'])
        for skill, branch in zip(available, call_schemas, strict=True):
            properties = branch['properties']['call']['properties']
            assert properties['name'] == {'const': skill.name}
            assert properties['call_id'] == {'const': prompt['next_call_id']}
            expected = skill.input_model.model_json_schema()
            assert all(schema['$defs'][name] == definition for name, definition in expected.pop('$defs', {}).items())
            if skill.name == 'evidence_verification':
                expected['properties']['event_id']['enum'] = observed_events
            assert properties['arguments'] == expected
        if not trace:
            action = model_call(prompt, 'data_inspection')
        elif trace[-1]['call']['name'] == 'data_inspection' and trace[-1]['result']['data']['status'] == 'ok':
            action = model_call(prompt, 'sleep_activity_analysis')
        elif trace[-1]['call']['name'] == 'sleep_activity_analysis':
            event = trace[-1]['result']['data']['events'][0]
            assert event['provenance'] == 'test'
            action = model_call(prompt, 'evidence_verification', event_id=event['event_id'],
                                event_source=event['source_ref'])
        elif trace[-1]['call']['name'] == 'evidence_verification':
            evidence = trace[-1]['result']['data']
            assert evidence['physiological_features']['pulse_rate_median_bpm'] == 60
            assert evidence['motion_features']['sample_count'] == 500
            assert evidence['verification_status'] == 'insufficient_evidence'
            action = model_call(prompt, 'historical_comparison', metric_version='test.v1',
                                history_window={'start': '1969-12-31T20:00:00Z', 'end': '1969-12-31T21:00:00Z'})
        else:
            if trace[-1]['call']['name'] == 'historical_comparison':
                assert trace[-1]['result']['data']['reason'] == 'no_comparable_history'
                assert trace[-1]['call']['arguments']['metric_version'] == 'test.v1'
            action = model_finish(prompt)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(action),
        }}]})

    async def execute(datasets):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = ChatCompletionsClient(http, 'https://model.invalid/v1/chat/completions', 'test-qwen',
                                           token='fixture-token')
            agent = create_agent(QwenPlanner(client, '帮我分析昨晚为什么睡得不安稳'), datasets,
                                 activities=[registered_event(scope)])
            return await agent.run(scope)

    normal = asyncio.run(execute({'offline': tmp_path}))
    missing = asyncio.run(execute({}))
    assert normal.termination == missing.termination == 'finished'
    assert [entry.call.name for entry in normal.trace] == [
        'data_inspection', 'sleep_activity_analysis', 'evidence_verification', 'historical_comparison',
    ]
    assert [entry.call.name for entry in missing.trace] == ['data_inspection']
    assert normal.trace[-1].result.data['reason'] == 'no_comparable_history'
    assert normal.trace[-1].result.data['delta_events_per_hour'] is None
    report = normal.finish.report
    assert [item.result for item in report.findings] == [entry.result for entry in normal.trace]
    assert 'synthetic_camera_event' in report.uncertainty_reasons
    assert 'no_comparable_history' in report.uncertainty_reasons
    assert '60 bpm' in report.answer
    assert '缺少可比较的历史夜间指标' in report.answer
    assert str(normal.trace[2].call.call_id) in report.answer
    assert 'raw_chunks' not in json.dumps(prompts)

    from .qwen_live_acceptance import focused_checks

    assert all(focused_checks('adaptive_event', normal).values())
    assert all(focused_checks('missing_inspection', missing).values())
    assert not focused_checks('single_inspection', normal)['one_usable_inspection_only']
    assert not focused_checks('adaptive_event', missing)['cited_verification_of_observed_event']


@pytest.mark.parametrize('has_activity', [False, True])
def test_http_planner_rejects_call_id_as_activity_event_id(scope, has_activity):
    feedback_requests = []

    async def respond(request):
        payload = json.loads(request.content)
        prompt = json.loads(payload['messages'][1]['content'])
        trace = prompt['trace']
        if trace:
            prior = trace[0]
            schema = payload['response_format']['json_schema']['schema']
            names = [branch['properties']['call']['properties']['name']['const'] for branch in schema['anyOf'][1:]]
            feedback_requests.append((prior['result']['data']['status'], 'evidence_verification' in names))
            # The mock deliberately ignores the advertised schema, as an incompatible server could.
            action = model_call(prompt, 'evidence_verification', event_id=prompt['run_ref'])
        else:
            action = model_call(prompt, 'sleep_activity_analysis')
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(action)}}]})

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = ChatCompletionsClient(http, 'https://model.invalid/v1/chat/completions', 'test-qwen')
            activities = [registered_event(scope)] if has_activity else []
            return await create_agent(QwenPlanner(client, 'offline test'), activities=activities).run(scope)

    result = asyncio.run(execute())
    assert feedback_requests == [('ok' if has_activity else 'unavailable', has_activity)]
    assert result.termination == 'planner_error' and result.finish is None
    assert [entry.call.name for entry in result.trace] == ['sleep_activity_analysis']


@pytest.mark.parametrize(('scenario', 'termination', 'error'), [
    ('unknown_skill', 'finished', 'unknown_skill'),
    ('invalid_arguments', 'finished', 'invalid_arguments'),
    ('invalid_json', 'planner_error', None),
    ('forged_evidence', 'planner_error', None),
    ('free_form_claim', 'planner_error', None),
    ('duplicate_id', 'duplicate_call_id', 'duplicate_call_id'),
    ('call_limit', 'call_limit', 'duplicate_unavailable_call'),
])
def test_qwen_model_outputs_are_bounded_and_validated(scope, scenario, termination, error):
    def script(prompt):
        if scenario == 'invalid_json':
            return 'not-json'
        if scenario == 'forged_evidence':
            return {'action': 'finish', 'evidence_call_ids': [str(uuid4())]}
        if scenario == 'free_form_claim':
            return {'action': 'finish', 'evidence_call_ids': [], 'answer': 'unsupported 90 bpm'}
        if scenario == 'duplicate_id' and prompt['trace']:
            return {'action': 'call', 'call': prompt['trace'][0]['call']}
        if scenario == 'unknown_skill':
            return model_finish(prompt) if prompt['trace'] else model_call(prompt, 'shell')
        if scenario == 'invalid_arguments':
            return model_finish(prompt) if prompt['trace'] else model_call(prompt, 'data_inspection', path='C:/private')
        return model_call(prompt, 'data_inspection')

    client = TestModelClient(script)
    agent = SleepAnalysisAgent(SkillRegistry(local_skills()), QwenPlanner(client, 'offline test'), max_calls=2)
    result = asyncio.run(agent.run(scope))
    assert result.termination == termination
    assert (result.trace[-1].result.error if result.trace else None) == error
    if scenario in ('invalid_json', 'forged_evidence', 'free_form_claim'):
        assert result.trace == () and result.finish is None
    if scenario == 'call_limit':
        assert len(result.trace) == 2


def test_qwen_context_limit_and_model_timeout(scope):
    client = TestModelClient(model_finish)
    capped = create_agent(QwenPlanner(client, 'offline test', max_context_chars=10))
    assert asyncio.run(capped.run(scope)).termination == 'planner_error'
    assert client.prompts == []

    async def timeout(request):
        raise httpx.ReadTimeout('test transport timeout', request=request)

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
            client = ChatCompletionsClient(http, 'https://model.invalid/v1/chat/completions', 'test-qwen')
            return await create_agent(QwenPlanner(client, 'offline test')).run(scope)

    result = asyncio.run(execute())
    assert result.termination == 'planner_timeout'
    assert result.trace == ()


def test_http_schema_keeps_denied_id_out_of_evidence(scope):
    async def respond(request):
        payload = json.loads(request.content)
        prompt = json.loads(payload['messages'][1]['content'])
        trace = prompt['trace']
        if trace:
            assert trace[0]['result']['error'] == 'out_of_scope'
            finish = payload['response_format']['json_schema']['schema']['anyOf'][0]
            assert finish['properties']['evidence']['properties'] == {}
            assert trace[0]['result']['data']['scope_fields'] == ['dataset_id']
            action = {'action': 'finish', 'run_ref': prompt['run_ref'], 'evidence': {'E1': True}}
        else:
            action = model_call(prompt, 'data_inspection', dataset_id='outside-scope')
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(action)}}]})

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = ChatCompletionsClient(http, 'https://model.invalid/v1/chat/completions', 'test-qwen')
            return await create_agent(QwenPlanner(client, 'offline test')).run(scope)

    result = asyncio.run(execute())
    assert result.termination == 'planner_error' and result.finish is None
    assert result.trace[0].result.status == 'denied'
    assert result.trace[0].result.error == 'out_of_scope'


@pytest.mark.parametrize(('selection', 'termination'), [
    ('valid', 'finished'), ('missing', 'incomplete_evidence'), ('unknown', 'planner_error'),
    ('duplicate', 'planner_error'), ('foreign_run', 'planner_error'),
])
def test_http_short_refs_resolve_only_eligible_originals(scope, selection, termination):
    sent = []

    async def respond(request):
        payload = json.loads(request.content)
        prompt = json.loads(payload['messages'][1]['content'])
        sent.append(prompt)
        if not prompt['trace']:
            content = json.dumps(model_call(prompt, 'data_inspection'))
        else:
            evidence = payload['response_format']['json_schema']['schema']['anyOf'][0]['properties']['evidence']
            assert evidence['required'] == ['E1'] and prompt['required_evidence'] == {'E1': 'unknown_dataset'}
            action = model_finish(prompt)
            if selection == 'missing':
                action['evidence'] = {}
            if selection == 'unknown':
                action['evidence'] = {'E2': True}
            if selection == 'foreign_run':
                action['run_ref'] = str(uuid4())
            content = json.dumps(action)
            if selection == 'duplicate':
                content = content.replace('"E1": true', '"E1": true, "E1": true')
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = ChatCompletionsClient(http, 'https://model.invalid/v1/chat/completions', 'test-qwen')
            return await create_agent(QwenPlanner(client, 'offline test')).run(scope)

    result = asyncio.run(execute())
    assert len(sent) == 2 and sent[0]['run_ref'] == sent[1]['run_ref'] == str(result.run_id)
    assert result.termination == termination
    if selection == 'valid':
        assert result.finish.report.findings[0].result == result.trace[0].result
        assert result.finish.report.limitations[0].call_id == result.trace[0].call.call_id
    else:
        assert result.finish is None


def test_http_feedback_merges_only_identical_arguments_and_results(scope):
    cases = [call(scope, 'sleep_activity_analysis') for _ in range(3)]
    narrower = scope.model_dump(mode='json')['window']
    narrower['start'] = '1970-01-01T00:16:43Z'
    cases[2].arguments['window'] = narrower
    registry = SkillRegistry(local_skills(activities=[registered_event(scope)]))
    result = run(scope, lambda _, trace: cases[len(trace)], registry, max_calls=3)
    feedback, refs = _evidence_feedback(scope, result.trace)
    assert refs == {'E1': cases[0].call_id, 'E3': cases[2].call_id}
    assert feedback[0]['repeats'] == 1
    assert feedback[0]['call'] == {'name': 'sleep_activity_analysis'}
    assert feedback[1]['call']['arguments']['window'] == narrower
    assert len(result.trace) == 3 and result.trace[1].result.status == 'succeeded'
    assert [_calls_without_new_usable_data(result.trace[:count]) for count in range(4)] == [0, 0, 1, 0]


@pytest.mark.parametrize(('status', 'attempts', 'termination'), [
    (503, 2, 'finished'), (400, 1, 'planner_error'), (None, 1, 'planner_timeout'),
])
def test_http_model_retry_limit(scope, status, attempts, termination):
    requests = []

    async def respond(request):
        requests.append(request.content)
        if len(requests) == 1:
            if status is None:
                raise httpx.ReadTimeout('test transport timeout', request=request)
            return httpx.Response(status)
        return httpx.Response(200, json={'choices': [{'message': {
            'content': json.dumps(model_finish(json.loads(json.loads(request.content)['messages'][1]['content']))),
        }}]})

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = ChatCompletionsClient(http, 'https://model.invalid/v1/chat/completions', 'test-qwen')
            return await create_agent(QwenPlanner(client, 'offline test')).run(scope)

    assert asyncio.run(execute()).termination == termination
    assert len(requests) == attempts and all(body == requests[0] for body in requests)


def test_run_deadline_keeps_completed_trace(scope):
    class SlowSecondPlan:
        async def plan(self, request, skills, trace):
            if trace:
                await asyncio.sleep(5)
            return call(request, 'data_inspection')

    result = asyncio.run(create_agent(SlowSecondPlan(), planner_timeout=1, run_timeout=0.1).run(scope))
    assert result.termination == 'run_timeout'
    assert len(result.trace) == 1 and result.trace[0].result.status == 'succeeded'


@pytest.mark.parametrize('path', ['early_finish', 'reverse', 'wrong_source', 'wrong_event', 'uncited'])
def test_explicit_activity_goal_checks_evidence_from_actual_tools(scope, tmp_path, offline_ppg, offline_imu, path):
    activity = registered_event(scope)
    observed = ('sleep_activity_analysis', {})
    verified = ('evidence_verification', {'event_id': activity.event.event_id})
    wrong_source = activity.event.source_ref.model_dump(mode='json')
    wrong_source['record_id'] = 'another-record'
    actions = {
        'early_finish': [observed, None, verified, None],
        'reverse': [verified, observed, None],
        'wrong_source': [observed, ('evidence_verification', {**verified[1], 'event_source': wrong_source}),
                         None, verified, None],
        'wrong_event': [observed, ('evidence_verification', {'event_id': 'another-event'}),
                        None, verified, None],
        'uncited': [observed, verified, None, None],
    }[path]
    rounds = iter(enumerate(actions))

    def respond(prompt):
        round_index, action = next(rounds)
        if action:
            name, extra = action
            return model_call(prompt, name, **extra)
        finish = model_finish(prompt)
        if path == 'uncited' and round_index == 2:
            finish['evidence_call_ids'] = finish['evidence_call_ids'][:1]
        return finish

    client = TestModelClient(respond)
    agent = create_agent(QwenPlanner(client, 'test request'), {'offline': tmp_path}, activities=[activity])
    result = asyncio.run(agent.run_authorized(
        target_window=scope, authorized_scope=scope, completion_goal='verify_observed_activity',
    ))
    assert result.termination == 'finished'
    assert result.completion_pending is None
    expected_rejection = {
        'early_finish': 'wearable_evidence_for_observed_activity',
        'wrong_source': 'wearable_evidence_for_observed_activity',
        'wrong_event': 'wearable_evidence_for_observed_activity',
        'uncited': 'cite_completion_evidence',
    }.get(path)
    assert [item.reason for item in result.finish_rejections] == ([expected_rejection] if expected_rejection else [])
    assert client.prompts[-1]['completion']['pending'] is None
    if expected_rejection:
        rejected_round = result.finish_rejections[0].round
        assert client.prompts[rejected_round]['completion']['previous_finish_rejected'] == expected_rejection
    assert [entry.step for entry in result.trace] == list(range(1, len(result.trace) + 1))


@pytest.mark.parametrize('scenario', ['usable_signals', 'missing_signals', 'no_activity', 'missing_wearable'])
def test_explicit_completion_accepts_genuine_limits_and_single_tool(
    scope, tmp_path, offline_ppg, offline_imu, scenario,
):
    activity = registered_event(scope)
    task = 'inspect_signals' if scenario in ('usable_signals', 'missing_signals') else 'verify_observed_activity'
    steps = {
        'usable_signals': [('data_inspection', {})],
        'missing_signals': [('data_inspection', {})],
        'no_activity': [('sleep_activity_analysis', {})],
        'missing_wearable': [('sleep_activity_analysis', {}),
                             ('evidence_verification', {'event_id': activity.event.event_id})],
    }[scenario]

    def respond(prompt):
        count = len(prompt['trace'])
        if count == len(steps):
            return model_finish(prompt)
        name, extra = steps[count]
        return model_call(prompt, name, **extra)

    agent = create_agent(QwenPlanner(TestModelClient(respond), 'test request'),
                         {'offline': tmp_path} if scenario == 'usable_signals' else {},
                         activities=[activity] if scenario == 'missing_wearable' else ())
    result = asyncio.run(agent.run_authorized(target_window=scope, authorized_scope=scope, completion_goal=task))
    assert result.termination == 'finished' and len(result.trace) == len(steps)
    assert not result.finish_rejections
    if scenario != 'usable_signals':
        reason = {'missing_signals': 'unknown_dataset', 'no_activity': 'not_implemented',
                  'missing_wearable': 'insufficient_evidence'}[scenario]
        assert result.trace[-1].result.data['status'] == 'unavailable'
        assert reason in result.finish.report.uncertainty_reasons


@pytest.mark.parametrize('action', ['finish', 'call'])
def test_unmet_completion_exhausts_existing_budgets(scope, action):
    client = TestModelClient(lambda prompt: model_finish(prompt) if action == 'finish'
                             else model_call(prompt, 'data_inspection'))
    agent = SleepAnalysisAgent(SkillRegistry(local_skills()), QwenPlanner(client, 'test request'), max_calls=2)
    result = asyncio.run(agent.run_authorized(target_window=scope, authorized_scope=scope,
                                              completion_goal='verify_observed_activity'))
    assert result.termination == 'task_incomplete' and result.finish is None
    assert result.completion_pending == 'registered_activity_in_target'
    assert len(result.trace) == (0 if action == 'finish' else 2)
    assert len(client.prompts) == (3 if action == 'finish' else 2)
    assert len(result.finish_rejections) == (3 if action == 'finish' else 0)


def test_http_schema_disallows_finish_only_while_trusted_goal_is_pending(scope):
    sent = []

    async def respond(request):
        payload = json.loads(request.content)
        prompt = json.loads(payload['messages'][1]['content'])
        alternatives = payload['response_format']['json_schema']['schema']['anyOf']
        by_action = [branch['properties']['action']['const'] for branch in alternatives]
        sent.append((prompt, by_action, alternatives))
        action = model_call(prompt, 'data_inspection') if len(sent) == 1 else model_finish(prompt)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                                                     'message': {'content': json.dumps(action)}}]})

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = ChatCompletionsClient(http, 'https://model.invalid/v1/chat/completions', 'test-qwen')
            agent = create_agent(QwenPlanner(client, 'test request'))
            return await agent.run_authorized(target_window=scope, authorized_scope=scope,
                                               completion_goal='inspect_signals')

    result = asyncio.run(execute())
    assert result.termination == 'finished'
    assert len(sent) == 2 and 'finish' not in sent[0][1] and 'finish' in sent[1][1]
    assert sent[0][0]['completion']['pending'] == 'target_signal_availability'
    assert sent[1][0]['completion']['cite_one_group'] == [['E1']]
    assert sent[1][2][0]['properties']['evidence']['required'] == ['E1']
