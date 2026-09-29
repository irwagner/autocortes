"""Produção: encontra filmes, analisa e mantém um estoque de cortes editados."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import analise, edicao, ferramentas, ia, modelos_visuais, selecao
from .config import Config
from .db import agora, transacao
from .midia import Interrompido
from .planejador import fila, sem_cortes_possiveis, status_ocupam_horario
from .textos import episodio_de, ler_metadados, tem_arquivo_meta, texto_topo, titulo_do_arquivo
from .util import ATIVIDADES, log, slug

EXTENSOES = {
    ".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".webm", ".ts", ".m2ts", ".mts", ".flv", ".mpg", ".mpeg",
}


def varrer_biblioteca(cfg: Config, conn: sqlite3.Connection) -> int:
    pasta = cfg.pasta_filmes
    pasta.mkdir(parents=True, exist_ok=True)
    encontrados: dict[str, Path] = {}
    for p in pasta.rglob("*"):
        if p.suffix.lower() not in EXTENSOES or not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size < 1_000_000 or time.time() - st.st_mtime < 60:
            continue  # arquivo pequeno demais ou ainda sendo copiado
        encontrados[str(p.resolve())] = p

    existentes = {r["caminho"]: r for r in conn.execute("SELECT id, caminho, status, titulo FROM filmes")}
    novos = 0
    t = agora()
    for caminho, p in encontrados.items():
        meta = ler_metadados(p)
        ignorar = bool(meta.get("ignorar"))
        linha = existentes.get(caminho)
        if linha is not None and not meta.get("titulo") and not (not meta and tem_arquivo_meta(p)):
            # episódio que entrou com o nome antigo ("The Great S01E01", ou "Show" sem o episódio):
            # passa a "The Great T1:E1". Título escrito pelo usuário fica no .json e não muda.
            antigo = str(linha["titulo"])
            novo_titulo, _ = titulo_do_arquivo(p.name)
            serie, temporada, _ = episodio_de(novo_titulo)
            if temporada is not None and novo_titulo != antigo and (
                    re.search(r"\bS\d{1,2}E\d{1,3}$", antigo, re.I) or serie == antigo):
                conn.execute("UPDATE filmes SET titulo=? WHERE id=? AND titulo=?", (novo_titulo, linha["id"], antigo))
                log.info("Nome do episódio atualizado: %s -> %s", antigo, novo_titulo)
        if linha is None:
            titulo, ano = titulo_do_arquivo(p.name)
            titulo = str(meta.get("titulo") or titulo)
            ano = meta.get("ano") or ano
            conn.execute(
                "INSERT INTO filmes (caminho, titulo, ano, tamanho, status, criado_em, atualizado_em) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (caminho, titulo, ano, p.stat().st_size, "ignorado" if ignorar else "novo", t, t),
            )
            novos += 1
            log.info("Filme novo na pasta: %s%s", titulo, " (ignorado pelos metadados)" if ignorar else "")
            continue
        status = linha["status"]
        if ignorar and status != "ignorado":
            conn.execute("UPDATE filmes SET status='ignorado', atualizado_em=? WHERE id=?", (t, linha["id"]))
        elif (status == "ausente") or (status == "ignorado" and not ignorar):
            tem_cortes = conn.execute("SELECT 1 FROM cortes WHERE filme_id=? LIMIT 1", (linha["id"],)).fetchone()
            conn.execute(
                "UPDATE filmes SET status=?, atualizado_em=? WHERE id=?",
                ("analisado" if tem_cortes else "novo", t, linha["id"]),
            )

    for caminho, linha in existentes.items():
        if caminho not in encontrados and linha["status"] != "ausente" and not Path(caminho).exists():
            conn.execute("UPDATE filmes SET status='ausente', atualizado_em=? WHERE id=?", (t, linha["id"]))
            log.warning("Filme saiu da pasta: %s", caminho)
    return novos


def escrever_textos_ia(cfg: Config, conn: sqlite3.Connection, corte_id: int, frases=None,
                       sem_postagens: bool = False) -> dict:
    """Pede à IA título, descrição e hashtags do corte e guarda no banco (lança ia.ErroIA).

    sem_postagens: só grava se o corte ainda não saiu em nenhuma rede (a IA leva alguns segundos, e o
    publicador pode postar nesse meio tempo; assim todas as redes recebem o mesmo texto).
    """
    corte = conn.execute("SELECT * FROM cortes WHERE id=?", (corte_id,)).fetchone()
    if corte is None:
        raise ia.ErroIA("corte não encontrado", do_corte=True)
    filme = conn.execute("SELECT * FROM filmes WHERE id=?", (corte["filme_id"],)).fetchone()
    if frases is None:
        _, frases = analise.carregar_para_render(cfg, filme["id"])
    textos = ia.gerar_textos(cfg, filme, corte, frases)
    sql, params = "UPDATE cortes SET ia_textos=? WHERE id=?", [json.dumps(textos, ensure_ascii=False), corte_id]
    if sem_postagens:
        feitos = status_ocupam_horario(cfg) + ("enviando",)
        sql += (" AND NOT EXISTS (SELECT 1 FROM postagens p WHERE p.corte_id = cortes.id "
                f"AND p.status IN ({','.join('?' * len(feitos))}))")
        params += feitos
    if conn.execute(sql, params).rowcount == 0 and sem_postagens:
        log.info("O corte %s já saiu numa rede: fica com o texto que usou lá", corte_id)
    return textos


MAX_TENTATIVAS_ANALISE = 5
MAX_TENTATIVAS_RENDER = 2
# uma edição por vez (o loop e o botão "editar agora" do painel não disputam a numeração das partes)
TRAVA_EDICAO = threading.Lock()
TRAVA_ANALISE = threading.Lock()


class Produtor:
    def __init__(self, cfg: Config, parar: threading.Event):
        self.cfg = cfg
        self.parar = parar
        self._ultima_varredura = 0.0
        self._aviso_sem_conteudo = 0.0
        self._ia_pausa_ate = 0.0  # depois de uma falha da IA, espera antes de pedir de novo
        self._ia_falhou: dict[int, float] = {}

    def pedir_varredura(self) -> None:
        self._ultima_varredura = 0.0

    # ------------------------------------------------------------ estoque
    def prontos_pendentes(self, conn: sqlite3.Connection) -> int:
        """Estoque de cortes editados à frente das postagens (em revisão + fila).

        Conta a fila da rede que está com menos cortes, ou seja, a que posta mais vezes.
        Assim ela nunca fica sem vídeo, e uma rede mais lenta (ou sem login) não trava
        a produção das outras.
        """
        em_revisao = conn.execute("SELECT COUNT(*) FROM cortes WHERE status='revisao'").fetchone()[0]
        # uma rede em que nenhum corte cabe (YouTube com limite abaixo da duração mínima) faria o
        # estoque parecer sempre vazio e o motor editaria sem parar
        ativas = [r for r in self.cfg.plataformas_ativas() if not sem_cortes_possiveis(self.cfg, r)]
        if not ativas:
            prontos = conn.execute("SELECT COUNT(*) FROM cortes WHERE status='pronto'").fetchone()[0]
            return prontos + em_revisao
        return min(len(fila(self.cfg, conn, rede)) for rede in ativas) + em_revisao

    def proximo_candidato(self, conn: sqlite3.Connection):
        # a mesma regra é simulada em planejador.previsao_edicao para o calendário
        ordem = "c.pontuacao DESC" if self.cfg["cortes"]["ordem_cortes"] == "melhores" else "c.inicio ASC"
        if self.cfg["cortes"]["ordem_filmes"] == "intercalar":
            prioridade = (
                "(SELECT COUNT(*) FROM cortes x WHERE x.filme_id = c.filme_id "
                "AND x.status IN ('revisao', 'pronto', 'concluido')) ASC, f.id ASC"
            )
        else:
            prioridade = "f.id ASC"
        return conn.execute(
            "SELECT c.* FROM cortes c JOIN filmes f ON f.id = c.filme_id "
            f"WHERE c.status = 'candidato' AND f.status = 'analisado' "
            f"ORDER BY c.prioridade DESC, {prioridade}, {ordem} LIMIT 1"
        ).fetchone()

    # ------------------------------------------------------------ ciclo
    def ciclo(self, conn: sqlite3.Connection) -> bool:
        """Faz uma unidade de trabalho. Retorna False quando não havia nada a fazer."""
        if time.time() - self._ultima_varredura > float(self.cfg["geral"]["varrer_a_cada_min"]) * 60:
            ATIVIDADES.definir("Procurando filmes novos na pasta")
            varrer_biblioteca(self.cfg, conn)
            self._ultima_varredura = time.time()

        buffer = int(self.cfg["geral"]["buffer_cortes"])
        estoque = self.prontos_pendentes(conn)
        falta_estoque = estoque < buffer
        if falta_estoque:
            corte = self.proximo_candidato(conn)
            if corte is not None:
                self.renderizar(conn, corte)
                return True

        # filmes novos (com espera crescente depois de falhas de download)
        filme = conn.execute(
            "SELECT * FROM filmes WHERE status='novo' AND atualizado_em + tentativas * 600 <= ? ORDER BY id LIMIT 1",
            (agora(),),
        ).fetchone()
        if filme is not None:
            restantes = conn.execute("SELECT COUNT(*) FROM cortes WHERE status='candidato'").fetchone()[0]
            if self.cfg["geral"]["analisar_antecipado"] or restantes < buffer * 2 or falta_estoque:
                self.analisar(conn, filme)
                return True

        if falta_estoque:
            corte = self.minerar_mais(conn)  # sem filmes novos: aproveita trechos restantes dos antigos
            if corte is not None:
                self.renderizar(conn, corte)
                return True

        if self._completar_textos_ia(conn):
            return True

        if falta_estoque:
            ATIVIDADES.definir("Sem trechos novos: adicione filmes na pasta")
            if time.time() - self._aviso_sem_conteudo > 6 * 3600:
                self._aviso_sem_conteudo = time.time()
                log.warning(
                    "Todos os filmes já foram aproveitados. Coloque filmes novos em %s para o loop continuar postando.",
                    self.cfg.pasta_filmes,
                )
        else:
            ATIVIDADES.definir(f"Estoque completo: {estoque} corte(s) editado(s) aguardando postagem")
        return False

    def _falha_ia(self, corte_id: int, erro: ia.ErroIA) -> None:
        self._ia_falhou[corte_id] = time.time()
        if erro.do_corte:  # resposta ruim só para este corte: segue com os outros
            log.warning("IA sem textos para o corte %s (usando os modelos): %s", corte_id, erro)
            return
        self._ia_pausa_ate = time.time() + 600  # IA fechada ou mal configurada: não insiste a cada corte
        log.warning("IA sem textos para o corte %s (usando os modelos; tento de novo em 10 min): %s", corte_id, erro)

    def _completar_textos_ia(self, conn: sqlite3.Connection) -> bool:
        """Com a IA ligada, escreve os textos dos cortes que já estavam na fila (um por ciclo, sem pressa)."""
        if not ia.disponivel(self.cfg) or time.time() < self._ia_pausa_ate:
            return False
        feitos = status_ocupam_horario(self.cfg) + ("enviando",)  # a tarefa à mão já mostra o texto
        linhas = conn.execute(
            "SELECT c.id, c.parte, f.titulo AS filme_titulo FROM cortes c JOIN filmes f ON f.id = c.filme_id "
            "WHERE c.status IN ('revisao', 'pronto') AND c.ia_textos IS NULL "
            "AND COALESCE(c.titulo_custom, '') = '' AND COALESCE(c.descricao_custom, '') = '' "
            # um corte que já saiu numa rede fica com o mesmo texto nas outras
            f"AND NOT EXISTS (SELECT 1 FROM postagens p WHERE p.corte_id = c.id AND p.status IN ({','.join('?' * len(feitos))})) "
            "ORDER BY c.prioridade DESC, c.fila_em, c.id LIMIT 20",
            feitos,
        ).fetchall()
        corte = next((c for c in linhas if time.time() - self._ia_falhou.get(c["id"], 0) > 6 * 3600), None)
        if corte is None:
            return False
        ATIVIDADES.definir(f"IA escrevendo os textos de '{corte['filme_titulo']}' - parte {corte['parte']}")
        try:
            escrever_textos_ia(self.cfg, conn, corte["id"], sem_postagens=True)
        except ia.ErroIA as e:
            self._falha_ia(corte["id"], e)
        return True

    def minerar_mais(self, conn: sqlite3.Connection):
        """Acabaram os candidatos: procura mais trechos nos filmes já analisados (usa o cache da análise)."""
        filme = conn.execute(
            "SELECT * FROM filmes WHERE status='analisado' AND esgotado=0 "
            "ORDER BY (SELECT COUNT(*) FROM cortes c WHERE c.filme_id = filmes.id) ASC, id LIMIT 1"
        ).fetchone()
        if filme is None:
            return None
        log.info("Procurando mais trechos em '%s'", filme["titulo"])
        self.analisar(conn, filme)
        return self.proximo_candidato(conn)

    # ------------------------------------------------------------ análise
    def analisar(self, conn: sqlite3.Connection, filme) -> int:
        with TRAVA_ANALISE:
            return self._analisar(conn, filme)

    def _analisar(self, conn: sqlite3.Connection, filme) -> int:
        conn.execute("UPDATE filmes SET status='analisando', atualizado_em=? WHERE id=?", (agora(), filme["id"]))
        ATIVIDADES.definir(f"Analisando '{filme['titulo']}'")
        inicio = time.time()
        try:
            a = analise.analisar(self.cfg, filme, self.parar)
            usados = [
                (r["inicio"], r["fim"])
                for r in conn.execute(
                    # descartados também contam: o trecho que você recusou não volta como candidato
                    "SELECT inicio, fim FROM cortes WHERE filme_id=? AND status != 'candidato'",
                    (filme["id"],),
                )
            ]
            candidatos = selecao.gerar_candidatos(
                self.cfg, a.info["duracao"], a.cenas, a.volume, a.falas, a.frases, ocupados=usados
            )
            t = agora()
            with transacao(conn):
                conn.execute("DELETE FROM cortes WHERE filme_id=? AND status='candidato'", (filme["id"],))
                conn.executemany(
                    "INSERT INTO cortes (filme_id, inicio, fim, pontuacao, detalhes, frase, status, criado_em) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'candidato', ?)",
                    [
                        (filme["id"], c.inicio, c.fim, c.pontuacao, json.dumps(c.detalhes), c.frase, t)
                        for c in candidatos
                    ],
                )
                conn.execute(
                    "UPDATE filmes SET status='analisado', duracao=?, erro=NULL, tentativas=0, esgotado=?, "
                    "atualizado_em=? WHERE id=?",
                    (a.info["duracao"], 0 if candidatos else 1, t, filme["id"]),
                )
            if candidatos:
                log.info(
                    "'%s' analisado em %.0f s: %d cortes candidatos", filme["titulo"], time.time() - inicio, len(candidatos)
                )
            else:
                log.info("'%s' não tem mais trechos aproveitáveis", filme["titulo"])
            return len(candidatos)
        except Interrompido:
            conn.execute("UPDATE filmes SET status='novo', atualizado_em=? WHERE id=?", (agora(), filme["id"]))
            raise
        except ferramentas.ErroDownload as e:
            tentativas = int(filme["tentativas"] or 0) + 1
            status = "novo" if tentativas < MAX_TENTATIVAS_ANALISE else "erro"
            log.warning("'%s': %s (tentativa %d de %d)", filme["titulo"], e, tentativas, MAX_TENTATIVAS_ANALISE)
            conn.execute(
                "UPDATE filmes SET status=?, erro=?, tentativas=?, atualizado_em=? WHERE id=?",
                (status, str(e)[:500], tentativas, agora(), filme["id"]),
            )
            return 0
        except Exception as e:
            log.error("Falha ao analisar '%s': %s", filme["titulo"], e, exc_info=not isinstance(e, analise.ErroMidia))
            conn.execute(
                "UPDATE filmes SET status='erro', erro=?, atualizado_em=? WHERE id=?",
                (str(e)[:500], agora(), filme["id"]),
            )
            return 0

    # ------------------------------------------------------------ edição
    def renderizar(self, conn: sqlite3.Connection, corte) -> Path | None:
        with TRAVA_EDICAO:
            atual = conn.execute("SELECT * FROM cortes WHERE id=?", (corte["id"],)).fetchone()
            if atual is None or atual["status"] != "candidato":
                return None  # outra thread já editou ou o corte foi descartado enquanto esperava
            return self._renderizar(conn, atual)

    def _renderizar(self, conn: sqlite3.Connection, corte) -> Path | None:
        filme = conn.execute("SELECT * FROM filmes WHERE id=?", (corte["filme_id"],)).fetchone()
        caminho = Path(filme["caminho"])
        if not caminho.exists():
            conn.execute("UPDATE filmes SET status='ausente', atualizado_em=? WHERE id=?", (agora(), filme["id"]))
            log.warning("Filme não encontrado para editar: %s", caminho)
            return None
        conn.execute("UPDATE cortes SET status='renderizando' WHERE id=?", (corte["id"],))
        try:
            info, frases = analise.carregar_para_render(self.cfg, filme["id"])
            # "reeditar" mantém o número da parte
            parte = corte["parte"] or (
                conn.execute("SELECT MAX(parte) FROM cortes WHERE filme_id=?", (filme["id"],)).fetchone()[0] or 0
            ) + 1
            destino = (
                self.cfg.pasta_cortes / str(filme["id"]) / f"{slug(filme['titulo'])}-parte{parte:02d}-c{corte['id']}.mp4"
            )
            log.info(
                "Editando '%s' parte %d (%.0fs a %.0fs, nota %.2f)",
                filme["titulo"], parte, corte["inicio"], corte["fim"], corte["pontuacao"],
            )
            # retrato do config: se o visual mudar no painel durante a edição, este corte não sai misturado
            cfg = self.cfg.copia()
            conn.execute("UPDATE cortes SET modelo=? WHERE id=?", (modelos_visuais.nome_ativo(cfg), corte["id"]))
            edicao.renderizar(
                cfg, caminho, info, frases, corte["inicio"], corte["fim"],
                texto_topo(cfg, filme, parte), destino, self.parar,
                rotulo=f"Editando '{filme['titulo']}' - parte {parte}",
            )
            if ia.disponivel(self.cfg) and not corte["ia_textos"] and time.time() >= self._ia_pausa_ate:
                # antes de liberar o corte, para ele não sair com o texto padrão enquanto a IA escreve
                try:
                    ATIVIDADES.definir(f"IA escrevendo os textos de '{filme['titulo']}' - parte {parte}")
                    escrever_textos_ia(self.cfg, conn, corte["id"], frases=frases, sem_postagens=True)
                except ia.ErroIA as e:
                    self._falha_ia(corte["id"], e)
            status = "revisao" if self.cfg["geral"]["exigir_aprovacao"] else "pronto"
            momento = agora()
            conn.execute(  # fila_em só é marcado na primeira vez: editar de novo não muda o lugar na fila
                "UPDATE cortes SET status=?, parte=?, arquivo=?, renderizado_em=?, "
                "fila_em=COALESCE(fila_em, ?), erro=NULL WHERE id=?",
                (status, parte, str(destino), momento, momento, corte["id"]),
            )
            log.info("Corte %s: %s", "aguardando aprovação" if status == "revisao" else "pronto", destino)
            return destino
        except Interrompido:
            conn.execute("UPDATE cortes SET status='candidato' WHERE id=?", (corte["id"],))
            raise
        except Exception as e:
            log.error("Falha ao editar o corte %s: %s", corte["id"], e, exc_info=not isinstance(e, analise.ErroMidia))
            tentativas = int(corte["tentativas"] or 0) + 1
            conn.execute(
                "UPDATE cortes SET status=?, erro=?, tentativas=? WHERE id=?",
                ("candidato" if tentativas < MAX_TENTATIVAS_RENDER else "erro", str(e)[:500], tentativas, corte["id"]),
            )
            return None
