"""Páginas do responsável: entra pelo link pessoal, sem login."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session, joinedload, undefer

from .core import render
from .db import get_db
from .models import (Answer, Evaluation, Guardian, Patient, Question,
                     Submission, Therapist, utcnow)
from .stats import open_round, patient_items

router = APIRouter()


def _msg(request, title, text, status=200):
    return render(request, "pub_msg.html", status_code=status, title=title, text=text)


def _context(db: Session, token: str):
    """Devolve (responsável, rodada, participação, itens) ou uma mensagem de erro."""
    g = (db.query(Guardian).options(joinedload(Guardian.patient))
         .filter(Guardian.token == token).first())
    if not g or not g.active or not g.patient.active:
        return None, ("Link não encontrado",
                      "Este link não é válido. Peça um novo link na recepção da clínica.", 404)
    rnd = open_round(db, g.patient.program)  # cada programa tem a sua rodada
    if not rnd:
        return None, ("Nenhuma avaliação aberta",
                      "No momento não há avaliação em andamento. "
                      "Avisaremos quando a próxima começar. Obrigado!", 200)
    sub = (db.query(Submission).options(joinedload(Submission.evaluations))
           .filter(Submission.round_id == rnd.id, Submission.guardian_id == g.id).first())
    if not sub:
        sub = Submission(round_id=rnd.id, guardian_id=g.id)
        db.add(sub)
        db.commit()
    evals = {e.assignment_id: e for e in sub.evaluations}
    items = [{"a": a, "ev": evals.get(a.id)} for a in patient_items(db, g.patient_id)]
    if not items:
        return None, ("Cadastro em atualização",
                      "Ainda não há terapeutas cadastrados para esta avaliação. "
                      "Por favor, avise a recepção.", 200)
    return (g, rnd, sub, items), None


def _questions(db):
    return (db.query(Question).filter(Question.active.is_(True))
            .order_by(Question.position, Question.id).all())


@router.get("/a/{token}")
def home(token: str, request: Request, db: Session = Depends(get_db)):
    ctx, err = _context(db, token)
    if err:
        return _msg(request, *err)
    g, rnd, sub, items = ctx
    pending = [i for i in items if not i["ev"]]
    return render(request, "pub_home.html", g=g, rnd=rnd, items=items, token=token, sub=sub,
                  saved=request.query_params.get("ok") == "1",
                  done=len(items) - len(pending),
                  next_id=pending[0]["a"].id if pending else None)


@router.post("/a/{token}/clinica")
async def clinic_comment(token: str, request: Request, db: Session = Depends(get_db)):
    ctx, err = _context(db, token)
    if err:
        return _msg(request, *err)
    g, rnd, sub, items = ctx
    form = await request.form()
    sub.clinic_comment = (form.get("clinic_comment") or "").strip()[:3000]
    db.commit()
    return RedirectResponse(f"/a/{token}?ok=1", 303)


@router.get("/a/{token}/{aid}")
def eval_form(token: str, aid: int, request: Request, db: Session = Depends(get_db)):
    ctx, err = _context(db, token)
    if err:
        return _msg(request, *err)
    g, rnd, sub, items = ctx
    idx = next((n for n, i in enumerate(items) if i["a"].id == aid), None)
    if idx is None:
        return RedirectResponse(f"/a/{token}", 303)
    item = items[idx]
    ev = item["ev"]
    answers = {a.question_id: a for a in ev.answers} if ev and ev.status == "done" else {}
    return render(request, "pub_eval.html", g=g, token=token, item=item, ev=ev,
                  answers=answers, questions=_questions(db), pos=idx + 1, total=len(items),
                  done=sum(1 for i in items if i["ev"]),
                  prev_id=items[idx - 1]["a"].id if idx > 0 else None, error=None)


@router.post("/a/{token}/{aid}")
async def eval_save(token: str, aid: int, request: Request, db: Session = Depends(get_db)):
    ctx, err = _context(db, token)
    if err:
        return _msg(request, *err)
    g, rnd, sub, items = ctx
    idx = next((n for n, i in enumerate(items) if i["a"].id == aid), None)
    if idx is None:
        return RedirectResponse(f"/a/{token}", 303)
    item = items[idx]
    a = item["a"]
    form = await request.form()
    questions = _questions(db)
    skip = form.get("action") == "skip"

    values = {}  # pergunta -> (nota, texto)
    if not skip:
        missing = False
        for q in questions:
            raw = (form.get(f"q{q.id}") or "").strip()
            if q.kind == "nota" and raw in ("1", "2", "3", "4", "5"):
                values[q.id] = (int(raw), "")
            elif q.kind == "simnao" and raw in ("sim", "nao"):
                values[q.id] = (1 if raw == "sim" else 0, "")
            elif q.kind == "simnao" and raw == "ns" and q.allow_ns:
                values[q.id] = (None, "ns")
            elif q.kind == "texto":
                values[q.id] = (None, raw[:2000])
            else:
                missing = True
        if missing:
            class Tmp:  # devolve o que já foi marcado para a pessoa não perder
                def __init__(self, score=None, text=""):
                    self.score, self.text = score, text
            answers = {qid: Tmp(*v) for qid, v in values.items()}
            return render(request, "pub_eval.html", g=g, token=token, item=item, ev=None,
                          answers=answers, questions=questions, pos=idx + 1, total=len(items),
                          done=sum(1 for i in items if i["ev"]),
                          prev_id=items[idx - 1]["a"].id if idx > 0 else None,
                          error="Faltou responder uma pergunta. Confira as que ficaram em branco.",
                          comment=(form.get("comment") or ""))

    ev = item["ev"]
    if not ev:
        ev = Evaluation(submission_id=sub.id, assignment_id=a.id, therapist_id=a.therapist_id)
        db.add(ev)
    ev.therapy = a.therapy
    ev.sector = a.sector
    ev.saved_at = utcnow()
    ev.answers.clear()
    if skip:
        ev.status, ev.comment = "skipped", ""
    else:
        ev.status = "done"
        ev.comment = (form.get("comment") or "").strip()[:2000]
        for q in questions:
            score, text = values[q.id]
            ev.answers.append(Answer(question_id=q.id, score=score, text=text))
    item["ev"] = ev
    pending = [i for i in items if not i["ev"]]
    sub.completed_at = None if pending else (sub.completed_at or utcnow())
    db.commit()

    after = [i for i in items[idx + 1:] if not i["ev"]] or pending
    if after:
        return RedirectResponse(f"/a/{token}/{after[0]['a'].id}", 303)
    return RedirectResponse(f"/a/{token}", 303)


@router.get("/foto/{tid}")
def photo(tid: int, db: Session = Depends(get_db)):
    t = db.query(Therapist).options(undefer(Therapist.photo)).filter(Therapist.id == tid).first()
    if not t or not t.photo:
        return Response(status_code=404)
    return Response(t.photo, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})
