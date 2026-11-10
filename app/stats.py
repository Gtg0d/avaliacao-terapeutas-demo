"""Cálculos: andamento da rodada, pendências de cadastro e notas."""
from collections import defaultdict

from sqlalchemy.orm import Session, joinedload

from .models import (Answer, Assignment, Evaluation, Guardian, Patient,
                     Question, Round, Submission, Therapist)

MIN_RESPOSTAS = 3  # abaixo disso a média é marcada como "poucas respostas"


def open_round(db: Session) -> Round | None:
    return db.query(Round).filter(Round.status == "open").order_by(Round.id.desc()).first()


def patient_items(db: Session, patient_id: int) -> list[Assignment]:
    """Terapeutas ativos que atendem a criança, na ordem em que aparecem para o responsável."""
    return (db.query(Assignment).join(Therapist)
            .options(joinedload(Assignment.therapist))
            .filter(Assignment.patient_id == patient_id, Therapist.active.is_(True))
            .order_by(Assignment.therapy, Therapist.name).all())


def tracking(db: Session, rnd: Round | None) -> list[dict]:
    """Uma linha por responsável ativo: quanto já respondeu na rodada."""
    guardians = (db.query(Guardian).join(Patient)
                 .options(joinedload(Guardian.patient))
                 .filter(Guardian.active.is_(True), Patient.active.is_(True))
                 .order_by(Patient.name, Guardian.name).all())
    assigns = defaultdict(set)
    for a in db.query(Assignment).join(Therapist).filter(Therapist.active.is_(True)):
        assigns[a.patient_id].add(a.id)
    subs = {}
    if rnd:
        for s in (db.query(Submission).options(joinedload(Submission.evaluations))
                  .filter(Submission.round_id == rnd.id)):
            subs[s.guardian_id] = s
    rows = []
    for g in guardians:
        ids = assigns.get(g.patient_id, set())
        sub = subs.get(g.id)
        done = len({e.assignment_id for e in sub.evaluations} & ids) if sub else 0
        total = len(ids)
        if total == 0:
            status = "sem_terapeutas"
        elif not sub:
            status = "nao_abriu"
        elif done >= total:
            status = "concluido"
        elif done == 0:
            status = "abriu"
        else:
            status = "andamento"
        rows.append({"g": g, "p": g.patient, "sub": sub, "done": done, "total": total,
                     "status": status})
    return rows


STATUS_LABEL = {
    "concluido": "Concluído",
    "andamento": "Parou no meio",
    "abriu": "Abriu e não respondeu",
    "nao_abriu": "Não abriu",
    "sem_terapeutas": "Sem terapeutas cadastrados",
}


def pendencias(db: Session, rnd: Round | None) -> list[dict]:
    """O que falta completar no cadastro."""
    out = []
    patients = (db.query(Patient).options(joinedload(Patient.guardians), joinedload(Patient.assignments))
                .filter(Patient.active.is_(True)).order_by(Patient.name).all())
    for p in patients:
        url = f"/painel/pacientes/{p.id}"
        gs = [g for g in p.guardians if g.active]
        if not gs:
            out.append({"txt": f"{p.name}: sem responsável cadastrado", "url": url})
        for g in gs:
            if not g.phone.strip():
                out.append({"txt": f"{p.name}: responsável {g.name} sem telefone", "url": url})
        if not p.assignments:
            out.append({"txt": f"{p.name}: nenhum terapeuta vinculado", "url": url})
        if p.notes.strip():
            out.append({"txt": f"{p.name}: {p.notes.strip()}", "url": url})
    for t in db.query(Therapist).filter(Therapist.active.is_(True)).order_by(Therapist.name):
        url = f"/painel/terapeutas/{t.id}"
        if not t.sector.strip():
            out.append({"txt": f"Terapeuta {t.name}: sem setor", "url": url})
        if not t.has_photo:
            out.append({"txt": f"Terapeuta {t.name}: sem foto", "url": url})
    if rnd:
        skipped = (db.query(Evaluation).join(Submission)
                   .options(joinedload(Evaluation.therapist),
                            joinedload(Evaluation.submission).joinedload(Submission.guardian)
                            .joinedload(Guardian.patient))
                   .filter(Submission.round_id == rnd.id, Evaluation.status == "skipped").all())
        for e in skipped:
            g = e.submission.guardian
            out.append({"txt": f"{g.name} informou que {e.therapist.name} ({e.therapy}) "
                               f"não atende {g.patient.name}: conferir o cronograma",
                        "url": f"/painel/pacientes/{g.patient_id}"})
    return out


def _mean(values):
    return sum(values) / len(values) if values else None


def _norm(kind: str, score: int) -> float:
    """Coloca qualquer resposta na escala 0..1 (Sim=1, Não=0; nota 1..5 -> 0..1)."""
    return float(score) if kind == "simnao" else (score - 1) / 4


def scores(db: Session, round_id: int | None = None) -> dict:
    """Resultado por terapeuta e por setor. round_id=None soma todas as rodadas.

    "avg" é o índice de 0 a 100 (percentual de respostas positivas).
    "q" guarda, por pergunta, a fração de Sim (0..1) ou a média de 1 a 5.
    """
    q = (db.query(Evaluation.id, Evaluation.therapist_id, Evaluation.sector,
                  Answer.question_id, Answer.score, Question.kind, Question.in_index)
         .join(Answer, Answer.evaluation_id == Evaluation.id)
         .join(Question, Question.id == Answer.question_id)
         .join(Submission, Submission.id == Evaluation.submission_id)
         .filter(Evaluation.status == "done", Answer.score.isnot(None)))
    if round_id:
        q = q.filter(Submission.round_id == round_id)
    therapists = {t.id: t for t in db.query(Therapist)}
    by_t = defaultdict(lambda: {"evals": set(), "all": [], "q": defaultdict(list)})
    by_s = defaultdict(lambda: {"evals": set(), "all": [], "ts": set()})
    for eid, tid, sector, qid, score, kind, in_index in q:
        t = therapists.get(tid)
        if not t:
            continue
        d = by_t[tid]
        d["evals"].add(eid)
        d["q"][qid].append(score)
        sd = by_s[(sector or t.sector or "").strip() or "Sem setor"]
        sd["evals"].add(eid)
        sd["ts"].add(tid)
        if in_index:
            v = _norm(kind, score)
            d["all"].append(v)
            sd["all"].append(v)
    rows = []
    for tid, d in by_t.items():
        n = len(d["evals"])
        rows.append({"t": therapists[tid], "n": n, "avg": (_mean(d["all"]) or 0) * 100,
                     "q": {qid: _mean(v) for qid, v in d["q"].items()},
                     "low": n < MIN_RESPOSTAS})
    rows.sort(key=lambda r: (-r["avg"], -r["n"], r["t"].name))
    sectors = [{"name": name, "avg": (_mean(d["all"]) or 0) * 100, "n": len(d["evals"]),
                "therapists": len(d["ts"]), "low": len(d["evals"]) < MIN_RESPOSTAS}
               for name, d in by_s.items()]
    sectors.sort(key=lambda x: (-x["avg"], -x["n"]))
    return {"therapists": rows, "sectors": sectors}


def scored_questions(db: Session) -> list[Question]:
    """Perguntas que geram número (Sim/Não e nota), na ordem do formulário."""
    return (db.query(Question).filter(Question.kind.in_(("simnao", "nota")))
            .order_by(Question.active.desc(), Question.position, Question.id).all())


def clinic_comments(db: Session, round_id: int | None = None) -> list[Submission]:
    q = (db.query(Submission)
         .options(joinedload(Submission.guardian).joinedload(Guardian.patient),
                  joinedload(Submission.round))
         .filter(Submission.clinic_comment != ""))
    if round_id:
        q = q.filter(Submission.round_id == round_id)
    return q.order_by(Submission.id.desc()).all()


def therapist_history(db: Session, therapist_id: int) -> list[dict]:
    """Evolução do terapeuta rodada a rodada, com comentários."""
    rounds = db.query(Round).order_by(Round.id).all()
    evals = (db.query(Evaluation)
             .options(joinedload(Evaluation.answers).joinedload(Answer.question),
                      joinedload(Evaluation.submission).joinedload(Submission.guardian)
                      .joinedload(Guardian.patient))
             .filter(Evaluation.therapist_id == therapist_id, Evaluation.status == "done").all())
    by_round = defaultdict(list)
    for e in evals:
        by_round[e.submission.round_id].append(e)
    out = []
    for r in rounds:
        es = by_round.get(r.id, [])
        if not es:
            continue
        allv, per_q, comments = [], defaultdict(list), []
        for e in es:
            g = e.submission.guardian
            who = f"{g.name} (resp. de {g.patient.name})"
            for a in e.answers:
                if a.score is not None:
                    if a.question.in_index:
                        allv.append(_norm(a.question.kind, a.score))
                    per_q[a.question_id].append(a.score)
                elif a.question.kind == "texto" and a.text.strip():
                    comments.append({"who": who, "txt": f"{a.question.text}: {a.text.strip()}",
                                     "therapy": e.therapy})
            if e.comment.strip():
                comments.append({"who": who, "txt": e.comment.strip(), "therapy": e.therapy})
        avg = _mean(allv)
        out.append({"round": r, "n": len(es), "avg": avg * 100 if avg is not None else None,
                    "q": {k: _mean(v) for k, v in per_q.items()},
                    "comments": comments, "low": len(es) < MIN_RESPOSTAS})
    return out
