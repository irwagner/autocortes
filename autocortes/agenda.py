"""Horários de postagem por rede: dias da semana, variação aleatória estável e intervalo mínimo."""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from .config import DIAS_SEMANA, Config

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
        # posts de imagem: a pelo menos 1 h 30 dos vídeos da mesma rede, e o TikTok fora do
        # horário do Instagram (os dois disputariam o navegador)
        "imagens": {"tiktok": ["10:30", "15:30"], "instagram": ["10:00", "16:00"]},
    },
    "leve": {
        "rotulo": "Leve (1 por dia)",
        "descricao": "Um post por dia no horário de maior audiência",
        "youtube": ["19:30"],
        "tiktok": ["19:00"],
        "instagram": ["19:15"],
        "kwai": ["19:45"],
        "bilibili": ["08:00"],
        "imagens": {"tiktok": ["16:30"], "instagram": ["17:00"]},
    },
    "moderado": {
        "rotulo": "Moderado (2 por dia)",
        "descricao": "Almoço e noite",
        "youtube": ["12:30", "20:00"],
        "tiktok": ["12:00", "19:30"],
        "instagram": ["12:15", "19:00"],
        "kwai": ["12:00", "19:30"],
        "bilibili": ["08:00", "10:30"],
        "imagens": {"tiktok": ["10:30", "16:00"], "instagram": ["10:00", "16:30"]},
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
        # o máximo de 6 por dia deixa uma imagem ao lado dos 5 vídeos
        "imagens": {"tiktok": ["10:15"], "instagram": ["17:15"]},
    },
}


def _hhmm(texto: str) -> tuple[int, int]:
    hh, mm = texto.strip().split(":")
    return int(hh), int(mm)


def dia_permitido(cfg: Config, plataforma: str, dia: date, midia: str = "video") -> bool:
    return DIAS_SEMANA[dia.weekday()] in cfg.dias(plataforma, midia)


def _sem_os_proximos(brutos: list[tuple[datetime, str]], minimo: timedelta
                     ) -> tuple[list[tuple[datetime, str]], list[tuple[datetime, str, datetime, str]]]:
    """Os horários (já sorteados) que respeitam o intervalo mínimo entre si, e os que saíram com o
    horário que ficou antes deles."""
    saida: list[tuple[datetime, str]] = []
    fora: list[tuple[datetime, str, datetime, str]] = []
    for h, chave in sorted(brutos):
        if saida and h - saida[-1][0] < minimo:
            fora.append((h, chave, *saida[-1]))
            continue
        saida.append((h, chave))
    return saida, fora


def horarios_com_chave(cfg: Config, plataforma: str, dia: date, midia: str = "video") -> list[tuple[datetime, str]]:
    """Horários reais do dia (com a variação sorteada), respeitando o intervalo mínimo.

    Cada um vem com a chave do horário da agenda ("AAAA-MM-DD HH:MM", antes da variação). É
    pela chave que a postagem feita fica ligada ao horário: mudar a variação ou o fuso no meio
    do dia muda a hora sorteada, mas não a chave, e o post já feito continua contando.
    Com midia="imagem", os horários de imagem da rede, com a chave "img AAAA-MM-DD HH:MM".
    """
    if midia == "imagem":
        return _imagens_do_dia(cfg, plataforma, dia)[0]
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
    return _sem_os_proximos(brutos, minimo)[0]


def _imagens_do_dia(cfg: Config, plataforma: str, dia: date) -> tuple[list[tuple[datetime, str]], list[dict]]:
    """Horários de imagem do dia (com a chave) e os que saíram, com o motivo.

    O vídeo tem preferência: o horário de imagem cujo HH:MM configurado fica a menos de
    intervalo_minimo_min + variacao_minutos do HH:MM configurado de um vídeo da rede (de ontem,
    hoje ou amanhã, para a virada da meia-noite) sai do dia. A conta é pelos configurados, para o
    mesmo par sair ou ficar todo dia; a folga da variação garante o intervalo depois do sorteio.
    Depois, vale entre as próprias imagens o filtro de sempre, pelos horários sorteados.
    """
    if not dia_permitido(cfg, plataforma, dia, "imagem"):
        return [], []
    variacao_min = cfg["agenda"]["variacao_minutos"]
    intervalo_min = cfg["agenda"]["intervalo_minimo_min"]
    precisa = timedelta(minutes=float(intervalo_min + variacao_min))
    videos: list[datetime] = []
    if cfg[plataforma].get("videos", True):
        for d in (dia - timedelta(days=1), dia, dia + timedelta(days=1)):
            if dia_permitido(cfg, plataforma, d):
                videos.extend(datetime.combine(d, time(*_hhmm(t))) for t in cfg.horarios(plataforma))
    variacao = float(variacao_min) * 60
    brutos: list[tuple[datetime, str]] = []
    descartados: list[dict] = []
    for texto in cfg.horarios(plataforma, "imagem"):
        hh, mm = _hhmm(texto)
        base = datetime.combine(dia, time(hh, mm))
        if videos:
            perto = min(videos, key=lambda v: abs(v - base))
            distancia = abs(perto - base)
            if distancia < precisa:
                descartados.append({"horario": f"{hh:02d}:{mm:02d}", "video": perto.strftime("%H:%M"),
                                    "minutos": int(distancia.total_seconds() // 60),
                                    "precisa": int(intervalo_min + variacao_min)})
                continue
        # semente própria: um horário de imagem igual a um de vídeo não sorteia o mesmo atraso
        semente = f"{plataforma}|imagem|{dia.isoformat()}|{texto}"
        atraso = random.Random(semente).uniform(0, variacao) if variacao else 0
        brutos.append((base + timedelta(seconds=atraso), f"img {dia.isoformat()} {hh:02d}:{mm:02d}"))
    saida, fora = _sem_os_proximos(brutos, timedelta(minutes=float(intervalo_min)))
    for h, chave, ficou, chave_ficou in fora:
        descartados.append({"horario": chave[-5:], "video": None, "imagem": chave_ficou[-5:],
                            "minutos": int((h - ficou).total_seconds() // 60), "precisa": int(intervalo_min)})
    return saida, descartados


def horarios_imagem_descartados(cfg: Config, plataforma: str, dia: date) -> list[dict]:
    """Horários de imagem que saem do dia: {"horario", "video", "minutos", "precisa"} (o HH:MM da imagem,
    o do vídeo mais perto, a distância e o mínimo pedido); perto de outra imagem, "video" é None e
    "imagem" traz o HH:MM da que ficou."""
    return _imagens_do_dia(cfg, plataforma, dia)[1]


def horarios_do_dia(cfg: Config, plataforma: str, dia: date, midia: str = "video") -> list[datetime]:
    """Horários reais do dia (com a variação sorteada) respeitando o intervalo mínimo."""
    return [h for h, _ in horarios_com_chave(cfg, plataforma, dia, midia)]


def horarios_entre_com_chave(cfg: Config, plataforma: str, inicio: datetime,
                             fim: datetime, midia: str = "video") -> list[tuple[datetime, str]]:
    saida: list[tuple[datetime, str]] = []
    dia = inicio.date() - timedelta(days=1)  # a variação pode empurrar 23:55 para depois da meia-noite
    while dia <= fim.date():
        saida.extend((h, c) for h, c in horarios_com_chave(cfg, plataforma, dia, midia) if inicio <= h < fim)
        dia += timedelta(days=1)
    return saida


def horarios_entre(cfg: Config, plataforma: str, inicio: datetime, fim: datetime,
                   midia: str = "video") -> list[datetime]:
    return [h for h, _ in horarios_entre_com_chave(cfg, plataforma, inicio, fim, midia)]


def horario_vigente_com_chave(cfg: Config, plataforma: str, agora: datetime,
                              midia: str = "video") -> tuple[datetime, str] | None:
    """Último horário que já chegou e ainda está dentro da tolerância, com a chave dele."""
    tolerancia = timedelta(minutes=max(5.0, float(cfg["agenda"]["tolerancia_minutos"])))
    vigentes = [
        (h, chave)
        for dia in (agora.date() - timedelta(days=1), agora.date())
        for h, chave in horarios_com_chave(cfg, plataforma, dia, midia)
        if h <= agora <= h + tolerancia
    ]
    return max(vigentes) if vigentes else None


def horario_vigente(cfg: Config, plataforma: str, agora: datetime, midia: str = "video") -> datetime | None:
    """Último horário que já chegou e ainda está dentro da tolerância."""
    vigente = horario_vigente_com_chave(cfg, plataforma, agora, midia)
    return vigente[0] if vigente else None


def proximo_horario(cfg: Config, plataforma: str, agora: datetime, midia: str = "video") -> datetime | None:
    for d in range(9):
        for h in horarios_do_dia(cfg, plataforma, agora.date() + timedelta(days=d), midia):
            if h > agora:
                return h
    return None


def posts_por_semana(cfg: Config, plataforma: str, referencia: date | None = None, midia: str = "video") -> int:
    inicio = referencia or date.today()
    return sum(len(horarios_do_dia(cfg, plataforma, inicio + timedelta(days=i), midia)) for i in range(7))


def consumo_diario(cfg: Config) -> float:
    """Quantos cortes novos são necessários por dia (a rede de vídeo mais rápida puxa a fila)."""
    taxas = [posts_por_semana(cfg, p) / 7 for p in cfg.redes_de_video()]
    return max(taxas) if taxas else 0.0
