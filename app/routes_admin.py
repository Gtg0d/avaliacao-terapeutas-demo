"""Painel interno: recepção, direção e administrador."""
import io
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from PIL import Image, ImageOps
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from .core import (ADMIN, GESTAO, PROGRAMS, ROLES, TODOS, check_password, flash,
                   guardian_link, hash_password, invite_text, need, new_token, program_of,
                   render, wa_link)
from .db import get_db
from .models import (Answer, Assignment, Evaluation, Guardian, Patient,
                     Question, Round, Submission, Therapist, User, utcnow)
from .stats import (MIN_COBERTURA, MIN_RESPOSTAS, STATUS_LABEL, clinic_comments, open_round,
                    pendencias, scored_questions, scores, therapist_history,
                    tracking)

router = APIRouter()


def go(url: str):
    return RedirectResponse(url, 303)


def clean(value, limit=200) -> str:
    return " ".join(str(value or "").split())[:limit]


# ======================= login =======================
_fails: dict[str, list[float]] = defaultdict(list)


@router.get("/login")
def login_page(request: Request):
    if request.session.get("uid"):
        return go("/painel")
    return render(request, "login.html", error=None)


@router.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    ip = request.client.host if request.client else "?"
    now = time.time()
    _fails[ip] = [t for t in _fails[ip] if now - t < 900]
    if len(_fails[ip]) >= 10:
        return render(request, "login.html", status_code=429,
                      error="Muitas tentativas. Aguarde 15 minutos e tente de novo.")
    username = clean(form.get("username")).lower()
    user = db.query(User).filter(func.lower(User.username) == username).first()
    if not user or not user.active or not check_password(str(form.get("password") or ""), user.password_hash):
        _fails[ip].append(now)
        return render(request, "login.html", status_code=401, error="Usuário ou senha incorretos.")
    request.session.clear()
    request.session["uid"] = user.id
    return go("/painel")


@router.get("/sair")
def logout(request: Request):
    request.session.clear()
    return go("/login")


@router.get("/painel/senha")
def password_page(request: Request, user: User = Depends(need())):
    return render(request, "senha.html")


@router.post("/painel/senha")
async def password_save(request: Request, db: Session = Depends(get_db), user: User = Depends(need())):
    form = await request.form()
    new = str(form.get("new") or "")
    if not check_password(str(form.get("current") or ""), user.password_hash):
        flash(request, "A senha atual não confere.", "erro")
    elif len(new) < 8:
        flash(request, "A nova senha precisa ter pelo menos 8 caracteres.", "erro")
    else:
        db.get(User, user.id).password_hash = hash_password(new)
        db.commit()
        flash(request, "Senha alterada.")
    return go("/painel/senha")


# ======================= início =======================
@router.get("/painel")
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(need())):
    blocks = []
    for prog, label in PROGRAMS.items():
        n_pat = db.query(Patient).filter(Patient.active.is_(True), Patient.program == prog).count()
        rnd = open_round(db, prog)
        if not n_pat and not rnd:
            continue
        rows = tracking(db, rnd, prog)
        counts = defaultdict(int)
        for r in rows:
            counts[r["status"]] += 1
        blocks.append({"program": prog, "label": label, "rnd": rnd, "counts": counts,
                       "total": len(rows), "n_patients": n_pat})
    open_rounds = [b["rnd"] for b in blocks if b["rnd"]]
    return render(request, "painel.html", blocks=blocks,
                  pend=pendencias(db, open_rounds),
                  n_patients=db.query(Patient).filter(Patient.active.is_(True)).count(),
                  n_therapists=db.query(Therapist).filter(Therapist.active.is_(True)).count())


# ======================= acompanhamento / links =======================
@router.get("/painel/acompanhamento")
def tracking_page(request: Request, f: str = "", p: str = "", db: Session = Depends(get_db),
                  user: User = Depends(need())):
    prog = program_of(p)
    rnd = open_round(db, prog)
    rows = tracking(db, rnd, prog)
    for r in rows:
        r["link"] = guardian_link(request, r["g"].token)
        r["msg"] = invite_text(r["g"].name, r["p"].first_name, r["link"])
        r["wa"] = wa_link(r["g"].phone, r["msg"])
        r["label"] = STATUS_LABEL[r["status"]]
    counts = defaultdict(int)
    for r in rows:
        counts[r["status"]] += 1
    if f == "pendentes":
        rows = [r for r in rows if r["status"] != "concluido"]
    elif f:
        rows = [r for r in rows if r["status"] == f]
    return render(request, "acompanhamento.html", rnd=rnd, rows=rows, f=f, counts=counts,
                  labels=STATUS_LABEL, prog=prog)


# ======================= rodadas =======================
@router.get("/painel/rodadas")
def rounds_page(request: Request, db: Session = Depends(get_db), user: User = Depends(need(*TODOS))):
    rounds = db.query(Round).order_by(Round.id.desc()).all()
    n = dict(db.query(Submission.round_id, func.count(Submission.id))
             .filter(Submission.completed_at.isnot(None)).group_by(Submission.round_id).all())
    return render(request, "rodadas.html", rounds=rounds, n=n)


@router.post("/painel/rodadas")
async def round_create(request: Request, db: Session = Depends(get_db),
                       user: User = Depends(need(*TODOS))):
    form = await request.form()
    name = clean(form.get("name"), 120)
    if not name:
        flash(request, "Dê um nome para a rodada.", "erro")
        return go("/painel/rodadas")
    prog = program_of(form.get("program"), "")
    if not prog:
        flash(request, "Escolha se a rodada é do Intensivo ou do Particular.", "erro")
        return go("/painel/rodadas")
    for r in db.query(Round).filter(Round.status == "open", Round.program == prog):
        r.status, r.closed_at = "closed", utcnow()
    db.add(Round(name=name, status="open", program=prog))
    db.commit()
    flash(request, f"Rodada “{name}” ({PROGRAMS[prog]}) aberta. Os links dos responsáveis "
                   f"do {PROGRAMS[prog]} já estão valendo.")
    return go("/painel/rodadas")


@router.post("/painel/rodadas/{rid}")
async def round_update(rid: int, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(need(*TODOS))):
    form = await request.form()
    r = db.get(Round, rid)
    if not r:
        raise HTTPException(404)
    action = form.get("action")
    if action == "delete":
        # Exclusão definitiva: só o administrador, e só de rodada encerrada.
        if user.role != "admin":
            raise HTTPException(403, "Só o administrador pode excluir rodadas.")
        if r.status == "open":
            flash(request, "Encerre a rodada antes de excluí-la.", "erro")
            return go("/painel/rodadas")
        subs = db.query(Submission).filter(Submission.round_id == rid).all()
        for s in subs:
            db.delete(s)  # apaga junto as avaliações e respostas (cascade)
        name = r.name
        db.delete(r)
        db.commit()
        flash(request, f"Rodada “{name}” excluída, com {len(subs)} participação(ões) de responsáveis.")
        return go("/painel/rodadas")
    if action == "close":
        r.status, r.closed_at = "closed", utcnow()
        flash(request, "Rodada encerrada.")
    elif action == "open":
        for o in db.query(Round).filter(Round.status == "open", Round.id != rid,
                                        Round.program == r.program):
            o.status, o.closed_at = "closed", utcnow()
        r.status, r.closed_at = "open", None
        flash(request, "Rodada reaberta.")
    elif action == "rename" and clean(form.get("name")):
        r.name = clean(form.get("name"), 120)
    db.commit()
    return go("/painel/rodadas")


# ======================= pacientes =======================
@router.get("/painel/pacientes")
def patients_page(request: Request, p: str = "", db: Session = Depends(get_db), user: User = Depends(need())):
    q = db.query(Patient).options(joinedload(Patient.guardians), joinedload(Patient.assignments))
    prog = p if p in PROGRAMS else ""
    if prog:
        q = q.filter(Patient.program == prog)
    patients = q.order_by(Patient.active.desc(), Patient.name).all()
    counts = dict(db.query(Patient.program, func.count(Patient.id)).group_by(Patient.program).all())
    return render(request, "pacientes.html", patients=patients, prog=prog, counts=counts)


@router.post("/painel/pacientes")
async def patient_create(request: Request, db: Session = Depends(get_db), user: User = Depends(need())):
    form = await request.form()
    name = clean(form.get("name"), 160)
    if not name:
        flash(request, "Informe o nome da criança.", "erro")
        return go("/painel/pacientes")
    prog = program_of(form.get("program"), "")
    if not prog:
        flash(request, "Marque se a criança é do Intensivo ou do Particular.", "erro")
        return go("/painel/pacientes")
    p = Patient(name=name, program=prog)
    db.add(p)
    db.commit()
    return go(f"/painel/pacientes/{p.id}")


def _patient(db, pid) -> Patient:
    p = db.get(Patient, pid)
    if not p:
        raise HTTPException(404)
    return p


@router.get("/painel/pacientes/{pid}")
def patient_page(pid: int, request: Request, db: Session = Depends(get_db), user: User = Depends(need())):
    p = _patient(db, pid)
    assigns = (db.query(Assignment).options(joinedload(Assignment.therapist))
               .filter(Assignment.patient_id == pid).join(Therapist)
               .order_by(Assignment.therapy, Therapist.name).all())
    guardians = []
    for g in p.guardians:
        link = guardian_link(request, g.token)
        msg = invite_text(g.name, p.first_name, link)
        guardians.append({"g": g, "link": link, "msg": msg, "wa": wa_link(g.phone, msg)})
    therapies = sorted({a.therapy for a in db.query(Assignment)})
    sectors = sorted({a.sector for a in db.query(Assignment) if a.sector})
    return render(request, "paciente.html", p=p, assigns=assigns, guardians=guardians, sectors=sectors,
                  therapists=db.query(Therapist).filter(Therapist.active.is_(True)).order_by(Therapist.name).all(),
                  therapies=therapies)


@router.post("/painel/pacientes/{pid}")
async def patient_update(pid: int, request: Request, db: Session = Depends(get_db),
                         user: User = Depends(need())):
    form = await request.form()
    p = _patient(db, pid)
    action = form.get("action")
    if action == "remove":
        has_data = (db.query(Submission).join(Guardian).filter(Guardian.patient_id == pid).count() > 0)
        if has_data:
            p.active = False
            db.commit()
            flash(request, f"{p.name} foi desativado(a): não aparece mais nos envios, "
                           "mas as avaliações já feitas continuam nos relatórios.")
        else:
            db.delete(p)
            db.commit()
            flash(request, "Paciente removido.")
        return go("/painel/pacientes")
    name = clean(form.get("name"), 160)
    if name:
        p.name = name
    p.active = form.get("active") == "1"
    p.notes = clean(form.get("notes"), 500)
    p.program = program_of(form.get("program"), p.program)
    db.commit()
    flash(request, "Dados salvos.")
    return go(f"/painel/pacientes/{pid}")


@router.post("/painel/pacientes/{pid}/responsavel")
async def guardian_add(pid: int, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(need())):
    form = await request.form()
    _patient(db, pid)
    name = clean(form.get("name"), 160)
    if not name:
        flash(request, "Informe o nome do responsável.", "erro")
    else:
        db.add(Guardian(patient_id=pid, name=name, phone=clean(form.get("phone"), 40), token=new_token()))
        db.commit()
        flash(request, "Responsável adicionado. O link dele já está disponível.")
    return go(f"/painel/pacientes/{pid}")


@router.post("/painel/responsaveis/{gid}")
async def guardian_update(gid: int, request: Request, db: Session = Depends(get_db),
                          user: User = Depends(need())):
    form = await request.form()
    g = db.get(Guardian, gid)
    if not g:
        raise HTTPException(404)
    pid = g.patient_id
    action = form.get("action")
    if action == "remove":
        if db.query(Submission).filter(Submission.guardian_id == gid).count():
            g.active = False
            flash(request, "Responsável desativado (as respostas antigas foram mantidas).")
        else:
            db.delete(g)
            flash(request, "Responsável removido.")
    elif action == "newlink":
        g.token = new_token()
        flash(request, "Novo link gerado. O link antigo deixou de funcionar.")
    else:
        name = clean(form.get("name"), 160)
        if name:
            g.name = name
        g.phone = clean(form.get("phone"), 40)
        g.active = True
        flash(request, "Responsável atualizado.")
    db.commit()
    return go(f"/painel/pacientes/{pid}")


def _get_or_create_therapist(db, name: str, sector: str = "") -> Therapist:
    t = db.query(Therapist).filter(func.lower(Therapist.name) == name.lower()).first()
    if not t:
        t = Therapist(name=name, sector=sector)
        db.add(t)
        db.flush()
    elif sector and not t.sector:
        t.sector = sector
    return t


def _default_sector(t: Therapist, therapy: str) -> str:
    """Setor de um vínculo quando não foi informado."""
    parts = [p.strip() for p in (t.sector or "").split("/") if p.strip()]
    if len(parts) == 1:
        return parts[0]
    for p in parts:
        if p.lower() == therapy.lower():
            return p
    return therapy if not parts else ""


@router.post("/painel/pacientes/{pid}/vinculo")
async def assignment_add(pid: int, request: Request, db: Session = Depends(get_db),
                         user: User = Depends(need())):
    form = await request.form()
    _patient(db, pid)
    therapy = clean(form.get("therapy"), 120)
    new_name = clean(form.get("new_name"), 120)
    tid = form.get("therapist_id")
    if not therapy or not (new_name or tid):
        flash(request, "Informe a terapia e o terapeuta.", "erro")
        return go(f"/painel/pacientes/{pid}")
    t = _get_or_create_therapist(db, new_name, therapy) if new_name else db.get(Therapist, int(tid))
    if not t:
        raise HTTPException(404)
    exists = db.query(Assignment).filter_by(patient_id=pid, therapist_id=t.id, therapy=therapy).first()
    if exists:
        flash(request, "Esse terapeuta já está vinculado nessa terapia.", "erro")
    else:
        sector = clean(form.get("sector"), 80) or _default_sector(t, therapy)
        db.add(Assignment(patient_id=pid, therapist_id=t.id, therapy=therapy, sector=sector,
                          for_guardian=form.get("for_guardian") == "1"))
        flash(request, f"{t.name} adicionado(a) em {therapy}.")
    db.commit()
    return go(f"/painel/pacientes/{pid}")


@router.post("/painel/vinculos/{aid}/remover")
def assignment_remove(aid: int, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(need())):
    a = db.get(Assignment, aid)
    if not a:
        raise HTTPException(404)
    pid = a.patient_id
    db.delete(a)  # avaliações antigas continuam guardadas, ligadas ao terapeuta
    db.commit()
    flash(request, "Terapeuta removido do cronograma desta criança.")
    return go(f"/painel/pacientes/{pid}")


# ======================= terapeutas =======================
@router.get("/painel/terapeutas")
def therapists_page(request: Request, db: Session = Depends(get_db), user: User = Depends(need())):
    ts = db.query(Therapist).order_by(Therapist.active.desc(), Therapist.name).all()
    n = dict(db.query(Assignment.therapist_id, func.count(func.distinct(Assignment.patient_id)))
             .group_by(Assignment.therapist_id).all())
    sectors = sorted({p.strip() for t in ts for p in (t.sector or "").split("/") if p.strip()})
    return render(request, "terapeutas.html", ts=ts, n=n, sectors=sectors)


@router.post("/painel/terapeutas")
async def therapist_create(request: Request, db: Session = Depends(get_db), user: User = Depends(need())):
    form = await request.form()
    name = clean(form.get("name"), 120)
    if not name:
        flash(request, "Informe o nome do terapeuta.", "erro")
        return go("/painel/terapeutas")
    t = Therapist(name=name, sector=clean(form.get("sector"), 160))
    db.add(t)
    db.commit()
    return go(f"/painel/terapeutas/{t.id}")


def _photo_bytes(raw: bytes) -> bytes:
    """Padroniza a foto: quadrada, 480 px, JPEG."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    img = ImageOps.fit(img.convert("RGB"), (480, 480), centering=(0.5, 0.35))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


@router.post("/painel/terapeutas/fotos")
async def therapist_photos(request: Request, db: Session = Depends(get_db), user: User = Depends(need())):
    """Envio em lote: o nome de cada arquivo deve ser o nome do terapeuta."""
    form = await request.form(max_files=300)
    by_name = {}
    for t in db.query(Therapist):
        by_name[_norm(t.name)] = t
        if t.full_name:
            by_name.setdefault(_norm(t.full_name), t)
    ok, unknown, bad = 0, [], []
    for up in form.getlist("photos"):
        filename = getattr(up, "filename", "") or ""
        if not filename:
            continue
        stem = filename.replace("\\", "/").split("/")[-1].rsplit(".", 1)[0]
        t = by_name.get(_norm(stem))
        if not t:
            unknown.append(stem)
            continue
        try:
            t.photo = _photo_bytes(await up.read())
        except Exception:
            bad.append(stem)
            continue
        t.has_photo = True
        t.photo_version += 1
        ok += 1
    db.commit()
    if ok:
        flash(request, f"{ok} foto(s) atualizada(s).")
    if unknown:
        flash(request, "Sem terapeuta com este nome: " + ", ".join(unknown[:30]), "erro")
    if bad:
        flash(request, "Não consegui ler: " + ", ".join(bad[:30]), "erro")
    if not (ok or unknown or bad):
        flash(request, "Nenhum arquivo selecionado.", "erro")
    return go("/painel/terapeutas")


@router.get("/painel/terapeutas/{tid}")
def therapist_page(tid: int, request: Request, db: Session = Depends(get_db), user: User = Depends(need())):
    t = db.get(Therapist, tid)
    if not t:
        raise HTTPException(404)
    assigns = (db.query(Assignment).options(joinedload(Assignment.patient))
               .filter(Assignment.therapist_id == tid).all())
    sectors = sorted({p.strip() for (s,) in db.query(Therapist.sector) for p in (s or "").split("/") if p.strip()})
    return render(request, "terapeuta.html", t=t, assigns=assigns, sectors=sectors)


@router.post("/painel/terapeutas/{tid}")
async def therapist_update(tid: int, request: Request, db: Session = Depends(get_db),
                           user: User = Depends(need())):
    form = await request.form()
    t = db.get(Therapist, tid)
    if not t:
        raise HTTPException(404)
    action = form.get("action")
    if action == "remove":
        if db.query(Evaluation).filter(Evaluation.therapist_id == tid).count():
            t.active = False
            flash(request, f"{t.name} foi desativado(a): não aparece mais para os responsáveis, "
                           "mas o histórico de avaliações foi mantido.")
        else:
            db.query(Assignment).filter(Assignment.therapist_id == tid).delete()
            db.delete(t)
            flash(request, "Terapeuta removido.")
        db.commit()
        return go("/painel/terapeutas")
    if action == "nophoto":
        t.photo, t.has_photo = None, False
        db.commit()
        return go(f"/painel/terapeutas/{tid}")
    name = clean(form.get("name"), 120)
    if name:
        t.name = name
    t.sector = clean(form.get("sector"), 160)
    t.full_name = clean(form.get("full_name"), 160)
    t.active = form.get("active") == "1"
    upload = form.get("photo")
    if upload is not None and getattr(upload, "filename", ""):
        try:
            t.photo, t.has_photo = _photo_bytes(await upload.read()), True
            t.photo_version += 1
        except Exception:
            flash(request, "Não consegui ler essa imagem. Use um arquivo JPG ou PNG.", "erro")
    db.commit()
    flash(request, "Dados salvos.")
    return go(f"/painel/terapeutas/{tid}")


# ======================= perguntas =======================
@router.get("/painel/perguntas")
def questions_page(request: Request, db: Session = Depends(get_db), user: User = Depends(need(*GESTAO))):
    qs = db.query(Question).order_by(Question.active.desc(), Question.position, Question.id).all()
    return render(request, "perguntas.html", qs=qs)


@router.post("/painel/perguntas")
async def question_create(request: Request, db: Session = Depends(get_db),
                          user: User = Depends(need(*GESTAO))):
    form = await request.form()
    text = clean(form.get("text"), 300)
    if text:
        last = db.query(func.max(Question.position)).scalar() or 0
        kind = form.get("kind") if form.get("kind") in ("simnao", "nota", "texto") else "simnao"
        db.add(Question(text=text, kind=kind, position=last + 1,
                        allow_ns=kind == "simnao" and form.get("allow_ns") == "1",
                        in_index=kind != "texto" and form.get("in_index") == "1"))
        db.commit()
    return go("/painel/perguntas")


@router.post("/painel/perguntas/{qid}")
async def question_update(qid: int, request: Request, db: Session = Depends(get_db),
                          user: User = Depends(need(*GESTAO))):
    form = await request.form()
    q = db.get(Question, qid)
    if not q:
        raise HTTPException(404)
    if form.get("action") == "remove":
        if db.query(Answer).filter(Answer.question_id == qid).count():
            q.active = False
            flash(request, "Pergunta desativada (já tinha respostas, então o histórico foi mantido).")
        else:
            db.delete(q)
    else:
        text = clean(form.get("text"), 300)
        if text:
            q.text = text
        try:
            q.position = int(form.get("position") or q.position)
        except ValueError:
            pass
        q.active = form.get("active") == "1"
        q.allow_ns = q.kind == "simnao" and form.get("allow_ns") == "1"
        q.in_index = q.kind != "texto" and form.get("in_index") == "1"
    db.commit()
    return go("/painel/perguntas")


# ======================= resultados =======================
def _pick_round(db, rodada: str):
    """rodada = id de uma rodada, ou "todas-intensivo" / "todas-particular".

    Devolve (rodadas, rodada escolhida, programa). Sem rodada escolhida = todas do programa.
    """
    rounds = db.query(Round).order_by(Round.id.desc()).all()
    if rodada.startswith("todas"):
        return rounds, None, program_of(rodada.partition("-")[2])
    rnd = db.get(Round, int(rodada)) if rodada.isdigit() else None
    if not rnd and rounds:
        rnd = rounds[0]
    return rounds, rnd, (rnd.program if rnd else "intensivo")


@router.get("/painel/resultados")
def results_page(request: Request, rodada: str = "", db: Session = Depends(get_db),
                 user: User = Depends(need(*GESTAO))):
    rounds, rnd, prog = _pick_round(db, rodada)
    rid = rnd.id if rnd else None
    data = scores(db, rid, prog)
    return render(request, "resultados.html", rounds=rounds, rnd=rnd, all_rounds=rnd is None,
                  prog=prog, data=data, clinic_notes=clinic_comments(db, rid, prog),
                  questions=scored_questions(db), min_n=MIN_RESPOSTAS, min_cov=MIN_COBERTURA)


@router.get("/painel/resultados/terapeuta/{tid}")
def result_therapist(tid: int, request: Request, db: Session = Depends(get_db),
                     user: User = Depends(need(*GESTAO))):
    t = db.get(Therapist, tid)
    if not t:
        raise HTTPException(404)
    return render(request, "resultado_terapeuta.html", t=t, hist=therapist_history(db, tid),
                  questions=scored_questions(db), min_n=MIN_RESPOSTAS)


@router.get("/painel/resultados/exportar")
def results_export(rodada: str = "", db: Session = Depends(get_db),
                   user: User = Depends(need(*GESTAO))):
    rounds, rnd, prog = _pick_round(db, rodada)
    all_rounds = rnd is None
    title = (f"Todas as rodadas ({PROGRAMS[prog]})" if all_rounds
             else f"{rnd.name} ({PROGRAMS[prog]})")
    rid = rnd.id if rnd else None
    data = scores(db, rid, prog)
    qs = scored_questions(db)

    def num(v, nd=1):
        return round(v, nd) if v is not None else None

    def qval(q, v):
        if v is None:
            return None
        return round(v * 100, 1) if q.kind == "simnao" else round(v, 2)

    wb = Workbook()
    bold = Font(bold=True)
    ws = wb.active
    ws.title = "Terapeutas"
    ws.append([f"Avaliação dos terapeutas: {title}"])
    ws.append(["Terapeuta", "Nome completo", "Setor", "Crianças atendidas", "Avaliações esperadas",
               "Avaliações recebidas", "Cobertura (%)", "Índice (%)", "Índice ajustado (%)"]
              + [f"{q.text} ({'% Sim' if q.kind == 'simnao' else 'média 1 a 5'})" for q in qs])
    for r in data["therapists"]:
        ws.append([r["t"].name, r["t"].full_name, r["t"].sector, r["children"], r["expected"],
                   r["n"], num(r["coverage"] * 100 if r["coverage"] is not None else None),
                   num(r["avg"]), num(r["adj"])]
                  + [qval(q, r["q"].get(q.id)) for q in qs])

    ws2 = wb.create_sheet("Setores")
    ws2.append(["Setor", "Terapeutas avaliados", "Avaliações esperadas", "Avaliações recebidas",
                "Cobertura (%)", "Índice (%)", "Índice ajustado (%)"])
    for sct in data["sectors"]:
        ws2.append([sct["name"], sct["therapists"], sct["expected"], sct["n"],
                    num(sct["coverage"] * 100 if sct["coverage"] is not None else None),
                    num(sct["avg"]), num(sct["adj"])])

    ws3 = wb.create_sheet("Comentários")
    ws3.append(["Rodada", "Terapeuta", "Terapia", "Responsável", "Paciente", "Comentário"])
    q = (db.query(Evaluation)
         .options(joinedload(Evaluation.therapist), joinedload(Evaluation.answers).joinedload(Answer.question),
                  joinedload(Evaluation.submission).joinedload(Submission.guardian).joinedload(Guardian.patient),
                  joinedload(Evaluation.submission).joinedload(Submission.round))
         .join(Submission).join(Round, Round.id == Submission.round_id)
         .filter(Evaluation.status == "done"))
    q = q.filter(Submission.round_id == rid) if rid else q.filter(Round.program == prog)
    for e in q:
        g = e.submission.guardian
        base = [e.submission.round.name, e.therapist.name, e.therapy, g.name, g.patient.name]
        if e.comment.strip():
            ws3.append(base + [e.comment.strip()])
        for a in e.answers:
            if a.question.kind == "texto" and a.text.strip():
                ws3.append(base + [f"{a.question.text}: {a.text.strip()}"])

    ws5 = wb.create_sheet("Sobre a clínica")
    ws5.append(["Rodada", "Responsável", "Paciente", "Observação ou sugestão"])
    for sub in clinic_comments(db, rid, prog):
        ws5.append([sub.round.name, sub.guardian.name, sub.guardian.patient.name, sub.clinic_comment])

    if rnd and not all_rounds:
        ws4 = wb.create_sheet("Pendentes")
        ws4.append(["Paciente", "Responsável", "Telefone", "Situação", "Respondidas", "Total"])
        for r in tracking(db, rnd):
            if r["status"] != "concluido":
                ws4.append([r["p"].name, r["g"].name, r["g"].phone, STATUS_LABEL[r["status"]],
                            r["done"], r["total"]])

    for sheet in wb.worksheets:
        header_row = 2 if sheet.title == "Terapeutas" else 1
        for c in sheet[header_row]:
            c.font = bold
        sheet["A1"].font = bold
        for col in sheet.columns:
            width = max((len(str(c.value)) for c in col[header_row:] if c.value is not None), default=8)
            sheet.column_dimensions[col[0].column_letter].width = min(max(width + 2, 14), 60)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="avaliacao-terapeutas.xlsx"'})


# ======================= usuários =======================
@router.get("/painel/usuarios")
def users_page(request: Request, db: Session = Depends(get_db), user: User = Depends(need(*ADMIN))):
    return render(request, "usuarios.html", users=db.query(User).order_by(User.name).all())


@router.post("/painel/usuarios")
async def user_create(request: Request, db: Session = Depends(get_db), user: User = Depends(need(*ADMIN))):
    form = await request.form()
    username = clean(form.get("username"), 60).lower()
    name = clean(form.get("name"), 120)
    password = str(form.get("password") or "")
    role = form.get("role")
    if not username or not name or role not in ROLES or len(password) < 8:
        flash(request, "Preencha nome, usuário, perfil e uma senha com pelo menos 8 caracteres.", "erro")
    elif db.query(User).filter(func.lower(User.username) == username).first():
        flash(request, "Já existe um usuário com esse login.", "erro")
    else:
        db.add(User(username=username, name=name, role=role, password_hash=hash_password(password)))
        db.commit()
        flash(request, f"Usuário {username} criado.")
    return go("/painel/usuarios")


@router.post("/painel/usuarios/{uid}")
async def user_update(uid: int, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(need(*ADMIN))):
    form = await request.form()
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404)
    role = form.get("role")
    active = form.get("active") == "1"
    if u.id == user.id and (role != "admin" or not active):
        flash(request, "Você não pode tirar o seu próprio acesso de administrador.", "erro")
        return go("/painel/usuarios")
    if role in ROLES:
        u.role = role
    u.active = active
    name = clean(form.get("name"), 120)
    if name:
        u.name = name
    password = str(form.get("password") or "")
    if password:
        if len(password) < 8:
            flash(request, "A senha precisa ter pelo menos 8 caracteres (as outras alterações foram salvas).", "erro")
        else:
            u.password_hash = hash_password(password)
    db.commit()
    flash(request, "Usuário atualizado.")
    return go("/painel/usuarios")


# ======================= importação por planilha =======================
COLS = ["Paciente", "Responsável 1", "Telefone 1", "Responsável 2", "Telefone 2",
        "Terapia", "Terapeuta", "Setor", "Para o responsável", "Pendência", "Programa"]
COLS_T = ["Terapeuta", "Nome completo", "Setores"]


@router.get("/painel/importar")
def import_page(request: Request, user: User = Depends(need())):
    return render(request, "importar.html", cols=COLS, cols_t=COLS_T, report=None)


@router.get("/painel/importar/modelo")
def import_template(user: User = Depends(need())):
    wb = Workbook()
    ws = wb.active
    ws.title = "Cronogramas"
    ws.append(COLS)
    ws.append(["Nome da Criança", "Nome da Mãe", "21999990000", "", "", "Fonoaudiologia",
               "Nome da Terapeuta", "Fonoaudiologia", "", "", "Intensivo"])
    wt = wb.create_sheet("Terapeutas")
    wt.append(COLS_T)
    wt.append(["Nome da Terapeuta", "Nome Completo da Terapeuta", "Fonoaudiologia"])
    for sheet in (ws, wt):
        for c in sheet[1]:
            c.font = Font(bold=True)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="modelo-importacao.xlsx"'})


def _norm(s) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return " ".join(s.lower().split())


def _sheet_rows(ws):
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return {}, []
    header = {_norm(h): i for i, h in enumerate(rows[0]) if h}

    def cell(row, col):
        i = header.get(_norm(col))
        return clean(row[i], 500) if i is not None and i < len(row) and row[i] is not None else ""
    return header, [(n, (lambda r: (lambda col: cell(r, col)))(row)) for n, row in enumerate(rows[1:], start=2)]


@router.post("/painel/importar")
async def import_run(request: Request, db: Session = Depends(get_db), user: User = Depends(need())):
    form = await request.form()
    upload = form.get("file")
    report = {"patients": 0, "guardians": 0, "therapists": 0, "links": 0, "warn": []}
    try:
        wb = load_workbook(io.BytesIO(await upload.read()), data_only=True)
    except Exception:
        flash(request, "Não consegui ler o arquivo. Envie uma planilha .xlsx no formato do modelo.", "erro")
        return go("/painel/importar")
    names = {_norm(n): n for n in wb.sheetnames}
    ws = wb[names["cronogramas"]] if "cronogramas" in names else wb.worksheets[0]
    header, rows = _sheet_rows(ws)
    if _norm("Paciente") not in header:
        flash(request, "A planilha precisa ter a coluna “Paciente”. Baixe o modelo para conferir.", "erro")
        return go("/painel/importar")

    # aba opcional com o cadastro dos terapeutas (nome completo e setores)
    if "terapeutas" in names:
        _, trows = _sheet_rows(wb[names["terapeutas"]])
        for n, cell in trows:
            tname = cell("Terapeuta")
            if not tname:
                continue
            t = db.query(Therapist).filter(func.lower(Therapist.name) == tname.lower()).first()
            if not t:
                t = Therapist(name=tname)
                db.add(t)
                db.flush()
                report["therapists"] += 1
            t.full_name = cell("Nome completo") or t.full_name
            t.sector = cell("Setores") or t.sector

    for n, cell in rows:
        pname = cell("Paciente")
        if not pname:
            continue
        p = db.query(Patient).filter(func.lower(Patient.name) == pname.lower()).first()
        if not p:
            p = Patient(name=pname, program=program_of(cell("Programa")))
            db.add(p)
            db.flush()
            report["patients"] += 1
        note = cell("Pendência")
        if note and note not in p.notes:
            p.notes = (p.notes + " | " + note).strip(" |")[:500]
        for k in ("1", "2"):
            gname, phone = cell(f"Responsável {k}"), cell(f"Telefone {k}")
            if not gname:
                continue
            g = next((x for x in p.guardians if x.name.lower() == gname.lower()), None)
            if not g:
                p.guardians.append(Guardian(name=gname, phone=phone, token=new_token()))
                report["guardians"] += 1
            elif phone and not g.phone:
                g.phone = phone
        therapy, tname = cell("Terapia"), cell("Terapeuta")
        if therapy and not tname:
            report["warn"].append(f"Linha {n}: {pname}, {therapy} sem nome do terapeuta (não importado).")
            continue
        if not tname:
            continue
        before = db.query(Therapist).count()
        t = _get_or_create_therapist(db, tname, cell("Setor"))
        report["therapists"] += db.query(Therapist).count() - before
        therapy = therapy or t.sector or "Terapia"
        if not db.query(Assignment).filter_by(patient_id=p.id, therapist_id=t.id, therapy=therapy).first():
            db.add(Assignment(patient_id=p.id, therapist_id=t.id, therapy=therapy,
                              sector=cell("Setor") or _default_sector(t, therapy),
                              for_guardian=_norm(cell("Para o responsável")) in ("sim", "s", "x", "1")))
            db.flush()
            report["links"] += 1
    db.commit()
    return render(request, "importar.html", cols=COLS, cols_t=COLS_T, report=report)
