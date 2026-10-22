"""Source packaging: Liara's ignore rules and size limit."""

import io
import tarfile

import pytest

from liara.liara_api import PackageError, package_dir
from liara.liara_api.package import is_ignored, parse_rules


def names(data: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        return set(t.getnames())


def write(root, rel, text="x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_always_excluded_and_gitignore(tmp_path):
    for rel in ("main.py", "requirements.txt", "liara.json", ".env", "node_modules/a/index.js", ".git/HEAD",
                "__pycache__/m.pyc", "static/app.js", "media/big.bin", "logs/x.log", "keep/logs.txt"):
        write(tmp_path, rel)
    write(tmp_path, ".gitignore", "media/\n*.log\n# comment\n!important.log\n")
    data, info = package_dir(tmp_path, "flask")
    assert names(data) == {"main.py", "requirements.txt", "static/app.js", ".gitignore", "keep/logs.txt"}
    assert info["ignore_file"] == ".gitignore" and info["files"] == 5


def test_liaraignore_wins_over_gitignore(tmp_path):
    write(tmp_path, "a.py")
    write(tmp_path, "b.py")
    write(tmp_path, ".gitignore", "a.py\n")
    write(tmp_path, ".liaraignore", "b.py\n")
    data, info = package_dir(tmp_path)
    assert info["ignore_file"] == ".liaraignore" and "a.py" in names(data) and "b.py" not in names(data)


def test_platform_extras(tmp_path):
    write(tmp_path, "index.php")
    write(tmp_path, "vendor/lib.php")
    assert names(package_dir(tmp_path, "php")[0]) == {"index.php"}


def test_too_big_and_empty(tmp_path):
    write(tmp_path, "blob.bin", "z" * 10)
    with pytest.raises(PackageError) as e:
        package_dir(tmp_path, max_mb=0)
    assert e.value.kind == "too_big"
    write(tmp_path, ".liaraignore", "*\n")
    with pytest.raises(PackageError) as e:
        package_dir(tmp_path)
    assert e.value.kind == "empty"


def test_anchored_and_single_level_globs(tmp_path):
    for rel in ("data/dump.sql", "app/data/models.py", "src/a.py", "src/sub/b.py", "build/out.js"):
        write(tmp_path, rel)
    write(tmp_path, ".gitignore", "/data\nsrc/*.py\nbuild/\n")
    got = names(package_dir(tmp_path)[0])
    assert "app/data/models.py" in got and "data/dump.sql" not in got        # /data is root-only
    assert "src/sub/b.py" in got and "src/a.py" not in got                   # * doesn't cross /
    assert "build/out.js" not in got
    # case-sensitive like the Linux builder (tested on the matcher: Windows folders can't differ only by case)
    rules = parse_rules("build/\n")
    assert not is_ignored("Build/keep.txt", False, rules) and is_ignored("build/x", False, rules)


def test_bom_and_whitelist_dockerignore(tmp_path):
    write(tmp_path, "a.log")
    write(tmp_path, "main.py")
    (tmp_path / ".gitignore").write_bytes("﻿*.log\n".encode("utf-8"))
    assert names(package_dir(tmp_path)[0]) == {"main.py", ".gitignore"}
    (tmp_path / ".gitignore").unlink()
    write(tmp_path, "src/app.py")
    write(tmp_path, "secret.txt")
    write(tmp_path, ".liaraignore", "*\n!src\n!main.py\n")
    assert names(package_dir(tmp_path)[0]) == {"src/app.py", "main.py"}


def test_demo_app_d_packs():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "demo-apps" / "app-d"
    assert names(package_dir(root, "flask")[0]) == {"main.py", "requirements.txt"}
