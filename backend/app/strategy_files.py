from __future__ import annotations

import subprocess
import re
import ast
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .config import settings  # Kept as the public strategy-files configuration hook.
from .git_versions import strategy_repo, sync_strategy_file
from .schemas import GitCommitCreate, StrategyFileCreate, StrategyFileMetadataUpdate, StrategyFileUpdate

router = APIRouter(prefix="/api/strategy-files", tags=["strategy-files"])
STRATEGY_DIR = Path(__file__).resolve().parent / "strategies"


def _path(name: str) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name) or name == "__init__":
        raise HTTPException(400, "文件名只能使用小写字母、数字和下划线")
    path = (STRATEGY_DIR / f"{name}.py").resolve()
    if path.parent != STRATEGY_DIR.resolve():
        raise HTTPException(400, "非法策略文件路径")
    return path


def _git(*args: str) -> str:
    repo = strategy_repo()
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise HTTPException(400, result.stderr.strip() or "Git 操作失败")
    return result.stdout.strip()


def _template(name: str, mode: str, description: str = "请填写策略说明", category: str = "自定义") -> str:
    class_name = "".join(part.capitalize() for part in name.split("_"))
    if mode == "PORTFOLIO":
        fields = "    instrument_ids: list[InstrumentId]\n    bar_types: list[BarType]\n"
        start = "        for bar_type in self.config.bar_types:\n            self.subscribe_bars(bar_type)"
        mode_value = "StrategyMode.PORTFOLIO"
        imports = "from nautilus_trader.model.data import BarType\nfrom nautilus_trader.model.identifiers import InstrumentId"
    else:
        fields = "    instrument_id: InstrumentId\n    bar_type: BarType\n"
        start = "        self.subscribe_bars(self.config.bar_type)"
        mode_value = "StrategyMode.SINGLE_INSTRUMENT"
        imports = "from nautilus_trader.model.data import BarType\nfrom nautilus_trader.model.identifiers import InstrumentId"
    return f'''from decimal import Decimal
import pandas as pd

from nautilus_trader.config import StrategyConfig
{imports}
from nautilus_trader.trading.strategy import Strategy

from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode


class {class_name}Config(StrategyConfig, frozen=True):
{fields}    trade_size: Decimal = Decimal("0.001")


class {class_name}Strategy(Strategy):
    def __init__(self, config: {class_name}Config) -> None:
        super().__init__(config)

    def on_start(self) -> None:
{start}

    # 在这里实现 on_bar、下单和风控逻辑


def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    # 所有 plot_config 引用的列都必须在这里计算。
    dataframe["ema_20"] = pd.to_numeric(dataframe["close"]).ewm(span=20, adjust=False).mean()
    return dataframe


STRATEGY_MANIFEST = StrategyManifest(
    slug="{name.replace('_', '-')}",
    name="{class_name}",
    version="0.1.0",
    description={description!r},
    category={category!r},
    strategy_path="app.strategies.{name}:{class_name}Strategy",
    config_path="app.strategies.{name}:{class_name}Config",
    parameters={{
        "trade_size": ParameterSpec("下单数量", "number", 0.001, 0.000001, 1000),
    }},
    timeframes=("1h",),
    primary_timeframe="1h",
    plot_config={{
        "main_plot": {{
            "ema_20": {{"name": "EMA 20", "type": "line", "color": "#43a5ff"}},
        }},
        "subplots": {{}},
    }},
    mode={mode_value},
)
'''


def _file_out(path: Path, include_content: bool = False) -> dict:
    relative = sync_strategy_file(path.stem, path)
    status = _git("status", "--porcelain", "--", relative)
    source = path.read_text(encoding="utf-8")
    def manifest_text(field: str) -> str | None:
        match = re.search(rf"^\s*{field}\s*=\s*(.+?),?\s*$", source, re.MULTILINE)
        if not match:
            return None
        try:
            value = ast.literal_eval(match.group(1))
            return value if isinstance(value, str) else None
        except (ValueError, SyntaxError):
            return None
    stat = path.stat()
    result = {"name": path.stem, "filename": path.name, "module": f"app.strategies.{path.stem}", "git_status": status[:2].strip() or "CLEAN", "draft_description": manifest_text("description"), "draft_category": manifest_text("category"), "created_at": getattr(stat, "st_birthtime", stat.st_ctime), "updated_at": stat.st_mtime}
    if include_content:
        result["content"] = source
    return result


@router.get("")
def list_files():
    return [_file_out(path) for path in sorted(STRATEGY_DIR.glob("*.py")) if path.name != "__init__.py"]


@router.get("/git/status")
def git_status():
    for path in STRATEGY_DIR.glob("*.py"):
        sync_strategy_file(path.stem, path)
    status = _git("status", "--porcelain", "--", "backend/app/strategies")
    return {"dirty": bool(status), "files": status.splitlines(), "head": _git("rev-parse", "HEAD"), "branch": _git("branch", "--show-current")}


@router.post("/git/commit")
def commit_files(data: GitCommitCreate):
    _git("add", "--", "backend/app/strategies")
    if not _git("diff", "--cached", "--name-only", "--", "backend/app/strategies"):
        raise HTTPException(409, "策略目录没有需要提交的修改")
    _git("commit", "-m", data.message, "--", "backend/app/strategies")
    return {"commit": _git("rev-parse", "HEAD"), "message": data.message}


@router.post("")
def create_file(data: StrategyFileCreate):
    path = _path(data.name)
    if path.exists():
        raise HTTPException(409, "策略文件已经存在")
    path.write_text(_template(data.name, data.mode, data.description, data.category), encoding="utf-8")
    return _file_out(path, include_content=True)


@router.get("/{name}")
def get_file(name: str):
    path = _path(name)
    if not path.exists():
        raise HTTPException(404, "策略文件不存在")
    return _file_out(path, include_content=True)


@router.put("/{name}")
def update_file(name: str, data: StrategyFileUpdate):
    path = _path(name)
    if not path.exists():
        raise HTTPException(404, "策略文件不存在")
    try:
        compile(data.content, path.name, "exec")
    except SyntaxError as exc:
        raise HTTPException(400, f"第 {exc.lineno} 行语法错误：{exc.msg}") from exc
    path.write_text(data.content, encoding="utf-8")
    return _file_out(path, include_content=True)


@router.patch("/{name}/metadata")
def update_file_metadata(name: str, data: StrategyFileMetadataUpdate):
    path = _path(name)
    if not path.exists():
        raise HTTPException(404, "策略文件不存在")
    source = path.read_text(encoding="utf-8")
    for field, value in (("description", data.description), ("category", data.category)):
        pattern = rf"({field}\s*=\s*)(['\"])(.*?)(\2)"
        source, count = re.subn(pattern, lambda match: f"{match.group(1)}{value!r}", source, count=1)
        if count != 1:
            raise HTTPException(422, f"STRATEGY_MANIFEST 缺少 {field} 字段")
    try:
        compile(source, path.name, "exec")
    except SyntaxError as exc:
        raise HTTPException(422, f"更新 Manifest 后语法错误：{exc.msg}") from exc
    path.write_text(source, encoding="utf-8")
    return _file_out(path, include_content=True)


@router.delete("/{name}", status_code=204)
def delete_file(name: str):
    path = _path(name)
    if not path.exists():
        raise HTTPException(404, "策略文件不存在")
    path.unlink()
    sync_strategy_file(name)
