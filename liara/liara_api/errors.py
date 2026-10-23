"""Liara API errors mapped to a small set of kinds, each with a plain Persian line for the user.

Mapping is from live responses plus Liara's documented error codes.
"""

from dataclasses import dataclass

# kind -> Persian sentence shown to a non-technical user
FA = {
    "auth": "توکن لیارا معتبر نیست یا منقضی شده. یک توکن تازه از کنسول لیارا بساز.",
    "no_funds": "اعتبار حساب لیارا کافی نیست. لطفاً حسابت رو شارژ کن و بگو «انجام دادم».",
    "frozen": "این برنامه به‌خاطر کمبود اعتبار متوقف (فریز) شده. بعد از شارژ حساب، از کنسول لیارا فعالش کن.",
    "free_plan": "پلن رایگان اجازه‌ی این کار رو نمی‌ده؛ یک پلن پولی لازمه.",
    "quota": "سهمیه‌ی دیپلوی امروزِ این بسته تموم شده. فردا دوباره می‌شه، یا بسته‌ی بالاتر سهمیه‌ی بیشتری داره.",
    "not_found": "این مورد در حساب لیارا پیدا نشد.",
    "name_taken": "این اسم قبلاً در لیارا گرفته شده؛ یک اسم دیگه لازمه.",
    "too_big": "حجم کد از سقف لیارا بیشتره. پوشه‌های اضافی (مثل node_modules یا فایل‌های بزرگ) باید کنار گذاشته بشن.",
    "precondition": "لیارا این درخواست رو با این مقدارها قبول نکرد.",
    "bad_request": "لیارا درخواست رو نامعتبر دونست.",
    "server": "سرور لیارا الان خطا داد؛ چند لحظه بعد دوباره امتحان می‌کنم.",
    "network": "به API لیارا وصل نشدم (شبکه یا قطعی موقت).",
    "not_in_plan": "بسته‌ی فعلی این برنامه این امکان رو نداره. برای داشتنش باید بسته رو از کنسول لیارا ارتقا بدی؛ "
                   "این کار از طریق API ممکن نیست.",
    "demo": "این نمایش ضبط‌شده است: فقط راه‌حلی که برای هر برنامه واقعاً روی لیارا انجام و ضبط شده (شبیه‌سازی‌شده) اجرا "
            "می‌شه. برای انجام واقعی، با کلید API خودت وصل شو.",
}


@dataclass
class LiaraError(Exception):
    kind: str
    status: int | None
    message: str          # Liara's own message (English), for logs and the receipts drawer
    path: str = ""

    @property
    def fa(self) -> str:
        return FA.get(self.kind, FA["bad_request"])

    def __str__(self) -> str:
        return f"{self.kind} ({self.status}) on {self.path}: {self.message}"


def error_kind(status: int, body) -> str:
    """Turn an HTTP status + Liara error body into one of the FA kinds."""
    text = " ".join(str(body.get(k, "")) for k in ("error", "message", "data")) if isinstance(body, dict) else str(body)
    low = text.lower()
    if "replay_read_only" in low:
        return "demo"
    if "plan does not support" in low:          # live: 428 "This plan does not support zero downtime" (basic bundle)
        return "not_in_plan"
    if status == 401:
        return "auth"
    if status == 402:
        return "no_funds"
    if "frozen_project" in low:
        return "frozen"
    if "free_plan" in low:
        return "free_plan"
    if "max_deployment_count_in_day" in low:
        return "quota"
    if status == 404:
        return "not_found"
    if status == 409:
        return "name_taken"
    if status == 413:
        return "too_big"
    if status == 428:
        return "precondition"
    if status >= 500:
        return "server"
    return "bad_request"


def from_response(status: int, body, path: str) -> LiaraError:
    if isinstance(body, dict):
        msg = body.get("message") or body.get("error") or "no message"
    else:
        msg = str(body)
    if "update-envs" in path:
        msg = f"env update rejected ({status})"   # an echoed body could contain env values: never keep it
    return LiaraError(error_kind(status, body), status, str(msg)[:300], path)
