import json
import unittest
from pathlib import Path
from pydantic import TypeAdapter, ValidationError
from shared.schemas.events import Event
from shared.schemas.api import BatchRequest, EventQuery, RealtimeMessage

ADAPTER = TypeAdapter(Event)
EXAMPLES = Path(__file__).resolve().parents[1] / 'shared' / 'examples' / 'events.json'


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.events = json.loads(EXAMPLES.read_text(encoding='utf-8'))

    def test_all_eight_types_roundtrip(self):
        self.assertEqual(len({e['event_type'] for e in self.events}), 8)
        for raw in self.events:
            event = ADAPTER.validate_python(raw)
            self.assertEqual(ADAPTER.validate_json(event.model_dump_json()), event)

    def test_confidence_bounds(self):
        for bad in (-0.01, 1.01, float('nan'), float('inf')):
            with self.assertRaises(ValidationError):
                ADAPTER.validate_python({**self.events[0], 'confidence': bad})

    def test_timezone_required_and_normalized(self):
        raw = self.events[0]
        with self.assertRaises(ValidationError):
            ADAPTER.validate_python({**raw, 'timestamp': '2026-10-30T12:00:00'})
        e = ADAPTER.validate_python({**raw, 'timestamp': '2026-10-30T12:00:00+08:00'})
        self.assertEqual(e.timestamp.hour, 4)
        self.assertTrue(e.model_dump(mode='json')['timestamp'].endswith('Z'))

    def test_payload_discriminator_and_unknown_fields(self):
        for override in ({'event_type': 'unknown'}, {'payload': {'label': 'x'}}, {'secret': 'x'}):
            with self.assertRaises(ValidationError):
                ADAPTER.validate_python({**self.events[0], **override})

    def test_missing_invalid_and_disconnected(self):
        raw = self.events[0]
        raw['payload']['connection'] = 'disconnected'
        raw['payload']['channels'][0].update(samples=[None], quality='missing', missing_reason='disconnect')
        ADAPTER.validate_python(raw)
        raw['payload']['channels'][0]['samples'] = [float('inf')]
        with self.assertRaises(ValidationError):
            ADAPTER.validate_python(raw)

    def test_batch_can_hold_invalid_peer_for_per_item_validation(self):
        request = BatchRequest(events=[self.events[0], {'event_type': 'bad'}])
        ADAPTER.validate_python(request.events[0])
        with self.assertRaises(ValidationError):
            ADAPTER.validate_python(request.events[1])

    def test_time_filters_and_limits(self):
        for query in ({'limit': 201}, {'start': '2026-10-31T00:00:00Z', 'end': '2026-10-30T00:00:00Z'}):
            with self.assertRaises(ValidationError):
                EventQuery(**query)

    def test_websocket_shape(self):
        with self.assertRaises(ValidationError):
            RealtimeMessage(kind='event', timestamp='2026-10-30T00:00:00Z', sequence=1)


if __name__ == '__main__':
    unittest.main()
