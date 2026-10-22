"""What Liara's API can't do, said plainly (rubric #4 "declaring API limits").

`api`: full | partial | none, verified live where marked (live tests). The SRE board shows the
Persian message + steps whenever a fix needs one of these, instead of pretending.
"""

CAPABILITIES: dict[str, dict] = {
    "top_up": {
        "api": "none", "verified": True,
        "fa": "شارژ حساب از طریق API لیارا ممکن نیست؛ باید خودت انجامش بدی.",
        "steps": ["console.liara.ir ← کیف پول ← افزایش اعتبار", "بعد از شارژ، اگر برنامه فریز شده، از صفحه‌ی برنامه روشنش کن",
                  "«انجام دادم» رو بزن تا دوباره بررسی کنم"],
    },
    "balance": {
        "api": "partial", "verified": True,
        "fa": "موجودی‌ای که API لیارا نشون می‌ده اعتبار هدیه/هکاتون رو شامل نمی‌شه؛ عدد دقیق رو در کنسول ببین.",
        "steps": ["console.liara.ir ← کیف پول"],
    },
    "rollback_console": {
        "api": "partial", "verified": True,
        "fa": "لیارا API بازگشت به نسخه‌ی قبلی نداره. من فقط نسخه‌هایی رو برمی‌گردونم که خودم منتشر کردم؛ این یکی از کنسول منتشر شده.",
        "steps": ["console.liara.ir ← برنامه ← استقرارها (Releases) ← نسخه‌ی سالم ← بازگشت"],
    },
    "bundle_change": {
        "api": "none", "verified": False,
        "fa": "تغییر بسته (برنزی/نقره‌ای/طلایی) از طریق API ممکن نیست.",
        "steps": ["console.liara.ir ← برنامه ← تنظیمات ← تغییر بسته"],
    },
    "unfreeze": {
        "api": "none", "verified": False,
        "fa": "روشن کردن برنامه‌ی فریزشده بعد از شارژ از طریق API پیدا نشد.",
        "steps": ["console.liara.ir ← برنامه ← روشن کردن"],
    },
    "irnic_ns": {
        "api": "none", "verified": True,
        "fa": "تنظیم DNS در پنل ثبت دامنه (مثل ایرنیک) بیرون از لیاراست؛ باید خودت انجامش بدی.",
        "steps": ["nic.ir ← ورود ← دامنه‌ها ← تغییر سرورهای نام (راهنمای ما؛ ممکنه پنل کمی فرق کنه)"],
    },
    "github_autodeploy": {
        "api": "none", "verified": True,
        "fa": "وصل کردن مخزن گیت‌هاب برای دیپلوی خودکار فقط از کنسول لیارا ممکنه (API و وب‌هوک نداره). "
              "راه دیگه: فایل GitHub Actions که من می‌سازم، با یک secret در گیت‌هاب.",
        "steps": ["console.liara.ir ← تنظیمات حساب ← گیت‌هاب ← دسترسی به مخزن رو بده",
                  "برنامه ← استقرار تازه ← گیت‌هاب ← مخزن و شاخه رو انتخاب کن",
                  "«دیپلوی بعد از موفقیت CI» یا «دیپلوی خودکار» ← اتصال",
                  "در این حالت در liara.json کلیدهای app و platform نباشن"],
    },
    "zero_downtime_basic": {
        "api": "none", "verified": True,
        "fa": "بسته‌ی پایه (برنزی) لیارا انتشار بدون قطعی نداره؛ لیارا درخواستش رو رد می‌کنه. تغییر بسته هم از API ممکن نیست.",
        "steps": ["console.liara.ir ← برنامه ← تنظیمات ← تغییر بسته", "بعد از ارتقا، «روشنش کن» رو در کارت خانه بزن"],
    },
    "http_metrics": {
        "api": "none", "verified": True,
        "fa": "لیارا آمار کد وضعیت و زمان پاسخ نمی‌ده؛ این اعداد از بررسی خودمون و از لاگ برنامه تخمین زده می‌شن.",
        "steps": [],
    },
    "alerts": {
        "api": "none", "verified": True,
        "fa": "لیارا وب‌هوک یا هشدار از طریق API نداره؛ وقتی مشکلی دیدی از من بپرس.",
        "steps": [],
    },
}


def capability(name: str) -> dict:
    c = CAPABILITIES.get(name)
    return {"name": name, **c} if c else {"name": name, "api": "unknown", "fa": "این مورد بررسی نشده.", "steps": []}
