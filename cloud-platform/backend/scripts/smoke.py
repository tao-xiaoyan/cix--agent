"""Live HTTP smoke test using an isolated SQLite file; not a PostgreSQL certification."""
import hashlib, json, os, secrets, subprocess, sys, tempfile, time
from pathlib import Path
import httpx

platform = Path(__file__).resolve().parents[2]
worktree = platform.parent
scratch = platform / '.test-data'
scratch.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(dir=scratch) as tmp:
    token = secrets.token_urlsafe(32)
    env = {**os.environ, 'PYTHONPATH': str(worktree) + os.pathsep + str(platform/'backend'),
        'PYTHONDONTWRITEBYTECODE': '1', 'DATABASE_URL': 'sqlite:///' + (Path(tmp)/'smoke.sqlite').as_posix(),
        'CURSOR_SECRET': secrets.token_urlsafe(40), 'MOCK_DEVICE_TOKEN': token, 'MOCK_WORKER_ENABLED': 'true',
        'CLOUD_API_URL': 'http://127.0.0.1:18002',
        'CLOUD_IDENTITIES': json.dumps([{'subject':'mock-device-001','role':'device','devices':['mock-device-001'],
                                       'token_sha256':hashlib.sha256(token.encode()).hexdigest()}])}
    subprocess.run([sys.executable,'-m','alembic','upgrade','head'],cwd=platform/'backend',env=env,check=True)
    process = subprocess.Popen([sys.executable,'-m','uvicorn','app.main:create_app','--factory','--host','127.0.0.1',
        '--port','18002','--no-access-log'],cwd=platform/'backend',env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        with httpx.Client(base_url=env['CLOUD_API_URL'],headers={'Authorization':'Bearer '+token}) as client:
            for _ in range(60):
                try:
                    if client.get('/health').status_code == 200: break
                except httpx.ConnectError: pass
                time.sleep(.2)
            else: raise RuntimeError('API did not start')
            subprocess.run([sys.executable,str(platform/'backend/scripts/import_mock.py')],env=env,check=True)
            assert len(client.get('/api/v1/events?limit=200').json()['items']) == 168
            task = client.post('/api/v1/analysis/tasks',headers={'Idempotency-Key':'live-smoke'},json={
                'device_id':'mock-device-001','start':'2020-01-01T00:00:00Z','end':'2030-01-01T00:00:00Z'}).json()
            for _ in range(60):
                result=client.get('/api/v1/analysis/tasks/'+task['task_id']).json()
                if result['status'] in ('succeeded','failed'): break
                time.sleep(.2)
            assert result['status']=='succeeded',result
            event=client.get('/api/v1/events/'+result['result_event_id']).json()
            assert event['source']=='mock' and 'MOCK' in event['payload']['summary']
            print('PASS: live API, Alembic, 168 imported events, persisted query, polling worker and result retrieval')
    finally:
        if os.name == 'nt':
            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,check=False)
        else:
            process.terminate()
        process.wait(timeout=10)
