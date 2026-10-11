"""Pair split extraction rows without crossing paper or material identity."""
import json
import hashlib

from app.services.rebuild_analysis_fields import source_ids

METHOD_KEYS = {
    'code': 'code', 'method': 'method', 'functional': 'functional',
    'exchange_correlation': 'functional', 'dispersion': 'dispersion',
    'dispersion_correction': 'dispersion', 'plane_wave_cutoff': 'cutoff',
    'plane_wave_cutoff_eV': 'cutoff', 'k_point_mesh': 'kmesh', 'k_points': 'kmesh',
    'graphene_supercell': 'supercell', 'supercell': 'supercell',
    'vacuum_angstrom': 'vacuum', 'temperature': 'temperature',
    'temperature_k': 'temperature', 'potential': 'potential',
    'coverage': 'coverage', 'solvent': 'solvent', 'hubbard_u': 'hubbard_u',
    'spin': 'spin', 'spin_polarization': 'spin', 'charge_state': 'charge_state',
}


def condition(cell):
    try:
        return json.loads(cell.get('analysis_context', {}).get('condition_key') or '{}')
    except (TypeError, ValueError):
        return {}


def method(cell):
    return {canonical: value for key, value in condition(cell).items()
            if (canonical := METHOD_KEYS.get(key)) and value is not None}


def compatible(left, right):
    if left['paper_id'] != right['paper_id'] or not left.get('catalyst_name') or left['catalyst_name'] != right.get('catalyst_name'):
        return False
    # Explicit contradictory identities/settings cannot be joined. Different
    # property calculation states (clean vs adsorbed, NEB vs adsorption) are
    # expected axes of a descriptor relationship, not different catalysts.
    for key in ('active_site', 'support', 'configuration', 'reaction'):
        a, b = left.get(key), right.get(key)
        if a and b and a != b:
            return False
    a, b = method(left), method(right)
    return all(a[key] == b[key] for key in a.keys() & b.keys())


def catalyst_entity_id(cell):
    identity=[cell.get(k) for k in ('paper_id','catalyst_name','active_site','support','configuration')]
    return 'rebuild-catalyst:'+hashlib.sha256(json.dumps(identity,ensure_ascii=False).encode()).hexdigest()[:24]


def context(left, right):
    return {'source_family': 'rebuild', 'paper_id': left['paper_id'],
            'reaction_type': left.get('reaction'), 'data_type': left.get('data_type'),
            'x_method': method(left), 'y_method': method(right),
            'x_adsorbate': left.get('adsorbate'), 'y_adsorbate': right.get('adsorbate'),
            'x_unit': left['target']['normalized_unit'], 'y_unit': right['target']['normalized_unit'],
            'x_reaction_step': condition(left).get('reaction_step'),
            'y_reaction_step': condition(right).get('reaction_step')}


def pair_rows(rows, x_field, y_field):
    lefts = [fields[x_field] for fields in rows.values() if x_field in fields]
    rights = [fields[y_field] for fields in rows.values() if y_field in fields]
    pairs, excluded = [], []
    seen = set()
    for left in lefts:
        if not left['exploratory_eligible']:
            excluded.append((left, [], left['exclusion_reason']))
            continue
        same_row = [r for r in rights if r['rebuild_row_id'] == left['rebuild_row_id']]
        candidates = same_row or [r for r in rights if compatible(left, r)]
        # Preserve exclusions instead of silently ignoring conflicting/estimated
        # alternatives and pretending there was only one measurement.
        bad = next((r['exclusion_reason'] for r in candidates if not r['exploratory_eligible']), None)
        if bad:
            excluded.append((left, candidates, bad))
            continue
        signatures = {(r['target']['normalized_value'], r['target']['normalized_unit'],
                       r.get('active_site'),r.get('support'),r.get('configuration'),
                       json.dumps(context(left,r), sort_keys=True, ensure_ascii=False)) for r in candidates}
        if not candidates or len(signatures) > 1:
            excluded.append((left, candidates, 'ambiguous_same_material_measurements' if candidates else 'missing_y_field_value'))
            continue
        right = dict(candidates[0])
        right['source_record_ids'] = list(dict.fromkeys(i for r in candidates for i in source_ids(r)))
        signature = (left['paper_id'], left.get('catalyst_name'),
                     left['target']['normalized_value'], right['target']['normalized_value'],
                     json.dumps(context(left, right), sort_keys=True, ensure_ascii=False))
        if signature not in seen:
            seen.add(signature)
            pairs.append((left, right))
    return pairs, excluded, lefts, rights
