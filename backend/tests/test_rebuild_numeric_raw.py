"""Exact raw notation contract; synthetic fixtures never use the production DB."""
from decimal import Decimal, localcontext

import pytest
from sqlalchemy import select

from app.db.models import RebuildDataRow, RebuildDataValue, RebuildValueSource
from app.services import rebuild_workflow_service as workflow
from test_rebuild_mutation_audit import audit_env, row_body


def cell(raw, number, **extra):
    return dict(raw_value=raw, numeric_value=number, value_type='explicit',
                sources=[dict(source_kind='table', page_number=1, quote=raw)], **extra)


@pytest.mark.no_test_database
@pytest.mark.parametrize('raw,number', [
    ('−2.40', -2.4), ('-2.40', -2.4), ('+2.40', 2.4), ('−0.00', -0.0),
    ('+0.00', 0.0), ('0', 0), ('−.50', -.5), ('  −2.40\n', -2.4),
    ('7.2 × 10^7', 72000000), ('7.2·10⁷', 72000000),
    ('−7.2 × 10^−7', -7.2e-7), ('10⁻⁵', 1e-5), ('10^-5', 1e-5),
    ('+7.20E−7', 7.2e-7), ('7.20e+7', 72000000), ('7.20 × 10⁺⁷', 72000000),
    ('0.123456789012345678901234567890123456789', .12345678901234568),
    ('1.234567890123456789012345678901 × 10^2', 123.45678901234568),
    ('4.9406564584124654 × 10^−324', 5e-324), ('1e-320', 1e-320),
])
def test_supported_projection_preserves_input(raw, number):
    payload = cell(raw, number)
    before = repr(payload)
    workflow._validate_value(payload)
    assert repr(payload) == before


@pytest.mark.no_test_database
@pytest.mark.parametrize('raw', [
    '10−5', '10-5', '1–2', '1-2', '1~2', '≤1', '>1', '≈1', '1 ± 0.1',
    '1.00s', '1 eV', '7.2 x 10^7', '7.2*10**7', '7.2 × 10-5',
    '10⁻5', '10^⁻⁵', '10^2+3', '(2)', '2/3', '1_000', 'NaN', 'Infinity',
    '１.２', '1\x00', '1e1000000', '__import__("os")',
])
def test_unsupported_text_requires_null_projection(raw):
    assert workflow._raw_numeric_decimal(raw) is None
    with pytest.raises(workflow.RebuildWorkflowError, match='requires_plain_numeric_raw'):
        workflow._validate_value(cell(raw, 1))
    workflow._validate_value(cell(raw, None))


@pytest.mark.no_test_database
@pytest.mark.parametrize('raw,number,error', [
    ('−2.40', 2.4, 'disagrees'), ('7.2 × 10^7', 7.2e6, 'disagrees'),
    ('1e-999', 0, 'disagrees'), ('1e309', 1, 'out_of_range'),
    ('1', float('inf'), 'must_be_finite'), ('1', float('nan'), 'must_be_finite'),
])
def test_inconsistent_or_nonfinite_projection_rejected(raw, number, error):
    with pytest.raises(workflow.RebuildWorkflowError, match=error):
        workflow._validate_value(cell(raw, number))


@pytest.mark.no_test_database
@pytest.mark.parametrize('raw,number,precision,error', [
    ('−2.40', -2.4, 2, None), ('1.20 × 10^2', 120, 2, None),
    ('1.20 × 10^−1', .12, 2, 'precision_exceeds'),
    ('1.230', 1.23, 2, 'precision_exceeds'), ('1.230', None, 2, 'precision_exceeds'), ('1.2', 1.2, 3, 'precision_exceeds'),
    ('10−5', None, 2, 'must_be_numeric'), ('1_000', None, 2, 'must_be_numeric'),
    ('9007199254740993', float(9007199254740993), 0, None),
])
def test_estimated_uses_exact_expanded_decimal_precision(raw, number, precision, error):
    payload = cell(raw, number, precision_digits=precision)
    payload.update(value_type='estimated', sources=[dict(source_kind='estimated', estimate_basis='synthetic plot axis')])
    if error:
        with pytest.raises(workflow.RebuildWorkflowError, match=error):
            workflow._validate_value(payload)
    else:
        workflow._validate_value(payload)


@pytest.mark.no_test_database
@pytest.mark.parametrize('kind,sources,error', [
    ('explicit', [], 'source_required'),
    ('explicit', [dict(source_kind='estimated', estimate_basis='plot')], 'explicit_value_requires'),
    ('estimated', [dict(source_kind='estimated')], 'basis_required'),
    ('derived', [dict(source_kind='derived', estimate_basis=''), dict(source_kind='derived', estimate_basis='formula')], 'basis_required'),
])
def test_provenance_rules_unchanged(kind, sources, error):
    payload = cell('−1.20', -1.2)
    payload.update(value_type=kind, sources=sources)
    with pytest.raises(workflow.RebuildWorkflowError, match=error):
        workflow._validate_value(payload)


@pytest.mark.no_test_database
def test_exact_decimal_signature_without_context_rounding():
    raw = '1.234567890123456789012345678901234567890 × 10^7'
    with localcontext() as context:
        context.prec = 3
        parsed = workflow._raw_numeric_decimal(raw)
        assert parsed == Decimal('12345678.90123456789012345678901234567890')
        assert parsed.as_tuple().exponent == -32
        a = dict(raw_value=raw, numeric_value=float(parsed), unit='eV')
        b = dict(a, raw_value='12345678.90123456789012345678901234567890')
        assert workflow._value_signature(a) == workflow._value_signature(b)
        assert workflow._value_signature(dict(a, raw_value='−0')) == workflow._value_signature(dict(a, raw_value='+0.00'))
        assert workflow._value_signature(dict(a, raw_value='1.00000000000000000000000001')) != workflow._value_signature(dict(a, raw_value='1.00000000000000000000000002'))
        assert workflow._value_signature(dict(a, unit='eV')) != workflow._value_signature(dict(a, unit='meV'))
        assert workflow._value_signature(dict(a, raw_value='10−5')) != workflow._value_signature(dict(a, raw_value='1e-5'))


def test_http_cells_raw_sources_and_equivalent_candidates(audit_env):
    env = audit_env
    body = row_body(env)
    cases = [('−2.40', -2.4), ('7.2 × 10^7', 72000000),
             ('4.9406564584124654 × 10^−324', 5e-324),
             ('0.12345678901234567890123456789', .12345678901234568),
             ('10−5', None), ('1.00s', None), ('−0.00', -0.0)]
    template = body['rows'][0]['values'][0]
    body['rows'][0]['values'] = [dict(template, field_name=f'v{i}', raw_value=raw, numeric_value=n,
                                    sources=[dict(template['sources'][0], quote=raw)])
                                 for i, (raw, n) in enumerate(cases)]
    body['rows'][0]['values'].append(dict(template, field_name='bad', raw_value='10−5', numeric_value=1e-5))
    response = env['client'].post('/api/rebuild/rows/import', json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert not result['complete'] and result['rows_partial'] == 1
    details = result['results'][0]['values']
    assert [d['persisted'] for d in details] == [True] * len(cases) + [False]
    assert [d['field_index'] for d in details] == list(range(len(cases) + 1))
    assert 'requires_plain_numeric_raw' in details[-1]['error']
    with env['factory']() as session:
        values = session.scalars(select(RebuildDataValue).order_by(RebuildDataValue.field_name)).all()
        assert [(v.raw_value, v.numeric_value) for v in values] == cases
        assert all(v.precision_digits == 4 and v.unit == 'eV' for v in values)
        sources = session.scalars(select(RebuildValueSource)).all()
        assert sorted(s.quote for s in sources) == sorted(raw for raw, _ in cases)
        assert all(s.file_id == env['file'] and s.page_number == 2 and s.table_column == 1 for s in sources)
    # Same fact in another supported notation: retain both raw candidates, no conflict.
    body['request_id'] = 'equivalent-notation'
    body['rows'][0]['values'] = [dict(body['rows'][0]['values'][0], raw_value='-2.400')]
    result = env['client'].post('/api/rebuild/rows/import', json=body).json()
    assert result['complete'] and not result['errors']
    with env['factory']() as session:
        value = session.scalar(select(RebuildDataValue).where(RebuildDataValue.field_name == 'v0'))
        assert value.raw_value == '−2.40'
        assert value.conflict['status'] == 'evidence_history'
        assert {c['raw_value'] for c in value.conflict['candidates']} == {'−2.40', '-2.400'}


    # CAS correction uses the same projection and preserves the new raw verbatim.
    path = f"/api/rebuild/values/{value.id}"
    current = env['client'].get(path)
    assert current.status_code == 200, current.text
    state = current.json()
    source = {k: v for k, v in state['sources'][0].items() if k not in {'id', 'source_key'}}
    correction = dict(raw_value='−2.400 × 10^0', numeric_value=-2.4, unit='eV', precision_digits=3,
                      source=source, expected_revision=state['value_revision'], request_id='00000000-0000-4000-8000-000000000001',
                      correction_reason='synthetic notation correction')
    response = env['client'].put(path, json=correction)
    assert response.status_code == 200, response.text
    state = env['client'].get(path).json()
    assert state['raw_value'] == '−2.400 × 10^0' and state['precision_digits'] == 3
    assert state['numeric_value'] == -2.4 and state['unit'] == 'eV'


def test_old_partial_receipt_replays_then_new_request_fills_same_row(audit_env, monkeypatch):
    env = audit_env
    body = row_body(env)
    body['rows'][0]['values'][0].update(raw_value='−2.40', numeric_value=-2.4)
    parse = workflow._raw_numeric_decimal
    # Simulate old ASCII validator on the identical request in the isolated DB.
    with monkeypatch.context() as old:
        old.setattr(workflow, '_raw_numeric_decimal', lambda raw: None if '−' in str(raw) else parse(raw))
        first = env['client'].post('/api/rebuild/rows/import', json=body).json()
    assert first['rows_partial'] == 1 and not first['complete']
    canonical_key = first['results'][0]['row_key']
    replay = env['client'].post('/api/rebuild/rows/import', json=body).json()
    assert replay['audit']['replayed'] and replay['rows_partial'] == 1
    with env['factory']() as session:
        assert not session.scalars(select(RebuildDataValue)).all()
    body['request_id'] = 'repair-new-request'
    body['rows'][0]['row_key'] = canonical_key
    repaired = env['client'].post('/api/rebuild/rows/import', json=body).json()
    assert repaired['complete'] and not repaired['audit']['replayed'], repaired
    with env['factory']() as session:
        assert len(session.scalars(select(RebuildDataRow)).all()) == 1
        values = session.scalars(select(RebuildDataValue)).all()
        assert [(v.raw_value, v.numeric_value) for v in values] == [('−2.40', -2.4)]
    assert env['client'].post('/api/rebuild/rows/import', json=body).json()['audit']['replayed']
