from app.services.payments import normalize_transfer_number, parser_transfer_from_payload, verify_hmac_v2
import hashlib, hmac, json, time


def test_normalize_cuban_numbers():
    assert normalize_transfer_number('+5359190241') == '59190241'
    assert normalize_transfer_number('5359190241') == '59190241'
    assert normalize_transfer_number('53559190241') == '59190241'


def test_parser_payload_received_transfer():
    payload = {
        'event': 'TRANSFER_DETECTED',
        'event_id': 'evt-1',
        'transaction': {
            'direction': 'RECIBIDO',
            'amount': 10,
            'currency': 'CUP',
            'sender_phone': '+5359190241',
            'receiver_phone': '+53512345678',
            'receiver_account': '12345678',
            'transaction_id': 'tx-1',
        },
    }
    parsed = parser_transfer_from_payload(payload)
    assert parsed is not None
    assert parsed['event_id'] == 'evt-1'
    assert parsed['provider_reference'] == 'tx-1'
    assert parsed['transfer_number'] == '59190241'
    assert parsed['amount'] == 10.0


def test_parser_ignores_non_received_transfer():
    payload = {'event': 'TRANSFER_DETECTED', 'transaction': {'direction': 'ENVIADO', 'amount': 10, 'sender_phone': '59190241'}}
    assert parser_transfer_from_payload(payload) is None


def test_hmac_v2():
    secret = 'secret'
    raw = b'{"event":"TRANSFER_DETECTED"}'
    timestamp = str(int(time.time()))
    expected = hmac.new(secret.encode(), f'{timestamp}.'.encode() + raw, hashlib.sha256).hexdigest()
    assert verify_hmac_v2(raw, secret, expected, timestamp)
    assert not verify_hmac_v2(raw, 'wrong', expected, timestamp)
