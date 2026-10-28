"""Planejamento: quais cortes vão para cada rede e quando (calendário e previsão do painel)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from . import agenda, analise, db, gravador, roteiro
from .config import REDES_IMAGEM, TIPOS_IMAGEM, Config, motivo_sem_imagens


# falhas comuns de um corte numa rede antes de desistir dele (a última vira 'pulado', com o motivo)
FALHAS_POR_CORTE = 4


def status_concluidos(cfg: Config) -> tuple[str, ...]:
    """Status de postagem que contam como 'já feito' para um corte numa rede.

    'aguardando' é a tarefa de postagem à mão: o corte já foi entregue para você postar.
    'conferir' é o envio que a rede recebeu sem confirmar: nunca é repetido sozinho.
    """
    base = ("publicado", "pulado", "enviando", "aguardando", "conferir")
    return base + ("simulado",) if cfg.simulacao else base


def status_sucesso(cfg: Config) -> tuple[str, ...]:
    return ("publicado", "simulado") if cfg.simulacao else ("publicado",)


def status_ocupam_horario(cfg: Config) -> tuple[str, ...]:
    """Postagens que atendem um horário da agenda e contam no intervalo mínimo e no limite do dia.

    A tarefa à mão ocupa o horário mesmo antes de você postar; o post 'a conferir' provavelmente
    saiu; e o ensaio também gasta o horário (senão o mesmo horário puxaria o corte seguinte).
    """
    return status_sucesso(cfg) + ("aguardando", "conferir", "ensaio")


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
        # os posts de imagem do corte não contam: o vídeo dele continua na fila da rede
        + "AND NOT EXISTS (SELECT 1 FROM postagens p WHERE p.corte_id = c.id AND p.plataforma = ? "
        f"               AND p.midia = 'video' AND p.status IN ({_marcas(len(feitos))})) "
        # o publicador transforma a última falha em 'pulado'; isto segura só o histórico antigo
        "AND (SELECT COUNT(*) FROM postagens p WHERE p.corte_id = c.id AND p.plataforma = ? "
        "     AND p.midia = 'video' AND p.status = 'falhou') < ? "
        "ORDER BY c.prioridade DESC, c.fila_em, c.id"
    )
    params: list = ([maximo + 0.5] if maximo is not None else []) + [rede, *feitos, rede, FALHAS_POR_CORTE]
    if limite:
        sql += " LIMIT ?"
        params.append(limite)
    return conn.execute(sql, params).fetchall()


def horario_atendido(conn: sqlite3.Connection, rede: str, ts: float, sucesso: tuple[str, ...],
                     limite_ts: float | None = None, chave: str | None = None, midia: str = "video") -> bool:
    """Um horário conta como atendido quando uma postagem da agenda saiu para ele.

    A ligação é pela chave do horário (agenda.horarios_com_chave); as postagens de antes da
    chave existir são comparadas pela hora exata. Só as postagens da mesma mídia contam, e as
    de antes da chave só existem no vídeo.
    """
    marc = _marcas(len(sucesso))
    if chave and conn.execute(
        f"SELECT 1 FROM postagens WHERE plataforma=? AND midia=? AND status IN ({marc}) AND slot=? LIMIT 1",
        (rede, midia, *sucesso, chave),
    ).fetchone():
        return True
    if midia != "video":
        return False
    if conn.execute(
        f"SELECT 1 FROM postagens WHERE plataforma=? AND midia='video' AND status IN ({marc}) AND horario IS NOT NULL "
        "AND slot IS NULL AND abs(horario - ?) < 1 LIMIT 1",
        (rede, *sucesso, ts),
    ).fetchone():
        return True
    # postagens antigas, de antes da coluna 'horario'
    sql = (f"SELECT 1 FROM postagens WHERE plataforma=? AND midia='video' AND status IN ({marc}) AND horario IS NULL "
           "AND manual=0 AND criado_em>=?")
    params: list = [rede, *sucesso, ts]
    if limite_ts is not None:
        sql += " AND criado_em<?"
        params.append(limite_ts)
    return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


# ---------------------------------------------------------------- imagens

# um post de imagem que falhou por culpa da gravação ("não saiu") ou do PC ("geração:") não gasta o corte
_FALHA_DO_CORTE = "COALESCE(erro, '') NOT LIKE 'gravação:%' AND COALESCE(erro, '') NOT LIKE 'geração:%'"


def ler_estado_imagens(conn: sqlite3.Connection, rede: str) -> dict:
    """O que o envio e a gravação descobriram da rede: {sem_carrossel, parado, sem_fotos: mensagem}."""
    valor = db.ler_estado(conn, f"imagens:{rede}", {})
    return valor if isinstance(valor, dict) else {}


def gravar_estado_imagens(conn: sqlite3.Connection, rede: str, valor: dict) -> None:
    db.gravar_estado(conn, f"imagens:{rede}", valor)


def limpar_estado_imagens(conn: sqlite3.Connection, rede: str) -> None:
    db.apagar_estado(conn, f"imagens:{rede}")


def _situacao_imagens(cfg: Config, conn: sqlite3.Connection, rede: str) -> tuple[str | None, str]:
    """(None, "") quando a rede pode postar imagens; senão o código do motivo e o texto.

    Códigos: "desligada" (fora de redes_de_imagem), "sem_roteiro" (falta o roteiro de imagem, ele
    tem erro ou o campo de arquivo da conta não aceita foto) e "parado" (o envio parou a rede).
    """
    if rede not in cfg.redes_de_imagem():
        return "desligada", motivo_sem_imagens(cfg, rede) or "as imagens estão desligadas nesta rede"
    texto = gravador.carregar(cfg, roteiro.nome_roteiro(rede, "imagem"))
    if texto is None:
        return "sem_roteiro", "falta gravar o roteiro de imagem desta rede"
    erros = roteiro.analisar(texto, roteiro.nome_roteiro(rede, "imagem"))[1]
    if erros:
        return "sem_roteiro", f"o roteiro de imagem tem erro: {erros[0]}"
    estado = ler_estado_imagens(conn, rede)
    if estado.get("sem_fotos"):
        return "sem_roteiro", str(estado["sem_fotos"])
    if estado.get("parado"):
        return "parado", str(estado["parado"])
    return None, ""


def imagens_prontas(cfg: Config, conn: sqlite3.Connection, rede: str) -> tuple[bool, str]:
    """A rede pode postar imagens agora (com o motivo em português quando não pode).

    Não olha a pausa da rede: essa é do ciclo e do plano, a mesma dos vídeos.
    """
    codigo, motivo = _situacao_imagens(cfg, conn, rede)
    return codigo is None, motivo


def tipos_efetivos(cfg: Config, conn: sqlite3.Connection, rede: str) -> list[str]:
    """Os [imagens].tipos na ordem fixa, sem o carrossel quando o campo da rede aceita uma imagem só."""
    ligados = set(cfg["imagens"]["tipos"])
    sem_carrossel = bool(ler_estado_imagens(conn, rede).get("sem_carrossel"))
    return [t for t in TIPOS_IMAGEM if t in ligados and not (t == "carrossel" and sem_carrossel)]


def _girar(tipos: list[str], ultimo: str | None) -> list[str]:
    """Os tipos começando logo depois do último (na ordem fixa de TIPOS_IMAGEM, em roda)."""
    if ultimo not in TIPOS_IMAGEM:
        return list(tipos)
    i = TIPOS_IMAGEM.index(ultimo)
    return sorted(tipos, key=lambda t: (TIPOS_IMAGEM.index(t) - i - 1) % len(TIPOS_IMAGEM))


def tipos_da_vez(cfg: Config, conn: sqlite3.Connection, rede: str) -> list[str]:
    """Os tipos efetivos em rodízio, a partir do que vem depois da última imagem postada na rede.

    A linha da gravação com menos imagens que os exemplos não tem tipo e não mexe no rodízio.
    """
    status = status_ocupam_horario(cfg) + ("pulado",)
    ultima = conn.execute(
        "SELECT imagem_tipo FROM postagens WHERE plataforma=? AND midia='imagem' AND imagem_tipo IS NOT NULL "
        f"AND status IN ({_marcas(len(status))}) ORDER BY criado_em DESC, id DESC LIMIT 1",
        (rede, *status),
    ).fetchone()
    return _girar(tipos_efetivos(cfg, conn, rede), ultima[0] if ultima else None)


def video_publicado(conn: sqlite3.Connection, corte_id: int, rede: str, aceitar_simulado: bool) -> bool:
    """O vídeo do corte saiu nesta rede (o simulado só conta com aceitar_simulado)."""
    status = ("publicado", "simulado") if aceitar_simulado else ("publicado",)
    return conn.execute(
        "SELECT 1 FROM postagens WHERE corte_id=? AND plataforma=? AND midia='video' "
        f"AND status IN ({_marcas(len(status))}) LIMIT 1",
        (corte_id, rede, *status),
    ).fetchone() is not None


def _cortes_para_imagem(cfg: Config, conn: sqlite3.Connection, rede: str, limite: int, tipos: list[str],
                        com_video: bool) -> list[sqlite3.Row]:
    ef = [t for t in TIPOS_IMAGEM if t in tipos]
    if not ef:
        return []
    concl = status_concluidos(cfg)
    # só os cortes com algum tipo por fazer, filtrados antes do LIMIT: os esgotados nunca enchem a página
    por_fazer, params_tipos = [], []
    for tipo in ef:
        cond = ("NOT EXISTS (SELECT 1 FROM postagens p WHERE p.corte_id = c.id AND p.plataforma = ? "
                f"AND p.midia = 'imagem' AND p.imagem_tipo = ? AND p.status IN ({_marcas(len(concl))}))")
        if tipo == "frase":
            cond = "TRIM(COALESCE(c.frase, '')) <> '' AND " + cond
        por_fazer.append(f"({cond})")
        params_tipos += [rede, tipo, *concl]
    sucesso = status_sucesso(cfg)
    base = ("FROM cortes c JOIN filmes f ON f.id = c.filme_id "
            "WHERE f.tipo = 'filme' AND f.status NOT IN ('ignorado', 'ausente') "
            "AND c.status IN ('pronto', 'concluido') "
            f"AND ({' OR '.join(por_fazer)}) ")
    if com_video:
        # a imagem chama para um vídeo que já saiu nesta rede: o mais recente primeiro
        video = (f"FROM postagens v WHERE v.corte_id = c.id AND v.plataforma = ? AND v.midia = 'video' "
                 f"AND v.status IN ({_marcas(len(sucesso))})")
        sql = (f"SELECT c.*, f.titulo AS filme_titulo, f.ano AS filme_ano, f.caminho AS filme_caminho, "
               f"(SELECT MAX(v.criado_em) {video}) AS video_em "
               + base + f"AND EXISTS (SELECT 1 {video}) ORDER BY video_em DESC, c.id DESC LIMIT ? OFFSET ?")
        params = [rede, *sucesso, *params_tipos, rede, *sucesso]
    else:
        sql = ("SELECT c.*, f.titulo AS filme_titulo, f.ano AS filme_ano, f.caminho AS filme_caminho, "
               "NULL AS video_em " + base + "ORDER BY c.prioridade DESC, c.fila_em, c.id LIMIT ? OFFSET ?")
        params = list(params_tipos)
    # filme no disco (um HDD desligado tira os filmes dele sem gastar nada): um stat por filme
    no_disco: dict[int, bool] = {}

    def tem_disco(c) -> bool:
        fid = c["filme_id"]
        if fid not in no_disco:
            no_disco[fid] = (Path(c["filme_caminho"]).is_file()
                             and (analise.pasta_do_filme(cfg, fid) / "info.json").is_file())
        return no_disco[fid]

    saida: list[sqlite3.Row] = []
    for pagina in range(3):
        linhas = conn.execute(sql, [*params, limite, pagina * limite]).fetchall()
        saida = [c for c in linhas if tem_disco(c)]
        # a página inteira fora do disco: tenta a seguinte (até 3)
        if saida or len(linhas) < limite:
            break
    return saida


def cortes_para_imagem(cfg: Config, conn: sqlite3.Connection, rede: str, limite: int = 200,
                       tipos: list[str] | None = None) -> list[sqlite3.Row]:
    """Cortes de filme editados de onde pode sair uma imagem nesta rede, na ordem de preferência.

    Rede com vídeos: só os cortes cujo vídeo já saiu nela, do vídeo mais recente para o mais velho.
    Rede só de imagens: os editados (pronto/concluido) na ordem da fila. Nos dois, só os que têm algum
    dos tipos (padrão: tipos_efetivos) por fazer, nunca candidato, revisão nem pauta, e só com o
    filme e a análise dele no disco.
    """
    if tipos is None:
        tipos = tipos_efetivos(cfg, conn, rede)
    return _cortes_para_imagem(cfg, conn, rede, limite, tipos, bool(cfg[rede].get("videos", True)))


def fila_imagens(cfg: Config, conn: sqlite3.Connection, rede: str, quantidade: int = 1,
                 forcar_tipo: str | None = None, qualquer_editado: bool = False) -> list[dict]:
    """As próximas imagens da rede, na ordem em que vão sair: [{"corte": Row, "tipo": str}].

    O publicador pede uma; o plano pede a lista toda, simulando o motor: cada imagem prevista conta
    como feita para a seguinte, e o rodízio dos tipos anda como andaria com o post gravado.
    forcar_tipo (a gravação) troca o rodízio por esse tipo, ignorando [imagens].tipos e o
    sem_carrossel. qualquer_editado (a gravação): sem nenhum corte com o vídeo publicado na rede,
    vale o corte editado mais antigo da fila.
    """
    if quantidade <= 0:
        return []
    ordem = [forcar_tipo] if forcar_tipo else tipos_da_vez(cfg, conn, rede)
    if not ordem:
        return []
    candidatos = cortes_para_imagem(cfg, conn, rede, tipos=ordem)
    if not candidatos and qualquer_editado and cfg[rede].get("videos", True):
        candidatos = _cortes_para_imagem(cfg, conn, rede, 200, ordem, com_video=False)
    if not candidatos:
        return []
    concl = status_concluidos(cfg)
    feitos = {(r[0], r[1]) for r in conn.execute(
        "SELECT corte_id, imagem_tipo FROM postagens WHERE plataforma=? AND midia='imagem' "
        f"AND imagem_tipo IS NOT NULL AND status IN ({_marcas(len(concl))})", (rede, *concl))}
    falhas: dict[tuple[int, str], int] = {}
    for r in conn.execute(
        "SELECT corte_id, imagem_tipo, COUNT(*) FROM postagens WHERE plataforma=? AND midia='imagem' "
        f"AND imagem_tipo IS NOT NULL AND status='falhou' AND {_FALHA_DO_CORTE} GROUP BY corte_id, imagem_tipo",
        (rede,),
    ):
        falhas[(r[0], r[1])] = r[2]

    saida: list[dict] = []
    while len(saida) < quantidade:
        escolha = None
        for tipo in ordem:
            for c in candidatos:
                if (c["id"], tipo) in feitos or falhas.get((c["id"], tipo), 0) >= FALHAS_POR_CORTE:
                    continue
                if tipo == "frase" and not str(c["frase"] or "").strip():
                    continue
                escolha = (c, tipo)
                break
            if escolha:
                break
        if escolha is None:
            break
        c, tipo = escolha
        saida.append({"corte": c, "tipo": tipo})
        feitos.add((c["id"], tipo))
        ordem = _girar(ordem, tipo)
    return saida


def estoque_imagens(cfg: Config, conn: sqlite3.Connection, rede: str) -> int:
    """Quantos cortes ainda rendem alguma imagem nesta rede (o estoque das redes só de imagens)."""
    return len(cortes_para_imagem(cfg, conn, rede))


def imagens_sem_rendimento(cfg: Config, conn: sqlite3.Connection, rede: str) -> bool:
    """Os últimos cortes editados não rendem nenhum dos tipos ligados (tipos = ["frase"] com filmes
    sem fala, por exemplo), e nenhum deles já deu imagem nesta rede.

    Com menos cortes editados que max(3, buffer_cortes), ainda não dá para dizer: falso.
    """
    n = max(3, int(cfg["geral"]["buffer_cortes"]))
    recentes = conn.execute(
        "SELECT c.id, c.frase FROM cortes c JOIN filmes f ON f.id = c.filme_id "
        "WHERE f.tipo = 'filme' AND c.status IN ('pronto', 'concluido') "
        "ORDER BY c.fila_em DESC, c.id DESC LIMIT ?", (n,)).fetchall()
    if len(recentes) < n:
        return False
    ef = tipos_efetivos(cfg, conn, rede)
    concl = status_concluidos(cfg)
    for c in recentes:
        if any(t != "frase" or str(c["frase"] or "").strip() for t in ef):
            return False
        if conn.execute(
            "SELECT 1 FROM postagens WHERE corte_id=? AND plataforma=? AND midia='imagem' "
            f"AND status IN ({_marcas(len(concl))}) LIMIT 1", (c["id"], rede, *concl),
        ).fetchone():
            return False
    return True


def imagens_possiveis(cfg: Config, conn: sqlite3.Connection, rede: str) -> bool:
    """A rede pode render imagens: roteiro pronto, algum tipo ligado e os cortes recentes servindo."""
    return (imagens_prontas(cfg, conn, rede)[0] and bool(tipos_efetivos(cfg, conn, rede))
            and not imagens_sem_rendimento(cfg, conn, rede))


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
    """Por quantos dias o conteúdo atual sustenta a agenda (dos vídeos)."""
    ativas = cfg.redes_de_video()
    uma = lambda sql, *p: conn.execute(sql, p).fetchone()[0] or 0  # noqa: E731
    # as contas de "quantos cortes um filme rende" são só dos filmes: uma pauta rende um vídeo
    candidatos = uma("SELECT COUNT(*) FROM cortes c JOIN filmes f ON f.id=c.filme_id "
                     "WHERE c.status='candidato' AND f.status='analisado' AND f.tipo='filme'")
    revisao = uma("SELECT COUNT(*) FROM cortes WHERE status='revisao'")
    filmes_novos = uma("SELECT COUNT(*) FROM filmes WHERE tipo='filme' AND status IN ('novo', 'analisando')")
    analisados = uma("SELECT COUNT(*) FROM filmes WHERE tipo='filme' AND status='analisado'")
    total_cortes = uma("SELECT COUNT(*) FROM cortes c JOIN filmes f ON f.id=c.filme_id "
                       "WHERE f.status='analisado' AND f.tipo='filme'")
    # pauta só vira vídeo com a criação ligada: desligada, ela não sustenta a agenda
    pautas_novas = uma("SELECT COUNT(*) FROM filmes WHERE tipo='pauta' AND status IN ('novo', 'criando')") \
        if cfg["criacao"]["ativo"] else 0
    media = total_cortes / analisados if analisados else min(20, int(cfg["cortes"]["max_por_filme"]))
    estimados = int(round(filmes_novos * media))

    por_rede, dias = {}, None
    for rede in ativas:
        taxa = agenda.posts_por_semana(cfg, rede) / 7
        na_fila = len(fila(cfg, conn, rede))
        if sem_cortes_possiveis(cfg, rede):  # já avisado no painel; não puxa a conta das outras redes
            por_rede[rede] = {"na_fila": 0, "por_dia": round(taxa, 2), "dias": None}
            continue
        disponiveis = na_fila + revisao + candidatos + estimados + pautas_novas
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
        "pautas": pautas_novas,
        "consumo_diario": round(agenda.consumo_diario(cfg), 2),
        "por_rede": por_rede,
    }


def _pausas_das_redes(conn: sqlite3.Connection) -> dict[str, float | None]:
    """Até quando cada rede está pausada pelo motor (None = até você testar a conta)."""
    from .publicador import ler_bloqueios  # aqui dentro: o publicador importa este módulo

    return {rede: (None if b.get("ate") is None else float(b["ate"])) for rede, b in ler_bloqueios(conn).items()}


def montar_plano(cfg: Config, conn: sqlite3.Connection, agora: datetime | None = None, dias: int = 7) -> dict:
    """Calendário de hoje até 'dias' à frente: postagens feitas e horários futuros com o corte previsto."""
    agora = agora or datetime.now()
    inicio = datetime.combine(agora.date(), dtime())
    fim = inicio + timedelta(days=dias)
    ocupam = status_ocupam_horario(cfg)
    tolerancia = timedelta(minutes=max(5.0, float(cfg["agenda"]["tolerancia_minutos"])))
    pausado = bool(cfg["agenda"]["pausado"])
    # rede parada pelo motor (bloqueio, cota, recusas): os horários dela até a pausa acabar não saem
    pausas = {} if cfg.simulacao else _pausas_das_redes(conn)
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
            "midia": p["midia"], "imagem_tipo": p["imagem_tipo"],
        })

    abertos: dict[str, tuple[list, list]] = {}
    necessarios = 0
    # rede só de imagens não tem horário de vídeo nem puxa a edição
    for rede in cfg.redes_de_video():
        horarios = agenda.horarios_entre_com_chave(cfg, rede, inicio, fim)
        pendentes: list[tuple[datetime, str]] = []
        for i, (h, chave) in enumerate(horarios):
            if h > agora:
                pendentes.append((h, "agendado"))
                continue
            limite = horarios[i + 1][0] if i + 1 < len(horarios) else h + tolerancia
            if horario_atendido(conn, rede, h.timestamp(), ocupam, min(limite, h + tolerancia).timestamp(), chave):
                continue  # a postagem feita (ou a tarefa à mão) já aparece no histórico
            if agora <= h + tolerancia:
                pendentes.append((h, "pendente"))
            else:
                itens.append({"tipo": "horario", "rede": rede, "quando": h.timestamp(), "estado": "perdido",
                              "midia": "video"})
        fila_rede = fila(cfg, conn, rede)
        abertos[rede] = (pendentes, fila_rede)
        if not sem_cortes_possiveis(cfg, rede):  # sem corte que caiba, editar mais não resolve
            necessarios = max(necessarios, len(pendentes) - len(fila_rede))

    # Os mesmos cortes editados vão para todas as redes, mas no YouTube só os que cabem num Short:
    # a previsão olha mais à frente para achar os que cabem lá
    com_limite = any(duracao_maxima(cfg, r) is not None for r in abertos)
    previstos = previsao_edicao(cfg, conn, necessarios * 3 + 5 if com_limite and necessarios else necessarios)
    for rede, (pendentes, fila_rede) in abertos.items():
        nenhum_cabe = sem_cortes_possiveis(cfg, rede)
        maximo = duracao_maxima(cfg, rede)
        cabem = [c for c in previstos if maximo is None or c["fim"] - c["inicio"] <= maximo + 0.5]
        for j, (h, estado) in enumerate(pendentes):
            parada = rede in pausas and (pausas[rede] is None or h.timestamp() < pausas[rede])
            item = {"tipo": "horario", "rede": rede, "quando": h.timestamp(), "midia": "video",
                    "estado": "pausado" if pausado or parada else estado}
            if j < len(fila_rede):
                c = fila_rede[j]
                item.update(corte_id=c["id"], filme=c["filme_titulo"], parte=c["parte"], previsto=False)
            elif not nenhum_cabe and j - len(fila_rede) < len(cabem):
                c = cabem[j - len(fila_rede)]
                item.update(corte_id=c["id"], filme=c["filme"], parte=c["parte"], previsto=True)
            else:
                item["sem_corte"] = True
            itens.append(item)

    # imagens: os horários de cada rede que posta imagens, com a mesma conta de atendido e perdido,
    # e a previsão igual à do motor (fila_imagens simula os posts um a um)
    for rede in cfg.redes_de_imagem():
        horarios = agenda.horarios_entre_com_chave(cfg, rede, inicio, fim, "imagem")
        pendentes = []
        for i, (h, chave) in enumerate(horarios):
            if h > agora:
                pendentes.append((h, "agendado"))
                continue
            limite = horarios[i + 1][0] if i + 1 < len(horarios) else h + tolerancia
            if horario_atendido(conn, rede, h.timestamp(), ocupam, min(limite, h + tolerancia).timestamp(), chave,
                                "imagem"):
                continue
            if agora <= h + tolerancia:
                pendentes.append((h, "pendente"))
            else:
                itens.append({"tipo": "horario", "rede": rede, "quando": h.timestamp(), "estado": "perdido",
                              "midia": "imagem"})
        if not pendentes:
            continue
        codigo, _ = _situacao_imagens(cfg, conn, rede)
        sem_tipo = codigo is None and not tipos_efetivos(cfg, conn, rede)
        previsao = fila_imagens(cfg, conn, rede, len(pendentes)) if codigo is None and not sem_tipo else []
        com_videos = bool(cfg[rede].get("videos", True))
        # os horários de vídeo desta rede que já têm corte previsto: a imagem deles vem depois
        videos_previstos = [x["quando"] for x in itens if x["tipo"] == "horario" and x["rede"] == rede
                            and x.get("midia") == "video" and x.get("corte_id") is not None]
        saiu_video = com_videos and conn.execute(
            "SELECT 1 FROM postagens WHERE plataforma=? AND midia='video' "
            f"AND status IN ({_marcas(len(status_sucesso(cfg)))}) LIMIT 1", (rede, *status_sucesso(cfg)),
        ).fetchone() is not None
        for j, (h, estado) in enumerate(pendentes):
            parada = rede in pausas and (pausas[rede] is None or h.timestamp() < pausas[rede])
            item = {"tipo": "horario", "rede": rede, "quando": h.timestamp(), "midia": "imagem",
                    "estado": "pausado" if pausado or parada else estado}
            if codigo is not None:
                item.update(sem_imagem=True, motivo=codigo)
            elif sem_tipo:
                item.update(sem_imagem=True, motivo="sem_tipo")
            elif j < len(previsao):
                c = previsao[j]["corte"]
                item.update(imagem_tipo=previsao[j]["tipo"], corte_id=c["id"], filme=c["filme_titulo"],
                            parte=c["parte"])
            elif com_videos and any(q < h.timestamp() for q in videos_previstos):
                item.update(sem_imagem=True, motivo="espera_video")
            else:
                item.update(sem_imagem=True, motivo="sem_corte")
                if com_videos and not saiu_video:
                    item["detalhe"] = "nenhum vídeo desta rede saiu ainda"
            itens.append(item)

    # os horários de imagem que saem do dia por ficarem perto de um vídeo (ou de outra imagem),
    # uma vez cada par no período
    descartados_imagem: dict[str, list[dict]] = {}
    for rede in cfg.plataformas_ativas():
        if rede not in REDES_IMAGEM or not cfg[rede].get("imagens"):
            continue
        vistos, lista = set(), []
        for d in range(dias):
            for x in agenda.horarios_imagem_descartados(cfg, rede, inicio.date() + timedelta(days=d)):
                par = (x["horario"], x.get("video"), x.get("imagem"))
                if par not in vistos:
                    vistos.add(par)
                    lista.append(x)
        descartados_imagem[rede] = lista

    itens.sort(key=lambda x: x["quando"])
    return {
        "agora": agora.timestamp(),
        "inicio": inicio.timestamp(),
        "dias": dias,
        "pausado": pausado,
        "itens": itens,
        "descartados_imagem": descartados_imagem,
        "cobertura": cobertura(cfg, conn),
    }
