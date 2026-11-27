import os
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import routes_admin, routes_public
from .core import BASE_DIR, LoginRequired, hash_password, render
from sqlalchemy import inspect, text

from .db import Base, SessionLocal, engine
from .models import Question, User

app = FastAPI(title="Avaliação dos Terapeutas", docs_url=None, redoc_url=None, openapi_url=None)

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    print("AVISO: SECRET_KEY não definida. Usando uma chave temporária "
          "(os logins caem a cada reinício). Defina SECRET_KEY em produção.")

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=60 * 60 * 12,
                   same_site="lax", https_only=os.getenv("HTTPS_ONLY", "0") == "1")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(routes_admin.router)
app.include_router(routes_public.router)


@app.middleware("http")
async def headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.exception_handler(LoginRequired)
async def login_required(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", 303)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    titles = {403: "Acesso não permitido", 404: "Página não encontrada"}
    return render(request, "pub_msg.html", status_code=exc.status_code,
                  title=titles.get(exc.status_code, "Algo deu errado"),
                  text=exc.detail if exc.status_code == 403 else "Confira o endereço e tente de novo.")


@app.get("/")
def index():
    return RedirectResponse("/login", 303)


@app.get("/robots.txt")
def robots():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


# Perguntas do formulário que a clínica já usava (editáveis em Painel > Perguntas).
# (texto, tipo, oferece "Não sei dizer", conta no índice)
PERGUNTAS_INICIAIS = [
    ("É pontual nos atendimentos?", "simnao", False, True),
    ("Desmarca pouco os atendimentos?", "simnao", False, True),
    ("Faz as reposições quando falta?", "simnao", True, True),
    ("Realiza as devolutivas em sala nos minutos finais do atendimento?", "simnao", False, True),
    ("Tira suas dúvidas sobre o tratamento?", "simnao", False, True),
    ("Você percebe evolução nesta terapia?", "simnao", True, False),
    ("De 1 a 5, qual a sua satisfação geral com o atendimento deste(a) terapeuta?", "nota", False, True),
]


# Colunas adicionadas depois que o sistema entrou no ar. create_all() só cria tabelas
# novas, então cada coluna nova de uma tabela existente entra aqui e é criada uma única vez.
# Os dados que já existem recebem o valor padrão (tudo o que havia era do intensivo).
MIGRACOES = [
    ("patients", "program", "VARCHAR(20) NOT NULL DEFAULT 'intensivo'"),
    ("rounds", "program", "VARCHAR(20) NOT NULL DEFAULT 'intensivo'"),
]


def migrate():
    existing = {t: {c["name"] for c in inspect(engine).get_columns(t)}
                for t in {t for t, _, _ in MIGRACOES}}
    with engine.begin() as conn:
        for table, column, ddl in MIGRACOES:
            if column not in existing[table]:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                print(f"Migração: coluna {table}.{column} criada.")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    migrate()
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            username = os.getenv("ADMIN_USER", "admin").lower()
            password = os.getenv("ADMIN_PASSWORD")
            if not password:
                password = secrets.token_urlsafe(9)
                print(f"\n*** Primeiro acesso. Usuário: {username}  Senha: {password} ***\n"
                      "*** Anote e troque em Painel > Minha senha. ***\n")
            db.add(User(username=username, name=os.getenv("ADMIN_NAME") or username.capitalize(), role="admin",
                        password_hash=hash_password(password)))
        if db.query(Question).count() == 0:
            for i, (text, kind, ns, idx) in enumerate(PERGUNTAS_INICIAIS, start=1):
                db.add(Question(text=text, kind=kind, allow_ns=ns, in_index=idx, position=i))
        db.commit()
        if os.getenv("DEMO", "0") == "1":
            from .demo import seed
            seed(db)
    finally:
        db.close()
