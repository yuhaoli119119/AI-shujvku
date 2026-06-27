from __future__ import annotations


DFT_REVIEW_FIELD_ALIASES = {
    "property_type": "energy_type",
    "energy": "energy_type",
    "energy_type": "energy_type",
    "unit": "unit",
    "energy_value": "value",
    "adsorbate": "adsorbate",
    "reaction_step": "reaction_step",
    "catalyst": "catalyst",
    "value": "value",
}

DFT_CORRECTION_FIELD_ALIASES = {
    "catalyst": "catalyst_sample_id",
    "catalyst_id": "catalyst_sample_id",
    "catalyst_sample": "catalyst_sample_id",
    "catalyst_sample_id": "catalyst_sample_id",
    "material_binding": "catalyst_sample_id",
    "structure_binding": "catalyst_sample_id",
    "energy_type": "property_type",
    "property_type": "property_type",
    "energy": "property_type",
    "value": "value",
    "energy_value": "value",
    "unit": "unit",
    "adsorbate": "adsorbate",
    "reaction_step": "reaction_step",
    "source_section": "source_section",
    "source_figure": "source_figure",
    "evidence_text": "evidence_text",
    "confidence": "confidence",
}
