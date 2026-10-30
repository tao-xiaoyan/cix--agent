"""Create local random credentials. Never commit the generated .env."""
import hashlib
import json
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[2]
target = root / '.env'
if target.exists():
    raise SystemExit('.env already exists; refusing to overwrite')
device, operator = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
identities = [{'subject': 'mock-device-001', 'role': 'device', 'devices': ['mock-device-001'],
               'token_sha256': hashlib.sha256(device.encode()).hexdigest()},
              {'subject': 'local-operator', 'role': 'operator', 'devices': ['mock-device-001'],
               'token_sha256': hashlib.sha256(operator.encode()).hexdigest()}]
password = secrets.token_urlsafe(24)
target.write_text(f'POSTGRES_USER=health\nPOSTGRES_DB=health_monitor\nPOSTGRES_PASSWORD={password}\n'
    f'DATABASE_URL=postgresql+psycopg://health:{password}@127.0.0.1:5433/health_monitor\n'
    f'CURSOR_SECRET={secrets.token_urlsafe(48)}\nCLOUD_IDENTITIES=\'{json.dumps(identities)}\'\n'
    f'MOCK_DEVICE_TOKEN={device}\nMOCK_WORKER_ENABLED=true\n', encoding='utf-8')
(root / '.operator-token.local').write_text(operator, encoding='utf-8')
print('Created cloud-platform/.env and .operator-token.local. Paste the operator token into the UI; do not share it.')
