import asyncio
import hashlib
import json
from uuid import uuid4

import httpx
import pytest

from shared.interfaces import ToolCall

from .sleep_analysis.agent import Finish, create_agent
from .sleep_analysis.qwen import ChatCompletionsClient, QwenPlanner
from .sleep_analysis.video_pose import VIDEO_POSE, VideoAccessDenied, VideoArchive, VideoExport, VideoPoseRequest, VideoRange
from .test_sleep_analysis import scope


@pytest.fixture
def pose_archive(tmp_path):
    video_hash, model_hash = 'a' * 64, 'b' * 64
    detections = [{'score': 0.9, 'frame_keypoints_xy_score': [[105, 1, 0.9]] + [[1, 2, 0.7]] * 16}]
    frames = b''.join(json.dumps({
        'frame_index': index, 'source_pts': 12345 + index, 'relative_pts': index, 'time_base': '1/25',
        'video_sha256': video_hash, 'model_sha256': model_hash, 'decoded_size_wh': [100, 100],
        'detections': detections if index != 1 else [],
    }).encode() + b'\n' for index in range(3))
    manifest = json.dumps({
        'schema': 'cix.pose.frames.v1', 'backend': 'CIX NOE NPU', 'source_modality': 'unverified',
        'capture_start_utc': None, 'frames': 3, 'source_pts_origin': 12345, 'source_stream': {'time_base': '1/25'},
        'model': {'family': 'YOLOv8s-Pose', 'sha256': model_hash}, 'input_video': {'sha256': video_hash},
        'outputs': {'jsonl': {'sha256': hashlib.sha256(frames).hexdigest(), 'path': '/foreign/ignored.jsonl'}},
    }).encode()
    folder = tmp_path / 'archive' / 'clip'
    folder.mkdir(parents=True)
    (folder / 'frames.jsonl').write_bytes(frames)
    (folder / 'frames.manifest.json').write_bytes(manifest)
    entry = VideoExport(directory='clip', range_ms=VideoRange(start_ms=0, end_ms=81),
                        manifest_sha256=hashlib.sha256(manifest).hexdigest())
    return VideoArchive(folder.parent, {'known': entry})


@pytest.mark.parametrize(('range_ms', 'indices', 'detections'), [
    (None, (0, 2), 2), ((40, 81), (1, 2), 1), ((40, 80), (1, 1), 0),
])
def test_pose_ranges_and_relative_pts(pose_archive, range_ms, indices, detections):
    grant = pose_archive.exports['known'].range_ms
    window = VideoRange(start_ms=range_ms[0], end_ms=range_ms[1]) if range_ms else None
    original = {path: path.read_bytes() for path in pose_archive.root.rglob('*') if path.is_file()}
    result = pose_archive.read(VideoPoseRequest(video_id='known', range_ms=window), {'known': grant})
    assert result.status == 'ok' and result.effective_range_ms == (window or grant)
    assert result.summary.frames_examined == indices[1] - indices[0] + 1
    assert result.summary.person_detections == detections
    assert result.summary.keypoints == result.summary.keypoints_score_gt_0_5 == 17 * detections
    assert result.summary.high_score_keypoints_outside_frame == detections
    assert ('no_pose_detections' in result.quality_flags) == (detections == 0)
    evidence = result.evidence
    assert evidence.time_basis == 'relative_source_pts' and evidence.time_base == '1/25'
    assert evidence.source_pts_origin == 12345
    assert (evidence.first_frame, evidence.last_frame) == (evidence.first_relative_pts, evidence.last_relative_pts) == indices
    assert evidence.frames_sha256 == hashlib.sha256(original[pose_archive.root/'clip'/'frames.jsonl']).hexdigest()
    assert all(path.read_bytes() == contents for path, contents in original.items())


@pytest.mark.parametrize(('file', 'change', 'reason'), [
    ('frames.jsonl', 'altered', 'export_hash_mismatch'),
    ('frames.manifest.json', 'altered', 'export_hash_mismatch'),
    ('frames.jsonl', 'missing', 'export_missing'),
])
def test_pose_rejects_unverified_or_missing_export(pose_archive, file, change, reason):
    path = pose_archive.root / 'clip' / file
    if change == 'missing':
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b' ')
    result = pose_archive.read(VideoPoseRequest(video_id='known'), {'known': pose_archive.exports['known'].range_ms})
    assert result.status == 'unavailable' and result.reason == reason
    assert result.evidence is None and result.summary is None


@pytest.mark.parametrize(('arguments', 'granted', 'reason'), [
    ({'video_id': '../clip'}, True, 'invalid_arguments'),
    ({'video_id': 'missing'}, True, 'video_not_found'),
    ({'video_id': 'known'}, False, 'video_not_authorized'),
    ({'video_id': 'known', 'range_ms': {'start_ms': 0, 'end_ms': 82}}, True, 'video_range_out_of_scope'),
])
def test_pose_executor_denies_invalid_or_unauthorized_calls(scope, pose_archive, arguments, granted, reason):
    class Planner:
        async def plan(self, request, skills, trace):
            if trace:
                return Finish(reason='done')
            assert [skill['name'] for skill in skills][-1] == VIDEO_POSE
            return ToolCall(call_id=uuid4(), name=VIDEO_POSE, arguments=arguments)

    grants = {'known': pose_archive.exports['known'].range_ms} if granted else {}
    result = asyncio.run(create_agent(Planner(), video_archive=pose_archive).run_authorized(
        target_window=scope, authorized_scope=scope, authorized_videos=grants,
    ))
    assert result.trace[0].result.status == 'denied' and result.trace[0].result.error == reason
    assert not result.trace[0].result.data


def test_pose_rejects_path_outside_archive(pose_archive):
    entry = pose_archive.exports['known'].model_copy(update={'directory': '../elsewhere'})
    archive = VideoArchive(pose_archive.root, {'known': entry})
    with pytest.raises(VideoAccessDenied, match='video_path_out_of_scope'):
        archive.read(VideoPoseRequest(video_id='known'), {'known': entry.range_ms})


def test_video_grant_is_independent_of_archive_extent(pose_archive):
    grant = VideoRange(start_ms=1, end_ms=40)
    result = pose_archive.read(VideoPoseRequest(video_id='known'), {'known': grant})
    assert result.status == 'unavailable' and result.reason == 'no_frames_in_range'
    assert result.effective_range_ms == grant
    assert result.evidence.first_frame is None and result.evidence.last_frame is None
    outside = VideoPoseRequest(video_id='known', range_ms=VideoRange(start_ms=0, end_ms=40))
    with pytest.raises(VideoAccessDenied, match='video_range_out_of_scope'):
        pose_archive.read(outside, {'known': grant})


def test_pose_route_and_evidence_reuse_strict_qwen_protocol(scope, pose_archive):
    prompts = []

    async def respond(request):
        payload = json.loads(request.content)
        prompt = json.loads(payload['messages'][1]['content'])
        prompts.append(prompt)
        if len(prompts) == 1:
            schema = payload['response_format']['json_schema']['schema']
            call_properties = [branch['properties']['call']['properties'] for branch in schema['anyOf']
                               if branch['properties']['action']['const'] == 'call']
            args = next(spec['arguments'] for spec in call_properties if spec['name']['const'] == VIDEO_POSE)
            assert args['additionalProperties'] is False
            assert set(args['properties']) == {'video_id', 'range_ms'}
            action = {'action': 'call', 'call': {'call_id': prompt['next_call_id'], 'name': VIDEO_POSE,
                                                'arguments': {'video_id': 'known'}}}
        else:
            outcome = prompt['trace'][0]['result']
            assert outcome['data']['summary']['frames_examined'] == 3
            assert 'frames_sha256' not in json.dumps(prompt)
            action = {'action': 'finish', 'run_ref': prompt['run_ref'], 'evidence': {outcome['ref']: True}}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                                                     'message': {'content': json.dumps(action)}}]})

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = ChatCompletionsClient(http, 'https://test.invalid/v1/chat/completions', 'test-qwen')
            agent = create_agent(QwenPlanner(client, 'pose review'), video_archive=pose_archive)
            assert len(agent.registry.discover()) == 4
            return await agent.run_authorized(
                target_window=scope, authorized_scope=scope, authorized_videos={'known': pose_archive.exports['known'].range_ms},
            )

    result = asyncio.run(execute())
    assert result.termination == 'finished'
    assert result.finish.report.analysis_scope is None
    finding = result.finish.report.findings[0]
    assert finding.skill == VIDEO_POSE and finding.result == result.trace[0].result
    assert finding.result.data['evidence']['manifest_sha256'] == pose_archive.exports['known'].manifest_sha256
    assert 'source_modality_unverified' in result.finish.report.uncertainty_reasons
