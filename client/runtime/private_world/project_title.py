"""Names for her projects: a name has to be a name, not a piece of the conversation.

An extracted update is named after the shortest span of the reply that locates the
action. Such a span is sometimes only what is left of a sentence ("也不用，") or a
sentence that trails off. Neither names a thing she does, and a project carrying one
is offered to the world decision again and again. The rules are structural: they look
at punctuation and grammar words, never at an activity vocabulary, so a real name in
any wording still passes.
"""

_EDGE = " \t\r\n\u3000，。！？、；：,.!?;:…·~～—-_\"'“”‘’「」『』()（）[]【】<>《》"
_ELLIPSIS = ("…", "...")
# Words that only link or colour a sentence. A short phrase made mostly of them is
# what is left of a sentence, not the name of an activity.
_GRAMMAR = frozenset("我你他她它们的了着过吗呢吧啊呀哦嗯也都就还又再才不没无别与或及而并但却么这那")
_SHORT = 6


def usable(text):
    """Whether the text can stand as the name of something she does."""
    if not isinstance(text, str):
        return False
    if text.strip().endswith(_ELLIPSIS):
        return False  # a sentence that trails off
    letters = [c for c in text.strip(_EDGE) if c.isalnum()]
    if len(letters) < 2:
        return False
    # Half of a two or three letter phrase, or two thirds of a longer short one, is grammar:
    # a remnant such as "也不用".
    grammar = sum(c in _GRAMMAR for c in letters)
    return not (len(letters) <= 3 and 2 * grammar >= len(letters)
                or len(letters) <= _SHORT and 3 * grammar >= 2 * len(letters))
