# liara-sre — connect your coding agent to the deploy assistant

One file, Python standard library only (`liara_sre_mcp.py`): nothing to `pip install`, so a flaky package mirror
can't stop it. It talks to the assistant's web backend and never sees your API token.

## Install

Claude Code:

```bash
claude mcp add liara-sre -- uv run --no-project python /path/to/liara_sre_mcp.py --url https://<assistant-host>
```

Cline / Kilo Code (MCP settings JSON):

```json
{ "mcpServers": { "liara-sre": {
    "command": "uv",
    "args": ["run", "--no-project", "python", "/path/to/liara_sre_mcp.py", "--url", "https://<assistant-host>"] } } }
```

For local development use `--url http://localhost:8771`. Plain `http://` is refused for any other host.

## Use

1. In the assistant's page press «وصل کردن به ایجنت کدنویسی» → an 8-character code (valid 10 minutes, single use).
2. Tell your agent: *"pair with the deploy assistant using code ABCD2345"*.
3. *"My site's checkout gives 500 — use liara-sre to find out why"* → `troubleshoot` returns the cause, the file and
   line (mapped to your project), and the recommended fix.
4. The agent edits the code → `deploy_project` → **you approve the card in the assistant's page** →
   `wait_for_approval` → `verify_fix` (checks the failing page against the success contract).
5. Still broken after three changes? `support_ticket` writes a redacted ticket for the platform⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ support.

| Tool | Does |
|---|---|
| `pair` | exchange the page's code for a bridge token (memory only; dies with the web session) |
| `troubleshoot` | evidence → ranked causes → files/lines → recommended fix (read-only) |
| `deploy_project` | pack the folder (the platform⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ ignore rules; `node_modules`, `.git`, `.env*`, `vendor`, `bin/obj` excluded; ≤ 128 MB) and propose a deploy |
| `wait_for_approval` | wait ≤ 55 s for your decision; call again on `timed_out` |
| `verify_fix` | check the fix worked (new container, failing route, new errors, memory trend) |
| `ci_setup` | the GitHub Actions workflow that deploys the app on every push; you write it into the repo, the user adds the `LIARA_API_TOKEN` secret themselves |
| `check_capability` | what the platform's⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ API can't do, and the manual steps |
| `support_ticket` | redacted ticket text from the latest incident |

There is no approve tool. Approval is only possible from the assistant's page in your browser.
