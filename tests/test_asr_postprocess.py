import pytest
from opencc import OpenCC

from src.apps.asr.postprocess import looks_like_filler, normalize_cantonese, postprocess

CONVERTER = OpenCC('s2hk')

PIPELINE_CASES = [
    ('你系边个？我系Angel。', '你係邊個？我係Angel。'),
    ('唔系呀，但系我觉得系咁哇。', '唔係呀，但係我覺得係咁哇。'),
    ('就系咁啦，都系你啱，真系唔系我错。', '就係咁啦，都係你啱，真係唔係我錯。'),
    ('系呀，系咩？系咪你？', '係呀，係咩？係咪你？'),
    ('我系学生，佢系老师，你系边个？', '我係學生，佢係老師，你係邊個？'),
    ('呢个系统嘅关系好复杂，要联系翻生产部。', '呢個系統嘅關係好複雜，要聯繫翻生產部。'),
    ('维系关系同语系系数系两个概念。', '維繫關係同語系係數係兩個概念。'),
    ('系统系咁嘅', '系統係咁嘅'),
    ('关系系好重要嘅', '關係係好重要嘅'),
    ('呢个系系统嚟嘅，我系系主任。', '呢個係系統嚟嘅，我係系主任。'),
    ('呢个系系统嘅问题，我系系主任。', '呢個係系統嘅問題，我係系主任。'),
    ('语系系唔同嘅嘢', '語系係唔同嘅嘢'),
]

KNOWN_LIMITATIONS = [('父系母系', '父係母係'), ('中文系', '中文係')]


@pytest.mark.parametrize(('raw', 'expected'), PIPELINE_CASES)
def test_postprocess(raw, expected):
    assert postprocess(raw, CONVERTER) == expected


@pytest.mark.parametrize(('raw', 'expected'), KNOWN_LIMITATIONS)
def test_postprocess_known_limitations(raw, expected):
    assert postprocess(raw, CONVERTER) == expected


@pytest.mark.parametrize(('raw', 'expected'), [('唔系呀', '唔係呀'), ('系统', '系统'), ('联系翻', '联系翻'), ('系呀', '係呀')])
def test_postprocess_without_converter(raw, expected):
    assert postprocess(raw, None) == expected


@pytest.mark.parametrize('raw', ['係。', '系。', '嗯。', '係，', '嗯'])
def test_filler_output_becomes_empty(raw):
    assert looks_like_filler(raw)
    assert postprocess(raw, CONVERTER) == ''


@pytest.mark.parametrize('raw', ['係呀', '好。', '得', '唔係', 'Angel', '嗯嗯'])
def test_real_short_replies_survive(raw):
    assert not looks_like_filler(raw)
    assert postprocess(raw, CONVERTER) != ''


def test_normalize_only_touches_copula():
    assert normalize_cantonese('系統關係') == '系統關係'


@pytest.mark.parametrize('raw', ['', ' ', '\n\t'])
def test_blank_input_becomes_empty(raw):
    assert postprocess(raw, CONVERTER) == ''


@pytest.mark.parametrize('raw', ['。', '？!', '，。、', '...'])
def test_punctuation_only_becomes_empty(raw):
    assert postprocess(raw, CONVERTER) == ''


@pytest.mark.parametrize(('raw', 'expected'), [('呢个系系统系咁嘅', '呢個係系統係咁嘅'), ('我系系主任', '我係系主任'), ('系統關係', '系統關係'), ('呢個係系統', '呢個係系統')])
def test_mixed_traditional_and_simplified(raw, expected):
    assert postprocess(raw, CONVERTER) == expected


def test_converter_can_be_disabled(monkeypatch):
    from src.apps.asr import postprocess as module

    monkeypatch.setattr(module, 'ASR_OPENCC', False)
    assert module.make_converter() is None
