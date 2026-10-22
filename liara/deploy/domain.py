"""Custom domain card: add the domain (T2) → the records Liara asks for → registrar / IRNIC steps →
«انجام دادم» re-checks → free SSL (T2) → probe https://domain.

Liara's API adds the domain and issues the certificate; the DNS change itself happens at the user's registrar
(for .ir: nic.ir, which only takes nameservers) — capability `irnic_ns`, said plainly. Record values come from
Liara's own `check` answer, never hard-coded. The IRNIC panel clicks are our own guide.
"""

import re

from liara.agent.actions import DOMAIN
from liara.agent.policy import Session
from liara.liara_api import LiaraError
from liara.liara_api.client import probe
from liara.sre.capabilities import capability

VERIFIED = {"OK", "ACTIVE", "VERIFIED", "CONNECTED", "DONE", "ENABLE", "ENABLED"}
SECOND_LEVEL = {"co", "ac", "org", "gov", "id", "net", "sch", "com", "edu"}   # example.co.ir, example.co.uk, example.com.tr


def clean_domain(text: str) -> str | None:
    d = re.sub(r"^[a-z]+://", "", (text or "").strip().lower()).split("/")[0].split(":")[0].rstrip(".")
    return d if DOMAIN.match(d) else None


def record_name(domain: str) -> str:
    """'@' for the domain itself, else the subdomain part (shop.example.ir → shop)."""
    labels = domain.split(".")
    apex = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SECOND_LEVEL else 2
    return "@" if len(labels) <= apex else ".".join(labels[:-apex])


def guide(domain: str) -> dict:
    if domain.endswith(".ir"):
        return {"title": "دامنه‌ی .ir (ایرنیک)", "ours": True, "steps": [
            "در ایرنیک (nic.ir) فقط «سرورهای نام» عوض می‌شه، نه رکورد؛ پس رکوردها رو در DNS لیارا می‌ذاریم.",
            "console.liara.ir ← DNS ← افزودن دامنه (بدون www و https). لیارا دو سرور نام بهت می‌ده (معمولاً ns1.liara.zone و ns2.liara.zone).",
            "nic.ir ← ورود ← دامنه‌های من ← همین دامنه ← تغییر سرورهای نام ← اون دو سرور نام رو بذار و ذخیره کن.",
            "در DNS لیارا رکوردهای بالا رو اضافه کن (اگر خودش اضافه نکرده بود).",
            "عوض شدن سرور نام تا ۲۴ ساعت طول می‌کشه. هر وقت خواستی «انجام دادم، بررسی کن» رو بزن."]}
    return {"title": "سایر ثبت‌کننده‌ها", "ours": True, "steps": [
        "وارد پنل DNS جایی شو که دامنه رو ازش خریدی (یا Cloudflare / ابر آروان اگر DNS اونجاست).",
        "رکوردهای بالا رو دقیقاً با همین نام و مقدار اضافه کن.",
        "اگر از CDN ابری استفاده می‌کنی، پروکسی رکورد _acme-challenge رو خاموش کن تا SSL صادر بشه.",
        "اعمال DNS تا ۲۴ ساعت طول می‌کشه. هر وقت خواستی «انجام دادم، بررسی کن» رو بزن."]}


async def domain_status(s: Session, app: str, domain: str, *, prober=probe) -> dict:
    """Where this domain stands, with the exact records to set. Never raises: Liara errors become a message."""
    try:
        found = next((d for d in await s.client.list_domains(app) if d.get("name") == domain), None)
        if not found:
            return {"domain": domain, "state": "not_added"}
        chk = await s.client.check_domain(found["_id"])
    except LiaraError as e:
        return {"domain": domain, "state": "error", "message": e.fa}
    info = {**found, **(chk.get("domain") or chk)}
    status, cert = str(info.get("status") or ""), str(info.get("certificatesStatus") or "")
    records = []
    if info.get("CNameRecord"):
        records.append({"type": "CNAME" if record_name(domain) != "@" else "ALIAS یا CNAME", "name": record_name(domain),
                        "value": info["CNameRecord"]})
    if info.get("verificationCode"):
        records.append({"type": "TXT", "name": record_name(domain), "value": info["verificationCode"],
                        "note": "برای تأیید مالکیت؛ اگر کنسول لیارا اسم دیگه‌ای گفت، همون درسته"})
    verified, ssl = status.upper() in VERIFIED, cert.upper() == "ACTIVE"
    out = {"domain": domain, "app": app, "state": "ssl_active" if ssl else "verified" if verified else "waiting_dns",
           "status": status, "certificates": cert, "records": records, "guide": guide(domain),
           "limit": capability("irnic_ns")}
    if ssl:
        r = (await prober(f"https://{domain}/", 1))[0]
        out["https"] = {"status": r.get("status"), "ms": r.get("ms")}
    return out
