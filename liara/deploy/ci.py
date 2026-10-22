"""Continuous deployment: the GitHub Actions workflow Liara documents (docs.liara.ir → CI/CD, Liara CLI v9).

`liara deploy` without --detach waits for the build and fails the job when the release fails; --no-app-logs keeps it
from following the app's logs forever. The Liara key stays a GitHub secret: it is never written into the file and
never passes through this assistant. (Liara's own "connect GitHub" auto-deploy exists only in the console — see the
`github_autodeploy` capability.)
"""

import re

WORKFLOW_PATH = ".github/workflows/liara.yml"
SECRET = "LIARA_API_TOKEN"
BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,100}\Z")
STEPS_FA = [
    f"فایل رو در مخزنت در مسیر {WORKFLOW_PATH} بذار.",
    "در console.liara.ir یک کلید API با کوتاه‌ترین اعتبار ممکن بساز (کلیدهای لیارا به همه‌چیز دسترسی دارن).",
    f"در گیت‌هاب: Settings ← Secrets and variables ← Actions ← New repository secret، با اسم {SECRET}.",
    "push کن و نتیجه رو در تب Actions ببین؛ اگر بیلد یا اجرا خراب بشه، کار قرمز می‌شه.",
]


def github_workflow(app: str, port: int, team_id: str | None, branch: str = "main") -> str:
    branch = branch if BRANCH.match(branch or "") else "main"
    team = f' --team-id="{team_id}"' if team_id else ""
    return f"""# Deploy {app} to Liara on every push to {branch}. Needs one repository secret: {SECRET}
# (GitHub → Settings → Secrets and variables → Actions → New repository secret).
name: Deploy to Liara
on:
  push:
    branches: [{branch}]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "22"
      - name: Deploy
        env:
          LIARA_TOKEN: ${{{{ secrets.{SECRET} }}}}
        run: |
          npm i -g @liara/cli@9
          liara deploy --app="{app}" --port={port}{team} --api-token="$LIARA_TOKEN" --no-app-logs --message="CI ${{{{ github.sha }}}}"
"""
