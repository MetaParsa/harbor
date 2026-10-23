# Demo apps

- `demo-shop/` — the SRE chapter (see its README): three releases of a Node shop with a missing-env fault, a code bug and a memory leak.
- `app-d/` — the first-deploy chapter. A tiny Flask app as a developer would hand it over, with three things that break a
  first deploy on the platform (verified live 2026-10-22, `liara/l0/RESULTS.md` §3):
  1. the app lives in `main.py`, but the platform's Flask platform looks for `app.py` → fixed **before** upload (preflight sets `appModule: main:app`);
  2. the platform's Flask platform installs `gunicorn==20.1.0` + the newest `setuptools`, which no longer has `pkg_resources` → every
     fresh Flask deploy crash-loops → healed from the app logs by pinning `setuptools<80` (a real platform bug, not planted);
  3. `main.py` imports `dotenv`, which `requirements.txt` forgot → healed from the app logs by adding `python-dotenv`.
  The release says READY each time; only the app logs show the crashes — which is the point of the demo.
