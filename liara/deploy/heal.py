"""Read why a first deploy failed and fix the project copy — the known-failure catalogue.

Every rule was seen live or comes from Liara's docs, and is deliberately narrow: a regex over the failing release's
build/app log lines → one small edit to the working copy, shown as a diff. Anything else is not guessed at: the
loop stops and hands over (coding-agent brief / support ticket) instead of guessing.

A missing module is only installed when it is a well-known package (PIP_NAME): a name read from a log could be one of
the project's own files that the zip forgot, or be planted by a dependency — installing whatever PyPI has under that
name would run a stranger's code in the user's app.
"""

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .analyze import Fix

# import name -> pip package. Only these are added automatically.
PIP_NAME = {"dotenv": "python-dotenv", "cv2": "opencv-python-headless", "PIL": "Pillow", "yaml": "PyYAML",
            "bs4": "beautifulsoup4", "jwt": "PyJWT", "sklearn": "scikit-learn", "dateutil": "python-dateutil",
            "docx": "python-docx", "Crypto": "pycryptodome", "psycopg2": "psycopg2-binary", "MySQLdb": "mysqlclient",
            "jinja2": "Jinja2", "flask": "Flask", "flask_cors": "Flask-Cors", "flask_sqlalchemy": "Flask-SQLAlchemy",
            "flask_login": "Flask-Login", "flask_wtf": "Flask-WTF", "werkzeug": "Werkzeug", "requests": "requests",
            "httpx": "httpx", "sqlalchemy": "SQLAlchemy", "pymysql": "PyMySQL", "pymongo": "pymongo", "redis": "redis",
            "gunicorn": "gunicorn", "uvicorn": "uvicorn", "fastapi": "fastapi", "pydantic": "pydantic",
            "numpy": "numpy", "pandas": "pandas", "markdown": "Markdown", "openai": "openai", "telegram": "python-telegram-bot",
            "django": "Django", "rest_framework": "djangorestframework", "corsheaders": "django-cors-headers",
            "whitenoise": "whitenoise", "decouple": "python-decouple", "environ": "django-environ"}
PKG_RESOURCES = re.compile(r"No module named 'pkg_resources'")
MODULE_MISSING = re.compile(r"ModuleNotFoundError: No module named '([A-Za-z_][\w.]*)'?")
NPM_START = re.compile(r"(must be runnable with 'npm start'|Missing script: \"start\")")
ERROR_LINE = re.compile(r"(Error|Exception|error:|ERR!)")
REQ_NAME = re.compile(r"[<>=!~\[ ;]")


@dataclass
class BuildFailure:
    rule: str | None
    line: str | None            # the log line that decided it
    explain: str                # one Persian sentence
    package: str | None = None  # the pip package to add, for py-module-missing
    module: str | None = None
    advice: str | None = None   # what the user should do when we won't fix it ourselves


def classify_failure(log_lines: list[str]) -> BuildFailure:
    """Match a release's log lines against the catalogue. Pure: changes nothing."""
    text = "\n".join(log_lines)
    if m := PKG_RESOURCES.search(text):
        return BuildFailure("py-pkg-resources", m.group(0),
                         "برنامه بالا نیومد: gunicorn روی لیارا ماژول pkg_resources رو پیدا نکرد (ایراد خود پلتفرم Flask لیارا).")
    if m := MODULE_MISSING.search(text):
        mod = m.group(1).split(".")[0]
        return BuildFailure("py-module-missing", m.group(0), f"برنامه بالا نیومد: کد «{mod}» رو import می‌کنه ولی نصب نشده.",
                         PIP_NAME.get(mod), mod)
    if m := NPM_START.search(text):
        return BuildFailure("node-start", m.group(0), "لیارا برنامه رو با npm start اجرا می‌کنه ولی اسکریپت start تعریف نشده.")
    last_err = next((ln for ln in reversed(log_lines) if ERROR_LINE.search(ln)), None)
    return BuildFailure(None, last_err, "این خطا در فهرست خطاهای شناخته‌شده‌ی من نیست؛ خودکار درستش نمی‌کنم.")


def _read(p: Path) -> str | None:
    """utf-8, or utf-16 (`pip freeze > requirements.txt` in Windows PowerShell writes that). None = leave it alone."""
    if not p.is_file():
        return ""
    raw = p.read_bytes()
    for enc in (("utf-16",) if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ()) + ("utf-8-sig",):
        try:
            return raw.decode(enc).replace("\r\n", "\n")
        except UnicodeDecodeError:
            continue
    return None


def _edit(root: Path, rel: str, old: str, new: str) -> str:
    (root / rel).write_bytes(new.encode("utf-8"))          # utf-8 + LF line ends: what the Linux build on Liara expects
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}"))


def _req_name(line: str) -> str:
    return REQ_NAME.split(line.strip(), maxsplit=1)[0].lower().replace("_", "-")


def _set_requirement(root: Path, line: str, *, replace: bool = False) -> str | None:
    """Add a requirement (or, with replace=True, pin an existing one to `line`). None = nothing new to try."""
    old = _read(root / "requirements.txt")
    if old is None:
        return None
    rows = old.splitlines()
    name = _req_name(line)
    hits = [i for i, ln in enumerate(rows) if ln.strip() and not ln.lstrip().startswith("#") and _req_name(ln) == name]
    if hits and (not replace or all(rows[i].replace(" ", "") == line for i in hits)):
        return None                                     # already there: the same fix again would just loop
    if hits:
        rows = [line if i == hits[0] else r for i, r in enumerate(rows) if i not in hits[1:]]
        new = "\n".join(rows) + "\n"
    else:
        new = (old if not old or old.endswith("\n") else old + "\n") + line + "\n"
    return _edit(root, "requirements.txt", old, new)


def _is_local(root: Path, mod: str) -> bool:
    return any(root.rglob(f"{mod}.py")) or any(p.is_dir() for p in root.rglob(mod))


def apply_fix(root: Path, d: BuildFailure) -> Fix | None:
    """Apply the catalogue's fix for this diagnosis to the working copy. None = no (new) fix to try;
    then d.advice may say what the user should do instead."""
    if d.rule == "py-pkg-resources":
        diff = _set_requirement(root, "setuptools<80", replace=True)
        return diff and Fix(d.rule, "setuptools<80 رو در requirements.txt گذاشتم تا gunicorn لیارا دوباره pkg_resources رو داشته باشه.",
                            "requirements.txt", diff)
    if d.rule == "py-module-missing":
        if d.module and _is_local(root, d.module):
            d.advice = f"«{d.module}» یکی از فایل‌های خود پروژه‌ست ولی جایی import می‌شه که پایتون پیداش نمی‌کنه؛ مسیر import رو بررسی کن."
            return None
        if not d.package:
            d.advice = f"اسم پکیج pip مربوط به «{d.module}» رو به requirements.txt اضافه کن؛ خودم حدس نمی‌زنم چون ممکنه پکیج اشتباهی نصب بشه."
            return None
        diff = _set_requirement(root, d.package)
        return diff and Fix(d.rule, f"«{d.package}» رو به requirements.txt اضافه کردم (کد بهش نیاز داشت ولی در فهرست نبود).",
                            "requirements.txt", diff)
    if d.rule == "node-start":
        return add_start_script(root)
    return None


def add_start_script(root: Path) -> Fix | None:
    p = root / "package.json"
    old = _read(p)
    try:
        pkg = json.loads(old or "")
    except ValueError:
        return None
    if not isinstance(pkg, dict) or not isinstance(pkg.get("scripts", {}), dict) or (pkg.get("scripts") or {}).get("start"):
        return None
    main = pkg.get("main") if isinstance(pkg.get("main"), str) else None
    main = main or next((n for n in ("server.js", "index.js", "app.js") if (root / n).is_file()), None)
    if not main:
        return None
    pkg.setdefault("scripts", {})["start"] = f"node {main}"
    diff = _edit(root, "package.json", old, json.dumps(pkg, ensure_ascii=False, indent=2) + "\n")
    return Fix("node-start", f"اسکریپت start نبود؛ «node {main}» رو گذاشتم (لیارا برنامه رو با npm start اجرا می‌کنه).",
               "package.json", diff)


def project_diff(orig: Path, work: Path) -> str:
    """Everything we changed, as one patch the user can apply to their own repo."""
    out = []
    names = {p.relative_to(work).as_posix() for p in work.rglob("*") if p.is_file()} | \
            {p.relative_to(orig).as_posix() for p in orig.rglob("*") if p.is_file()}
    for rel in sorted(names):
        a, b = orig / rel, work / rel
        old = a.read_text(encoding="utf-8", errors="replace").splitlines(True) if a.is_file() else []
        new = b.read_text(encoding="utf-8", errors="replace").splitlines(True) if b.is_file() else []
        if old != new:
            out.append("".join(difflib.unified_diff(old, new, f"a/{rel}", f"b/{rel}")))
    return "".join(out)
