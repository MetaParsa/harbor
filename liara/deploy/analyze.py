"""Read a project folder: what it is, how Liara should run it, what it needs, and what to fix before uploading.

Detection follows the Liara CLI's order, simplified to what the first-deploy chapter
supports well: Flask / Django / FastAPI-or-plain Python, Node (Next, React/Vue/Angular SPA, plain), static, Docker.
Preflight fixes are config-only (liara.json-style platform settings) and shown to the user before the upload;
failures that only show at runtime (missing packages, platform bugs) are healed from the logs (heal.py).
"""

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from liara.liara_api.models import Plans
from liara.text import fa_num, ltr

PY_ENV = re.compile(r"""os\.(?:environ(?:\.get)?\(?\[?|getenv\()\s*['"]([A-Z][A-Z0-9_]+)""")
JS_ENV = re.compile(r"process\.env\.([A-Z][A-Z0-9_]+)|process\.env\[['\"]([A-Z][A-Z0-9_]+)")
FLASK_APP = re.compile(r"^(\w+)\s*=\s*Flask\(", re.M)
DB_HINTS = {"psycopg": "postgres", "psycopg2": "postgres", "asyncpg": "postgres", "pymysql": "mysql", "mysqlclient": "mysql",
            "pymongo": "mongodb", "redis": "redis", "pg": "postgres", "mysql2": "mysql", "mongoose": "mongodb",
            "ioredis": "redis", "prisma": "postgres"}
# Port each Liara platform listens on by default (used when the user or analysis gives none).
DEFAULT_PORT = {"node": 3000, "next": 3000, "flask": 80, "django": 80, "python": 80, "static": 80, "react": 80,
                "vue": 80, "angular": 80, "php": 80, "laravel": 80, "go": 8080, "dotnet": 80, "docker": 80}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}


@dataclass
class Fix:
    rule: str
    title: str                  # one Persian sentence
    file: str                   # file it touches, or "liara.json" for platform settings
    diff: str = ""              # unified diff (code) or the settings change
    applied: bool = True


@dataclass
class Analysis:
    platform: str
    framework: str
    port: int
    platform_config: dict = field(default_factory=dict)
    env_vars: list[str] = field(default_factory=list)
    database: str | None = None
    files: int = 0
    size_kb: int = 0
    summary: str = ""
    method: str = "آپلود مستقیم کد (بدون گیت‌هاب و بدون Docker)"
    preflight: list[Fix] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return asdict(self)


def _py_files(root: Path):
    for p in root.rglob("*.py"):
        if not any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            yield p


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def analyze(root: Path) -> Analysis:
    files = [p for p in root.rglob("*") if p.is_file() and not any(x in SKIP_DIRS for x in p.relative_to(root).parts)]
    size_kb = round(sum(p.stat().st_size for p in files) / 1024)
    has = lambda name: (root / name).is_file()   # noqa: E731
    try:
        pkg = json.loads(_read(root / "package.json") or "{}") if has("package.json") else {}
    except ValueError:
        pkg = {}
    pkg = pkg if isinstance(pkg, dict) else {}
    deps = {k: v for part in ("dependencies", "devDependencies") if isinstance(pkg.get(part), dict) for k, v in pkg[part].items()}
    reqs = _read(root / "requirements.txt").lower() if has("requirements.txt") else ""
    req_names = {re.split(r"[<>=!~\[ ]", ln.strip(), maxsplit=1)[0] for ln in reqs.splitlines() if ln.strip() and not ln.startswith("#")}

    if has("Dockerfile") and not (has("package.json") or has("requirements.txt")):
        a = Analysis("docker", "Docker", 80)
        a.warnings.append("پورت رو از EXPOSE در Dockerfile بخون؛ پیش‌فرض ۸۰ گذاشتم.")
    elif has("requirements.txt") or has("pyproject.toml") or any(_py_files(root)):
        sources = {p: _read(p) for p in _py_files(root)}
        if has("manage.py") or "django" in req_names:
            a = Analysis("django", "Django", 80)
        elif "fastapi" in req_names:        # the root's own requirements win over a Flask app in some subfolder
            a = Analysis("python", "FastAPI", 8000, {"pythonVersion": "3.12"})
        elif "flask" in req_names or any("Flask(" in t for t in sources.values()):
            a = Analysis("flask", "Flask", 80, {"pythonVersion": "3.12"})
            for p, text in sources.items():
                if m := FLASK_APP.search(text):
                    module = p.relative_to(root).with_suffix("").as_posix().replace("/", ".")
                    if (module, m.group(1)) != ("app", "app"):
                        a.platform_config["appModule"] = f"{module}:{m.group(1)}"
                        a.preflight.append(Fix(
                            "flask-app-module",
                            f"برنامه‌ی Flask در {p.name} و اسمش «{m.group(1)}»ه، ولی لیارا دنبال app.py می‌گرده؛ "
                            f"در تنظیمات لیارا appModule رو {module}:{m.group(1)} گذاشتم.",
                            "liara.json", f'+ "flask": {{"appModule": "{module}:{m.group(1)}"}}'))
                    break
        else:
            a = Analysis("python", "Python", 80, {"pythonVersion": "3.12"})
        a.env_vars = sorted({m for t in sources.values() for m in PY_ENV.findall(t)})
        a.database = next((db for name, db in DB_HINTS.items() if name in req_names), None)
    elif pkg:
        if "next" in deps:
            a = Analysis("next", "Next.js", 3000)
        elif "react" in deps or "vue" in deps or "@angular/core" in deps:
            a = Analysis("react" if "react" in deps else "vue" if "vue" in deps else "angular",
                         "React" if "react" in deps else "Vue" if "vue" in deps else "Angular", 80)
        else:
            a = Analysis("node", "Node.js", 3000)
            scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
            if not scripts.get("start"):
                main = (pkg.get("main") if isinstance(pkg.get("main"), str) else None) or next((n for n in ("server.js", "index.js", "app.js") if has(n)), None)
                if main:
                    a.preflight.append(Fix("node-start", f"اسکریپت start نبود؛ «node {main}» رو گذاشتم (لیارا با npm start اجرا می‌کنه).",
                                           "package.json", f'+ "start": "node {main}"', applied=False))
        js = " ".join(_read(p) for p in root.rglob("*.js") if not any(x in SKIP_DIRS for x in p.relative_to(root).parts))
        a.env_vars = sorted({x or y for x, y in JS_ENV.findall(js)} - {"PORT"})
        a.database = next((db for name, db in DB_HINTS.items() if name in deps), None)
    elif has("index.html"):
        a = Analysis("static", "سایت ثابت (HTML)", 80)
    else:
        a = Analysis("unknown", "نامشخص", 80)
        a.warnings.append("نوع پروژه رو تشخیص ندادم؛ فایل package.json، requirements.txt یا Dockerfile پیدا نشد.")
    _own_liara_json(root, a)
    a.files, a.size_kb = len(files), size_kb
    if a.database:
        a.warnings.append(f"این پروژه به دیتابیس {a.database} وصل می‌شه؛ بعد از آنلاین شدن باید دیتابیس بسازیم (هزینه داره و تأیید می‌خواد).")
    size = f"{fa_num(a.size_kb)} کیلوبایت" if a.size_kb >= 1 else "کمتر از ۱ کیلوبایت"
    a.summary = (f"{ltr(a.framework)} · {fa_num(a.files)} فایل · {size}"
                 + (f" · متغیرهای محیطی: {ltr(', '.join(a.env_vars))}" if a.env_vars else ""))
    return a


def _own_liara_json(root: Path, a: Analysis) -> None:
    """The project's own liara.json is the developer's decision: its platform and port win over our guess."""
    try:
        own = json.loads(_read(root / "liara.json") or "{}") if (root / "liara.json").is_file() else {}
    except ValueError:
        a.warnings.append("فایل liara.json پروژه خونده نشد (JSON نامعتبر)؛ تشخیص خودم رو گذاشتم.")
        return
    if not isinstance(own, dict):
        return
    platform, port = own.get("platform"), own.get("port")
    if isinstance(platform, str) and platform in DEFAULT_PORT and platform != a.platform:
        a.platform, a.framework, a.platform_config = platform, f"{a.framework} (طبق liara.json)", {}
        a.preflight = [f for f in a.preflight if f.rule != "flask-app-module"]
    if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        a.port = port
    if isinstance(own.get(a.platform), dict):
        a.platform_config = dict(own[a.platform])         # the same block the Liara CLI would send
    if platform or port:
        a.warnings.append("liara.json خود پروژه رو دیدم و پلتفرم و پورتش رو همون‌طور گذاشتم.")


def recommend_plan(a: Analysis, plans: Plans) -> dict:
    """Smallest plan that fits, with one plain sentence of reasoning and the real monthly price (plan + bundle)."""
    iran = plans.iran_available()
    need_ram = 1.0 if a.platform in ("next", "django") or a.database else 0.5
    plan = next((p for p in iran if p.ram_gb >= need_ram), iran[0] if iran else None)
    if not plan:
        return {"plan_id": None, "reason": "فهرست پلن‌ها از لیارا خوانده نشد."}
    why = {"next": "Next.js موقع اجرا حافظه‌ی بیشتری می‌خواد", "django": "Django با کتابخونه‌هاش حافظه‌ی بیشتری می‌خواد"}.get(
        a.platform, f"{a.framework} ساده، بدون دیتابیس، بدون آپلود فایل" if not a.database else "دیتابیس داره")
    hour, month = plans.price(plan.id, "basic")
    return {"plan_id": plan.id, "bundle_id": "basic", "cpu": plan.cpu, "ram_gb": plan.ram_gb, "month": month, "hour": hour,
            "reason": f"{why}؛ {fa_num(plan.ram_gb, 1)} گیگ رم کافیه"}
