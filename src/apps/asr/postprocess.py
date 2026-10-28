import logging
import re

from opencc import OpenCC

from src.config import ASR_OPENCC, ASR_OPENCC_CONFIG

logger = logging.getLogger(__name__)

COMPOUND_PREV = '关關联聯维維语語直星水派院系'
COMPOUND_NEXT = '统統列別别數数譜谱主任争爭系'

COPULA = '係'
SIMPLIFIED_COPULA = '系'

_COPULA_RE = re.compile(f'(?<![{COMPOUND_PREV}]){SIMPLIFIED_COPULA}(?={SIMPLIFIED_COPULA})|(?<={SIMPLIFIED_COPULA}){SIMPLIFIED_COPULA}(?![{COMPOUND_NEXT}])|(?<![{COMPOUND_PREV}]){SIMPLIFIED_COPULA}(?![{COMPOUND_NEXT}])')

FILLER_OUTPUTS = frozenset('係系嗯')
_NON_CONTENT_RE = re.compile(r'[\W_]+')


def normalize_cantonese(text: str) -> str:
    """Rewrite the simplified copula 系 to Cantonese 係 without touching compounds.

    Qwen3-ASR answers Cantonese in simplified characters and no system prompt changes
    that, so the text has to be converted afterwards. OpenCC handles every morphological
    use of 系 correctly (系统->系統, 联系->聯繫, 语系->語系) but ignores the copula, and
    even mistranslates it to 繫 when two 系 meet (呢个系系统 -> 呢個繫系統). So the copula
    has to be fixed before OpenCC sees the text.

    系 touching a character in COMPOUND_PREV or COMPOUND_NEXT is a compound. Three rules
    run in order: the first 系 of a doubled pair with no compound before it, the second 系
    of a pair with no compound after it, then any remaining copula. The order matters,
    because only the first rule catches 呢个[系]系统 before OpenCC turns it into 繫.

    COMPOUND_PREV deliberately omits 父 母 學 文 體: a real copula often follows them
    (我父母[系]老師), and losing a copula hurts more than mistaking 父系 or 中文系.
    Both errors are glyph level only, so the agent still understands the sentence.
    """
    return _COPULA_RE.sub(COPULA, text)


def looks_like_filler(text: str) -> bool:
    """Report whether the text carries no content: a lone 係 or 嗯, or punctuation only.

    Given silence, a DC offset, a pure tone or a noise burst, Qwen3-ASR reliably emits
    one two-token sentence (係。 or 嗯。) instead of an empty string. Accepting it would
    send a fake user message into the agent.
    """
    stripped = _NON_CONTENT_RE.sub('', text)
    return not stripped or stripped in FILLER_OUTPUTS


def make_converter() -> OpenCC | None:
    if not ASR_OPENCC:
        return None
    try:
        return OpenCC(ASR_OPENCC_CONFIG)
    except ValueError:
        logger.warning('opencc config unavailable config=%s, cantonese text stays simplified', ASR_OPENCC_CONFIG)
        return None


def postprocess(text: str, converter: OpenCC | None) -> str:
    """Turn raw ASR output into text that is safe to hand downstream."""
    text = normalize_cantonese(text)
    if converter is not None:
        text = converter.convert(text)
    if looks_like_filler(text):
        return ''
    return text
