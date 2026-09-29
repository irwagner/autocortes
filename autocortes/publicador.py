"""Publicação: decide quando e o que postar em cada rede, e registra o resultado."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from . import agenda, metricas, planejador
from .config import PLATAFORMAS, ROTULOS, Config
from .db import agora
from .midia import ErroMidia, sondar
from .plataformas import ErroPublicacao, Plataforma, criar
from .plataformas import manual as tarefas
from .plataformas.instagram import FACEBOOK_MAX_SEG
from .plataformas.youtube import motivo_nao_short
from .textos import Conteudo, montar_conteudo
from .util import ATIVIDADES, log

DESBLOQUEIO_SEG = 6 * 3600  # depois de um bloqueio, tenta a rede de novo após 6 horas
# um envio por vez em cada rede, mesmo com o "postar agora" do painel rodando junto com o loop
TRAVAS_REDE = {p: threading.Lock() for p in (*PLATAFORMAS, "facebook")}
# Reel da Página do Facebook: tentativas por corte (a primeira junto com o Instagram)
MAX_TENTATIVAS_FACEBOOK = 3


class Publicador:
    def __init__(self, cfg: Config, parar: threading.Event):
        self.cfg = cfg
        self.parar = parar
        self.plataformas: dict[str, Plataforma] = {nome: criar(nome, cfg) for nome in cfg.plataformas_ativas()}
        self.bloqueadas: dict[str, tuple[str, float]] = {}
        self._avisos: dict[str, float] = {}
        self._proxima_metrica = time.time() + 120  # primeira coleta 2 min depois de ligar

    def sincronizar(self) -> None:
        """Acompanha redes ligadas/desligadas e trocas de envio (oficial, Upload-Post ou à mão) feitas no painel."""
        ativas = self.cfg.plataformas_ativas()
        for nome in list(self.plataformas):
            if nome not in ativas or self.plataformas[nome].via != self.cfg[nome].get("envio", "oficial"):
                del self.plataformas[nome]
                self.bloqueadas.pop(nome, None)
        for nome in ativas:
            if nome not in self.plataformas:
                self.plataformas[nome] = criar(nome, self.cfg)

    def _avisar(self, chave: str, mensagem: str, *args, intervalo: float = 3600) -> None:
        if time.time() - self._avisos.get(chave, 0) >= intervalo:
            self._avisos[chave] = time.time()
            log.warning(mensagem, *args)

    # ------------------------------------------------------------ agenda
    def horario_devido(self, conn: sqlite3.Connection, nome: str, momento: datetime) -> datetime | None:
        horario = agenda.horario_vigente(self.cfg, nome, momento)
        if horario is None:
            return None
        ts = horario.timestamp()
        # a tarefa à mão também ocupa o horário, o intervalo mínimo e o limite do dia
        ocupam = planejador.status_ocupam_horario(self.cfg)
        if planejador.horario_atendido(conn, nome, ts, ocupam):
            return None

        a = self.cfg["agenda"]
        tentativas = conn.execute(
            "SELECT status, atualizado_em FROM postagens WHERE plataforma=? AND horario IS NOT NULL "
            "AND abs(horario - ?) < 1 AND status IN ('falhou', 'interrompido', 'pulado') ORDER BY criado_em",
            (nome, ts),
        ).fetchall()
        if len(tentativas) >= int(a["max_tentativas"]):
            self._avisar(f"{nome}-tentativas-{ts}", "%s: desistindo do horário %s após %d tentativas",
                         nome, horario.strftime("%H:%M"), len(tentativas))
            return None
        falhas = [t for t in tentativas if t["status"] != "pulado"]
        if falhas:
            espera = float(a["espera_erro_min"]) * 60 * 2 ** (len(falhas) - 1)
            if time.time() - falhas[-1]["atualizado_em"] < espera:
                return None

        marc = ",".join("?" * len(ocupam))
        ultimo = conn.execute(
            f"SELECT MAX(criado_em) FROM postagens WHERE plataforma=? AND status IN ({marc})", (nome, *ocupam)
        ).fetchone()[0]
        if ultimo and time.time() - ultimo < float(a["intervalo_minimo_min"]) * 60:
            return None  # respeita o intervalo mínimo (ex.: logo depois de um "postar agora")

        feitos_24h = conn.execute(
            f"SELECT COUNT(*) FROM postagens WHERE plataforma=? AND status IN ({marc}) AND criado_em>=?",
            (nome, *ocupam, time.time() - 86400),
        ).fetchone()[0]
        if feitos_24h >= int(a["maximo_por_dia"]):
            self._avisar(f"{nome}-limite", "%s: limite de %d posts em 24h atingido", nome, feitos_24h)
            return None
        return horario

    def proximo_corte(self, conn: sqlite3.Connection, nome: str):
        linhas = planejador.fila(self.cfg, conn, nome, limite=1)
        return linhas[0] if linhas else None

    @staticmethod
    def tarefas_esperando(conn: sqlite3.Connection, nome: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM postagens WHERE plataforma=? AND status='aguardando'", (nome,)
        ).fetchone()[0]

    # ------------------------------------------------------------ postagem
    def postar(self, conn: sqlite3.Connection, nome: str, corte, horario: datetime | None = None,
               manual: bool = False) -> str:
        trava = TRAVAS_REDE[nome]
        if not trava.acquire(blocking=False):
            return "ocupado"
        try:
            if nome == "facebook":
                return self._facebook_avulso(conn, corte, manual)
            return self._postar(conn, nome, corte, horario, manual)
        finally:
            trava.release()

    def _arquivo_do_corte(self, conn, corte) -> Path | None:
        arquivo = Path(corte["arquivo"] or "")
        if not corte["arquivo"] or not arquivo.exists():
            conn.execute("UPDATE cortes SET status='erro', erro='arquivo editado não encontrado' WHERE id=?", (corte["id"],))
            log.error("Arquivo do corte %s sumiu: %s", corte["id"], arquivo)
            return None
        return arquivo

    def _nao_e_short(self, arquivo: Path) -> str | None:
        """Motivo para o vídeo não ser um Short do YouTube (None = é um Short)."""
        try:
            sonda = sondar(self.cfg["ferramentas"]["ffprobe"], arquivo)
        except ErroMidia as e:
            log.warning("YouTube: não consegui conferir o formato de %s (%s)", arquivo.name, e)
            return None
        return motivo_nao_short(sonda, int(self.cfg["youtube"]["max_segundos"]))

    def _postar(self, conn, nome: str, corte, horario: datetime | None, manual: bool) -> str:
        plataforma = self.plataformas.get(nome) or criar(nome, self.cfg)
        filme = conn.execute("SELECT * FROM filmes WHERE id=?", (corte["filme_id"],)).fetchone()
        arquivo = self._arquivo_do_corte(conn, corte)
        if arquivo is None:
            return "erro"
        conteudo = montar_conteudo(self.cfg, filme, corte)
        t = agora()
        horario_ts = horario.timestamp() if horario else None

        if nome == "youtube":  # o YouTube só recebe Shorts, pela API, pelo Upload-Post ou à mão
            motivo = self._nao_e_short(arquivo)
            if motivo:
                conn.execute(
                    "INSERT INTO postagens (corte_id, plataforma, status, erro, horario, manual, via, criado_em, "
                    "atualizado_em) VALUES (?, 'youtube', 'pulado', ?, ?, ?, ?, ?, ?)",
                    (corte["id"], f"Não é um Short: {motivo}", horario_ts, int(manual), plataforma.via, t, t),
                )
                log.warning("YouTube: pulando %s (não é um Short: %s)", arquivo.name, motivo)
                return "pulado"

        if plataforma.via == "manual":
            return self._criar_tarefa(conn, nome, corte, arquivo, conteudo, horario_ts, manual)

        postagem = conn.execute(
            "INSERT INTO postagens (corte_id, plataforma, status, horario, manual, via, criado_em, atualizado_em) "
            "VALUES (?, ?, 'enviando', ?, ?, ?, ?, ?)",
            (corte["id"], nome, horario_ts, int(manual), plataforma.via, t, t),
        ).lastrowid

        def registrar(status: str, **campos) -> None:
            conn.execute(
                "UPDATE postagens SET status=?, id_remoto=?, url=?, erro=?, atualizado_em=? WHERE id=?",
                (status, campos.get("id_remoto"), campos.get("url"), campos.get("erro"), agora(), postagem),
            )

        if self.cfg.simulacao:
            log.info(
                "[SIMULAÇÃO] %s receberia %s\n    título: %s\n    texto: %s",
                plataforma.rotulo, arquivo.name, conteudo.titulo, conteudo.descricao.replace("\n", " / "),
            )
            registrar("simulado")
            if nome == "instagram":
                self._postar_facebook(conn, corte, arquivo, conteudo, plataforma, horario_ts, manual)
            return "simulado"

        log.info("%s: publicando %s", plataforma.rotulo, arquivo.name)
        ATIVIDADES.definir(f"Enviando '{conteudo.titulo}' para o {plataforma.rotulo}", 0)
        try:
            resultado = plataforma.publicar(arquivo, conteudo, self.parar)
        except ErroPublicacao as e:
            if self.parar.is_set():
                registrar("interrompido", erro=str(e))
                return "interrompido"
            status = "pulado" if e.tipo == "corte" else "falhou"
            registrar(status, erro=str(e))
            if e.tipo == "bloqueio":
                self.bloqueadas[nome] = (str(e), time.time())
                log.error("%s pausado: %s", plataforma.rotulo, e)
            else:
                log.warning("%s: %s (%s)", plataforma.rotulo, e,
                            "pulando este corte" if status == "pulado" else "vai tentar de novo")
            return status
        except Exception as e:  # erro inesperado: registra e segue o loop
            log.exception("%s: erro inesperado ao publicar", plataforma.rotulo)
            registrar("falhou", erro=f"{e.__class__.__name__}: {e}"[:500])
            return "falhou"
        finally:
            ATIVIDADES.limpar()

        registrar("publicado", id_remoto=resultado.id_remoto, url=resultado.url, erro=resultado.observacao)
        log.info(
            "%s: publicado %s%s", plataforma.rotulo, resultado.url or resultado.id_remoto,
            f" ({resultado.observacao})" if resultado.observacao else "",
        )
        if nome == "instagram":
            self._postar_facebook(conn, corte, arquivo, conteudo, plataforma, horario_ts, manual)
        return "publicado"

    # ------------------------------------------------------------ postagem à mão
    def _criar_tarefa(self, conn, nome: str, corte, arquivo: Path, conteudo: Conteudo, horario_ts, manual: bool) -> str:
        """Tarefa para você postar: nada é enviado, o painel mostra o vídeo e os textos da rede."""
        if conn.execute("SELECT 1 FROM postagens WHERE corte_id=? AND plataforma=? AND status='aguardando'",
                        (corte["id"], nome)).fetchone():
            return "aguardando"  # este corte já está esperando você nesta rede
        t = agora()
        tarefa = conn.execute(
            "INSERT INTO postagens (corte_id, plataforma, status, horario, manual, via, criado_em, atualizado_em) "
            "VALUES (?, ?, 'aguardando', ?, ?, 'manual', ?, ?)",
            (corte["id"], nome, horario_ts, int(manual), t, t),
        ).lastrowid
        log.info("%s: tarefa de postagem à mão criada para %s (veja no Início do painel)", ROTULOS[nome], arquivo.name)
        try:
            copia = tarefas.copiar_para_pasta(self.cfg, nome, tarefa, arquivo, conteudo, corte["parte"])
            if copia is not None:
                log.info("%s: vídeo e textos copiados para %s", ROTULOS[nome], copia.parent)
        except OSError as e:
            log.warning("%s: não consegui copiar a tarefa para a pasta sincronizada (%s)", ROTULOS[nome], e)
        return "aguardando"

    # ------------------------------------------------------------ Página do Facebook
    def facebook_ligado(self) -> bool:
        """O Reel do Instagram vai também para a Página do Facebook (API oficial ou Upload-Post)."""
        ig = self.cfg["instagram"]
        return bool(ig["ativo"] and ig["pagina_facebook"] and ig["envio"] in ("oficial", "upload_post"))

    def _postar_facebook(self, conn, corte, arquivo: Path, conteudo: Conteudo, instagram: Plataforma,
                         horario_ts, manual: bool) -> str:
        """Companheiro do Instagram: fica registrado como a rede 'facebook' no histórico."""
        if not self.facebook_ligado() or not hasattr(instagram, "publicar_facebook"):
            return "desligado"
        with TRAVAS_REDE["facebook"]:  # espera um "postar agora" da Página que esteja no meio
            return self._enviar_facebook(conn, corte, arquivo, conteudo, instagram, horario_ts, manual)

    def _facebook_avulso(self, conn, corte, manual: bool) -> str:
        """Só a Página: nova tentativa automática ou "postar agora" do painel (já com a trava)."""
        instagram = self.plataformas.get("instagram") or criar("instagram", self.cfg)
        if not hasattr(instagram, "publicar_facebook"):
            return "desligado"
        filme = conn.execute("SELECT * FROM filmes WHERE id=?", (corte["filme_id"],)).fetchone()
        arquivo = self._arquivo_do_corte(conn, corte)
        if arquivo is None:
            return "erro"
        conteudo = montar_conteudo(self.cfg, filme, corte)
        return self._enviar_facebook(conn, corte, arquivo, conteudo, instagram, None, manual)

    def _enviar_facebook(self, conn, corte, arquivo: Path, conteudo: Conteudo, instagram: Plataforma,
                         horario_ts, manual: bool) -> str:
        t = agora()
        postagem = conn.execute(
            "INSERT INTO postagens (corte_id, plataforma, status, horario, manual, via, criado_em, atualizado_em) "
            "VALUES (?, 'facebook', 'enviando', ?, ?, ?, ?, ?)",
            (corte["id"], horario_ts, int(manual), instagram.via, t, t),
        ).lastrowid

        def registrar(status: str, erro: str | None = None, id_remoto: str | None = None, url: str | None = None) -> None:
            conn.execute(
                "UPDATE postagens SET status=?, erro=?, id_remoto=COALESCE(?, id_remoto), url=?, atualizado_em=? "
                "WHERE id=?",
                (status, erro, id_remoto, url, agora(), postagem),
            )

        if conteudo.duracao > FACEBOOK_MAX_SEG + 0.5:
            registrar("pulado", erro=f"o Reel da Página aceita até {FACEBOOK_MAX_SEG} s")
            return "pulado"
        if self.cfg.simulacao:
            log.info("[SIMULAÇÃO] Página do Facebook receberia %s", arquivo.name)
            registrar("simulado")
            return "simulado"

        def avancar(video_id: str, url: str | None = None) -> None:
            conn.execute("UPDATE postagens SET id_remoto=?, url=?, atualizado_em=? WHERE id=?",
                         (video_id, url, agora(), postagem))

        log.info("Página do Facebook: publicando %s", arquivo.name)
        ATIVIDADES.definir(f"Enviando '{conteudo.titulo}' para a Página do Facebook", 0)
        try:
            resultado = instagram.publicar_facebook(arquivo, conteudo, self.parar, avancar)
        except ErroPublicacao as e:
            linha = conn.execute("SELECT url FROM postagens WHERE id=?", (postagem,)).fetchone()
            if self.parar.is_set():
                if linha is not None and linha["url"]:  # o Facebook já tinha aceitado: não envia de novo
                    registrar("publicado", erro="envio interrompido enquanto o Facebook processava o Reel",
                              url=linha["url"])
                    return "publicado"
                registrar("interrompido", erro=str(e))
                return "interrompido"
            # 'corte' (vídeo recusado) e 'bloqueio' (falta permissão) não adiantam repetir; o painel avisa
            status = "falhou" if e.tipo == "temporario" else "pulado"
            registrar(status, erro=str(e))
            log.warning("Página do Facebook: %s (%s)", e, "vai tentar de novo" if status == "falhou" else "não vai repetir")
            return status
        except Exception as e:
            log.exception("Página do Facebook: erro inesperado ao publicar")
            registrar("falhou", erro=f"{e.__class__.__name__}: {e}"[:500])
            return "falhou"
        finally:
            ATIVIDADES.limpar()
        registrar("publicado", erro=resultado.observacao, id_remoto=resultado.id_remoto, url=resultado.url)
        log.info("Página do Facebook: publicado %s%s", resultado.url or resultado.id_remoto,
                 f" ({resultado.observacao})" if resultado.observacao else "")
        return "publicado"

    def _repetir_facebook(self, conn: sqlite3.Connection) -> None:
        """Tenta de novo os Reels que não chegaram à Página (erro temporário ou envio interrompido)."""
        if self.cfg.simulacao or not self.facebook_ligado():
            return
        instagram = self.plataformas.get("instagram")
        if instagram is None or not hasattr(instagram, "publicar_facebook") or not instagram.pronta()[0]:
            return
        linhas = conn.execute(
            "SELECT corte_id, COUNT(*) AS n, MAX(atualizado_em) AS ultima FROM postagens WHERE plataforma='facebook' "
            "GROUP BY corte_id HAVING SUM(status IN ('publicado', 'pulado', 'enviando')) = 0 "
            "AND SUM(status IN ('falhou', 'interrompido')) BETWEEN 1 AND ? ORDER BY ultima",
            (MAX_TENTATIVAS_FACEBOOK - 1,),
        ).fetchall()
        for linha in linhas:
            espera = max(600.0, float(self.cfg["agenda"]["espera_erro_min"]) * 60 * 2 ** (linha["n"] - 1))
            if time.time() - linha["ultima"] < espera:
                continue
            corte = conn.execute(
                "SELECT * FROM cortes WHERE id=? AND status IN ('pronto', 'concluido') AND arquivo IS NOT NULL",
                (linha["corte_id"],),
            ).fetchone()
            if corte is None or not Path(corte["arquivo"]).exists() or self.parar.is_set():
                continue
            log.info("Página do Facebook: tentando de novo o corte %s (tentativa %d de %d)",
                     corte["id"], linha["n"] + 1, MAX_TENTATIVAS_FACEBOOK)
            self.postar(conn, "facebook", corte)
            return  # uma por ciclo

    def _facebook_pendente(self, conn: sqlite3.Connection, corte_id: int) -> bool:
        """O Reel do corte ainda pode ir para a Página (o corte espera antes de ser concluído)."""
        estados = [r[0] for r in conn.execute(
            "SELECT status FROM postagens WHERE corte_id=? AND plataforma='facebook'", (corte_id,))]
        if not estados or any(s in ("publicado", "pulado") for s in estados):
            return False
        if "enviando" in estados:
            return True
        return sum(s in ("falhou", "interrompido") for s in estados) < MAX_TENTATIVAS_FACEBOOK

    # ------------------------------------------------------------ ciclo
    def atualizar_concluidos(self, conn: sqlite3.Connection) -> None:
        """Marca como concluídos os cortes já publicados em todas as redes ativas.

        A tarefa à mão ('aguardando') só conta quando você marca como feita ou pula.
        """
        ativas = list(self.plataformas)
        if self.cfg.simulacao or not ativas:
            return  # em simulação nada é concluído: os cortes continuam valendo para o modo real
        facebook = self.facebook_ligado()
        marc = ",".join("?" * len(ativas))
        for corte in conn.execute("SELECT id, arquivo FROM cortes WHERE status='pronto'").fetchall():
            n = conn.execute(
                f"SELECT COUNT(DISTINCT plataforma) FROM postagens WHERE corte_id=? AND plataforma IN ({marc}) "
                "AND status IN ('publicado', 'pulado')",
                (corte["id"], *ativas),
            ).fetchone()[0]
            if n < len(ativas):
                continue
            if facebook and self._facebook_pendente(conn, corte["id"]):
                continue  # o Reel da Página ainda vai ser tentado de novo com este arquivo
            if conn.execute("SELECT 1 FROM postagens WHERE corte_id=? AND status='aguardando' LIMIT 1",
                            (corte["id"],)).fetchone():
                continue  # tarefa à mão de uma rede que foi desligada: o vídeo fica até você resolver
            conn.execute("UPDATE cortes SET status='concluido' WHERE id=?", (corte["id"],))
            if self.cfg["geral"]["apagar_apos_postar"] and corte["arquivo"]:
                # a miniatura (.jpg) fica para o histórico do painel
                for extensao in (".mp4", ".ass"):
                    Path(corte["arquivo"]).with_suffix(extensao).unlink(missing_ok=True)

    def ciclo(self, conn: sqlite3.Connection) -> None:
        if self.cfg["agenda"]["pausado"]:
            self.atualizar_concluidos(conn)
            self._coletar_metricas(conn)
            return
        momento = datetime.now()
        for nome, plataforma in self.plataformas.items():
            if self.parar.is_set():
                return
            if nome in self.bloqueadas:
                motivo, desde = self.bloqueadas[nome]
                if time.time() - desde < DESBLOQUEIO_SEG:
                    continue
                log.info("%s: tentando de novo depois do bloqueio (%s)", plataforma.rotulo, motivo)
                del self.bloqueadas[nome]
            if not self.cfg.simulacao:
                pronta, motivo = plataforma.pronta()
                if not pronta:
                    self.bloqueadas[nome] = (motivo, time.time())
                    log.error("%s não está configurado: %s", plataforma.rotulo, motivo)
                    continue
            if plataforma.via == "manual":
                esperando = self.tarefas_esperando(conn, nome)
                if esperando >= tarefas.MAX_TAREFAS_POR_REDE:
                    self._avisar(f"{nome}-tarefas", "%s: %d tarefas de postagem à mão esperando você; os próximos "
                                 "horários ficam sem tarefa nova até você postar ou pular", ROTULOS[nome], esperando)
                    continue
            for _ in range(3):  # até 3 cortes recusados no mesmo horário
                horario = self.horario_devido(conn, nome, momento)
                if horario is None:
                    break
                corte = self.proximo_corte(conn, nome)
                if corte is None:
                    self._avisar(f"{nome}-sem-corte", "%s: chegou o horário %s, mas não há corte pronto ainda",
                                 plataforma.rotulo, horario.strftime("%H:%M"), intervalo=1800)
                    break
                if self.postar(conn, nome, corte, horario=horario) != "pulado":
                    break
        if not self.parar.is_set():
            self._repetir_facebook(conn)
        self.atualizar_concluidos(conn)
        self._coletar_metricas(conn)

    def _coletar_metricas(self, conn: sqlite3.Connection) -> None:
        if self.cfg.simulacao or not self.cfg["metricas"]["ativo"] or time.time() < self._proxima_metrica:
            return
        self._proxima_metrica = time.time() + 1800  # confere a cada 30 min quais posts precisam de dados novos
        try:
            metricas.coletar(self.cfg, conn, self.plataformas)
        except Exception:
            log.exception("Erro ao coletar métricas")
