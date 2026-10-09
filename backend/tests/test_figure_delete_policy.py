from app.utils.figure_delete_policy import direct_delete_eligibility, normalized_figure_identity


def test_figure_and_scheme_with_same_number_are_not_duplicates():
    figure = {"figure_label": "fig_1", "caption": "Figure 1. Model architecture."}
    scheme = {"figure_label": "scheme_1", "caption": "Scheme 1. Screening workflow."}

    assert normalized_figure_identity(figure) == "figure:1"
    assert normalized_figure_identity(scheme) == "scheme:1"
    assert normalized_figure_identity(figure) != normalized_figure_identity(scheme)
    assert direct_delete_eligibility(figure, duplicate_group_size=1) == (False, None)


def test_same_figure_number_still_forms_a_duplicate_identity():
    first = {"figure_label": "fig_2", "caption": "Figure 2. Result plot."}
    second = {"figure_label": "figure_2", "caption": "Figure 2. Duplicate crop."}

    assert normalized_figure_identity(first) == normalized_figure_identity(second) == "figure:2"
