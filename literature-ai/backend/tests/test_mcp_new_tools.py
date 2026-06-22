"""Tests for the 4 new MCP tools: retrieve_evidence, review_paper, import_analysis, compare_papers."""

from __future__ import annotations

import os

import json
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.services.external_analysis_service import ExternalAnalysisNormalizedModel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_settings():
    """Create a mock Settings object with the required attributes."""
    settings = MagicMock()
    settings.database_url = os.environ["LITAI_TEST_DATABASE_URL"]
    settings.embedding_dimension = 64
    return settings


def _make_paper_detail(paper_id=None, title="Test Paper", year=2024):
    """Create a mock PaperDetailResponse."""
    detail = MagicMock()
    detail.id = paper_id or uuid4()
    detail.title = title
    detail.doi = "10.1234/test"
    detail.year = year
    detail.journal = "Test Journal"
    detail.authors = ["Author A"]
    detail.abstract = "Test abstract"
    detail.oa_status = None
    detail.comprehensive_analysis = {"paper_type": "A1"}
    detail.counts = MagicMock()
    detail.counts.model_dump = MagicMock(return_value={"sections": 2})

    # Create mock items with model_dump
    for attr in ("sections", "dft_settings_items", "catalyst_samples_items",
                 "dft_results_items", "electrochemical_performance_items",
                 "mechanism_claims_items", "writing_cards_items",
                 "references", "outgoing_relationships", "incoming_relationships",
                 "figure_data_points_items"):
        items = []
        for i in range(2):
            item = MagicMock()
            item.model_dump = MagicMock(return_value={"field": f"value_{i}"})
            items.append(item)
        setattr(detail, attr, items)

    return detail


# ---------------------------------------------------------------------------
# retrieve_evidence
# ---------------------------------------------------------------------------

class TestRetrieveEvidence:
    @patch("app.mcp.server.session_scope")
    @patch("app.mcp.server.get_settings")
    @patch("app.mcp.server.require_mcp_capability")
    @patch("app.mcp.server.Retriever")
    def test_basic_retrieve(self, MockRetriever, mock_auth, mock_settings, mock_session_scope):
        from app.mcp.server import retrieve_evidence

        mock_auth.return_value = MagicMock()
        mock_settings.return_value = _make_mock_settings()

        # Mock session context manager
        mock_session = MagicMock()
        mock_session_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_scope.return_value.__exit__ = MagicMock(return_value=False)

        # Mock retriever results
        mock_item = MagicMock()
        mock_item.model_dump = MagicMock(return_value={"text": "test evidence", "score": 0.9})
        mock_retriever_instance = MagicMock()
        mock_retriever_instance.retrieve.return_value = {
            "sections": [mock_item],
            "dft_results": [mock_item],
        }
        MockRetriever.return_value = mock_retriever_instance

        result = retrieve_evidence(query="oxygen reduction reaction catalyst")

        assert "results" in result
        assert "sections" in result["results"]
        assert "dft_results" in result["results"]
        mock_retriever_instance.retrieve.assert_called_once_with(
            query="oxygen reduction reaction catalyst",
            paper_ids=None,
            limit_per_type=5,
            target_paper_type=None,
        )

    @patch("app.mcp.server.session_scope")
    @patch("app.mcp.server.get_settings")
    @patch("app.mcp.server.require_mcp_capability")
    @patch("app.mcp.server.Retriever")
    def test_evidence_types_filter(self, MockRetriever, mock_auth, mock_settings, mock_session_scope):
        from app.mcp.server import retrieve_evidence

        mock_auth.return_value = MagicMock()
        mock_settings.return_value = _make_mock_settings()

        mock_session = MagicMock()
        mock_session_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_scope.return_value.__exit__ = MagicMock(return_value=False)

        mock_item = MagicMock()
        mock_item.model_dump = MagicMock(return_value={"text": "test"})
        mock_retriever_instance = MagicMock()
        mock_retriever_instance.retrieve.return_value = {
            "sections": [mock_item],
            "dft_results": [mock_item],
            "mechanism_claims": [mock_item],
        }
        MockRetriever.return_value = mock_retriever_instance

        result = retrieve_evidence(
            query="test",
            evidence_types=["dft_results", "mechanism_claims"],
        )

        assert "evidence_types_requested" in result
        assert "dft_results" in result["results"]
        assert "mechanism_claims" in result["results"]
        assert "sections" not in result["results"]


# ---------------------------------------------------------------------------
# review_paper
# ---------------------------------------------------------------------------

class TestReviewPaper:
    @patch("app.mcp.server.require_mcp_capability")
    @pytest.mark.asyncio
    async def test_review_paper_is_disabled(self, mock_auth):
        from app.mcp.server import review_paper

        mock_auth.return_value = MagicMock()

        with pytest.raises(ValueError, match="Backend-owned LLM review is disabled"):
            await review_paper(paper_id=str(uuid4()))


# ---------------------------------------------------------------------------
# propose_correction
# ---------------------------------------------------------------------------

class TestProposeCorrection:
    @patch("app.mcp.server._serialize_correction")
    @patch("app.mcp.server.ReviewService")
    @patch("app.mcp.server.session_scope")
    @patch("app.mcp.server.get_settings")
    @patch("app.mcp.server.require_mcp_capability")
    def test_non_dft_is_applied_immediately(
        self,
        mock_auth,
        mock_settings,
        mock_session_scope,
        MockReviewService,
        mock_serialize,
    ):
        from app.mcp.server import propose_correction

        mock_auth.return_value.source_prefix = "codex"
        mock_settings.return_value = _make_mock_settings()
        mock_session = MagicMock()
        mock_session_scope.return_value.__enter__.return_value = mock_session
        approved = MagicMock()
        approved.status = "approved"
        MockReviewService.return_value.approve_correction.return_value = approved
        mock_serialize.return_value = {"status": "approved"}

        result = propose_correction(
            paper_id=str(uuid4()),
            field_name="figures",
            target_path=f"figures:{uuid4()}:caption",
            operation="replace",
            proposed_value="Correct caption",
            reason="Checked against PDF.",
        )

        assert result["status"] == "approved"
        MockReviewService.return_value.approve_correction.assert_called_once()

    @patch("app.mcp.server._serialize_correction")
    @patch("app.mcp.server.ReviewService")
    @patch("app.mcp.server.session_scope")
    @patch("app.mcp.server.get_settings")
    @patch("app.mcp.server.require_mcp_capability")
    def test_dft_stays_pending(
        self,
        mock_auth,
        mock_settings,
        mock_session_scope,
        MockReviewService,
        mock_serialize,
    ):
        from app.mcp.server import propose_correction

        mock_auth.return_value.source_prefix = "codex"
        mock_settings.return_value = _make_mock_settings()
        mock_session = MagicMock()
        mock_session_scope.return_value.__enter__.return_value = mock_session
        mock_serialize.return_value = {"status": "pending"}

        result = propose_correction(
            paper_id=str(uuid4()),
            field_name="dft_results",
            target_path=f"dft_results:{uuid4()}:value",
            operation="replace",
            proposed_value=-1.2,
            reason="DFT evidence review.",
        )

        assert result["status"] == "pending"
        MockReviewService.return_value.approve_correction.assert_not_called()


# ---------------------------------------------------------------------------
# import_analysis
# ---------------------------------------------------------------------------

class TestImportAnalysis:
    @patch("app.mcp.server.session_scope")
    @patch("app.mcp.server.get_settings")
    @patch("app.mcp.server.require_mcp_capability")
    @patch("app.mcp.server.ExternalAnalysisService")
    @patch("app.mcp.server.VerificationSessionService")
    def test_import_analysis_with_text(self, MockVerificationSessionService, MockService, mock_auth, mock_settings, mock_session_scope):
        from app.mcp.server import import_analysis

        mock_auth.return_value = MagicMock()
        mock_settings.return_value = _make_mock_settings()

        mock_session = MagicMock()
        mock_session_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_scope.return_value.__exit__ = MagicMock(return_value=False)

        paper_id = str(uuid4())
        mock_service_instance = MagicMock()
        mock_run = MagicMock()
        mock_run.id = uuid4()
        mock_run.mapping_status = "normalized_with_llm"
        mock_run.mapping_error = None
        mock_service_instance.import_run.return_value = mock_run
        mock_service_instance.list_candidates.return_value = []
        MockService.return_value = mock_service_instance
        MockVerificationSessionService.return_value.apply_import_rules_for_paper.return_value = {"paper_id": paper_id}

        result = import_analysis(
            paper_id=paper_id,
            source="cursor",
            source_label="Cursor Agent",
            raw_text="This paper has incorrect catalyst composition data.",
        )

        assert result["mapping_status"] == "normalized_with_llm"
        assert result["candidate_count"] == 0
        assert result["auto_apply_review_rules"] is True
        mock_service_instance.import_run.assert_called_once()
        MockVerificationSessionService.return_value.apply_import_rules_for_paper.assert_called_once()
        assert (
            MockVerificationSessionService.return_value.apply_import_rules_for_paper.call_args.kwargs["candidate_run_id"]
            == mock_run.id
        )
        mock_session.commit.assert_called_once()

    @patch("app.mcp.server.session_scope")
    @patch("app.mcp.server.get_settings")
    @patch("app.mcp.server.require_mcp_capability")
    @patch("app.mcp.server.ExternalAnalysisService")
    @patch("app.mcp.server.VerificationSessionService")
    def test_import_analysis_with_payload(self, MockVerificationSessionService, MockService, mock_auth, mock_settings, mock_session_scope):
        from app.mcp.server import import_analysis

        mock_auth.return_value = MagicMock()
        mock_settings.return_value = _make_mock_settings()

        mock_session = MagicMock()
        mock_session_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_scope.return_value.__exit__ = MagicMock(return_value=False)

        paper_id = str(uuid4())
        candidate = MagicMock()
        candidate.id = uuid4()
        candidate.candidate_type = "note"
        candidate.confidence = 0.8
        candidate.status = "pending"
        candidate.normalized_payload = {"content": "Test note"}

        mock_service_instance = MagicMock()
        mock_run = MagicMock()
        mock_run.id = uuid4()
        mock_run.mapping_status = "normalized"
        mock_run.mapping_error = None
        mock_service_instance.import_run.return_value = mock_run
        mock_service_instance.list_candidates.return_value = [candidate]
        MockService.return_value = mock_service_instance
        MockVerificationSessionService.return_value.apply_import_rules_for_paper.return_value = {"paper_id": paper_id}

        result = import_analysis(
            paper_id=paper_id,
            source="deepseek-chat",
            raw_payload={"review_notes": [{"content": "Test note"}]},
        )

        assert result["candidate_count"] == 1
        assert result["candidates"][0]["type"] == "note"
        MockVerificationSessionService.return_value.apply_import_rules_for_paper.assert_called_once()
        assert (
            MockVerificationSessionService.return_value.apply_import_rules_for_paper.call_args.kwargs["candidate_run_id"]
            == mock_run.id
        )


# ---------------------------------------------------------------------------
# compare_papers
# ---------------------------------------------------------------------------

class TestComparePapers:
    @patch("app.mcp.server.session_scope")
    @patch("app.mcp.server.get_settings")
    @patch("app.mcp.server.require_mcp_capability")
    @patch("app.mcp.server.PaperQueryService")
    def test_compare_two_papers(self, MockQueryService, mock_auth, mock_settings, mock_session_scope):
        from app.mcp.server import compare_papers

        mock_auth.return_value = MagicMock()
        mock_settings.return_value = _make_mock_settings()

        mock_session = MagicMock()
        mock_session_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_scope.return_value.__exit__ = MagicMock(return_value=False)

        pid1, pid2 = str(uuid4()), str(uuid4())
        detail1 = _make_paper_detail(paper_id=pid1, title="Paper 1", year=2023)
        detail2 = _make_paper_detail(paper_id=pid2, title="Paper 2", year=2024)

        mock_qs = MagicMock()
        mock_qs.get_paper_detail.side_effect = [detail1, detail2]
        MockQueryService.return_value = mock_qs

        result = compare_papers(paper_ids=[pid1, pid2])

        assert result["paper_count"] == 2
        assert len(result["papers"]) == 2
        assert "dft_settings" in result["papers"][0]

    @patch("app.mcp.server.session_scope")
    @patch("app.mcp.server.get_settings")
    @patch("app.mcp.server.require_mcp_capability")
    @patch("app.mcp.server.PaperQueryService")
    def test_compare_with_field_filter(self, MockQueryService, mock_auth, mock_settings, mock_session_scope):
        from app.mcp.server import compare_papers

        mock_auth.return_value = MagicMock()
        mock_settings.return_value = _make_mock_settings()

        mock_session = MagicMock()
        mock_session_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_scope.return_value.__exit__ = MagicMock(return_value=False)

        pid1, pid2 = str(uuid4()), str(uuid4())
        detail1 = _make_paper_detail(paper_id=pid1, title="Paper 1")
        detail2 = _make_paper_detail(paper_id=pid2, title="Paper 2")

        mock_qs = MagicMock()
        mock_qs.get_paper_detail.side_effect = [detail1, detail2]
        MockQueryService.return_value = mock_qs

        result = compare_papers(
            paper_ids=[pid1, pid2],
            fields=["dft_results", "mechanism_claims"],
        )

        assert result["compared_fields"] == ["dft_results", "mechanism_claims"]
        assert "dft_settings" not in result["papers"][0]
        assert "dft_results" in result["papers"][0]

    @patch("app.mcp.server.require_mcp_capability")
    def test_compare_invalid_count(self, mock_auth):
        from app.mcp.server import compare_papers

        mock_auth.return_value = MagicMock()

        with pytest.raises(ValueError, match="2-10 papers"):
            compare_papers(paper_ids=[str(uuid4())])

        with pytest.raises(ValueError, match="2-10 papers"):
            compare_papers(paper_ids=[str(uuid4())] * 11)
