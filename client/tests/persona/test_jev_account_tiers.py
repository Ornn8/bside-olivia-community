import hashlib
import json
import pytest
from runtime.reply import jev_billing as billing, jev_tier_trust
from runtime import ed25519

KEY = 'olivia-synthetic-tier-key'


@pytest.mark.parametrize('percent', [50,70,85,100])
@pytest.mark.parametrize('tokens', [0,17,17001])
@pytest.mark.parametrize('minimum', [False,True])
def test_signed_account_tariff_and_replay(monkeypatch, percent, tokens, minimum):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    monkeypatch.setattr(jev_tier_trust, 'PUBLIC_KEY', private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    monkeypatch.setenv('OLIVIA_JEV_BILLING_ENABLED','1')
    monkeypatch.setattr(billing, '_account_key', lambda:KEY)
    receipt = dict(v=1, turn_id='test:turn', account_key_digest=hashlib.sha256(KEY.encode()).hexdigest(),
        input_digest='b'*64, price_version=billing.PRICE_VERSION, input_tokens=tokens,
        operation_id='c'*64, purpose='character-emotion')
    tariff = dict(version=billing.TIER_PRICE_VERSION, account_key_digest=receipt['account_key_digest'],
        operation_id=receipt['operation_id'],turn_id=receipt['turn_id'],percent=percent,minimum_enabled=minimum)
    signature = private.sign(json.dumps(tariff,sort_keys=True,separators=(',',':')).encode()).hex()
    exact = (tokens*101*percent+99)//100
    amount = max(exact,1000000) if exact and minimum else exact
    answer = dict(status='settled',turn_id=receipt['turn_id'],operation_id=receipt['operation_id'],
        price_version=billing.TIER_PRICE_VERSION,input_tokens=tokens,charged_units=amount,
        debited_units=amount,replayed=False,account_tariff=dict(tariff=tariff,signature=signature))
    monkeypatch.setattr(billing,'_post_settlement',lambda *args:answer)
    with billing.billing_scope('test:turn'):
        assert billing.settle_receipt_sync(dict(receipt=receipt,signature='a'*64),'b'*64)['charged_units']==amount
        answer.update(replayed=True,debited_units=0)
        assert billing.settle_receipt_sync(dict(receipt=receipt,signature='a'*64),'b'*64)['debited_units']==0
        if minimum and 0 < exact < 1000000 and percent < 100:
            answer.update(charged_units=max(exact,1000000*percent//100))
            with pytest.raises(ValueError,match='JEV_BILLING_RESPONSE_INVALID'):
                billing.settle_receipt_sync(dict(receipt=receipt,signature='a'*64),'b'*64)
            answer.update(charged_units=amount)
        tariff['percent'] = 70 if percent==50 else 50
        with pytest.raises(ValueError,match='JEV_BILLING_RESPONSE_INVALID'):
            billing.settle_receipt_sync(dict(receipt=receipt,signature='a'*64),'b'*64)
