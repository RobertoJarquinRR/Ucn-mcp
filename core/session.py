"""Moodle session management: login via HTTP, cookie persistence."""
from __future__ import annotations

import json
import os
import pickle
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COOKIE_DIR = PROJECT_ROOT / "storage"
COOKIE_FILE = COOKIE_DIR / "session.pkl"

DEFAULT_BASE = "https://ucnvirtual.edu.ni/grado"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class MoodleSession:
    """A persistent requests.Session bound to a Moodle instance."""

    def __init__(self, base: str = DEFAULT_BASE, username: str = "", password: str = ""):
        self.base = base.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        })
        self.logged_in = False

    # ---- constructors -------------------------------------------------
    @classmethod
    def from_env(cls) -> "MoodleSession":
        """Build from env vars (loaded from .env or Hermes vault if present)."""
        _load_dotenv()
        base = os.environ.get("UCN_BASE_URL", DEFAULT_BASE)
        user = os.environ.get("UCN_USERNAME", "")
        pwd = os.environ.get("UCN_PASSWORD", "")
        return cls(base=base, username=user, password=pwd)

    # ---- persistence --------------------------------------------------
    def save_cookies(self) -> None:
        COOKIE_DIR.mkdir(parents=True, exist_ok=True)
        with open(COOKIE_FILE, "wb") as f:
            pickle.dump(self.session.cookies, f)

    def load_cookies(self) -> bool:
        if not COOKIE_FILE.exists():
            return False
        try:
            with open(COOKIE_FILE, "rb") as f:
                self.session.cookies = pickle.load(f)
            return True
        except Exception:
            return False

    # ---- auth ---------------------------------------------------------
    def _fetch_logintoken(self) -> str:
        r = self.session.get(f"{self.base}/login/index.php", timeout=30)
        soup = BeautifulSoup(r.text, "lxml")
        el = soup.find("input", {"name": "logintoken"})
        return el["value"] if el else ""

    def is_logged_in(self) -> bool:
        r = self.session.get(f"{self.base}/my/", timeout=30, allow_redirects=True)
        return "/login/" not in r.url

    def login(self, use_cached: bool = True) -> bool:
        """Log in (or reuse cached cookies). Returns True on success."""
        if use_cached and self.load_cookies():
            if self.is_logged_in():
                self.logged_in = True
                return True

        if not self.username or not self.password:
            raise RuntimeError(
                "No credentials. Set UCN_USERNAME / UCN_PASSWORD in .env or the vault."
            )

        token = self._fetch_logintoken()
        payload = {
            "username": self.username,
            "password": self.password,
            "logintoken": token,
            "anchor": "",
        }
        r = self.session.post(
            f"{self.base}/login/index.php", data=payload, timeout=30, allow_redirects=True
        )
        if "Invalid login" in r.text or "/login/" in r.url:
            raise RuntimeError("Moodle login failed: invalid credentials")

        self.logged_in = True
        self.save_cookies()
        return True

    # ---- low-level helpers -------------------------------------------
    def get_soup(self, url: str) -> BeautifulSoup:
        r = self.session.get(url, timeout=30)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")


def _load_dotenv() -> None:
    """Minimal .env loader (no dependency surprises)."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)
