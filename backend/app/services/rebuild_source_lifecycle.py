"""Current-evidence projection. Physical source rows and original candidates survive."""
from __future__ import annotations


def inactive_source_ids(value):
    return {entry['source_id'] for entry in (value.conflict or {}).get('source_lifecycle', [])
            if entry.get('action') == 'deactivate'}


def source_is_current(value, source_id):
    return str(source_id) not in inactive_source_ids(value)


def current_sources(value, sources):
    inactive = inactive_source_ids(value)
    return [s for s in sources if str(s.id) not in inactive]


def current_conflict(value):
    # Project candidate associations only; never rewrite saved scientific history
    # or clear an unresolved scientific conflict as a side effect of retirement.
    conflict = dict(value.conflict or {})
    inactive = inactive_source_ids(value)
    if inactive and 'candidates' in conflict:
        candidates = []
        for original in conflict['candidates']:
            candidate = dict(original)
            ids = candidate.get('source_ids', [])
            candidate['source_ids'] = [sid for sid in ids if sid not in inactive]
            if not ids or candidate['source_ids']:
                candidates.append(candidate)
        conflict['candidates'] = candidates
    return conflict
