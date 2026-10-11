"""Sidecar handler for bounded choice questions. Provider Client is injected."""
import hashlib
import json
import math
import re

from runtime.reply.jev_limits import JEV_MAX_INPUT_BYTES as SEMANTIC_REQUEST_MAX_BYTES


def _decision_detail(answer, criteria):
    """Validate native Choice metadata; legacy choices carry no invented certainty."""
    if (not isinstance(answer, dict) or not isinstance(answer.get('choice'), str)
            or answer['choice'] not in criteria or answer.get('type', 'choice') != 'choice'):
        raise ValueError('invalid_provider_response')
    choice = answer['choice']
    detail = {'choice': choice}
    if 'probabilities' not in answer:
        if 'confidence' in answer or answer.get('confidence_source', 'unavailable') != 'unavailable':
            raise ValueError('invalid_provider_response')
        return {**detail, 'confidence_source': 'unavailable'}
    probabilities = answer['probabilities']
    def probability(value):
        return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)
    if (not isinstance(probabilities, dict) or set(probabilities) != set(criteria)
            or not all(probability(value) for value in probabilities.values())
            or abs(sum(probabilities.values()) - 1) > .02 + 1e-12):
        raise ValueError('invalid_provider_response')
    n = len(criteria)
    confidence = answer.get('confidence')
    if ('confidence' not in answer and answer.get('confidence_source') != 'unavailable'
            and (probabilities[choice] == max(probabilities.values())
                 or answer.get('confidence_source') in ('probability-derived', 'deterministic'))):
        # A fallback statistic is diagnostic only; native choice remains authoritative.
        confidence = max(0.0, (probabilities[choice] - 1 / n) / (1 - 1 / n)) if n > 1 else 1.0
    source = answer.get('confidence_source', 'upstream' if 'confidence' in answer else
                        'probability-derived' if confidence is not None else 'unavailable')
    if (('confidence' in answer and not probability(confidence))
            or source not in ('upstream', 'probability-derived', 'deterministic', 'unavailable')
            or ((confidence is None) != (source == 'unavailable'))
            or (source == 'upstream' and 'confidence' not in answer)
            or (source == 'deterministic' and (n != 1 or probabilities[choice] != 1 or confidence != 1))):
        raise ValueError('invalid_provider_response')
    return {**detail, 'probabilities': dict(probabilities),
            **({'confidence': confidence} if confidence is not None else {}), 'confidence_source': source}


def decide(client, packet):
    if not isinstance(packet, dict) or set(packet) != {'state', 'questions', 'purpose'}:
        raise ValueError('invalid_request')
    if not isinstance(packet['purpose'], str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', packet['purpose']):
        raise ValueError('invalid_purpose')
    questions = packet['questions']
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 384:
        raise ValueError('invalid_questions')
    native = {}
    for key, value in questions.items():
        if (not isinstance(key, str) or not re.fullmatch('[a-zA-Z0-9_.:-]{1,200}', key)
                or not isinstance(value, dict) or set(value) != {'instructions', 'criteria'}
                or not isinstance(value['instructions'], str) or not 1 <= len(value['instructions']) <= 12000
                or not isinstance(value['criteria'], dict) or not 1 <= len(value['criteria']) <= 255
                or any(not isinstance(k, str) or not k or len(k) > 200 for k in value['criteria'])):
            raise ValueError('invalid_question')
        native[key] = {'type': 'choice', **value}
        # Older clients used numeric descriptions for these 0-100 labels.
        # Normalize only the provider projection; request/billing digests stay unchanged.
        if packet['purpose'] == 'historical-relationship' and key in {
                'familiarity', 'trust', 'comfort', 'closeness', 'tension'}:
            native[key]['criteria'] = {
                label: str(description) if type(description) is int
                and 0 <= description <= 100 and label == str(description) else description
                for label, description in value['criteria'].items()}
    encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(encoded.encode('utf-8')) > SEMANTIC_REQUEST_MAX_BYTES:
        raise ValueError('invalid_body_size')
    client.purpose = packet['purpose']
    fixed = {key: next(iter(q['criteria'])) for key, q in native.items() if len(q['criteria']) == 1}
    questions = {key:q for key,q in native.items() if key not in fixed}
    answers = client.ask(packet['state'], questions) if questions else {}
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ValueError('invalid_provider_response')
    details = {key: _decision_detail(value, native[key]['criteria']) for key, value in answers.items()}
    details.update({key: {'choice': choice, 'probabilities': {choice: 1.0}, 'confidence': 1.0,
                          'confidence_source': 'deterministic'} for key, choice in fixed.items()})
    return dict(decisions={key: value['choice'] for key, value in details.items()}, decision_details=details,
                input_digest=hashlib.sha256(encoded.encode('utf-8')).hexdigest())
