import ast
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .config import settings
from .git_versions import code_hash
from .schemas import StrategyFileCreate, StrategyFileMetadataUpdate, StrategyFileUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategy-files", tags=["strategy-files"])
STRATEGY_DIR = (Path(__file__).resolve().parent / "strategies").resolve()
PERSISTENT_STRATEGY_DIR = (settings.data_root / "strategies").resolve()


def ensure_strategy_storage() -> None:
    """Ensure persistent strategy directory exists and sync strategies two-way on startup/access."""
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    PERSISTENT_STRATEGY_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Sync from STRATEGY_DIR to PERSISTENT_STRATEGY_DIR (preserve newer strategies)
    for p in STRATEGY_DIR.glob("*.py"):
        if p.name == "__init__.py":
            continue
        dest = PERSISTENT_STRATEGY_DIR / p.name
        if not dest.exists() or p.stat().st_mtime >= dest.stat().st_mtime:
            try:
                shutil.copy2(p, dest)
            except Exception as e:
                logger.warning("同步策略到持久化目录失败 %s: %s", p.name, e)

    # 2. Restore from PERSISTENT_STRATEGY_DIR to STRATEGY_DIR if missing in STRATEGY_DIR
    for p in PERSISTENT_STRATEGY_DIR.glob("*.py"):
        if p.name == "__init__.py":
            continue
        dest = STRATEGY_DIR / p.name
        if not dest.exists():
            try:
                shutil.copy2(p, dest)
            except Exception as e:
                logger.warning("从持久化目录恢复策略失败 %s: %s", p.name, e)

    # 3. Recover any generated strategies from agent worktrees if missing or outdated
    worktrees_root = settings.data_root / "agent" / "worktrees"
    if worktrees_root.exists():
        for wt_file in worktrees_root.glob("*/backend/app/strategies/*.py"):
            if wt_file.name == "__init__.py":
                continue
            canonical_file = STRATEGY_DIR / wt_file.name
            persisted_file = PERSISTENT_STRATEGY_DIR / wt_file.name
            should_recover = False
            if not canonical_file.exists() or not persisted_file.exists():
                should_recover = True
            else:
                # If canonical file is a blank template but worktree has actual generated code
                try:
                    c_size = canonical_file.stat().st_size
                    wt_size = wt_file.stat().st_size
                    if wt_size > c_size and wt_size > 1200:
                        should_recover = True
                except Exception:
                    pass

            if should_recover:
                try:
                    content = wt_file.read_text(encoding="utf-8")
                    if content.strip():
                        canonical_file.write_text(content, encoding="utf-8")
                        persisted_file.write_text(content, encoding="utf-8")
                        logger.info("已从 Agent 工作区自动恢复策略文件：%s", wt_file.name)
                except Exception as e:
                    logger.warning("恢复 Agent 策略文件失败 %s: %s", wt_file.name, e)


def _atomic_write_text(path: Path, content: str) -> None:
    """Replace a text file atomically without exposing a partial strategy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def save_strategy_code(name: str, code: str) -> Path:
    """Save strategy code to both ephemeral STRATEGY_DIR and persistent PERSISTENT_STRATEGY_DIR."""
    from .agent.strategy_verifier import extract_python_strategy_code, _clean_code_lines

    clean_code = extract_python_strategy_code(code) if ("```" in code or not code.startswith(("from", "import", "#", "class", "\"\"\"", "'''"))) else code
    if not clean_code.strip():
        clean_code = _clean_code_lines(code) or code.strip()

    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    PERSISTENT_STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    canonical = (STRATEGY_DIR / f"{name}.py").resolve()
    persisted = (PERSISTENT_STRATEGY_DIR / f"{name}.py").resolve()
    # Persistent storage is replaced first. If the second replace fails, startup
    # recovery restores the canonical copy from this complete persisted file.
    _atomic_write_text(persisted, clean_code)
    _atomic_write_text(canonical, clean_code)
    return canonical


def _path(name: str) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name) or name == "__init__":
        raise HTTPException(400, "文件名只能使用小写字母、数字和下划线")
    path = (STRATEGY_DIR / f"{name}.py").resolve()
    if path.parent != STRATEGY_DIR.resolve():
        raise HTTPException(400, "非法策略文件路径")

    # If missing in STRATEGY_DIR, check PERSISTENT_STRATEGY_DIR or worktrees
    if not path.exists():
        persisted = (PERSISTENT_STRATEGY_DIR / f"{name}.py").resolve()
        if persisted.exists():
            shutil.copy2(persisted, path)
            return path

        worktrees_root = settings.data_root / "agent" / "worktrees"
        if worktrees_root.exists():
            candidates = sorted(worktrees_root.glob(f"*/backend/app/strategies/{name}.py"), key=lambda x: x.stat().st_mtime, reverse=True)
            if candidates:
                try:
                    content = candidates[0].read_text(encoding="utf-8")
                    if content.strip():
                        save_strategy_code(name, content)
                        return path
                except Exception:
                    pass

    return path



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
    source = path.read_text(encoding="utf-8") if path.exists() else ""

    def manifest_text(field: str) -> str | None:
        match = re.search(rf"^\s*{field}\s*=\s*(.+?),?\s*$", source, re.MULTILINE)
        if not match:
            return None
        try:
            value = ast.literal_eval(match.group(1))
            return value if isinstance(value, str) else None
        except (ValueError, SyntaxError):
            return None

    stat = path.stat() if path.exists() else None
    result = {
        "name": path.stem,
        "filename": path.name,
        "module": f"app.strategies.{path.stem}",
        "code_hash": code_hash(source) if source else "",
        "draft_description": manifest_text("description"),
        "draft_category": manifest_text("category"),
        "created_at": getattr(stat, "st_birthtime", stat.st_ctime) if stat else None,
        "updated_at": stat.st_mtime if stat else None,
    }
    if include_content:
        result["content"] = source
    return result


@router.get("")
def list_files():
    ensure_strategy_storage()
    return [_file_out(path) for path in sorted(STRATEGY_DIR.glob("*.py")) if path.name != "__init__.py"]


@router.post("")
def create_file(data: StrategyFileCreate):
    path = _path(data.name)
    if path.exists():
        raise HTTPException(409, "策略文件已经存在")
    code = _template(data.name, data.mode, data.description, data.category)
    path = save_strategy_code(data.name, code)
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
    path = save_strategy_code(name, data.content)
    return _file_out(path, include_content=True)


@router.patch("/{name}/metadata")
def update_file_metadata(name: str, data: StrategyFileMetadataUpdate):
    path = _path(name)
    if not path.exists():
        raise HTTPException(404, "策略文件不存在")
    source = path.read_text(encoding="utf-8")
    for field, value in (("description", data.description), ("category", data.category)):
        pattern = rf"({field}\s*=\s*)(['\"])(.*?)(\2)"
        if re.search(pattern, source):
            source = re.sub(pattern, rf"\g<1>{value!r}", source, count=1)
    path = save_strategy_code(name, source)
    return _file_out(path, include_content=True)


@router.delete("/{name}", status_code=204)
def delete_file(name: str):
    path = _path(name)
    persisted = PERSISTENT_STRATEGY_DIR / f"{name}.py"
    if not path.exists() and not persisted.exists():
        raise HTTPException(404, "策略文件不存在")
    if path.exists():
        path.unlink(missing_ok=True)
    if persisted.exists():
        persisted.unlink(missing_ok=True)
