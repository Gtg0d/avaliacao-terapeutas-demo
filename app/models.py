from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, ForeignKey, Integer, LargeBinary,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    """Data/hora em UTC, sem fuso (igual no SQLite e no Postgres)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    """Quem entra no painel: admin, direcao ou recepcao."""
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Therapist(Base):
    __tablename__ = "therapists"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))  # como os pais conhecem
    full_name: Mapped[str] = mapped_column(String(160), default="")
    sector: Mapped[str] = mapped_column(String(160), default="")  # pode ser "A / B"
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    photo: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)
    has_photo: Mapped[bool] = mapped_column(Boolean, default=False)
    photo_version: Mapped[int] = mapped_column(Integer, default=0)
    assignments: Mapped[list["Assignment"]] = relationship(back_populates="therapist")


class Patient(Base):
    __tablename__ = "patients"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    program: Mapped[str] = mapped_column(String(20), default="intensivo")  # intensivo | particular
    guardians: Mapped[list["Guardian"]] = relationship(
        back_populates="patient", cascade="all, delete-orphan", order_by="Guardian.id")
    assignments: Mapped[list["Assignment"]] = relationship(
        back_populates="patient", cascade="all, delete-orphan")

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name.strip() else self.name


class Guardian(Base):
    """Responsável. Cada um tem o seu link (token)."""
    __tablename__ = "guardians"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(40), default="")
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    patient: Mapped[Patient] = relationship(back_populates="guardians")


class Assignment(Base):
    """Vínculo: este terapeuta atende esta criança nesta terapia."""
    __tablename__ = "assignments"
    __table_args__ = (UniqueConstraint("patient_id", "therapist_id", "therapy"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    therapist_id: Mapped[int] = mapped_column(ForeignKey("therapists.id"))
    therapy: Mapped[str] = mapped_column(String(120))
    sector: Mapped[str] = mapped_column(String(80), default="")  # setor desta terapia (ranking)
    for_guardian: Mapped[bool] = mapped_column(Boolean, default=False)  # quem é atendido é o responsável
    patient: Mapped[Patient] = relationship(back_populates="assignments")
    therapist: Mapped[Therapist] = relationship(back_populates="assignments")


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(10), default="simnao")  # simnao | nota | texto
    allow_ns: Mapped[bool] = mapped_column(Boolean, default=False)  # oferece "Não sei dizer"
    in_index: Mapped[bool] = mapped_column(Boolean, default=True)  # conta no índice do terapeuta
    position: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Round(Base):
    """Rodada de avaliação (ex.: Novembro/2026 - 1ª quinzena)."""
    __tablename__ = "rounds"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | closed
    program: Mapped[str] = mapped_column(String(20), default="intensivo")  # a quem a rodada se destina
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Submission(Base):
    """Um responsável dentro de uma rodada."""
    __tablename__ = "submissions"
    __table_args__ = (UniqueConstraint("round_id", "guardian_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"))
    guardian_id: Mapped[int] = mapped_column(ForeignKey("guardians.id"))
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    clinic_comment: Mapped[str] = mapped_column(Text, default="")  # observação sobre a clínica
    round: Mapped[Round] = relationship()
    guardian: Mapped[Guardian] = relationship()
    evaluations: Mapped[list["Evaluation"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan")


class Evaluation(Base):
    """Avaliação de um terapeuta por um responsável em uma rodada."""
    __tablename__ = "evaluations"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"))
    assignment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    therapist_id: Mapped[int] = mapped_column(ForeignKey("therapists.id"))
    therapy: Mapped[str] = mapped_column(String(120), default="")
    sector: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(10), default="done")  # done | skipped
    comment: Mapped[str] = mapped_column(Text, default="")
    saved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    submission: Mapped[Submission] = relationship(back_populates="evaluations")
    therapist: Mapped[Therapist] = relationship()
    answers: Mapped[list["Answer"]] = relationship(
        back_populates="evaluation", cascade="all, delete-orphan")


class Answer(Base):
    __tablename__ = "answers"
    id: Mapped[int] = mapped_column(primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluations.id"))
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"))
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    evaluation: Mapped[Evaluation] = relationship(back_populates="answers")
    question: Mapped[Question] = relationship()
