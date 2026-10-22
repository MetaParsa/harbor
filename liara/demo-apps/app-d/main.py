"""A tiny Flask shop page (reads SHOP_NAME from the environment or a local .env file)."""

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
