"""Read-only semantic aliases for evidence-backed physical analysis fields.

Raw extracted cells remain untouched. Atomic net charges, predictions, model
scores and method parameters are deliberately not catalyst descriptors here.
"""
from collections import defaultdict
from math import isclose
from copy import deepcopy

from app.normalizers.chemistry_normalizer import ADSORBATE_MAP, canonicalize_adsorbate


# key, Chinese label, canonical unit, exact raw aliases
FIELDS = [
    ('li2s_adsorption_energy', 'Li₂S 吸附能', 'eV', ('adsorption_energy_li2s_ev',)),
    ('li2s_dissociation_barrier', 'Li₂S 分解 / 解离能垒', 'eV', ('li2s_decomposition_barrier_ev', 'li2s_dissociation_barrier_ev')),
    ('li2s_bader_charge_transfer', 'Li₂S Bader 电荷转移', 'e', ()),
    ('li_s_bond_max', 'Li₂S 中 Li–S 最大键长', 'Å', ()),
    ('li1_s_bond_length', 'Li₂S 中 Li1–S 键长', 'Å', ('li1_s_bond_length_angstrom',)),
    ('li2_s_bond_length', 'Li₂S 中 Li2–S 键长', 'Å', ('li2_s_bond_length_angstrom',)),
    ('d_band_center', 'd 带中心', 'eV', ('d_band_center_ev',)),
    ('rds_delta_g', '决速步吉布斯自由能变化', 'eV', ('delta_g_rds_ev', 'rate_determining_step_gibbs_free_energy_ev')),
    ('adsorption_energy', '吸附能（吸附物未分类）', 'eV', ('adsorption_energy_ev',)),
    ('li_s_bond_length', 'Li–S 键长（未注明是否最大）', 'Å', ('li_s_bond_length_angstrom',)),
    ('bader_charge_transfer', 'Bader 电荷转移（吸附物未分类）', 'e', ('bader_charge_transfer_e',)),
    ('binding_energy', '结合能', 'eV', ('binding_energy_ev',)),
    ('band_gap', '带隙', 'eV', ('band_gap_ev',)),
    ('formation_energy', '形成能', 'eV', ('formation_energy_ev',)),
    ('defect_formation_energy', '缺陷形成能', 'eV', ('defect_formation_energy_ev',)),
    ('li_ion_diffusion_barrier', '锂离子扩散能垒', 'eV', ('li_ion_diffusion_barrier_ev',)),
    ('li2s_delithiation_barrier', 'Li₂S 脱锂能垒', 'eV', ('li2s_delithiation_barrier_ev',)),
    ('metal_s_bond_length', '金属–硫键长', 'Å', ('m_s_bond_length_angstrom', 'tm_s_bond_length_angstrom')),
    ('icohp', '积分晶体轨道哈密顿布居（ICOHP）', 'eV', ('icohp_ev',)),
]
for species in ('s8', 'li2s8', 'li2s6', 'li2s4', 'li2s2'):
    name = {'s8': 'S₈', 'li2s8': 'Li₂S₈', 'li2s6': 'Li₂S₆', 'li2s4': 'Li₂S₄', 'li2s2': 'Li₂S₂'}[species]
    FIELDS.append((species + '_adsorption_energy', name + ' 吸附能', 'eV', ('adsorption_energy_' + species + '_ev',)))

DEFINITIONS = {key: {'key': key, 'field': key, 'label': label, 'label_zh': label,
                     'unit': unit, 'type': 'number', 'category': 'numeric'}
               for key, label, unit, _ in FIELDS}
ALIASES = {alias: key for key, _, _, aliases in FIELDS for alias in (key, *aliases)}
PRESET_FIELDS = {'li2s_adsorption_energy', 'li2s_dissociation_barrier',
                 'li2s_bader_charge_transfer', 'li_s_bond_max', 'd_band_center', 'rds_delta_g'}

# Explicit species requirements for dedicated variables, including canonical
# names and their raw aliases. Unrecognized/missing objects are not conflicts.
FIELD_SPECIES = {key: 'Li2S' for key in (
    'li2s_adsorption_energy', 'li2s_dissociation_barrier', 'li2s_bader_charge_transfer',
    'li2s_delithiation_barrier', 'li_s_bond_max', 'li1_s_bond_length', 'li2_s_bond_length')}
FIELD_SPECIES.update({species.lower() + '_adsorption_energy': species
                      for species in ('S8', 'Li2S8', 'Li2S6', 'Li2S4', 'Li2S2')})
KNOWN_SPECIES = set(ADSORBATE_MAP.values()) - {'LiPS'}


def declared_species(record):
    text = record.get('adsorbate') or ''
    return canonicalize_adsorbate(text.translate(str.maketrans('₀₁₂₃₄₅₆₇₈₉', '0123456789')))


def source_ids(cell):
    return cell.get('source_record_ids') or [cell['record_id']]


def merge(cells, field):
    """Never silently pick between divergent aliases or discard bad evidence."""
    result = dict(cells[0], field_name=field)
    result['source_record_ids'] = list(dict.fromkeys(i for c in cells for i in source_ids(c)))
    result['raw_field_names'] = list(dict.fromkeys(c['field_name'] for c in cells))
    reason = next((c['exclusion_reason'] for c in cells if c['exclusion_reason']), None)
    expected = FIELD_SPECIES.get(field)
    if expected and any((actual := declared_species(c)) in KNOWN_SPECIES
                        and actual != expected for c in cells):
        reason = 'field_adsorbate_conflict'
        # Keep every original candidate, reason, raw value and source, including
        # conflicting aliases; this projection never modifies stored records.
        result['raw_evidence'] = deepcopy(cells)
    unit = DEFINITIONS[field]['unit']
    if not reason and any(c['target']['normalized_unit'] != unit for c in cells):
        reason = 'incompatible_analysis_unit'
    if not reason and any(not isclose(c['target']['normalized_value'], cells[0]['target']['normalized_value'], rel_tol=1e-9, abs_tol=1e-9) for c in cells[1:]):
        reason = 'conflicting_field_aliases'
    result.update(exclusion_reason=reason, exploratory_eligible=reason is None)
    return result


def project_rows(records):
    grouped = defaultdict(list)
    for record in records:
        grouped[record['rebuild_row_id']].append(record)
    projected = {}
    for identity, row in grouped.items():
        candidates = defaultdict(list)
        for record in row:
            raw = record['field_name']
            species = declared_species(record)
            field = ALIASES.get(raw)
            if raw in {'li1_s_bond_length_angstrom', 'li2_s_bond_length_angstrom'} and species not in KNOWN_SPECIES:
                field = None
            # A known adsorbate gives one canonical variable, not both a
            # generic alias and its species-specific projection.
            if raw in {'adsorption_energy_ev', 'adsorption_energy'} and species:
                species_field = str(species).lower() + '_adsorption_energy'
                if species_field in DEFINITIONS:
                    field = species_field
            if raw in {'bader_charge_transfer_e', 'bader_charge_transfer'} and species == 'Li2S':
                field = 'li2s_bader_charge_transfer'
            if field:
                candidates[field].append(record)
        fields = {key: merge(cells, key) for key, cells in candidates.items()}
        # A maximum is defined only when both explicitly indexed bonds are
        # recorded in this same row, never from another material/configuration.
        bonds = [fields.get(key) for key in ('li1_s_bond_length', 'li2_s_bond_length')]
        if all(bonds) and 'li_s_bond_max' not in fields:
            cell = dict(bonds[0], field_name='li_s_bond_max')
            reason = next((c['exclusion_reason'] for c in bonds if c['exclusion_reason']), None)
            cell.update(source_record_ids=list(dict.fromkeys(i for c in bonds for i in source_ids(c))),
                        derivation='max(Li1–S, Li2–S); same rebuild row',
                        exploratory_eligible=reason is None, exclusion_reason=reason)
            cell['target'] = {'normalized_value': max(c['target']['normalized_value'] for c in bonds) if reason is None else None,
                              'normalized_unit': 'Å', 'normalization_status': reason or 'derived'}
            fields['li_s_bond_max'] = cell
        projected[identity] = fields
    return projected
