"""Métricas dos posts (visualizações, curtidas, comentários) lidas das redes depois da publicação.

Hoje: YouTube (1 unidade de cota a cada 50 vídeos) e Instagram (permissão
instagram_manage_insights) pela API oficial. O TikTok oficial só publica em privado
e o Upload-Post não expõe esse dado no mesmo formato, então ficam de fora por ora.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time

from .config import Config
from .plataformas import ErroPublicacao, Plataforma
from .util import log

_ID_VALIDO = re.compile(r"^[\w-]{5,64}$")


def coletar(cfg: Config, conn: sqlite3.Connection, plataformas: dict[str, Plataforma]) -> int:
    """Atualiza as métricas dos posts recentes. Devolve quantos posts foram atualizados."""
    m = cfg["metricas"]
    agora = time.time()
    desde = agora - float(m["janela_dias"]) * 86400
    antes_de = agora - float(m["intervalo_horas"]) * 3600
    total = 0
    for nome, plataforma in plataformas.items():
        if plataforma.via != "oficial" or not hasattr(plataforma, "estatisticas"):
            continue
        if not plataforma.pronta()[0]:
            continue
        linhas = conn.execute(
            "SELECT id, id_remoto FROM postagens WHERE plataforma=? AND status='publicado' AND id_remoto IS NOT NULL "
            "AND criado_em>=? AND (metricas_em IS NULL OR metricas_em<?) ORDER BY criado_em DESC LIMIT 200",
            (nome, desde, antes_de),
        ).fetchall()
        linhas = [linha for linha in linhas if _ID_VALIDO.match(str(linha["id_remoto"]))]
        if not linhas:
            continue
        try:
            dados = plataforma.estatisticas([linha["id_remoto"] for linha in linhas])
        except ErroPublicacao as e:
            log.info("Métricas do %s indisponíveis agora: %s", plataforma.rotulo, e)
            continue
        for linha in linhas:
            d = dados.get(linha["id_remoto"])
            if not d:
                continue
            conn.execute(
                "UPDATE postagens SET visualizacoes=?, curtidas=?, comentarios=?, compartilhamentos=?, "
                "metricas_extra=?, metricas_em=? WHERE id=?",
                (d.get("visualizacoes"), d.get("curtidas"), d.get("comentarios"), d.get("compartilhamentos"),
                 json.dumps(d.get("extra") or {}), agora, linha["id"]),
            )
            total += 1
    if total:
        log.info("Métricas atualizadas de %d post(s)", total)
    return total


def resumo(conn: sqlite3.Connection, dias: int = 7) -> dict:
    """Totais por rede nos últimos dias e os cortes com mais visualizações (para o painel)."""
    desde = time.time() - dias * 86400
    por_rede = {
        r["plataforma"]: {"posts": r["posts"], "visualizacoes": r["v"] or 0, "curtidas": r["c"] or 0,
                          "comentarios": r["m"] or 0, "com_metricas": r["com"]}
        for r in conn.execute(
            "SELECT plataforma, COUNT(*) AS posts, SUM(visualizacoes) AS v, SUM(curtidas) AS c, "
            "SUM(comentarios) AS m, SUM(metricas_em IS NOT NULL) AS com FROM postagens "
            "WHERE status='publicado' AND criado_em>=? GROUP BY plataforma",
            (desde,),
        )
    }
    melhores = [
        dict(r) for r in conn.execute(
            "SELECT c.id AS corte_id, c.parte, f.titulo AS filme, SUM(p.visualizacoes) AS visualizacoes, "
            "SUM(p.curtidas) AS curtidas FROM postagens p JOIN cortes c ON c.id=p.corte_id "
            "JOIN filmes f ON f.id=c.filme_id WHERE p.status='publicado' AND p.visualizacoes IS NOT NULL "
            "AND p.criado_em>=? GROUP BY c.id ORDER BY visualizacoes DESC LIMIT 5",
            (time.time() - 30 * 86400,),
        )
    ]
    return {"dias": dias, "por_rede": por_rede, "melhores": melhores,
            "tem_dados": any(v["com_metricas"] for v in por_rede.values())}
