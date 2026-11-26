from __future__ import annotations

import json
import pytest

from app.services.ai.parsing import AiResponseError, parse_result, split_terms
from app.services.ai.prompts import build_prompt

FIELDS = [{"key": "material", "label": "材质", "hint": "主要材料"},
          {"key": "festival", "label": "节日场景", "hint": "适用的具体节日；不针对节日时留空"}]


def test_free_text_keeps_original_and_missing_extra_fields_are_tolerated() -> None:
    result = parse_result('```json\n{"tags":{"material":" 陶瓷、胡桃木，陶瓷/304 不锈钢；木 ; 金属 ","extra":"丢弃"},"free_text":"描述"}\n```', FIELDS)
    assert set(result['tags']) == {'material', 'festival'}
    assert result['tags']['material'] == {'value': ' 陶瓷、胡桃木，陶瓷/304 不锈钢；木 ; 金属 ',
        'terms': ['陶瓷', '胡桃木', '304', '不锈钢', '木', '金属'], 'source': 'ai',
        'ai_value': ' 陶瓷、胡桃木，陶瓷/304 不锈钢；木 ; 金属 ', 'flag': None}
    assert result['tags']['festival']['value'] == ''
    assert result['free_text'] == '描述' and result['flags'] == []


def test_terms_do_not_normalize_synonyms_or_limit_quantity() -> None:
    assert split_terms('过年，春节 领导/企业客户;员工；长辈、儿童,儿童\t朋友') == [
        '过年', '春节', '领导', '企业客户', '员工', '长辈', '儿童', '朋友']
    assert split_terms('  \t\n ') == []


def test_only_three_requested_yellow_flags_and_relation_ids_are_validated() -> None:
    raw = json.dumps({'tags': {'material': '无法判断', 'festival': '通用'}, 'free_text': '描述',
        'multi_object': True, 'flags': ['任意额外标黄'], 'relation_items': [1, '2', 2, True, 1.0, 99, {}, -1, '2.0']})
    item = parse_result(raw, FIELDS)
    assert item['tags']['material']['flag'] == 'uncertain'
    assert item['tags']['material']['terms'] == [] and item['tags']['material']['value'] == ''
    assert item['tags']['festival']['flag'] is None and item['tags']['festival']['value'] == ''
    assert item['flags'] == ['multi_object'] and item['relation_items'] == []
    bundle = parse_result(raw, FIELDS, 'set', {1: 101, 2: 102})
    assert bundle['flags'] == []
    assert bundle['relation_items'] == [{'batch_product_id': 101, 'source': 'ai'}, {'batch_product_id': 102, 'source': 'ai'}]
    empty = parse_result(raw, FIELDS, 'set', {3: 103})
    assert empty['flags'] == ['empty_relation']


@pytest.mark.parametrize('raw', ['not-json', '[1,2]', '{"tags":[]}', '{"tags":null}', '{}', '{"tags":{"material":12}}'])
def test_invalid_json_shape_requests_a_safe_retry(raw: str) -> None:
    with pytest.raises(AiResponseError):
        parse_result(raw, FIELDS)


def test_direct_fields_non_string_values_and_unexpected_description() -> None:
    result = parse_result('{"material": ["陶瓷"], "festival": " 春节 ", "free_text": {}}', FIELDS)
    assert result['tags']['material']['value'] == ''
    assert result['tags']['festival']['value'] == ' 春节 '
    assert result['free_text'] == ''


def test_prompts_include_updated_hints_and_only_allowed_candidate_metadata() -> None:
    fields = [*FIELDS, {'key': 'new_field', 'label': '新字段', 'hint': '新的填写说明原文'}]
    plain = build_prompt(fields)
    assert 'new_field（新字段）：新的填写说明原文' in plain
    assert '没有可选值' in plain and '没有数量上限' in plain
    bundle = build_prompt(fields, [{'index': 1, 'name': '展示名', 'tags': {'material': {'value': '木', 'source': 'ai'}, 'old_disabled': '不发送旧字段'},
                                    'nas_path': '/禁止发送', 'batch_product_id': 600}])
    assert '"index": 1' in bundle and '"material": "木"' in bundle
    assert '/禁止发送' not in bundle and 'batch_product_id' not in bundle and '"source"' not in bundle
    assert '不发送旧字段' not in bundle
