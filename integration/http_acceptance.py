"""Real loopback HTTP integration; isolated SQLite and ephemeral credentials.
Run with the integration venv from repository root. Only owns its subprocesses.
"""
import asyncio, hashlib, importlib.util, json, os, secrets, socket, sqlite3
import subprocess, sys, time
from pathlib import Path
from datetime import datetime, timedelta, timezone
import httpx
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
DATA=ROOT/'.venv'/('integration-'+secrets.token_hex(4))
DATA.mkdir(parents=True,exist_ok=True)
TOKEN=secrets.token_urlsafe(32)
OP=secrets.token_urlsafe(32)
ENV=os.environ.copy()
ENV.update(PYTHONPATH=str(ROOT)+os.pathsep+str(ROOT/'cloud-platform/backend'),
 DATABASE_URL='sqlite:///'+(DATA/'cloud.sqlite').as_posix(), CURSOR_SECRET=secrets.token_hex(32),
 CLOUD_IDENTITIES=json.dumps([{'subject':'edge-mock-001','role':'device','devices':['edge-mock-001'],'token_sha256':hashlib.sha256(TOKEN.encode()).hexdigest()},
 {'subject':'integration-operator','role':'operator','devices':['edge-mock-001'],'token_sha256':hashlib.sha256(OP.encode()).hexdigest()}]),
 EDGE_CLOUD_URL='http://127.0.0.1:8002',EDGE_CLOUD_TOKEN=TOKEN,EDGE_OPERATOR_TOKEN=OP,
 EDGE_DB_PATH=str(DATA/'edge.sqlite'),EDGE_MOCK='1',PYTHONDONTWRITEBYTECODE='1')
children=[]
def start(args,name):
 f=open(DATA/(name+'.log'),'a',encoding='utf-8')
 p=subprocess.Popen(args,cwd=ROOT,env=ENV,stdout=f,stderr=f,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));f.close();children.append(p);return p
def wait(fn,timeout=45):
 end=time.time()+timeout
 while time.time()<end:
  try:
   value=fn()
   if value:return value
  except (httpx.HTTPError,KeyError,IndexError):pass
  time.sleep(.5)
 raise AssertionError('condition timed out')
def cloud():
 return start([sys.executable,'-m','uvicorn','app.main:create_app','--factory','--host','127.0.0.1','--port','8002','--no-access-log'],'cloud')
def get(port,path):
 r=httpx.get(f'http://127.0.0.1:{port}'+path,headers={'Authorization':'Bearer '+OP},timeout=3);r.raise_for_status();return r.json()
def tasks():return get(8001,'/api/v1/analysis/tasks/local')['items']
def stop(p):
 p.terminate();p.wait(timeout=15)
def main():
 for port in [8001,8002,5173,5174]:
  with socket.socket() as sock:
   assert sock.connect_ex(('127.0.0.1',port))!=0,f'Port occupied: {port}; inspect owner before running'
 subprocess.run([sys.executable,'-m','alembic','-c','cloud-platform/backend/alembic.ini','upgrade','head'],env=ENV,cwd=ROOT,check=True)
 c=cloud();wait(lambda:get(8002,'/health'))
 start([sys.executable,'-m','uvicorn','edge-platform.backend.app:app','--host','127.0.0.1','--port','8001','--no-access-log'],'edge')
 wait(lambda:get(8001,'/health'))
 completed=wait(lambda:next((t for t in tasks() if t['status']=='succeeded'),None))
 task=get(8002,'/api/v1/analysis/tasks/'+completed['cloud_task_id'])
 result=get(8002,'/api/v1/events/'+task['result_event_id'])
 anomaly=get(8002,'/api/v1/events/'+completed['task_key'])
 assert anomaly['event_type']=='anomaly_event'
 local=get(8001,'/api/v1/events/recent?event_type=cloud_analysis_result')['items']
 assert any(e['event_id']==result['event_id'] for e in local)
 assert anomaly['event_id'] in result['payload']['related_event_ids']
 decisions=get(8002,'/api/v1/events?event_type=agent_decision')['items']
 assert any(anomaly['event_id'] in e['payload']['related_event_ids'] for e in decisions)
 headers={'Authorization':'Bearer '+TOKEN}
 r=httpx.post('http://127.0.0.1:8002/api/v1/events/batch',headers=headers,json={'events':[anomaly]});assert r.json()['results'][0]['status']=='duplicate'
 req=json.loads(completed['request_json'])
 for _ in range(2):
  r=httpx.post('http://127.0.0.1:8002/api/v1/analysis/tasks',headers={**headers,'Idempotency-Key':completed['task_key']},json=req)
  assert r.json()['task_id']==task['task_id']
 with sqlite3.connect(DATA/'edge.sqlite') as db:
  assert db.execute('SELECT count(*) FROM outbox WHERE event_id=?',(result['event_id'],)).fetchone()[0]==0
 print('PASS anomaly -> decision -> upload -> task -> Mock worker -> result -> local persistence; duplicate event/task; no feedback loop',flush=True)
 # A stalled real TCP server exercises actual HTTP read timeout without a MockClient.
 spec=importlib.util.spec_from_file_location('edge_integration',ROOT/'edge-platform/backend/app.py');edge=importlib.util.module_from_spec(spec);spec.loader.exec_module(edge)
 async def timeout_case():
  async def stall(reader,writer):
   try:await asyncio.sleep(1)
   finally:writer.close()
  server=await asyncio.start_server(stall,'127.0.0.1',0)
  async with server:
   port=server.sockets[0].getsockname()[1]
   try:await edge.HttpCloudClient(f'http://127.0.0.1:{port}',TOKEN,.05).upload([anomaly])
   except httpx.TimeoutException:return
   raise AssertionError('timeout expected')
 asyncio.run(timeout_case());print('PASS actual HTTP timeout',flush=True)
 stop(c)
 before=get(8001,'/api/v1/device/status')['pending_upload_count'];time.sleep(5)
 assert get(8001,'/api/v1/device/status')['pending_upload_count']>before
 # A previously running task must become failed on worker restart, then propagate to Edge.
 with sqlite3.connect(DATA/'cloud.sqlite') as db:
  db.execute("UPDATE analysis_tasks SET status='running' WHERE task_id=?",(task['task_id'],))
 with sqlite3.connect(DATA/'edge.sqlite') as db:
  db.execute("UPDATE tasks SET status='running' WHERE cloud_task_id=?",(task['task_id'],))
 c=cloud();wait(lambda:get(8002,'/health'))
 wait(lambda:any(t['cloud_task_id']==task['task_id'] and t['status']=='failed' for t in tasks()))
 wait(lambda:get(8001,'/api/v1/device/status')['pending_upload_count']==0,90)
 assert get(8002,'/api/v1/events/'+anomaly['event_id'])['event_id']==anomaly['event_id']
 print('PASS offline cache, cloud restart persistence, resumed upload, interrupted task failure propagation',flush=True)
 node=Path('C:/Users/txy/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe')
 for platform,port in [('edge',5173),('cloud',5174)]:
  folder=ROOT/f'{platform}-platform/frontend'
  start([str(node),str(folder/'node_modules/vite/bin/vite.js'),str(folder),'--host','127.0.0.1','--port',str(port),'--strictPort'],platform+'-ui')
  wait(lambda:httpx.get(f'http://127.0.0.1:{port}',timeout=3).status_code==200)
 # Verify Vite's actual proxy routes, not merely its HTML response.
 with httpx.Client(base_url='http://127.0.0.1:5173') as browser:
  assert browser.post('/api/v1/session',headers={'Authorization':'Bearer '+OP}).is_success
  assert browser.get('/api/v1/analysis/tasks/local').json()['items']
  assert browser.get('/api/v1/events/recent?event_type=cloud_analysis_result').json()['items']
 assert get(5174,'/api/v1/events?event_type=cloud_analysis_result')['items']
 assert get(5174,'/api/v1/analysis/tasks')['items']
 stats=get(5174,'/api/v1/dashboard');assert stats['device_count']==1 and stats['event_count']>0
 (DATA/'session.json').write_text(json.dumps({'operator_token':OP,'process_ids':[p.pid for p in children],'stats':stats}),encoding='utf-8')
 from websockets.sync.client import connect
 with connect('ws://127.0.0.1:5173/ws/realtime', additional_headers={'Authorization':'Bearer '+OP}, origin='http://127.0.0.1:5173') as ws:
  packet=json.loads(ws.recv(timeout=20))
  assert packet['kind']=='event' and packet['event']['device_id']=='edge-mock-001'
 print('PASS real authenticated WebSocket through Vite proxy',flush=True)
 print('PASS frontend proxy authenticated data and device/event stats. Browser rendering remains manual.',flush=True)
 (ROOT/'.venv/latest-integration.txt').write_text(str(DATA),encoding='utf-8')
 print('Services left running for manual acceptance; private token and owned PIDs in .venv/integration/session.json',flush=True)
if __name__=='__main__':
 try:main()
 except BaseException:
  for p in children:
   if p.poll() is None:stop(p)
  raise
