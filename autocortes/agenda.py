"""Horários de postagem por rede: dias da semana, variação aleatória estável e intervalo mínimo."""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from .config import DIAS_SEMANA, PLATAFORMAS, Config

# Sugestões para público brasileiro (hora local). Os picos mais citados são o
# almoço (12h-14h) e o começo da noite (18h-21h); no YouTube o meio da tarde
# também aparece bem. O ideal é ajustar depois olhando as métricas de cada conta.
# O Bilibili tem público na China: 08:00 em Brasília é 19:00 em Pequim, o começo da noite de lá.
PRESETS: dict[str, dict] = {
    "recomendado": {
        "rotulo": "Recomendado",
        "descricao": "Almoço, tarde e noite, com horários diferentes em cada rede",
        "youtube": ["12:30", "15:00", "20:00"],
        "tiktok": ["12:00", "18:30", "21:00"],
        "instagram": ["12:15", "19:00"],
        "kwai": ["12:00", "19:30"],
        "bilibili": ["08:00"],
    },
    "leve": {
        "rotulo": "Leve (1 por dia)",
        "descricao": "Um post por dia no horário de maior audiência",
        "youtube": ["19:30"],
        "tiktok": ["19:00"],
        "instagram": ["19:15"],
        "kwai": ["19:45"],
        "bilibili": ["08:00"],
    },
    "moderado": {
        "rotulo": "Moderado (2 por dia)",
        "descricao": "Almoço e noite",
        "youtube": ["12:30", "20:00"],
        "tiktok": ["12:00", "19:30"],
        "instagram": ["12:15", "19:00"],
        "kwai": ["12:00", "19:30"],
        "bilibili": ["08:00", "10:30"],
    },
    "intenso": {
        "rotulo": "Intenso (5 por dia)",
        "descricao": "Para crescer rápido; exige bastante filme na pasta",
        "youtube": ["09:00", "12:30", "15:30", "18:30", "21:30"],
        "tiktok": ["08:30", "12:00", "15:00", "18:30", "21:00"],
        "instagram": ["09:15", "12:15", "15:15", "19:00", "21:15"],
        "kwai": ["09:00", "12:00", "15:00", "18:30", "21:00"],
        # 75 min entre um e outro: com 1 h só, a variação sorteada juntava dois horários abaixo do
        # intervalo mínimo e um deles sumia do dia (18:00 a 23:00 em Pequim)
        "bilibili": ["07:00", "08:15", "09:30", "10:45", "12:00"],
    },
}


def _hhmm(texto: str) -> tuple[int, int]:
    hh, mm = texto.strip().split(":")
    return int(hh), int(mm)


def dia_permitido(cfg: Config, plataforma: str, dia: date) -> bool:
    return DIAS_SEMANA[dia.weekday()] in cfg.dias(plataforma)


def horarios_com_chave(cfg: Config, plataforma: str, dia: date) -> list[tuple[datetime, str]]:
    """Horários reais do dia (com a variação sorteada), respeitando o intervalo mínimo.

    Cada um vem com a chave do horário da agenda ("AAAA-MM-DD HH:MM", antes da variação). É
    pela chave que a postagem feita fica ligada ao horário: mudar a variação ou o fuso no meio
    do dia muda a hora sorteada, mas não a chave, e o post já feito continua contando.
    """
    if not dia_permitido(cfg, plataforma, dia):
        return []
    variacao = float(cfg["agenda"]["variacao_minutos"]) * 60
    brutos = []
    for texto in cfg.horarios(plataforma):
        hh, mm = _hhmm(texto)
        base = datetime.combine(dia, time(hh, mm))
        # mesma semente para o mesmo dia/horário: reiniciar o programa não muda o sorteio
        atraso = random.Random(f"{plataforma}|{dia.isoformat()}|{texto}").uniform(0, variacao) if variacao else 0
        brutos.append((base + timedelta(seconds=atraso), f"{dia.isoformat()} {hh:02d}:{mm:02d}"))
    minimo = timedelta(minutes=float(cfg["agenda"]["intervalo_minimo_min"]))
    saida: list[tuple[datetime, str]] = []
    for h, chave in sorted(brutos):
        if saida and h - saida[-1][0] < minimo:
            continue
        saida.append((h, chave))
    return saida


def horarios_do_dia(cfg: Config, plataforma: str, dia: date) -> list[datetime]:
    """Horários reais do dia (com a variação sorteada) respeitando o intervalo mínimo."""
    return [h for h, _ in horarios_com_chave(cfg, plataforma, dia)]


def horarios_entre_com_chave(cfg: Config, plataforma: str, inicio: datetime,
                             fim: datetime) -> list[tuple[datetime, str]]:
    saida: list[tuple[datetime, str]] = []
    dia = inicio.date() - timedelta(days=1)  # a variação pode empurrar 23:55 para depois da meia-noite
    while dia <= fim.date():
        saida.extend((h, c) for h, c in horarios_com_chave(cfg, plataforma, dia) if inicio <= h < fim)
        dia += timedelta(days=1)
    return saida


def horarios_entre(cfg: Config, plataforma: str, inicio: datetime, fim: datetime) -> list[datetime]:
    return [h for h, _ in horarios_entre_com_chave(cfg, plataforma, inicio, fim)]


def horario_vigente_com_chave(cfg: Config, plataforma: str, agora: datetime) -> tuple[datetime, str] | None:
    """Último horário que já chegou e ainda está dentro da tolerância, com a chave dele."""
    tolerancia = timedelta(minutes=max(5.0, float(cfg["agenda"]["tolerancia_minutos"])))
    vigentes = [
        (h, chave)
        for dia in (agora.date() - timedelta(days=1), agora.date())
        for h, chave in horarios_com_chave(cfg, plataforma, dia)
        if h <= agora <= h + tolerancia
    ]
    return max(vigentes) if vigentes else None


def horario_vigente(cfg: Config, plataforma: str, agora: datetime) -> datetime | None:
    """Último horário que já chegou e ainda está dentro da tolerância."""
    vigente = horario_vigente_com_chave(cfg, plataforma, agora)
    return vigente[0] if vigente else None


def proximo_horario(cfg: Config, plataforma: str, agora: datetime) -> datetime | None:
    for d in range(9):
        for h in horarios_do_dia(cfg, plataforma, agora.date() + timedelta(days=d)):
            if h > agora:
                return h
    return None


def posts_por_semana(cfg: Config, plataforma: str, referencia: date | None = None) -> int:
    inicio = referencia or date.today()
    return sum(len(horarios_do_dia(cfg, plataforma, inicio + timedelta(days=i))) for i in range(7))


def consumo_diario(cfg: Config) -> float:
    """Quantos cortes novos são necessários por dia (a rede mais rápida puxa a fila)."""
    taxas = [posts_por_semana(cfg, p) / 7 for p in PLATAFORMAS if cfg[p]["ativo"]]
    return max(taxas) if taxas else 0.0
