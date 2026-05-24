import os

import fitz
from sqlmodel import Session, delete, select

from ..core.models import Chunk


class PDFParser:
    def __init__(self, session: Session):
        self.session = session

    def parse_to_chunks(self, paper_id: str, file_id: str, file_path: str):
        if not os.path.exists(file_path):
            raise FileNotFoundError(file_path)

        self.session.exec(delete(Chunk).where(Chunk.file_id == file_id))
        self.session.commit()

        chunk_count = 0
        with fitz.open(file_path) as document:
            for page_index, page in enumerate(document):
                text = page.get_text("text")
                if not text.strip():
                    continue

                paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
                if not paragraphs:
                    paragraphs = [text.strip()]

                for local_index, paragraph in enumerate(paragraphs):
                    chunk = Chunk(
                        paper_id=paper_id,
                        file_id=file_id,
                        section_title=f"Page {page_index + 1}",
                        page_start=page_index + 1,
                        page_end=page_index + 1,
                        chunk_index=(page_index * 1000) + local_index,
                        text=paragraph,
                        token_count=max(1, len(paragraph) // 4),
                    )
                    self.session.add(chunk)
                    chunk_count += 1

        self.session.commit()
        return chunk_count

    def get_chunk_count(self, paper_id: str) -> int:
        return len(self.session.exec(select(Chunk).where(Chunk.paper_id == paper_id)).all())
