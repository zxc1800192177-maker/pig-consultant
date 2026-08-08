"""架構守衛:core/ 必須維持純計算,不得碰 AI、網路、子行程、檔案系統以外的世界。

憲法第二條要求確定性計算與 AI 生成分離。這個規則若只寫在文件裡,
幾次改動後就會被繞過。這個測試讓它變成會失敗的建置條件。
"""

import ast
import pathlib

import pytest

CORE_DIR = pathlib.Path(__file__).parent.parent / "core"

# core/ 底下禁止出現的 import。碰到這些代表計算層開始有副作用。
FORBIDDEN_MODULES = {
    "subprocess",   # 呼叫 CLI
    "socket",
    "urllib",
    "http",
    "requests",
    "asyncio",
    "threading",
    "multiprocessing",
    "random",       # 破壞確定性
    "time",         # 破壞確定性(同樣輸入不同輸出)
    "datetime",     # 同上
}

FORBIDDEN_PREFIXES = ("ai",)  # core 不得反向依賴 ai 層


def _core_modules():
    return sorted(CORE_DIR.glob("*.py"))


def _imported_names(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                yield node.module


def test_core_directory_exists():
    assert CORE_DIR.is_dir(), "core/ 目錄不存在"


@pytest.mark.parametrize("path", _core_modules(), ids=lambda p: p.name)
def test_no_forbidden_imports(path):
    for name in _imported_names(path):
        root = name.split(".")[0]
        assert root not in FORBIDDEN_MODULES, (
            f"{path.name} import 了 {name} —— core/ 必須維持純計算(憲法第二條)"
        )
        assert not name.startswith(FORBIDDEN_PREFIXES), (
            f"{path.name} import 了 {name} —— 計算層不得依賴 AI 層,資料只能單向流動"
        )


def test_at_least_one_core_module_scanned():
    """避免 glob 抓不到檔案時,上面的測試靜默通過。"""
    modules = [p for p in _core_modules() if p.name != "__init__.py"]
    assert modules, "core/ 底下沒有掃到任何模組,守衛形同虛設"


# --- SRP 守衛:鎖住模組間的依賴方向 ---
#
# 每個模組只列出「它可以依賴誰」。沒列的就是不准。
# 重點是資料層不得依賴呈現層 —— 否則改一句顯示文字會牽動資料存取,
# 那正是我們拆開它們要避免的事。
ALLOWED_DEPENDENCIES = {
    "coercion": set(),                        # 最底層,不依賴任何 core 模組
    "grading": set(),                         # 純演算法
    "benchmark": set(),                       # 資料存取,不得依賴 labels
    "reportable": set(),                      # 法定傳染病偵測,自帶資料來源
    "metrics": {"coercion", "benchmark"},     # 驗證規則
    "diagnosis": {"benchmark", "grading"},    # 排序邏輯
    "labels": {"benchmark", "grading"},       # 呈現層,可依賴下層
}


@pytest.mark.parametrize("path", _core_modules(), ids=lambda p: p.name)
def test_dependency_direction(path):
    module = path.stem
    if module == "__init__":
        return
    allowed = ALLOWED_DEPENDENCIES.get(module)
    assert allowed is not None, (
        f"新模組 {module} 尚未在 ALLOWED_DEPENDENCIES 宣告可依賴的對象 —— "
        f"請先想清楚它的職責與所在層級"
    )
    for name in _imported_names(path):
        if not name.startswith("core."):
            continue
        target = name.split(".")[1]
        assert target in allowed, (
            f"{module} 不該依賴 {target}。"
            f"目前允許:{sorted(allowed) or '無'}"
        )


def test_data_layer_does_not_depend_on_presentation():
    """單獨點名這條:benchmark 反過來 import labels 是最容易犯的錯。"""
    assert "labels" not in ALLOWED_DEPENDENCIES["benchmark"]
    benchmark = CORE_DIR / "benchmark.py"
    assert "labels" not in [n.split(".")[-1] for n in _imported_names(benchmark)]
