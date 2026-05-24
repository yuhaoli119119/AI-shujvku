import json
import os
import sqlite3
from typing import Optional

from sqlmodel import SQLModel, Session, create_engine, func, select

from . import models  # noqa: F401
from .paper_naming import sanitize_filename_component


class ProjectManager:
    def __init__(self):
        self.current_project_path: Optional[str] = None
        self.current_project_name: Optional[str] = None
        self.engine = None

    def _dispose_engine(self):
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None

    def create_project(self, name: str, base_path: str) -> str:
        project_dir = os.path.join(base_path, name)

        subdirs = [
            "papers/pdf",
            "papers/text",
            "papers/tables",
            "papers/tei",
            "exports",
            "logs",
            "config",
        ]
        for subdir in subdirs:
            os.makedirs(os.path.join(project_dir, subdir), exist_ok=True)

        config = {
            "project_name": name,
            "created_at": str(os.path.getctime(project_dir)),
            "last_opened": str(os.path.getmtime(project_dir)),
        }
        with open(os.path.join(project_dir, "config/project_config.json"), "w", encoding="utf-8") as file:
            json.dump(config, file, indent=4, ensure_ascii=False)

        self.init_database(project_dir)
        self.current_project_path = project_dir
        self.current_project_name = name
        return project_dir

    def init_database(self, project_dir: str):
        self._dispose_engine()
        db_path = os.path.join(project_dir, "database.sqlite")
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False}
        )
        SQLModel.metadata.create_all(self.engine)
        print(f"数据库初始化完成: {db_path}")

    def load_project(self, project_dir: str):
        config_path = os.path.join(project_dir, "config/project_config.json")
        if not os.path.exists(config_path):
            raise FileNotFoundError("未找到项目配置文件")

        with open(config_path, "r", encoding="utf-8") as file:
            config = json.load(file)

        self.current_project_name = config.get("project_name")
        self.current_project_path = project_dir

        db_path = os.path.join(project_dir, "database.sqlite")
        self._dispose_engine()
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False}
        )
        SQLModel.metadata.create_all(self.engine)
        self._migrate_database(db_path)
        return config

    def _migrate_database(self, db_path: str):
        try:
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("PRAGMA table_info(papers)")
                columns = [info[1] for info in cursor.fetchall()]

                expected_columns = {
                    "paper_number": "INTEGER",
                    "chinese_title": "TEXT",
                    "remote_paper_id": "TEXT",
                    "oa_url": "TEXT",
                    "oa_status": "TEXT",
                    "license": "TEXT",
                    "is_oa": "INTEGER DEFAULT 0",
                    "abstract": "TEXT",
                    "source": "TEXT",
                    "publisher": "TEXT",
                    "journal": "TEXT",
                    "impact_factor": "REAL",
                    "year": "INTEGER",
                }

                for col_name, col_type in expected_columns.items():
                    if col_name not in columns:
                        print(f"正在迁移数据库：papers 表添加列 {col_name}")
                        cursor.execute(f"ALTER TABLE papers ADD COLUMN {col_name} {col_type}")
                conn.commit()
        except Exception as exc:
            print(f"数据库迁移失败: {exc}")

    def get_session(self):
        if not self.engine:
            raise RuntimeError("请先打开或创建一个项目")
        return Session(self.engine)

    @staticmethod
    def next_paper_number(session: Session) -> int:
        result = session.exec(select(func.max(models.Paper.paper_number))).one()
        return int(result or 0) + 1

    @staticmethod
    def build_pdf_filename(paper_number: int | None, title: str | None) -> str:
        prefix = f"{int(paper_number):03d}" if paper_number else "000"
        safe_title = sanitize_filename_component(title or "paper")
        return f"{prefix}_{safe_title}.pdf"

    def close(self):
        self._dispose_engine()
