"""Shared review scope: preferences warn; specific claims require evidence."""

SOFT_STYLE_CODES = frozenset({'STYLE_DRIFT', 'GENERIC_COUNSELOR', 'IDENTITY_DRIFT'})

# Reused by detection and confirmation rather than a word-based classifier.
FACT_REVIEW_SCOPE = (
    '只核对候选明确断言的具体现实事实：用户的过往事件、习惯、位置、动作、次数，'
    '说话人归属，以及角色声称自己记得、亲历、听见或看见的真实感知。'
    '普通亲昵、想念、关心、调侃、拒绝、角色当下感受与生活表达不是用户事实；'
    '没有具体事件、位置或次数的日常建议，不因停止、继续、再次等措辞推定用户已经做过什么。'
    '明确的具体主张仍须核对来源、说话人、时间和全文；关心语气不能支持编造的具体经历。'
    '当前用户自述支持转述，不证明角色记得或亲历；文字转写不支持声音特征。'
    '纯故事、比喻、条件和未来可选建议不当成已发生事实；冻结世界明确矛盾仍须纠正。'
)


def evidence_severity(code: str, claim_kind: str) -> str:
    """Text integrity remains required; stylistic preferences remain optional."""
    if code == 'GENERIC_COUNSELOR' or (
        code == 'STYLE_DRIFT' and claim_kind in {
            'forced_question', 'generic_assistant_tone', 'fixed_structure',
            'forced_uplift', 'voice_mismatch',
        }
    ):
        return 'soft'
    return 'hard'


def fact_sentence_spans(candidate: str, spans) -> list[tuple[int, int]]:
    """Expand factual spans to complete sentences, never remove a clause alone."""
    expanded = []
    for start, end in spans:
        while start > 0 and candidate[start - 1] not in '。！？!?\r\n':
            start -= 1
        while end < len(candidate) and candidate[end - 1] not in '。！？!?\r\n':
            end += 1
        # Close quotes opened in this deleted sentence. A quote opened in an
        # earlier retained sentence must keep its closing quote.
        closing_quotes = {'”': '“', '’': '‘', '」': '「', '』': '『'}
        while (end < len(candidate) and candidate[end] in closing_quotes
               and candidate[start:end].count(closing_quotes[candidate[end]])
               > candidate[start:end].count(candidate[end])):
            end += 1
        while start < end and candidate[start] in '”’」』':
            start += 1
        expanded.append((start, end))
    merged = []
    for start, end in sorted(set(expanded)):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def preserves_quote_balance(original: str, repaired: str) -> bool:
    """Do not turn a balanced quoted passage into a fragment by deleting a sentence."""
    for opening, closing in [('“', '”'), ('‘', '’'), ('「', '」'), ('『', '』')]:
        if original.count(opening) == original.count(closing) and repaired.count(opening) != repaired.count(closing):
            return False
    return True
