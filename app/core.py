"""Peças comuns: senha, sessão, templates, helpers."""
import hashlib
import hmac
import os
import re
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .db import get_db
from .models import User

BASE_DIR = Path(__file__).parent
CLINIC_NAME = os.getenv("CLINIC_NAME", "Clínica Exemplo")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")

ROLES = {"admin": "Administrador", "direcao": "Direção", "recepcao": "Recepção"}
# Programas de atendimento. Cada rodada é de um programa e os resultados não se misturam.
PROGRAMS = {"intensivo": "Intensivo", "particular": "Particular"}


def program_of(value, default: str = "intensivo") -> str:
    value = str(value or "").strip().lower()
    return value if value in PROGRAMS else default
TODOS = ("admin", "direcao", "recepcao")
GESTAO = ("admin", "direcao")
ADMIN = ("admin",)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ---------- senha ----------
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return f"pbkdf2${salt}${h}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return hmac.compare_digest(calc, h)


def new_token() -> str:
    return secrets.token_urlsafe(16)


# ---------- login ----------
class LoginRequired(Exception):
    pass


def need(*roles):
    """Dependência: exige login e, se informado, um dos perfis."""
    def dep(request: Request, db: Session = Depends(get_db)) -> User:
        uid = request.session.get("uid")
        user = db.get(User, uid) if uid else None
        if not user or not user.active:
            request.session.clear()
            raise LoginRequired()
        if roles and user.role not in roles:
            raise HTTPException(403, "Seu perfil não tem acesso a esta página.")
        request.state.user = user
        return user
    return dep


# ---------- helpers ----------
def flash(request: Request, msg: str, kind: str = "ok"):
    request.session.setdefault("flash", []).append([kind, msg])


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx["flashes"] = request.session.pop("flash", [])
    ctx["clinic"] = CLINIC_NAME
    ctx["ROLES"] = ROLES
    ctx["PROGRAMS"] = PROGRAMS
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def base_url(request: Request) -> str:
    return BASE_URL or str(request.base_url).rstrip("/")


def guardian_link(request: Request, token: str) -> str:
    return f"{base_url(request)}/a/{token}"


def wa_link(phone: str, text: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) in (10, 11):
        digits = "55" + digits
    if len(digits) < 12:
        return ""
    return f"https://wa.me/{digits}?text={quote(text)}"


def invite_text(guardian_name: str, child_first_name: str, link: str) -> str:
    first = guardian_name.split()[0] if guardian_name.strip() else ""
    hello = f"Olá, {first}!" if first else "Olá!"
    return (f"{hello} A {CLINIC_NAME} quer ouvir você. Avalie os terapeutas que atendem "
            f"{child_first_name}: leva poucos minutos e você pode parar e continuar depois "
            f"pelo mesmo link.\n{link}")


# ---------- filtros de template ----------
def f_dt(value):
    if not value:
        return ""
    return (value - timedelta(hours=3)).strftime("%d/%m/%Y %H:%M")  # horário de Brasília


def f_nota(value):
    if value is None:
        return "–"
    return f"{value:.1f}".replace(".", ",")


def f_pct(value):
    return "–" if value is None else f"{value:.0f}%"


def f_qfmt(value, kind):
    """Resultado de uma pergunta: % de Sim, ou média de 1 a 5."""
    if value is None:
        return "–"
    return f"{value * 100:.0f}%" if kind == "simnao" else f_nota(value)


def f_initials(name: str) -> str:
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "?"
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()


templates.env.filters["dt"] = f_dt
templates.env.filters["nota"] = f_nota
templates.env.filters["initials"] = f_initials
templates.env.filters["pct"] = f_pct
templates.env.filters["qfmt"] = f_qfmt
