-- downgrade_paper_type.sql
-- 从 papers 表中移除 paper_type/type_confidence/classification_source 列
-- 注意：SQLite 在 3.35.0 版本之后才支持 DROP COLUMN 语法

-- 方案 A: 适用于 SQLite 3.35.0+ 及 PostgreSQL
DROP INDEX IF EXISTS ix_papers_paper_type;
DROP INDEX IF EXISTS ix_papers_type_confidence;

ALTER TABLE papers DROP COLUMN paper_type;
ALTER TABLE papers DROP COLUMN type_confidence;
ALTER TABLE papers DROP COLUMN classification_source;

-- 方案 B: 如果执行上面的 ALTER TABLE 报错 (如 SQLite 版本太低)
-- 请手动使用以下步骤重建表:
-- 1. 备份原表
-- CREATE TABLE papers_backup AS SELECT id, library_name, doi, title, year, journal, authors, abstract, pdf_path, source_path, oa_status, license, tei_path, docling_json_path, markdown_path, serial_number, comprehensive_analysis, created_at FROM papers;
-- 2. 删原表
-- DROP TABLE papers;
-- 3. 改名并重建索引
-- ALTER TABLE papers_backup RENAME TO papers;
-- (再重新执行原本定义的 CREATE INDEX)
