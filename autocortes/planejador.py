"""Planejamento: quais cortes vão para cada rede e quando (calendário e previsão do painel)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, time as dtime, timedelta

from . import agenda
from .config import Config


def status_concluidos(cfg: Config) -> tuple[str, ...]:
    """Status de postagem que contam como 'já feito' para um corte numa rede.

    'aguardando' é a tarefa de postagem à mão: o corte já foi entregue para você postar.
    """
    base = ("publicado", "pulado", "enviando", "aguardando")
    return base + ("simulado",) if cfg.simulacao else base


def status_sucesso(cfg: Config) -> tuple[str, ...]:
    return ("publicado", "simulado") if cfg.simulacao else ("publicado",)


def status_ocupam_horario(cfg: Config) -> tuple[str, ...]:
    """Postagens que atendem um horário da agenda e contam no intervalo mínimo e no limite do dia
    (a tarefa à mão ocupa o horário, mesmo antes de você postar)."""
    return status_sucesso(cfg) + ("aguardando",)


def duracao_maxima(cfg: Config, rede: str) -> float | None:
    """Duração máxima de um corte nesta rede (o YouTube só recebe Shorts)."""
    return float(cfg["youtube"]["max_segundos"]) if rede == "youtube" else None


def sem_cortes_possiveis(cfg: Config, rede: str) -> bool:
    """Nenhum corte cabe na rede: no YouTube, a duração mínima dos cortes passa do limite dos Shorts."""
    maximo = duracao_maxima(cfg, rede)
    return maximo is not None and float(cfg["cortes"]["duracao_min"]) > maximo + 0.5


def _marcas(n: int) -> str:
    return ",".join("?" * n)


def fila(cfg: Config, conn: sqlite3.Connection, rede: str, limite: int | None = None) -> list[sqlite3.Row]:
    """Cortes prontos que ainda não foram para esta rede, na ordem em que vão sair."""
    feitos = status_concluidos(cfg)
    maximo = duracao_maxima(cfg, rede)
    sql = (
        "SELECT c.*, f.titulo AS filme_titulo, f.ano AS filme_ano FROM cortes c JOIN filmes f ON f.id = c.filme_id "
        "WHERE c.status = 'pronto' AND c.arquivo IS NOT NULL AND f.status != 'ignorado' "
        + ("AND (c.fim - c.inicio) <= ? " if maximo is not None else "")
        + "AND NOT EXISTS (SELECT 1 FROM postagens p WHERE p.corte_id = c.id AND p.plataforma = ? "
        f"               AND p.status IN ({_marcas(len(feitos))})) "
        "AND (SELECT COUNT(*) FROM postagens p WHERE p.corte_id = c.id AND p.plataforma = ? "
        "     AND p.status IN ('falhou', 'interrompido')) < 4 "
        "ORDER BY c.prioridade DESC, c.fila_em, c.id"
    )
    params: list = ([maximo + 0.5] if maximo is not None else []) + [rede, *feitos, rede]
    if limite:
        sql += " LIMIT ?"
        params.append(limite)
    return conn.execute(sql, params).fetchall()


def horario_atendido(conn: sqlite3.Connection, rede: str, ts: float, sucesso: tuple[str, ...],
                     limite_ts: float | None = None) -> bool:
    """Um horário conta como atendido quando uma postagem da agenda saiu para ele."""
    marc = _marcas(len(sucesso))
    if conn.execute(
        f"SELECT 1 FROM postagens WHERE plataforma=? AND status IN ({marc}) AND horario IS NOT NULL "
        "AND abs(horario - ?) < 1 LIMIT 1",
        (rede, *sucesso, ts),
    ).fetchone():
        return True
    # postagens antigas, de antes da coluna 'horario'
    sql = (f"SELECT 1 FROM postagens WHERE plataforma=? AND status IN ({marc}) AND horario IS NULL "
           "AND manual=0 AND criado_em>=?")
    params: list = [rede, *sucesso, ts]
    if limite_ts is not None:
        sql += " AND criado_em<?"
        params.append(limite_ts)
    return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def previsao_edicao(cfg: Config, conn: sqlite3.Connection, quantidade: int) -> list[dict]:
    """Ordem em que os próximos candidatos serão editados (mesma regra do produtor)."""
    if quantidade <= 0:
        return []
    candidatos = conn.execute(
        "SELECT c.id, c.filme_id, c.inicio, c.fim, c.pontuacao, c.prioridade, c.frase, f.titulo AS filme_titulo "
        "FROM cortes c JOIN filmes f ON f.id = c.filme_id WHERE c.status='candidato' AND f.status='analisado'"
    ).fetchall()
    contagem = {r[0]: r[1] for r in conn.execute(
        "SELECT filme_id, COUNT(*) FROM cortes WHERE status IN ('revisao', 'pronto', 'concluido') GROUP BY filme_id")}
    partes = {r[0]: r[1] or 0 for r in conn.execute("SELECT filme_id, MAX(parte) FROM cortes GROUP BY filme_id")}
    melhores = cfg["cortes"]["ordem_cortes"] == "melhores"
    intercalar = cfg["cortes"]["ordem_filmes"] == "intercalar"

    def chave(c) -> tuple:
        k: list = [-c["prioridade"]]
        if intercalar:
            k.append(contagem.get(c["filme_id"], 0))
        k.append(c["filme_id"])
        k.append(-c["pontuacao"] if melhores else c["inicio"])
        return tuple(k)

    restantes = list(candidatos)
    saida: list[dict] = []
    while restantes and len(saida) < quantidade:
        escolhido = min(restantes, key=chave)
        restantes.remove(escolhido)
        fid = escolhido["filme_id"]
        contagem[fid] = contagem.get(fid, 0) + 1
        partes[fid] = partes.get(fid, 0) + 1
        saida.append({
            "id": escolhido["id"], "filme_id": fid, "filme": escolhido["filme_titulo"], "parte": partes[fid],
            "inicio": escolhido["inicio"], "fim": escolhido["fim"], "frase": escolhido["frase"],
        })
    return saida


def cobertura(cfg: Config, conn: sqlite3.Connection) -> dict:
    """Por quantos dias o conteúdo atual sustenta a agenda."""
    ativas = cfg.plataformas_ativas()
    uma = lambda sql, *p: conn.execute(sql, p).fetchone()[0] or 0  # noqa: E731
    candidatos = uma("SELECT COUNT(*) FROM cortes c JOIN filmes f ON f.id=c.filme_id "
                     "WHERE c.status='candidato' AND f.status='analisado'")
    revisao = uma("SELECT COUNT(*) FROM cortes WHERE status='revisao'")
    filmes_novos = uma("SELECT COUNT(*) FROM filmes WHERE status IN ('novo', 'analisando')")
    analisados = uma("SELECT COUNT(*) FROM filmes WHERE status='analisado'")
    total_cortes = uma("SELECT COUNT(*) FROM cortes c JOIN filmes f ON f.id=c.filme_id WHERE f.status='analisado'")
    media = total_cortes / analisados if analisados else min(20, int(cfg["cortes"]["max_por_filme"]))
    estimados = int(round(filmes_novos * media))

    por_rede, dias = {}, None
    for rede in ativas:
        taxa = agenda.posts_por_semana(cfg, rede) / 7
        na_fila = len(fila(cfg, conn, rede))
        if sem_cortes_possiveis(cfg, rede):  # já avisado no painel; não puxa a conta das outras redes
            por_rede[rede] = {"na_fila": 0, "por_dia": round(taxa, 2), "dias": None}
            continue
        disponiveis = na_fila + revisao + candidatos + estimados
        d = disponiveis / taxa if taxa else None
        por_rede[rede] = {"na_fila": na_fila, "por_dia": round(taxa, 2), "dias": None if d is None else round(d, 1)}
        if d is not None:
            dias = d if dias is None else min(dias, d)
    return {
        "dias": None if dias is None else round(dias, 1),
        "candidatos": candidatos,
        "revisao": revisao,
        "filmes_novos": filmes_novos,
        "estimados": estimados,
        "consumo_diario": round(agenda.consumo_diario(cfg), 2),
        "por_rede": por_rede,
    }


def montar_plano(cfg: Config, conn: sqlite3.Connection, agora: datetime | None = None, dias: int = 7) -> dict:
    """Calendário de hoje até 'dias' à frente: postagens feitas e horários futuros com o corte previsto."""
    agora = agora or datetime.now()
    inicio = datetime.combine(agora.date(), dtime())
    fim = inicio + timedelta(days=dias)
    ocupam = status_ocupam_horario(cfg)
    tolerancia = timedelta(minutes=max(5.0, float(cfg["agenda"]["tolerancia_minutos"])))
    pausado = bool(cfg["agenda"]["pausado"])
    itens: list[dict] = []

    for p in conn.execute(
        "SELECT p.*, c.parte, f.titulo AS filme_titulo FROM postagens p "
        "JOIN cortes c ON c.id = p.corte_id JOIN filmes f ON f.id = c.filme_id "
        "WHERE p.criado_em >= ? ORDER BY p.criado_em",
        (inicio.timestamp(),),
    ):
        itens.append({
            "tipo": "postagem", "rede": p["plataforma"], "quando": p["criado_em"], "estado": p["status"],
            "corte_id": p["corte_id"], "filme": p["filme_titulo"], "parte": p["parte"],
            "url": p["url"], "erro": p["erro"], "manual": bool(p["manual"]),
        })

    abertos: dict[str, tuple[list, list]] = {}
    necessarios = 0
    for rede in cfg.plataformas_ativas():
        horarios = agenda.horarios_entre(cfg, rede, inicio, fim)
        pendentes: list[tuple[datetime, str]] = []
        for i, h in enumerate(horarios):
            if h > agora:
                pendentes.append((h, "agendado"))
                continue
            limite = horarios[i + 1] if i + 1 < len(horarios) else h + tolerancia
            if horario_atendido(conn, rede, h.timestamp(), ocupam, min(limite, h + tolerancia).timestamp()):
                continue  # a postagem feita (ou a tarefa à mão) já aparece no histórico
            if agora <= h + tolerancia:
                pendentes.append((h, "pendente"))
            else:
                itens.append({"tipo": "horario", "rede": rede, "quando": h.timestamp(), "estado": "perdido"})
        fila_rede = fila(cfg, conn, rede)
        abertos[rede] = (pendentes, fila_rede)
        if not sem_cortes_possiveis(cfg, rede):  # sem corte que caiba, editar mais não resolve
            necessarios = max(necessarios, len(pendentes) - len(fila_rede))

    previstos = previsao_edicao(cfg, conn, necessarios)
    for rede, (pendentes, fila_rede) in abertos.items():
        nenhum_cabe = sem_cortes_possiveis(cfg, rede)
        for j, (h, estado) in enumerate(pendentes):
            item = {"tipo": "horario", "rede": rede, "quando": h.timestamp(),
                    "estado": "pausado" if pausado else estado}
            if j < len(fila_rede):
                c = fila_rede[j]
                item.update(corte_id=c["id"], filme=c["filme_titulo"], parte=c["parte"], previsto=False)
            elif not nenhum_cabe and j - len(fila_rede) < len(previstos):
                c = previstos[j - len(fila_rede)]
                item.update(corte_id=c["id"], filme=c["filme"], parte=c["parte"], previsto=True)
            else:
                item["sem_corte"] = True
            itens.append(item)

    itens.sort(key=lambda x: x["quando"])
    return {
        "agora": agora.timestamp(),
        "inicio": inicio.timestamp(),
        "dias": dias,
        "pausado": pausado,
        "itens": itens,
        "cobertura": cobertura(cfg, conn),
    }
