from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, Paper, PaperChunk, PaperSection
from app.services.paper_chunking import split_text_into_chunks


def test_split_text_into_overlapping_chunks():
    text = " ".join(f"token{i}" for i in range(1200))

    chunks = split_text_into_chunks(text, target_tokens=500, overlap_tokens=100)

    assert len(chunks) == 3
    assert chunks[0].token_count == 500
    assert "token400" in chunks[1].text
    assert chunks[0].content_hash != chunks[1].content_hash


def test_paper_chunk_persists_1536_dimension_embedding_in_sqlite():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(f"sqlite:///{Path(tmpdir) / 'chunks.db'}", future=True)
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                paper = Paper(title="Chunk Paper", pdf_path="paper.pdf", authors=[])
                session.add(paper)
                session.flush()
                section = PaperSection(
                    paper_id=paper.id,
                    section_title="Results",
                    section_type="body",
                    text="A chunkable section.",
                    embedding=[0.0] * 1536,
                )
                session.add(section)
                session.flush()
                session.add(
                    PaperChunk(
                        paper_id=paper.id,
                        section_id=section.id,
                        chunk_index=0,
                        text="A chunkable section.",
                        token_count=3,
                        embedding=[0.001] * 1536,
                        embedding_model="text-embedding-3-small",
                        embedding_dimension=1536,
                        content_hash="hash",
                    )
                )
                session.commit()

                chunk = session.query(PaperChunk).one()

                assert chunk.embedding_dimension == 1536
                assert len(chunk.embedding or []) == 1536
        finally:
            engine.dispose()
