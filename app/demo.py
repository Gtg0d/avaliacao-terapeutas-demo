"""Dados fictícios para demonstração. Ativado com a variável de ambiente DEMO=1.

Todos os nomes abaixo são inventados. Nunca use DEMO=1 em produção:
ele cria usuários com senha conhecida.
"""
import random

from sqlalchemy.orm import Session

from .core import hash_password, new_token
from .models import (Answer, Assignment, Evaluation, Guardian, Patient,
                     Question, Round, Submission, Therapist, User, utcnow)

SENHA_DEMO = "demo1234"

# (nome exibido, nome completo, setor, "qualidade" de 0 a 1 usada para sortear as respostas)
TERAPEUTAS = [
    ("Helena", "Helena Duarte Pires", "Fonoaudiologia", 0.97),
    ("Rafael", "Rafael Antunes Lobo", "Fonoaudiologia", 0.90),
    ("Marina", "Marina Queiroz Sá", "Terapia Ocupacional", 0.95),
    ("Otávio", "Otávio Brandão Reis", "Terapia Ocupacional", 0.82),
    ("Lorena", "Lorena Castilho Maia", "Psicologia", 0.93),
    ("Davi", "Davi Moreno Teles", "Psicologia", 0.88),
    ("Isadora", "Isadora Falcão Nery", "Psicopedagogia", 0.96),
    ("Caetano", "Caetano Vidal Rosa", "Psicomotricidade", 0.86),
    ("Paloma", "Paloma Estrela Lins", "Psicomotricidade / Fisioterapia Aquática", 0.92),
    ("Túlio", "Túlio Sampaio Leme", "Fisioterapia Motora", 0.78),
    ("Bianca", "Bianca Serrano Luz", "Fisioterapia Motora", 0.94),
    ("Érica", "Érica Bastos Prado", "Fisioterapia Respiratória", 0.91),
    ("Noel", "Noel Guimarães Faro", "Musicoterapia", 0.98),
    ("Jade", "Jade Monteiro Paz", "Nutrição", 0.89),
]

# (criança, [(responsável, telefone)], quantos terapeutas atendem)
PACIENTES = [
    ("Theo Almeida Rios", [("Carolina Almeida", "21990000001"), ("Bruno Rios", "21990000002")], 9),
    ("Lia Nogueira Campos", [("Patrícia Nogueira", "21990000003")], 8),
    ("Benício Tavares Leal", [("Juliana Tavares", "21990000004")], 10),
    ("Maitê Coelho Dias", [("Fernanda Coelho", "21990000005"), ("André Dias", "")], 7),
    ("Gael Pacheco Lima", [("Renata Pacheco", "21990000006")], 9),
    ("Aurora Vieira Mota", [("Simone Vieira", "21990000007")], 6),
    ("Ravi Fontes Barros", [("Débora Fontes", "21990000008")], 8),
    ("Cecília Ramos Paiva", [("Viviane Ramos", "")], 7),
    ("Noah Siqueira Abreu", [("Tatiane Siqueira", "21990000009")], 9),
    ("Olívia Prates Cunha", [("Camila Prates", "21990000010")], 8),
    ("Joaquim Leite Xavier", [("Aline Leite", "21990000011")], 6),
    ("Elisa Borges Farias", [("Márcia Borges", "21990000012")], 8),
]

TERAPIA_DO_SETOR = {"Fisioterapia Aquática": "Terapia Aquática"}

COMENTARIOS_BONS = [
    "Sempre explica com calma o que foi trabalhado no dia.",
    "Meu filho adora as sessões e pede para voltar.",
    "Muito atenciosa, percebo evolução a cada semana.",
    "Tira todas as minhas dúvidas na devolutiva.",
    "Profissional excelente, pontual e carinhoso com as crianças.",
]
COMENTARIOS_RUINS = [
    "Algumas devolutivas ficaram corridas no fim do atendimento.",
    "Houve remarcações este mês e demorou para repor.",
    "Gostaria de mais orientações para continuar em casa.",
]
COMENTARIOS_CLINICA = [
    "A sala de espera ficou ótima com os brinquedos novos.",
    "Seria bom ter mais vagas no estacionamento.",
    "Equipe da recepção sempre muito atenciosa.",
]


def seed(db: Session) -> None:
    if db.query(Patient).count():
        return
    rnd = random.Random(2026)

    for username, name, role in (("admin", "Administrador", "admin"),
                                 ("direcao", "Direção (demo)", "direcao"),
                                 ("recepcao", "Recepção (demo)", "recepcao")):
        u = db.query(User).filter(User.username == username).first()
        if u:
            u.password_hash = hash_password(SENHA_DEMO)
        else:
            db.add(User(username=username, name=name, role=role,
                        password_hash=hash_password(SENHA_DEMO)))

    quality = {}
    therapists = []
    for name, full, sector, q in TERAPEUTAS:
        t = Therapist(name=name, full_name=full, sector=sector)
        db.add(t)
        therapists.append(t)
        quality[name] = q
    db.flush()

    guardians = []
    for pname, resp, n in PACIENTES:
        p = Patient(name=pname)
        db.add(p)
        db.flush()
        for gname, phone in resp:
            g = Guardian(patient_id=p.id, name=gname, phone=phone, token=new_token())
            db.add(g)
            guardians.append(g)
        for t in rnd.sample(therapists, n):
            sector = rnd.choice([s.strip() for s in t.sector.split("/")])
            db.add(Assignment(patient_id=p.id, therapist_id=t.id, sector=sector,
                              therapy=TERAPIA_DO_SETOR.get(sector, sector)))
        # um atendimento voltado ao responsável, para mostrar esse caso
        if n >= 8:
            psi = next(t for t in therapists if t.sector == "Psicologia")
            db.add(Assignment(patient_id=p.id, therapist_id=psi.id, sector="Psicologia",
                              therapy="Psicologia Responsável", for_guardian=True))
    db.flush()

    questions = db.query(Question).filter(Question.active.is_(True)).all()
    rounds = [Round(name="Outubro/2026 – 1ª quinzena", status="closed", closed_at=utcnow()),
              Round(name="Outubro/2026 – 2ª quinzena", status="closed", closed_at=utcnow()),
              Round(name="Novembro/2026 – 1ª quinzena", status="open")]
    db.add_all(rounds)
    db.flush()

    for ri, r in enumerate(rounds):
        for gi, g in enumerate(guardians):
            assigns = db.query(Assignment).filter(Assignment.patient_id == g.patient_id).all()
            last = ri == len(rounds) - 1
            if last and gi % 5 == 3:
                continue                                   # ainda não abriu o link
            sub = Submission(round_id=r.id, guardian_id=g.id)
            db.add(sub)
            db.flush()
            todo = assigns
            if last and gi % 5 == 1:
                todo = assigns[: len(assigns) // 2]        # parou no meio
            elif last and gi % 5 == 4:
                todo = []                                  # abriu e não respondeu
            for a in todo:
                q = min(0.995, quality[a.therapist.name] + 0.02 * ri)   # melhora leve a cada rodada
                ev = Evaluation(submission_id=sub.id, assignment_id=a.id, therapist_id=a.therapist_id,
                                therapy=a.therapy, sector=a.sector)
                bad = 0
                for qu in questions:
                    if qu.kind == "simnao":
                        if qu.allow_ns and rnd.random() < 0.3:
                            ev.answers.append(Answer(question_id=qu.id, text="ns"))
                        else:
                            yes = rnd.random() < q
                            bad += 0 if yes else 1
                            ev.answers.append(Answer(question_id=qu.id, score=1 if yes else 0))
                    elif qu.kind == "nota":
                        score = max(1, min(5, round(1 + 4 * q + rnd.uniform(-0.9, 0.5))))
                        ev.answers.append(Answer(question_id=qu.id, score=score))
                if rnd.random() < 0.18:
                    ev.comment = rnd.choice(COMENTARIOS_RUINS if bad else COMENTARIOS_BONS)
                db.add(ev)
            if len(todo) == len(assigns):
                sub.completed_at = utcnow()
                if rnd.random() < 0.2:
                    sub.clinic_comment = rnd.choice(COMENTARIOS_CLINICA)
    db.commit()
    print("DEMO: dados fictícios carregados. Logins: admin, direcao, recepcao (senha: %s)" % SENHA_DEMO)
