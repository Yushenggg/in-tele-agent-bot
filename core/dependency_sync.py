import asyncio
import ast
import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from core.config import WORKING_DIR, app_config

logger = logging.getLogger("DEPENDENCY_SYNC")

PROJECT_ROOT = WORKING_DIR.parent
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"
LOCK_PATH = PROJECT_ROOT / "uv.lock"
MANIFEST_PATH = WORKING_DIR / "deps.txt"
BASE_PYPROJECT_PATH = PROJECT_ROOT / "base" / "pyproject.toml"

UV_TIMEOUT_SECONDS = 300

# Optional dependency group defined under [project.optional-dependencies].
NANOGATEWAY_EXTRA = "nanogateway"

_WORKING_SUBDIRS = ("handlers", "tools", "subagents")


@dataclass
class SyncResult:
    synced: bool
    added: list[str]
    removed: list[str] | None = None
    kept: list[str] = field(default_factory=list)
    error: str | None = None


def read_manifest() -> list[str]:
    if not MANIFEST_PATH.exists():
        return []
    deps: list[str] = []
    for raw in MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        deps.append(line)
    return deps


def _validate(dep: str) -> str | None:
    if not dep or len(dep) > 200:
        return f"empty or oversized entry: {dep!r}"
    if any(ch in dep for ch in ("\n", "\r", "`", "$", "&", "|", ";")):
        return f"invalid characters in entry: {dep!r}"
    return None


def _base_deps() -> list[str]:
    try:
        text = BASE_PYPROJECT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning(
            "Missing %s — falling back to current pyproject.toml",
            BASE_PYPROJECT_PATH,
        )
        text = PYPROJECT_PATH.read_text(encoding="utf-8")
    return _extract_dependencies(text)


def _normalize(dep: str) -> str:
    name = re.split(r"[<>=!~\[\s]", dep)[0].strip().lower()
    return name.replace("_", "-")


def _imported_top_level_names() -> set[str]:
    names: set[str] = set()
    for subdir in _WORKING_SUBDIRS:
        d = WORKING_DIR / subdir
        if not d.exists():
            continue
        for f in sorted(d.glob("*.py")):
            if f.name == "__init__.py":
                continue
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        names.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 0 and node.module:
                        names.add(node.module.split(".")[0])
    return names


def _resolve_target_deps(manifest: list[str]) -> list[str]:
    base = _base_deps()
    base_norm = {_normalize(d) for d in base}
    merged = list(base)
    for dep in manifest:
        if _normalize(dep) not in base_norm and dep not in merged:
            merged.append(dep)
    return merged


def _extract_dependencies(pyproject_text: str) -> list[str]:
    try:
        data = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError:
        return []
    project = data.get("project", {})
    return [str(d) for d in project.get("dependencies", [])]


def _find_dependencies_end(text: str, start: int) -> int:
    depth = 0
    in_str = False
    escaped = False
    i = start
    while i < len(text):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def _apply_to_pyproject(target_deps: list[str]) -> None:
    text = PYPROJECT_PATH.read_text(encoding="utf-8")
    existing = _extract_dependencies(text)
    if target_deps == existing:
        return

    m = re.search(r"dependencies\s*=\s*\[", text)
    if not m:
        raise RuntimeError("Could not locate dependencies block in pyproject.toml")
    end = _find_dependencies_end(text, m.end() - 1)
    if end == -1:
        raise RuntimeError("Could not locate end of dependencies block in pyproject.toml")

    lines = [f'    "{d}",' for d in target_deps]
    block = "dependencies = [\n" + "\n".join(lines) + "\n]"
    new_text = text[: m.start()] + block + text[end + 1 :]
    PYPROJECT_PATH.write_text(new_text, encoding="utf-8")


def _backup_project_files() -> dict[Path, bytes]:
    backups: dict[Path, bytes] = {}
    for path in (PYPROJECT_PATH, LOCK_PATH):
        backups[path] = path.read_bytes() if path.exists() else b""
    return backups


def _restore_project_files(backups: dict[Path, bytes]) -> None:
    for path, data in backups.items():
        if data:
            path.write_bytes(data)
        elif path.exists():
            path.unlink()


def snapshot_project_files() -> dict[Path, bytes]:
    return _backup_project_files()


async def revert_project_files(backups: dict[Path, bytes]) -> None:
    _restore_project_files(backups)
    await _run_uv_sync()


def _requested_extras() -> list[str]:
    return [NANOGATEWAY_EXTRA] if app_config.use_nanogateway else []


def ensure_extra(extra: str) -> tuple[bool, str]:
    """Blocking sync of one optional dependency group (for the supervisor)."""
    return asyncio.run(_run_uv_sync(extras=[extra]))


async def _run_uv_sync(extras: list[str] | None = None) -> tuple[bool, str]:
    if extras is None:
        extras = _requested_extras()
    cmd = ["uv", "sync"]
    for extra in extras:
        cmd += ["--extra", extra]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(PROJECT_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return False, "uv executable not found on PATH"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=UV_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return False, f"uv sync timed out after {UV_TIMEOUT_SECONDS}s"
    text = (out or b"").decode("utf-8", errors="replace")
    if proc.returncode != 0:
        tail = "\n".join(text.strip().splitlines()[-20:])
        return False, f"uv sync failed (exit {proc.returncode}):\n{tail}"
    return True, text.strip()


async def sync_dependencies() -> SyncResult:
    manifest_deps = read_manifest()
    for dep in manifest_deps:
        err = _validate(dep)
        if err:
            logger.warning("Rejecting invalid manifest entry: %s", err)
            return SyncResult(synced=False, added=[], error=err)

    current = _extract_dependencies(PYPROJECT_PATH.read_text(encoding="utf-8"))
    target = _resolve_target_deps(manifest_deps)

    current_norm = {_normalize(d) for d in current}
    target_norm = {_normalize(d) for d in target}
    added = [d for d in target if _normalize(d) not in current_norm]
    removed = [d for d in current if _normalize(d) not in target_norm]

    kept: list[str] = []
    if removed:
        imports = _imported_top_level_names()
        for dep in removed:
            if _normalize(dep) in imports:
                kept.append(dep)
                target.append(dep)
        if kept:
            logger.info(
                "Keeping deps still imported by working code: %s",
                ", ".join(kept),
            )
        removed = [d for d in removed if d not in kept]

    if not added and not removed:
        if kept:
            return SyncResult(synced=False, added=[], kept=kept)
        logger.info("Dependencies up to date")
        return SyncResult(synced=False, added=[])

    logger.info(
        "Syncing deps: +%s -%s",
        ", ".join(added) or "-",
        ", ".join(removed) or "-",
    )
    backups = _backup_project_files()

    try:
        _apply_to_pyproject(target)
    except Exception as e:
        _restore_project_files(backups)
        return SyncResult(synced=False, added=[], error=f"Failed to update pyproject.toml: {e}")

    ok, output = await _run_uv_sync()
    if not ok:
        logger.error("uv sync failed, rolling back project files: %s", output)
        _restore_project_files(backups)
        await _run_uv_sync()
        return SyncResult(synced=False, added=added, error=output)

    logger.info("uv sync complete: +%s -%s", ", ".join(added) or "-", ", ".join(removed) or "-")
    return SyncResult(synced=True, added=added, removed=removed, kept=kept)
