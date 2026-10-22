"""Demo app D: a tiny Flask site with two deliberate deploy bugs.

1. The app lives in main.py, but Liara's Flask platform looks for app.py
   (fix: liara.json {"flask": {"appModule": "main:app"}}).
2. It imports python-dotenv, which requirements.txt forgot
   (fix: add python-dotenv to requirements.txt).
"""

import os

from dotenv import load_dotenv
from flask import Flask

load_dotenv()

app = Flask(__name__)


@app.get("/")
def home():
    name = os.environ.get("SHOP_NAME", "فروشگاه دمو")
    return f"<h1>{name}</h1><p>app-d ok</p>"


@app.get("/health")
def health():
    return {"ok": True}
