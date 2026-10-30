"""Upload deterministic synthetic records through the authenticated public API."""
import json
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
import httpx
from dotenv import load_dotenv

root = Path(__file__).resolve().parents[2]
load_dotenv(root / '.env')
token = os.environ['MOCK_DEVICE_TOKEN']
url = os.getenv('CLOUD_API_URL', 'http://127.0.0.1:8002')
examples = json.loads((root.parent / 'shared/examples/events.json').read_text(encoding='utf-8'))
start = datetime.now(timezone.utc) - timedelta(hours=24)
with httpx.Client(base_url=url, headers={'Authorization': f'Bearer {token}'}, timeout=30) as client:
    for hour in range(24):
        batch = []
        for template in examples:
            if template['event_type'] == 'cloud_analysis_result':
                continue
            event = deepcopy(template)
            event.update(event_id=str(uuid4()), timestamp=(start + timedelta(hours=hour)).isoformat(), source='mock',
                         model_name='mock-fixture', model_version='1')
            if event['event_type'] == 'physiological_window':
                event['payload'].update(window_start=event['timestamp'], window_end=event['timestamp'], connection='connected')
                event['payload']['channels'] = [{'name': 'mock-channel', 'unit': 'synthetic', 'sampling_rate_hz': None,
                    'samples': [float(hour), float(hour + 1), None], 'quality': 'unknown'}]
            if event['event_type'] == 'sleep_posture':
                event['payload'] = {'label': ['mock-left', 'mock-right', 'mock-supine'][hour % 3], 'status': 'available'}
            batch.append(event)
        response = client.post('/api/v1/events/batch', json={'events': batch})
        response.raise_for_status()
        result = response.json()
        if any(r['status'] not in ('accepted', 'duplicate') for r in result['results']):
            raise SystemExit(result)
print('Imported 168 clearly labelled MOCK events (24 hours × 7 types); no medical interpretation.')
