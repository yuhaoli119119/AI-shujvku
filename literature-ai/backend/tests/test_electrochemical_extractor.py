from types import SimpleNamespace

from app.extractors.electrochemical_performance_extractor import ElectrochemicalPerformanceExtractor


def test_extracts_core_electrochemical_fields_from_section():
    extractor = ElectrochemicalPerformanceExtractor()
    document = {
        "sections": [
            SimpleNamespace(
                text=(
                    "The sulfur loading was 4.5 mg/cm2 with sulfur content of 60 wt%. "
                    "The E/S ratio was maintained at 15 uL/mg. "
                    "The cell delivered 1200 mAh/g at 0.5C. "
                    "After 500 cycles, the decay rate was 0.03% per cycle."
                ),
                section_title="Electrochemical Performance",
                page_start=3,
            )
        ],
        "tables": [],
        "figures": [],
        "abstract": "",
    }

    results = extractor.extract(document)
    fields = {item["field_name"] for item in results}

    assert "sulfur_loading" in fields
    assert "sulfur_content" in fields
    assert "electrolyte_sulfur_ratio" in fields
    assert "capacity" in fields
    assert "rate" in fields
    assert "cycle_number" in fields
    assert "decay_per_cycle" in fields


def test_accepts_dict_list_and_object_inputs():
    extractor = ElectrochemicalPerformanceExtractor()
    dict_results = extractor.extract({"abstract": "The cell delivered 800 mAh/g at 0.2C.", "sections": [], "tables": [], "figures": []})
    list_results = extractor.extract([SimpleNamespace(text="The sulfur loading was 3.2 mg/cm2.", section_title="Results", page_start=2)])
    object_results = extractor.extract(SimpleNamespace(abstract="The E/S ratio was 10 uL/mg.", sections=[], tables=[], figures=[]))

    assert any(item["field_name"] == "capacity" for item in dict_results)
    assert any(item["field_name"] == "sulfur_loading" for item in list_results)
    assert any(item["field_name"] == "electrolyte_sulfur_ratio" for item in object_results)


def test_rate_pattern_does_not_confuse_cycle_number():
    extractor = ElectrochemicalPerformanceExtractor()
    results = extractor.extract(
        {
            "abstract": "After 200 cycles at 1C, the capacity faded to 400 mAh/g.",
            "sections": [],
            "tables": [],
            "figures": [],
        }
    )

    rates = [item["rate"] for item in results if item["field_name"] == "rate"]
    cycles = [item["cycle_number"] for item in results if item["field_name"] == "cycle_number"]
    assert rates == ["1C"]
    assert cycles == [200]
