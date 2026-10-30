import json
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / '.env', override=False)

@dataclass
class Settings:
    database_url: str
    cursor_secret: str
    identities: list[dict]
    worker_enabled: bool = True
    origins: tuple[str, ...] = ('http://localhost:5174', 'http://127.0.0.1:5174')

    @classmethod
    def from_env(cls):
        value = cls(os.environ['DATABASE_URL'], os.environ['CURSOR_SECRET'],
                    json.loads(os.environ['CLOUD_IDENTITIES']),
                    os.getenv('MOCK_WORKER_ENABLED', 'true').lower() == 'true',
                    tuple(os.getenv('CORS_ORIGINS', 'http://localhost:5174,http://127.0.0.1:5174').split(',')))
        if len(value.cursor_secret) < 32:
            raise ValueError('CURSOR_SECRET must have at least 32 characters')
        for identity in value.identities:
            if identity.get('role') not in ('device', 'operator') or len(identity.get('token_sha256', '')) != 64:
                raise ValueError('Invalid identity configuration')
            if not identity.get('subject') or not identity.get('devices'):
                raise ValueError('Identity requires subject and explicit device scope')
            if identity['role'] == 'device' and identity['devices'] != [identity['subject']]:
                raise ValueError('Device identity must only scope itself')
        return value
