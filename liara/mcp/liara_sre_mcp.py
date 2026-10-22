#!/usr/bin/env python3
"""Liara deploy + SRE assistant — MCP server for coding agents (Claude Code, Cline, Kilo Code, Cursor).

Standard library only (no pip install: package mirrors can be flaky in Iran), one file, stdio JSON-RPC.
It is a thin client of the assistant's web backend and NEVER holds the Liara token:

  1. In the assistant's web page press «وصل کردن به ایجنت کدنویسی» → a 6-character code.
  2. Tell your coding agent: "pair with the Liara assistant using code ABC123" (tool `pair`).
  3. The agent can then troubleshoot, deploy your local folder, wait for YOUR approval (given only in the web
     page — there is no approve tool here), verify the fix, and write a support ticket.

Install (Claude Code):
  claude mcp add liara-sre -- uv run --no-project python <path>/liara_sre_mcp.py --url https://<assistant-host>
Cline / Kilo Code: same command in the MCP settings JSON ("command": "uv", "args": [...]).
"""

import argparse
import http.client
import io
import json
import os
import re
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "0.1.0"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")       # newest first
ERROR_STATUSES = {"error", "refused", "failed", "expired"}

# ---------- packing (kept in sync with liara/liara_api/package.py; a test compares them) ----------

ALWAYS = {".git", ".idea", ".vscode", ".next", ".dockerignore", "liara.json", "node_modules", "bower_components",
          ".DS_Store", "__pycache__", ".venv", "venv", ".env", ".pytest_cache"}
IGNORE_FILES = (".liaraignore", ".dockerignore", ".gitignore")
ENV_KEEP = {".env.example", ".env.sample", ".env.template"}
MAX_MB = 128


def _marker_excludes(root: Path) -> set:
    out = set()
    if (root / "composer.json").is_file():
        out.add("vendor")
    if any(root.glob("*.csproj")):
        out |= {"bin", "obj"}
    return out


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


def _rules(root: Path):
    for name in IGNORE_FILES:
        f = root / name
        if f.is_file():
            rules = []
            for line in f.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                p = line.strip()
                if not p or p.startswith("#"):
                    continue
                neg = p.startswith("!")
                p = p[1:] if neg else p
                dir_only = p.endswith("/")
                p = p.rstrip("/")
                anchored = "/" in p
                p = p.lstrip("/")
                if p:
                    rules.append((re.compile(_glob_to_regex(p) + r"\Z"), neg, dir_only, anchored))
            return name, rules
    return None, []


def _ignored(rel: str, rules) -> bool:
    parts, ignored = rel.split("/"), False
    for regex, neg, dir_only, anchored in rules:
        for depth in range(len(parts), 0, -1):
            is_dir = depth != len(parts)
            if dir_only and not is_dir:
                continue
            if regex.match("/".join(parts[:depth]) if anchored else parts[depth - 1]):
                ignored = not neg
                break
    return ignored


def pack(root: Path) -> tuple[bytes, dict]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"not a folder: {root}")
    ignore_file, rules = _rules(root)
    always = ALWAYS | _marker_excludes(root)
    buf, files = io.BytesIO(), 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in always)
            for fn in sorted(filenames):
                p = Path(dirpath) / fn
                rel = p.relative_to(root).as_posix()
                secret_env = fn.startswith(".env.") and fn not in ENV_KEEP
                if fn in always or secret_env or p.suffix == ".pyc" or _ignored(rel, rules):
                    continue
                tar.add(p, arcname=rel, recursive=False)
                files += 1
    data = buf.getvalue()
    if files == 0:
        raise ValueError("ignore rules left nothing to upload")
    if len(data) > MAX_MB * 1024 * 1024:
        raise ValueError(f"project is {len(data) / 1048576:.0f} MB packed; Liara's limit is {MAX_MB} MB")
    return data, {"files": files, "bytes": len(data), "ignore_file": ignore_file}


# ---------- backend client ----------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):                      # a redirect would forward the bearer token elsewhere
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class Backend:
    def __init__(self, url: str, token: str | None = None):
        self.url = url.rstrip("/")
        self.token = token                                   # bridge token: memory only, dies with this process
        parts = urllib.parse.urlparse(self.url)
        self.url_error = None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            self.url_error = f"bad --url {url!r}"
        elif parts.scheme == "http" and parts.hostname not in LOCAL_HOSTS:
            self.url_error = "refusing plain http to a remote host (the bridge token would travel in clear text); use https"

    def call(self, method: str, path: str, body=None, *, raw: bytes | None = None, timeout: float = 60) -> dict:
        if self.url_error:
            return {"status": "error", "error": "bad_url", "message": self.url_error}
        headers = {"Accept": "application/json"}
        data = None
        if raw is not None:
            data, headers["Content-Type"] = raw, "application/gzip"
        elif body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                out = json.loads(r.read() or b"{}")
            return out if isinstance(out, dict) else {"status": "error", "error": "bad_reply"}
        except urllib.error.HTTPError as e:
            try:
                out = json.loads(e.read() or b"{}")
            except (ValueError, http.client.HTTPException, OSError):
                out = {}
            return {"status": "error", "http": e.code, **(out if isinstance(out, dict) else {})}
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
            return {"status": "error", "error": "unreachable", "message": f"assistant backend not reachable: {type(e).__name__}"}
        except ValueError:
            return {"status": "error", "error": "bad_reply"}


# ---------- tools ----------

NEVER_APPROVE = ("Approval happens only in the Liara assistant's web page, by the user. "
                 "You cannot approve it and must not try; ask the user to tap the card there.")

TOOLS = [
    {"name": "pair",
     "description": "Connect to the user's Liara assistant session with the 6-character code shown in its web page "
                    "(button «وصل کردن به ایجنت کدنویسی»). Needed once per session.",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "troubleshoot",
     "description": "Diagnose a problem on a live Liara app (500 errors, slowness, crashes) from Liara's logs, events and "
                    "metrics. Returns ranked causes with evidence, the files/lines involved (mapped to the local project "
                    "if project_path is given) and the recommended fix. Read-only.",
     "inputSchema": {"type": "object", "properties": {
         "app": {"type": "string", "description": "Liara app name"},
         "symptom": {"type": "string", "description": "the user's complaint, any language"},
         "project_path": {"type": "string", "description": "local project folder, to map /app/... paths"}},
         "required": ["app", "symptom"]}},
    {"name": "deploy_project",
     "description": "Pack a local project folder (Liara's ignore rules; node_modules, .git, .env* and vendor/bin/obj "
                    "excluded; 128 MB max) and propose deploying it to an existing Liara app. Usually returns a "
                    "pending_action_id that the user must approve (the first 3 builds of an app the user created through "
                    "the assistant run directly). " + NEVER_APPROVE + " Then call wait_for_approval.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string"}, "app": {"type": "string"},
         "port": {"type": "integer", "description": "only if not the platform default"},
         "message": {"type": "string", "description": "short release note"}},
         "required": ["path", "app"]}},
    {"name": "wait_for_approval",
     "description": "Wait up to timeout_s (default 45, max 55) for the user to approve or reject a pending action in the "
                    "web page, and return its result (for a deploy: build state and seconds). If it returns "
                    "timed_out, call it again. If it returns stale, wait for result.new_action instead. Read-only.",
     "inputSchema": {"type": "object", "properties": {
         "action_id": {"type": "string"}, "timeout_s": {"type": "integer", "default": 45}},
         "required": ["action_id"]}},
    {"name": "verify_fix",
     "description": "After the fix is live, check it against the success contract from the diagnosis (waits for the new "
                    "container, probes the failing route, checks new errors in logs, memory trend if relevant). "
                    "Closes the incident when it passes.",
     "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}, "incident_id": {"type": "string"}}}},
    {"name": "ci_setup",
     "description": "Get the GitHub Actions workflow that deploys this Liara app on every push (Liara's documented CLI "
                    "flow). Write `content` to `path` in the user's repo, then tell the user the steps in `user_steps`: "
                    "they add the Liara key as the GitHub secret themselves. Never ask for the key. Read-only.",
     "inputSchema": {"type": "object", "properties": {
         "app": {"type": "string"}, "branch": {"type": "string", "default": "main"}}, "required": ["app"]}},
    {"name": "check_capability",
     "description": "Ask whether Liara's API can do something (top_up, balance, rollback_console, bundle_change, unfreeze, "
                    "irnic_ns, github_autodeploy, http_metrics, alerts) and get the manual steps when it can't.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "support_ticket",
     "description": "Write a redacted ticket for Liara support from the app's latest incident (diagnosis, evidence, "
                    "actions tried, verify result).",
     "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]}},
]


def local_files(files: list[dict], project_path: str | None) -> list[dict]:
    out = []
    for f in files or []:
        item = dict(f)
        if project_path:
            p = (Path(project_path) / f["path"]).resolve()
            item["local_path"] = str(p)
            item["exists"] = p.is_file()
        out.append(item)
    return out


def summarize_board(b: dict, project_path: str | None) -> dict:
    if b.get("status") == "error" or "hypotheses" not in b:
        return b
    hyps = []
    for h in b["hypotheses"]:
        hyps.append({"id": h["id"], "cause": h["title"], "confidence": h["confidence"],
                     "evidence": [e["text"] for e in b.get("evidence", []) if e["id"] in h.get("supports", [])],
                     "files": local_files(h.get("files"), project_path),
                     "fixes": [{"kind": fx["kind"], "label": fx.get("label"), "tool": fx.get("tool"),
                                "success": fx.get("contract_fa")} for fx in h.get("fixes", [])]})
    return {"incident_id": b["incident"], "headline": b["headline"], "causes": hyps,
            "ruled_out": [r["text"] for r in b.get("ruled_out", [])], "recommended": b.get("recommended"),
            "brief_for_coding_agent": b.get("brief"),
            "next": "If the top fix is a code change: edit the files above, then deploy_project → (user approves in "
                    "the web page) → wait_for_approval → verify_fix. " + NEVER_APPROVE}


class Server:
    def __init__(self, backend: Backend, code: str | None = None, sleep=time.sleep):
        self.backend, self.sleep = backend, sleep
        self.pending_code = code

    def _ensure_paired(self) -> dict | None:
        if self.backend.token:
            return None
        if self.pending_code:
            res = self.tool_pair({"code": self.pending_code})
            self.pending_code = None
            if self.backend.token:
                return None
            return res
        return {"status": "error", "error": "not_paired",
                "message": "Not paired yet. Ask the user for the code from the assistant page, then call pair."}

    def tool_pair(self, a):
        res = self.backend.call("POST", "/api/bridge/pair", {"code": str(a.get("code", ""))[:12]}, timeout=20)
        if res.get("token"):
            self.backend.token = res["token"]
            return {"status": "paired", "message": "Connected to the user's Liara assistant session. " + NEVER_APPROVE}
        return res

    def tool_troubleshoot(self, a):
        b = self.backend.call("POST", "/api/bridge/troubleshoot", {"app": a["app"], "symptom": a["symptom"]}, timeout=90)
        return summarize_board(b, a.get("project_path"))

    def tool_deploy_project(self, a):
        try:
            data, info = pack(Path(a["path"]))
        except (ValueError, OSError) as e:
            return {"status": "error", "error": "pack_failed", "message": str(e)}
        q = f"?app_name={urllib.parse.quote(a['app'])}"
        if a.get("port"):
            q += f"&port={int(a['port'])}"
        if a.get("message"):
            q += f"&message={urllib.parse.quote(str(a['message'])[:120])}"
        res = self.backend.call("POST", "/api/bridge/deploy" + q, raw=data, timeout=600)
        res["packed"] = info
        if res.get("status") == "waiting_for_user":
            res["next"] = "Tell the user to approve the deploy card in the Liara assistant page, then call " \
                          f"wait_for_approval with action_id={res.get('pending_action_id')}. " + NEVER_APPROVE
        return res

    def tool_wait_for_approval(self, a):
        # stay under common client tool timeouts (Cline: 60 s); the agent calls again on timed_out
        deadline = time.monotonic() + max(1, min(int(a.get("timeout_s") or 45), 55))
        res, blips = {}, 0
        path = f"/api/bridge/actions/{urllib.parse.quote(str(a['action_id']))}"
        while time.monotonic() < deadline:
            left = deadline - time.monotonic()
            res = self.backend.call("GET", path, timeout=max(2, min(15, left)))
            if res.get("error") == "unreachable" and blips < 3:
                blips += 1                                     # a network blip is not the user's answer
            elif res.get("status") not in ("pending", "executing"):
                return res
            if time.monotonic() + 3 >= deadline:
                break
            self.sleep(3)
        return {**res, "status": res.get("status", "pending"), "timed_out": True,
                "message": "Still waiting for the user's decision in the web page; call wait_for_approval again."}

    def tool_verify_fix(self, a):
        return self.backend.call("POST", "/api/bridge/verify",
                                 {"app": a.get("app"), "incident_id": a.get("incident_id")}, timeout=420)

    def tool_ci_setup(self, a):
        return self.backend.call("POST", "/api/bridge/ci", {"app": a["app"], "branch": a.get("branch") or "main"}, timeout=30)

    def tool_check_capability(self, a):
        return self.backend.call("GET", f"/api/bridge/capability/{urllib.parse.quote(a['name'])}", timeout=20)

    def tool_support_ticket(self, a):
        return self.backend.call("POST", "/api/bridge/ticket", {"app": a["app"]}, timeout=30)

    # ---- JSON-RPC ----
    def handle(self, msg: dict) -> dict | None:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            return None                                       # notification (e.g. notifications/initialized)
        if method == "initialize":
            asked = params.get("protocolVersion")
            result = {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "liara-sre", "version": VERSION},
                      "instructions": "Liara deploy + SRE assistant. " + NEVER_APPROVE}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method in ("resources/list", "prompts/list"):
            result = {method.split("/")[0]: []}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            fn = getattr(self, f"tool_{name}", None)
            if fn is None or name not in {t["name"] for t in TOOLS}:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
            try:
                if not isinstance(args, dict):
                    raise TypeError("arguments must be an object")
                if name != "pair" and (err := self._ensure_paired()):
                    out = err
                else:
                    out = fn(args)
            except (KeyError, TypeError, ValueError) as e:
                out = {"status": "error", "error": "bad_arguments", "message": str(e)[:200]}
            except Exception as e:                                # noqa: BLE001 — one bad call must not kill the server
                out = {"status": "error", "error": "internal", "message": type(e).__name__}
            if not isinstance(out, dict):
                out = {"status": "error", "error": "bad_reply"}
            if out.get("error") == "not_paired" and name != "pair":
                self.backend.token = None                         # e.g. the assistant restarted: the token is gone
                out["message"] = ("Not paired with the Liara assistant (it may have restarted; pending cards from "
                                  "before are gone). Ask the user to press «وصل کردن به ایجنت کدنویسی» in the "
                                  "assistant page and give you the new code, then call pair.")
            is_error = out.get("status") in ERROR_STATUSES
            result = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=1)}],
                      "isError": is_error}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    ap = argparse.ArgumentParser(description="Liara SRE MCP server (stdio)")
    ap.add_argument("--url", default=os.environ.get("LIARA_SRE_URL", "http://localhost:8771"))
    ap.add_argument("--code", default=os.environ.get("LIARA_SRE_CODE"))
    args = ap.parse_args()
    backend = Backend(args.url)
    if backend.url_error:
        print(f"liara-sre: {backend.url_error}", file=sys.stderr)   # stderr only: stdout is the protocol
    server = Server(backend, args.code)
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            msg = None
        if not isinstance(msg, dict):
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}) + "\n")
            stdout.flush()
            continue
        try:
            reply = server.handle(msg)
        except Exception as e:                                    # noqa: BLE001 — keep serving
            reply = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": type(e).__name__}}
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


if __name__ == "__main__":
    main()
