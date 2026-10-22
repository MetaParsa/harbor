"""Pack a project folder into the tar.gz Liara expects, with Liara's ignore rules.

Rules (Liara CLI create-archive.ts): always drop editor/VCS/build junk; then the root's first ignore file among
.liaraignore > .dockerignore > .gitignore applies, gitignore-style:
- a pattern with a leading or middle `/` is anchored to the root; otherwise it matches at any depth;
- `*` and `?` don't cross `/`, `**` does; a trailing `/` matches directories only;
- a pattern matching a parent directory matches everything inside it; the last matching pattern wins, so
  `!pattern` re-includes (this also covers whitelist-style .dockerignore files: `*` then `!src`).
Simplified: nested ignore files aren't read. Matching is case-sensitive, like Linux builders.
liara.json is never uploaded — its content goes in the release body.
"""

import io
import os
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path

ALWAYS = {".git", ".idea", ".vscode", ".next", ".dockerignore", "liara.json", "node_modules", "bower_components",
          ".DS_Store", "__pycache__", ".venv", "venv", ".env", ".pytest_cache"}
PER_PLATFORM = {
    "php": {"vendor"}, "laravel": {"vendor"}, "dotnet": {"bin", "obj"},
}
IGNORE_FILES = (".liaraignore", ".dockerignore", ".gitignore")
ENV_KEEP = {".env.example", ".env.sample", ".env.template"}     # every other .env.* (local, production…) stays home


def marker_excludes(root: Path) -> set[str]:
    """Platform build folders, detected from marker files (the MCP side doesn't know the platform)."""
    out = set()
    if (root / "composer.json").is_file():
        out.add("vendor")
    if any(root.glob("*.csproj")):
        out |= {"bin", "obj"}
    return out


def secret_env_file(name: str) -> bool:
    return name.startswith(".env.") and name not in ENV_KEEP


class PackageError(ValueError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # too_big | empty


@dataclass
class Rule:
    regex: re.Pattern
    negate: bool
    dir_only: bool
    anchored: bool


def _glob_to_regex(glob: str) -> str:
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif glob.startswith("**", i):
            out, i = out + ".*", i + 2
        elif glob[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif glob[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(glob[i]), i + 1
    return out


def parse_rules(text: str) -> list[Rule]:
    rules = []
    for line in text.splitlines():
        p = line.strip()
        if not p or p.startswith("#"):
            continue
        negate = p.startswith("!")
        p = p[1:] if negate else p
        dir_only = p.endswith("/")
        p = p.rstrip("/")
        anchored = "/" in p          # leading or middle slash
        p = p.lstrip("/")
        if not p:
            continue
        rules.append(Rule(re.compile(_glob_to_regex(p) + r"\Z"), negate, dir_only, anchored))
    return rules


def ignore_rules(root: Path) -> tuple[str | None, list[Rule]]:
    for name in IGNORE_FILES:
        f = root / name
        if f.is_file():
            return name, parse_rules(f.read_text(encoding="utf-8-sig", errors="replace"))
    return None, []


def _rule_hits(rule: Rule, rel: str, is_dir: bool) -> bool:
    parts = rel.split("/")
    # the path itself, then each parent directory (a match on a parent covers everything inside it)
    for depth in range(len(parts), 0, -1):
        candidate = "/".join(parts[:depth])
        candidate_is_dir = is_dir if depth == len(parts) else True
        if rule.dir_only and not candidate_is_dir:
            continue
        target = candidate if rule.anchored else parts[depth - 1]
        if rule.regex.match(target):
            return True
    return False


def is_ignored(rel: str, is_dir: bool, rules: list[Rule]) -> bool:
    ignored = False
    for rule in rules:
        if _rule_hits(rule, rel, is_dir):
            ignored = not rule.negate
    return ignored


def package_dir(root: Path, platform: str = "", max_mb: int = 128) -> tuple[bytes, dict]:
    """Return (tar.gz bytes, info). info lists the ignore file used, file count and size, for the UI and logs."""
    root = root.resolve()
    always = ALWAYS | PER_PLATFORM.get(platform, set()) | marker_excludes(root)
    ignore_file, rules = ignore_rules(root)
    buf, files = io.BytesIO(), 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in always)   # never descend into node_modules etc.
            for fn in sorted(filenames):
                p = Path(dirpath) / fn
                rel = p.relative_to(root).as_posix()
                if fn in always or secret_env_file(fn) or p.suffix == ".pyc" or is_ignored(rel, False, rules):
                    continue
                tar.add(p, arcname=rel, recursive=False)
                files += 1
    data = buf.getvalue()
    if files == 0:
        raise PackageError("empty", "ignore rules left nothing to upload")
    if len(data) > max_mb * 1024 * 1024:
        raise PackageError("too_big", f"source is {len(data) / 1048576:.1f} MB, limit {max_mb} MB")
    return data, {"ignore_file": ignore_file, "files": files, "bytes": len(data)}
