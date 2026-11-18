"""Run the archived real-data Skill cases against the CIX Qwen SSH forward."""

import argparse
import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import httpx

from .sleep_analysis.agent import create_agent
from .sleep_analysis.contracts import InspectionRequest
from .sleep_analysis.qwen import ChatCompletionsClient, QwenPlanner, parse_http_action


BASE = 'http://127.0.0.1:18080'
TASK = (
    'Assess the availability and engineering quality of PPG and GSR/IMU in the exact authorized '
    '10-second offline window. Ground the report in observed signal quality and cite the original '
    'inspection evidence, including partial or unavailable signals. No activity records or historical '
    'reference are supplied for this analysis. Finish when enough evidence is available.'
)


def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def hashes(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_file():
            with path.open('rb') as stream:
                result[path.relative_to(root).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return result


class ExactWindowClient(ChatCompletionsClient):
    """Bind window precision and require the observed inspection in the report."""

    async def complete(self, *, system, prompt, response_schema=None):
        plan = json.loads(prompt)
        inspection_ref = next((entry['result']['ref'] for entry in plan['trace']
                               if entry['call']['name'] == 'data_inspection' and 'ref' in entry['result']), None)
        for branch in response_schema['anyOf']:
            call = branch['properties'].get('call', {}).get('properties', {})
            if call.get('name', {}).get('const') in ('data_inspection', 'sleep_activity_analysis'):
                call['arguments'] = {'const': plan['scope']}
            evidence = branch['properties'].get('evidence')
            if evidence is not None and inspection_ref is not None and inspection_ref not in evidence['required']:
                evidence['required'].append(inspection_ref)
        return await super().complete(system=system, prompt=prompt, response_schema=response_schema)


async def verify(archive, record):
    manifest = load(archive / 'inputs.json')
    source = Path(manifest['source']).resolve(strict=True)
    sqlite_root = (archive / 'sqlite').resolve(strict=True)
    expected = {'source': manifest['sha256'], 'sqlite': load(archive / 'results.json')['sqlite_sha256']}

    def snapshot():
        return {'source': hashes(source), 'sqlite': hashes(sqlite_root)}

    before = snapshot()
    record('hashes_before', paths={'source': str(source), 'sqlite': str(sqlite_root)}, sha256=before,
           historical_match=before == expected)
    if before != expected or set(before['sqlite']) != {'ppg.sqlite3', 'gsr.sqlite3'}:
        raise ValueError('archive files differ from the historical manifests')

    passed = True
    try:
        verified = load(source / 'verified.json')
        cases = {row['result']['data']['status']: row for row in load(archive / 'skill_results.json')}
        targets = {status: InspectionRequest.model_validate(cases[status]['call']['arguments'])
                   for status in ('ok', 'partial')}
        for status, target in targets.items():
            start = datetime.fromtimestamp(verified['start_utc'] + cases[status]['offset_s'], timezone.utc)
            if (target.session_id != verified['session'] or target.window.start != start
                    or target.window.end.timestamp() > verified['end_utc']):
                raise ValueError('historical Skill scope does not match the verified export')
        for file in before['sqlite']:
            uri = (sqlite_root / file).as_uri() + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True)) as db:
                db.execute('PRAGMA query_only=ON')
                if db.execute('SELECT id FROM sessions').fetchall() != [(verified['session'],)]:
                    raise ValueError('SQLite session differs from the verified export')

        case = None
        requests = {}

        async def on_request(request):
            if request.method == 'POST':
                payload = json.loads(request.content)
                requests[request] = perf_counter()
                record('request', case=case, payload=payload)

        async def on_response(response):
            if response.request.method != 'POST':
                return
            await response.aread()
            elapsed = perf_counter() - requests.pop(response.request)
            record('response', case=case, seconds=elapsed, status=response.status_code, raw=response.text)
            if response.is_success:
                body = response.json()
                choice = body['choices'][0]
                record('usage', case=case, usage=body.get('usage'), finish_reason=choice.get('finish_reason'))
                try:
                    action = parse_http_action(choice['message']['content'])
                except (ValueError, TypeError, KeyError):
                    return
                record('model_action', case=case, action=action.model_dump(mode='json'))

        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5), trust_env=False,
                                     event_hooks={'request': [on_request], 'response': [on_response]}) as http:
            for path in ('/health', '/v1/models', '/props'):
                response = await http.get(BASE + path, timeout=5)
                record('preflight', path=path, status=response.status_code, raw=response.text)
                response.raise_for_status()
                if path == '/v1/models':
                    model = response.json()['data'][0]['id']
                    if Path(model).name != 'Qwen3-4B-Q4_K_M.gguf':
                        raise ValueError('unexpected model at the SSH forward')
            for case, target in targets.items():
                record('scope', case=case, target=target.model_dump(mode='json'), history_authorized=False)
                client = ExactWindowClient(http, BASE + '/v1/chat/completions', model, max_tokens=384)
                agent = create_agent(QwenPlanner(client, TASK), {target.dataset_id: sqlite_root},
                                     planner_timeout=120, run_timeout=480)
                start = perf_counter()
                result = await agent.run_authorized(target_window=target, authorized_scope=target)
                report = result.finish.report if result.finish else None
                actual = {entry.call.call_id: entry for entry in result.trace if entry.result.status == 'succeeded'}
                expected_data = cases[case]['result']['data']
                cites_inspection = report and any(
                    finding.skill == 'data_inspection' and finding.result.data == expected_data
                    for finding in report.findings
                )
                case_passed = bool(result.termination == 'finished' and cites_inspection
                                   and report.analysis_scope is not None
                                   and report.analysis_scope.model_dump() == target.model_dump()
                                   and all(f.result.call_id in actual and actual[f.result.call_id].result == f.result
                                           for f in report.findings))
                record('run_result', case=case, seconds=perf_counter() - start, passed=case_passed,
                       result=result.model_dump(mode='json'))
                passed &= case_passed
                if requests:
                    record('no_complete_response', case=case, count=len(requests))
                    break
    finally:
        after = snapshot()
        record('hashes_after', sha256=after, unchanged=after == before)
    return passed and after == before


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path, help='Trusted directory of the prior real-data acceptance')
    parser.add_argument('output', type=Path, help='New local JSONL path')
    options = parser.parse_args()
    archive = options.archive.resolve(strict=True)
    source = Path(load(archive / 'inputs.json')['source']).resolve(strict=True)
    output = options.output.resolve()
    if output.is_relative_to(archive) or output.is_relative_to(source):
        parser.error('output must be outside the verified inputs')
    with output.open('x', encoding='utf-8') as stream:
        def record(kind, **data):
            print(json.dumps({'kind': kind, 'utc': datetime.now(timezone.utc).isoformat(), **data},
                             ensure_ascii=True), file=stream, flush=True)

        try:
            return 0 if asyncio.run(verify(archive, record)) else 1
        except (ValueError, OSError, sqlite3.Error, httpx.HTTPError, KeyError, IndexError) as exc:
            record('blocked', error=repr(exc))
            print(f'Demo blocked. See {output}')
            return 2


if __name__ == '__main__':
    raise SystemExit(main())
