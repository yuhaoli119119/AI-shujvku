import pytest
from unittest.mock import MagicMock
from app.services.evidence_service import EvidenceService

def test_derived_claims_invalid_target_id():
    mock_session = MagicMock()
    service = EvidenceService(mock_session)
    
    # Should safely return an empty list without crashing on UUID parse error
    results = service._derived_claims(paper_id=None, target_type="dft_result", target_id="not-a-uuid")
    assert results == []
    
    # Verify no query was executed on the session
    mock_session.scalars.assert_not_called()
