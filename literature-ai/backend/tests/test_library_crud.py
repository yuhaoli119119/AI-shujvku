"""文件夹库功能验证测试 — 创建/激活/导入/移除/列表。"""

import json
import os
import shutil
import tempfile
from pathlib import Path

# 确保从 backend 目录运行
os.chdir(Path(__file__).parent)

from app.services.library_manager import LibraryManager, LibraryInfo


def test_library_manager():
    """完整验证 LibraryManager 的 CRUD 操作。"""
    # 使用临时目录避免污染真实数据
    original_registry = Path("data/library_registry.json")
    original_data = None
    if original_registry.exists():
        original_data = original_registry.read_text(encoding="utf-8")

    # 使用临时目录替代 data/
    tmp_dir = Path(tempfile.mkdtemp(prefix="litai_test_"))
    try:
        # 临时修改 LibraryManager 的注册表路径
        LibraryManager.REGISTRY_PATH = tmp_dir / "library_registry.json"
        LibraryManager.DEFAULT_LIBRARY_ROOT = tmp_dir / "libraries" / "default"

        mgr = LibraryManager()
        print("✓ LibraryManager 初始化成功")

        # 1. 列出库（应该有默认库）
        libs = mgr.list_libraries()
        assert len(libs) >= 1, f"应有默认库，但找到 {len(libs)} 个"
        default_lib = libs[0]
        assert default_lib.is_active, "默认库应为活跃状态"
        print(f"✓ 列出库: {len(libs)} 个，默认库 '{default_lib.name}' 为活跃")

        # 2. 创建新库
        test_lib = mgr.create_library(
            name="测试文献库",
            description="自动化测试用库",
        )
        assert test_lib.name == "测试文献库"
        assert test_lib.paper_count == 0
        assert not test_lib.is_active
        print(f"✓ 创建库: '{test_lib.name}', 路径: {test_lib.root_path}")

        # 验证目录结构已创建
        root = Path(test_lib.root_path)
        assert root.exists(), "库根目录应存在"
        assert (root / "database.sqlite").exists(), "数据库应存在"
        assert (root / "library.json").exists(), "元数据应存在"
        assert (root / "papers").exists(), "papers 存储目录应存在"
        for subdir in ("pdf", "text", "tei", "docling_json", "figures", "tables", "markdown"):
            assert (root / "papers" / subdir).exists(), f"papers/{subdir} 应存在"
        print("✓ 目录结构验证通过")

        # 3. 激活新库
        activated = mgr.activate_library("测试文献库")
        assert activated.is_active, "激活后应为活跃状态"
        libs_after = mgr.list_libraries()
        active_count = sum(1 for l in libs_after if l.is_active)
        assert active_count == 1, f"应只有1个活跃库，但有 {active_count} 个"
        print(f"✓ 激活库: '{activated.name}' → 活跃")

        # 4. 激活回默认库
        reactivated = mgr.activate_library("默认文献库")
        assert reactivated.is_active
        print("✓ 切换回默认库")

        # 5. 导入已有库
        import_dir = tmp_dir / "import_test"
        import_dir.mkdir(parents=True, exist_ok=True)
        # 创建一个模拟的已有库结构
        (import_dir / "papers").mkdir()
        (import_dir / "papers" / "pdf").mkdir()
        (import_dir / "config").mkdir()
        (import_dir / "config" / "project_config.json").write_text(
            json.dumps({"project_name": "导入测试库"}), encoding="utf-8"
        )
        (import_dir / "library.json").write_text(
            json.dumps({"name": "导入测试库", "description": "从外部导入", "storage_mode": "papers", "library_kind": "shared_project", "created_at": "2026-01-01T00:00:00"}), encoding="utf-8"
        )

        imported = mgr.import_library(str(import_dir))
        assert imported.name == "导入测试库"
        assert not imported.is_active
        print(f"✓ 导入库: '{imported.name}'")

        # 6. 移除导入的库（不删除文件）
        removed = mgr.unregister_library("导入测试库")
        assert removed.name == "导入测试库"
        assert import_dir.exists(), "移除后文件应保留"
        print("✓ 移除库: 文件保留")

        # 7. 移除测试库
        removed2 = mgr.unregister_library("测试文献库")
        assert removed2.name == "测试文献库"
        libs_final = mgr.list_libraries()
        assert len(libs_final) == 1, f"移除后应只剩默认库，但有 {len(libs_final)} 个"
        print("✓ 移除测试库成功")

        # 8. 不能移除默认库
        try:
            mgr.unregister_library("默认文献库")
            assert False, "应抛出异常"
        except ValueError as e:
            assert "不能移除" in str(e)
            print("✓ 默认库不可移除: 正确拦截")

        # 9. 重复创建同名库
        try:
            mgr.create_library(name="测试文献库")
            mgr.create_library(name="测试文献库")
            assert False, "应抛出异常"
        except ValueError as e:
            assert "已存在" in str(e)
            print("✓ 重复创建拦截")

        print("\n🎉 文件夹库所有验证通过！")

    finally:
        # 清理临时目录
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # 恢复原始注册表
        if original_data is not None:
            original_registry.write_text(original_data, encoding="utf-8")


if __name__ == "__main__":
    test_library_manager()
