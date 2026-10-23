"""Deploy from a public GitHub repo link: fetch the repo's zip from GitHub, then the same analysis as an uploaded zip.

Only github.com links are accepted, and the download may only land on GitHub's own archive host (no open redirect,
no other host: the server never fetches a URL the user typed). Private repos need a GitHub token, which we don't
ask for: the page says to upload a zip instead. Size is capped while streaming, like an upload.
"""

import re

import httpx

LINK = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100}?)"
                  r"(?:\.git)?(?:/tree/((?:[A-Za-z0-9_-][A-Za-z0-9._-]*/?){1,10}))?/?$")
ARCHIVE_HOSTS = {"github.com", "codeload.github.com"}
TIMEOUT_S = 60


class GithubError(Exception):
    def __init__(self, fa: str):
        super().__init__(fa)
        self.fa = fa


def parse_link(url: str) -> tuple[str, str, str | None] | None:
    """(owner, repo, branch or None) from a github.com repo link, else None."""
    m = LINK.match((url or "").strip())
    if not m or m.group(2) in (".", "..") or ".." in (m.group(3) or ""):
        return None
    return m.group(1), m.group(2), m.group(3)


async def fetch_zip(owner: str, repo: str, branch: str | None, limit: int,
                    transport: httpx.AsyncBaseTransport | None = None) -> bytes:
    """The repo as a zip (GitHub's archive of the default branch, or of `branch`), at most `limit` bytes."""
    ref = f"refs/heads/{branch}" if branch else "HEAD"
    url = f"https://github.com/{owner}/{repo}/archive/{ref}.zip"
    async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_S, follow_redirects=False) as c:
        for _ in range(3):                                   # github.com → codeload.github.com, nowhere else
            req = c.build_request("GET", url)
            resp = await c.send(req, stream=True)
            if resp.status_code in (301, 302, 303, 307, 308):
                nxt = httpx.URL(resp.headers.get("location", ""))
                await resp.aclose()
                if nxt.scheme != "https" or nxt.host not in ARCHIVE_HOSTS:
                    raise GithubError("گیت‌هاب به آدرس غیرمنتظره‌ای فرستاد؛ دانلود انجام نشد.")
                url = str(nxt)
                continue
            try:
                if resp.status_code == 404:
                    raise GithubError("این مخزن پیدا نشد یا خصوصیه. برای مخزن خصوصی، zip پروژه رو آپلود کن.")
                if resp.status_code != 200:
                    raise GithubError(f"گیت‌هاب الان جواب درست نداد ({resp.status_code})؛ کمی بعد دوباره امتحان کن.")
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise GithubError(f"حجم این مخزن از {limit // (1024 * 1024)} مگابایت بیشتره؛ zip کوچک‌تری آپلود کن.")
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                await resp.aclose()
    raise GithubError("گیت‌هاب بیش از حد جابه‌جا کرد؛ دانلود انجام نشد.")
