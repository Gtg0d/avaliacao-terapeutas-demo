"""Cálculos: andamento da rodada, pendências de cadastro e notas."""
from collections import defaultdict

from sqlalchemy.orm import Session, joinedload

from .models import (Answer, Assignment, Evaluation, Guardian, Patient,
                     Question, Round, Submission, Therapist)

MIN_RESPOSTAS = 3      # sem cadastro para comparar, abaixo disso o resultado é marcado como "poucas"
MIN_COBERTURA = 0.5    # abaixo de 50% das avaliações esperadas, o resultado é marcado como "cobertura baixa"


def open_round(db: Session, program: str = "intensivo") -> Round | None:
    return (db.query(Round).filter(Round.status == "open", Round.program == program)
            .order_by(Round.id.desc()).first())


def patient_items(db: Session, patient_id: int) -> list[Assignment]:
    """Terapeutas ativos que atendem a criança, na ordem em que aparecem para o responsável."""
    return (db.query(Assignment).join(Therapist)
            .options(joinedload(Assignment.therapist))
            .filter(Assignment.patient_id == patient_id, Therapist.active.is_(True))
            .order_by(Assignment.therapy, Therapist.name).all())


def tracking(db: Session, rnd: Round | None, program: str = "intensivo") -> list[dict]:
    """Uma linha por responsável ativo do programa: quanto já respondeu na rodada."""
    if rnd:
        program = rnd.program
    guardians = (db.query(Guardian).join(Patient)
                 .options(joinedload(Guardian.patient))
                 .filter(Guardian.active.is_(True), Patient.active.is_(True),
                         Patient.program == program)
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


def pendencias(db: Session, rounds: list[Round]) -> list[dict]:
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
    if rounds:
        skipped = (db.query(Evaluation).join(Submission)
                   .options(joinedload(Evaluation.therapist),
                            joinedload(Evaluation.submission).joinedload(Submission.guardian)
                            .joinedload(Guardian.patient))
                   .filter(Submission.round_id.in_([r.id for r in rounds]),
                           Evaluation.status == "skipped").all())
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


def expected(db: Session, program: str) -> dict:
    """O que cada terapeuta e cada setor deveriam receber em UMA rodada, pelo cadastro atual.

    Crianças: pacientes ativos do programa que o terapeuta atende.
    Esperadas: uma avaliação por vínculo (terapia) por responsável ativo da criança.
    """
    gcount = defaultdict(int)
    for (pid,) in (db.query(Guardian.patient_id).join(Patient)
                   .filter(Guardian.active.is_(True), Patient.active.is_(True),
                           Patient.program == program)):
        gcount[pid] += 1
    by_t = defaultdict(lambda: {"children": set(), "expected": 0})
    by_s = defaultdict(int)
    rows = (db.query(Assignment, Therapist).join(Therapist).join(Patient)
            .filter(Patient.active.is_(True), Patient.program == program))
    for a, t in rows:
        d = by_t[t.id]
        d["children"].add(a.patient_id)
        d["expected"] += gcount.get(a.patient_id, 0)
        by_s[(a.sector or t.sector or "").strip() or "Sem setor"] += gcount.get(a.patient_id, 0)
    return {"therapists": by_t, "sectors": by_s}


def _adjust(avg, n, exp, overall):
    """Cobertura e índice ajustado.

    O índice ajustado mistura o índice do terapeuta com a média geral da rodada, na proporção
    das avaliações que faltaram: com 12 de 12 respostas fica igual ao índice; com 3 de 12,
    vale 25% o índice dele e 75% a média geral. Assim poucas respostas não decidem sozinhas.
    """
    cov = min(n / exp, 1.0) if exp else None
    if avg is None:
        adj = None
    elif cov is None or overall is None:
        adj = avg
    else:
        adj = cov * avg + (1 - cov) * overall
    low = (cov < MIN_COBERTURA) if cov is not None else (n < MIN_RESPOSTAS)
    return cov, adj, low


def scores(db: Session, round_id: int | None = None, program: str = "intensivo") -> dict:
    """Resultado por terapeuta e por setor de uma rodada, ou de todas as rodadas do programa.

    "avg" é o índice de 0 a 100 (percentual de respostas positivas); "adj" é o índice ajustado
    pela cobertura (ver _adjust). "q" guarda, por pergunta, a fração de Sim (0..1) ou a média
    de 1 a 5. Em "todas as rodadas", as esperadas são as de uma rodada vezes o nº de rodadas,
    sempre pelo cadastro atual.
    """
    if round_id:
        rnd = db.get(Round, round_id)
        program = rnd.program if rnd else program
        n_rounds = 1
    else:
        n_rounds = db.query(Round).filter(Round.program == program).count()
    q = (db.query(Evaluation.id, Evaluation.therapist_id, Evaluation.sector,
                  Answer.question_id, Answer.score, Question.kind, Question.in_index)
         .join(Answer, Answer.evaluation_id == Evaluation.id)
         .join(Question, Question.id == Answer.question_id)
         .join(Submission, Submission.id == Evaluation.submission_id)
         .join(Round, Round.id == Submission.round_id)
         .filter(Evaluation.status == "done", Answer.score.isnot(None)))
    q = q.filter(Submission.round_id == round_id) if round_id else q.filter(Round.program == program)
    therapists = {t.id: t for t in db.query(Therapist)}
    by_t = defaultdict(lambda: {"evals": set(), "all": [], "q": defaultdict(list)})
    by_s = defaultdict(lambda: {"evals": set(), "all": [], "ts": set()})
    every = []
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
            every.append(v)
    overall = _mean(every) * 100 if every else None
    exp = expected(db, program)

    rows = []
    for tid in set(by_t) | {tid for tid, e in exp["therapists"].items() if e["expected"]}:
        t = therapists.get(tid)
        if not t or (tid not in by_t and not t.active):
            continue
        d = by_t.get(tid, {"evals": set(), "all": [], "q": {}})
        e = exp["therapists"].get(tid, {"children": set(), "expected": 0})
        n = len(d["evals"])
        avg = _mean(d["all"]) * 100 if d["all"] else None
        want = e["expected"] * n_rounds
        cov, adj, low = _adjust(avg, n, want, overall)
        rows.append({"t": t, "n": n, "avg": avg, "adj": adj, "children": len(e["children"]),
                     "expected": want, "coverage": cov, "low": low,
                     "q": {qid: _mean(v) for qid, v in d["q"].items()}})
    rows.sort(key=lambda r: (r["adj"] is None, -(r["adj"] or 0), -r["n"], r["t"].name))

    sectors = []
    for name in set(by_s) | {s for s, v in exp["sectors"].items() if v}:
        d = by_s.get(name, {"evals": set(), "all": [], "ts": set()})
        n = len(d["evals"])
        avg = _mean(d["all"]) * 100 if d["all"] else None
        want = exp["sectors"].get(name, 0) * n_rounds
        cov, adj, low = _adjust(avg, n, want, overall)
        sectors.append({"name": name, "avg": avg, "adj": adj, "n": n, "expected": want,
                        "coverage": cov, "therapists": len(d["ts"]), "low": low})
    sectors.sort(key=lambda x: (x["adj"] is None, -(x["adj"] or 0), -x["n"]))
    return {"therapists": rows, "sectors": sectors, "overall": overall,
            "program": program, "n_rounds": n_rounds}


def scored_questions(db: Session) -> list[Question]:
    """Perguntas que geram número (Sim/Não e nota), na ordem do formulário."""
    return (db.query(Question).filter(Question.kind.in_(("simnao", "nota")))
            .order_by(Question.active.desc(), Question.position, Question.id).all())


def clinic_comments(db: Session, round_id: int | None = None,
                    program: str = "intensivo") -> list[Submission]:
    q = (db.query(Submission).join(Round, Round.id == Submission.round_id)
         .options(joinedload(Submission.guardian).joinedload(Guardian.patient),
                  joinedload(Submission.round))
         .filter(Submission.clinic_comment != ""))
    q = q.filter(Submission.round_id == round_id) if round_id else q.filter(Round.program == program)
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
    last = {}  # último índice por programa, para a variação comparar rodadas do mesmo programa
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
        avg = _mean(allv) * 100 if allv else None
        prev = last.get(r.program)
        delta = avg - prev if (avg is not None and prev is not None) else None
        if avg is not None:
            last[r.program] = avg
        out.append({"round": r, "n": len(es), "avg": avg, "delta": delta,
                    "q": {k: _mean(v) for k, v in per_q.items()},
                    "comments": comments, "low": len(es) < MIN_RESPOSTAS})
    return out
