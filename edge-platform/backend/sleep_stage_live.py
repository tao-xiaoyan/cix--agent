"""One board-local Qwen -> authorized AAUWSS epoch -> NOE NPU acceptance."""

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

import httpx

from .sleep_analysis.agent import create_agent
from .sleep_analysis.contracts import WindowRequest
from .sleep_analysis.qwen import ChatCompletionsClient, QwenPlanner, parse_http_action
from .sleep_analysis.sleep_stage import AuthorizedEpoch, SleepStageRequest, SleepStageRuntime, STAGE_SKILL


async def run(options, record):
    root = options.assets
    metadata = json.loads((root / 'epoch_source.json').read_text())
    asset = AuthorizedEpoch(
        request=SleepStageRequest(dataset_id='aauwss', subject_id=12, epoch_id=1),
        input_path=root/'subject12_epoch1_input.npy', input_sha256=metadata['input_npy_sha256'],
        source_pickle_sha256=metadata['source_pickle_sha256'], source_row=metadata['row'],
        human_annotation=metadata['true_label'],
    )
    pending = {}

    async def request_sent(request):
        if request.method == 'POST':
            payload = json.loads(request.content)
            round_id = json.loads(payload['messages'][1]['content'])['next_call_id']
            request.extensions['round_id'] = round_id
            pending[round_id] = perf_counter()
            record('request', round_id=round_id, payload=payload)

    async def response_received(response):
        if response.request.method == 'POST':
            await response.aread()
            round_id = response.request.extensions['round_id']
            record('response', round_id=round_id, seconds=perf_counter()-pending.pop(round_id),
                   status=response.status_code, raw=response.text)
            try:
                choice = response.json()['choices'][0]
                action = parse_http_action(choice['message']['content'])
                record('parsed_action', round_id=round_id, action=action.model_dump(mode='json'),
                       finish_reason=choice['finish_reason'])
            except (ValueError, KeyError, TypeError, IndexError):
                pass

    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5), trust_env=False,
                                 event_hooks={'request': [request_sent], 'response': [response_received]}) as http:
        model = None
        for path in ('/health', '/v1/models', '/props'):
            response = await http.get('http://127.0.0.1:18080'+path, timeout=5)
            record('preflight', path=path, status=response.status_code, raw=response.text)
            response.raise_for_status()
            if path == '/v1/models':
                model = response.json()['data'][0]['id']
        client = ChatCompletionsClient(http, 'http://127.0.0.1:18080/v1/chat/completions', model, max_tokens=384)
        task = ('Determine the model-predicted sleep stage for dataset_id aauwss, subject_id 12, epoch_id 1 using '
                'sleep_stage_inference. Cite its actual result and retain model quality limitations. '
                'The generic scope is an orchestration anchor, not the acquisition time of this epoch.')
        agent = create_agent(QwenPlanner(client, task), stage_runtime=SleepStageRuntime(root/'sleep_cnn.cix'),
                             planner_timeout=120, run_timeout=480)
        # Existing WindowRequest anchors the legacy tools. Epoch authorization is separate.
        now = datetime.now(timezone.utc)
        scope = WindowRequest(dataset_id='aauwss', session_id='subject_12',
                              window={'start': now, 'end': now+timedelta(seconds=30)})
        start = perf_counter()
        result = await agent.run_authorized(target_window=scope, authorized_scope=scope,
                                            completion_goal='infer_sleep_stage', authorized_epochs=(asset,))
        record('run_result', seconds=perf_counter()-start, result=result.model_dump(mode='json'))
        findings = result.finish.report.findings if result.finish and result.finish.report else []
        actual = [e for e in result.trace if e.call.name == STAGE_SKILL and e.result.status == 'succeeded'
                  and e.result.data.get('status') == 'ok']
        checks = {
            'finished': result.termination == 'finished',
            'actual_npu_cited': any(f.result == e.result and f.skill == STAGE_SKILL
                                   and e.result.data['backend'] == 'CIX NOE NPU'
                                   and e.result.data['source']['input_sha256'] == asset.input_sha256
                                   for f in findings for e in actual),
            'model_limitations_in_report': bool(result.finish and result.finish.report and
                                               'experimental_low_accuracy_model' in result.finish.report.uncertainty_reasons),
        }
        record('verdict', passed=all(checks.values()), checks=checks)
        return 0 if all(checks.values()) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    options = parser.parse_args()
    with options.output.open('x', encoding='utf8') as output:
        def record(kind, **data):
            output.write(json.dumps({'kind': kind, 'utc': datetime.now(timezone.utc).isoformat(), **data})+'\n')
            output.flush()

        record('metadata', source='real AAUWSS aligned PPG epoch; board Qwen and NOE NPU',
               source_sha256={str(p.relative_to(Path(__file__).parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in [Path(__file__), *Path(__file__).with_name('sleep_analysis').glob('*.py')]})
        return asyncio.run(run(options, record))


if __name__ == '__main__':
    raise SystemExit(main())
