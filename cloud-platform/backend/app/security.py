import base64
import hashlib
import hmac
import json
import time
from fastapi import HTTPException


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def fail(status, code, message):
    raise HTTPException(status, detail={'code': code, 'message': message, 'details': {}})


def identity(settings, authorization):
    if not authorization or not authorization.startswith('Bearer '):
        fail(401, 'unauthorized', 'Bearer authentication required')
    hashed = hashlib.sha256(authorization[7:].encode()).hexdigest()
    for item in settings.identities:
        if hmac.compare_digest(item['token_sha256'], hashed) and not item.get('revoked', False):
            return item
    fail(401, 'unauthorized', 'Invalid credentials')


def check_device(user, device_id):
    if device_id not in user['devices']:
        fail(403, 'forbidden', 'Device is outside the authorized scope')


def scope(user):
    return {'subject': user['subject'], 'role': user['role'], 'devices': sorted(user['devices'])}


def encode_cursor(settings, context, position, snapshot, expires=None):
    payload = canonical({'context': context, 'position': position, 'snapshot': snapshot,
                         'expires': expires or int(time.time()) + 3600}).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip('=')
    signature = hmac.new(settings.cursor_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return body + '.' + signature


def decode_cursor(settings, token, context):
    try:
        body, signature = token.split('.')
        expected = hmac.new(settings.cursor_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        value = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
        if value['context'] != context:
            raise ValueError()
        if value['expires'] < time.time():
            fail(400, 'cursor_expired', 'Restart the query')
        return value
    except (ValueError, KeyError, TypeError):
        fail(400, 'invalid_cursor', 'Invalid cursor or changed filters')
