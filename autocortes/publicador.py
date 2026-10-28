"""Publicação: decide quando e o que postar em cada rede, e registra o resultado.

Como cada erro é tratado (o tipo vem de ErroPublicacao):

- a rede já tinha recebido o vídeo (Entrega aceita): "conferir", nunca repetido sozinho;
- internet caída ou cota/limite da rede antes de ela receber: a linha some e a rede espera;
- "bloqueio" (login, configuração, a rede bloqueou): a rede pausa; o bloqueio fica no banco e
  vale depois de reiniciar. O que não passa sozinho (verificação, conta restrita) só sai quando
  você testa a conta de novo;
- "corte" (este vídeo não serve): pula o corte; duas recusas seguidas pausam a rede;
- "ensaio": o horário fica gasto, mas o corte continua na fila;
- o resto é falha comum: tenta de novo, e depois de algumas o corte é pulado naquela rede.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from . import agenda, db, metricas, planejador
from .config import PLATAFORMAS, ROTULOS, Config
from .db import agora
from .midia import ErroMidia, sondar
from .plataformas import Entrega, ErroPublicacao, Plataforma, criar
from .plataformas import manual as tarefas
from .plataformas.instagram import FACEBOOK_MAX_SEG
from .plataformas.youtube import motivo_nao_short
from .textos import Conteudo, montar_conteudo
from .util import ATIVIDADES, log

DESBLOQUEIO_SEG = 6 * 3600     # bloqueio que passa sozinho (login, configuração): tenta de novo depois de 6 h
PAUSA_REDE_SEG = 600           # a internet caiu antes de a rede responder
PAUSA_COTA_SEG = 3600          # limite de uso sem prazo informado pela rede
PAUSA_RECUSAS_SEG = 6 * 3600   # vídeos recusados em seguida: algo mudou na rede ou na configuração
RECUSAS_PARA_PAUSAR = 2
FALHAS_POR_CORTE = planejador.FALHAS_POR_CORTE
# um envio por vez em cada rede, mesmo com o "postar agora" do painel rodando junto com o loop
TRAVAS_REDE = {p: threading.Lock() for p in (*PLATAFORMAS, "facebook")}
# Reel da Página do Facebook: tentativas por corte (a primeira junto com o Instagram)
MAX_TENTATIVAS_FACEBOOK = 3


# ---------------------------------------------------------------- pausas e bloqueios (no banco)

def _chave_bloqueio(rede: str) -> str:
    return f"bloqueio:{rede}"


def _bloqueios_brutos(conn: sqlite3.Connection) -> list[tuple[str, str, dict]]:
    saida = []
    for linha in conn.execute("SELECT chave, valor FROM estado WHERE chave LIKE 'bloqueio:%'").fetchall():
        try:
            dados = json.loads(linha["valor"])
        except ValueError:
            continue
        if isinstance(dados, dict):
            saida.append((str(linha["chave"]), str(linha["chave"]).split(":", 1)[1], dados))
    return saida


def ler_bloqueios(conn: sqlite3.Connection) -> dict[str, dict]:
    """Redes pausadas ou bloqueadas agora: {rede: {motivo, tipo, desde, ate}} (ate None = até você testar).

    tipo: "bloqueio" (login, configuração, a rede bloqueou), "cota" (limite de uso da rede),
    "recusas" (vídeos recusados em seguida) ou "rede" (a internet caiu).
    """
    agora_ = time.time()
    return {rede: dados for _, rede, dados in _bloqueios_brutos(conn)
            if dados.get("ate") is None or float(dados["ate"]) > agora_}


def bloquear(conn: sqlite3.Connection, rede: str, motivo: str, tipo: str, segundos: float | None) -> dict:
    agora_ = time.time()
    dados = {"motivo": str(motivo)[:600], "tipo": tipo, "desde": agora_,
             "ate": None if segundos is None else agora_ + float(segundos)}
    db.gravar_estado(conn, _chave_bloqueio(rede), dados)
    return dados


def desbloquear(conn: sqlite3.Connection, rede: str, persistente: bool = False) -> bool:
    """Tira a pausa da rede. O bloqueio que não passa sozinho só sai com persistente=True
    (você testou a conta ou conectou de novo)."""
    atual = db.ler_estado(conn, _chave_bloqueio(rede))
    if not isinstance(atual, dict):
        return False
    if atual.get("ate") is None and not persistente:
        return False
    db.apagar_estado(conn, _chave_bloqueio(rede))
    return True


def _quando(segundos: float) -> str:
    minutos = int(round(segundos / 60))
    if minutos < 90:
        return f"{max(1, minutos)} min"
    return f"{minutos / 60:.0f} h"


class Publicador:
    def __init__(self, cfg: Config, parar: threading.Event):
        self.cfg = cfg
        self.parar = parar
        self.plataformas: dict[str, Plataforma] = {nome: criar(nome, cfg) for nome in cfg.plataformas_ativas()}
        # retrato das redes paradas (as do banco e as sem configuração), para o resumo e o painel
        self.bloqueadas: dict[str, dict] = {}
        self._nao_prontas: dict[str, dict] = {}
        self._recusas: dict[str, int] = {}
        self._avisos: dict[str, float] = {}
        self._proxima_metrica = time.time() + 120  # primeira coleta 2 min depois de ligar

    def sincronizar(self) -> None:
        """Acompanha redes ligadas/desligadas e trocas de envio (oficial, Upload-Post ou à mão) feitas no painel."""
        ativas = self.cfg.plataformas_ativas()
        for nome in list(self.plataformas):
            if nome not in ativas or self.plataformas[nome].via != self.cfg[nome].get("envio", "oficial"):
                del self.plataformas[nome]
                self._nao_prontas.pop(nome, None)
                self._recusas.pop(nome, None)
        for nome in ativas:
            if nome not in self.plataformas:
                self.plataformas[nome] = criar(nome, self.cfg)

    def _avisar(self, chave: str, mensagem: str, *args, intervalo: float = 3600) -> None:
        if time.time() - self._avisos.get(chave, 0) >= intervalo:
            self._avisos[chave] = time.time()
            log.warning(mensagem, *args)

    # ------------------------------------------------------------ pausas
    def _bloqueios_vigentes(self, conn: sqlite3.Connection) -> dict[str, dict]:
        """As pausas que ainda valem; as vencidas saem do banco (e a rede volta a ser tentada)."""
        agora_ = time.time()
        ativos: dict[str, dict] = {}
        for chave, rede, dados in _bloqueios_brutos(conn):
            ate = dados.get("ate")
            if ate is not None and float(ate) <= agora_:
                db.apagar_estado(conn, chave)
                if rede in self.plataformas:
                    log.info("%s: tentando de novo depois da pausa (%s)", ROTULOS.get(rede, rede), dados.get("motivo"))
                continue
            ativos[rede] = dados
        self.bloqueadas = {**self._nao_prontas, **ativos}
        return ativos

    def _pausar(self, conn: sqlite3.Connection, nome: str, motivo: str, tipo: str, segundos: float | None) -> None:
        self.bloqueadas[nome] = bloquear(conn, nome, motivo, tipo, segundos)

    def liberar(self, conn: sqlite3.Connection, rede: str, persistente: bool = False) -> None:
        """A rede volta a ser tentada (login novo, configuração mudou, conta testada)."""
        self._nao_prontas.pop(rede, None)
        self._recusas.pop(rede, None)
        desbloquear(conn, rede, persistente)
        self._bloqueios_vigentes(conn)

    # ------------------------------------------------------------ agenda
    def horario_devido(self, conn: sqlite3.Connection, nome: str, momento: datetime,
                       midia: str = "video") -> tuple[datetime, str] | None:
        vigente = agenda.horario_vigente_com_chave(self.cfg, nome, momento, midia)
        if vigente is None:
            return None
        horario, chave = vigente
        ts = horario.timestamp()
        # a tarefa à mão também ocupa o horário, o intervalo mínimo e o limite do dia
        ocupam = planejador.status_ocupam_horario(self.cfg)
        if planejador.horario_atendido(conn, nome, ts, ocupam, chave=chave, midia=midia):
            return None

        a = self.cfg["agenda"]
        tentativas = conn.execute(
            "SELECT status, atualizado_em FROM postagens WHERE plataforma=? AND midia=? "
            "AND (slot=? OR (slot IS NULL AND horario IS NOT NULL AND abs(horario - ?) < 1)) "
            "AND status IN ('falhou', 'interrompido', 'pulado') ORDER BY criado_em",
            (nome, midia, chave, ts),
        ).fetchall()
        if len(tentativas) >= int(a["max_tentativas"]):
            self._avisar(f"{nome}-tentativas-{chave}", "%s: desistindo do horário %s após %d tentativas",
                         nome, horario.strftime("%H:%M"), len(tentativas))
            return None
        falhas = [t for t in tentativas if t["status"] != "pulado"]
        if falhas:
            espera = float(a["espera_erro_min"]) * 60 * 2 ** (len(falhas) - 1)
            if time.time() - falhas[-1]["atualizado_em"] < espera:
                return None

        # o intervalo mínimo e o limite do dia contam vídeos e imagens juntos: são posts da mesma conta
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
        return horario, chave

    def proximo_corte(self, conn: sqlite3.Connection, nome: str):
        linhas = planejador.fila(self.cfg, conn, nome, limite=1)
        return linhas[0] if linhas else None

    @staticmethod
    def tarefas_esperando(conn: sqlite3.Connection, nome: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM postagens WHERE plataforma=? AND midia='video' AND status='aguardando'", (nome,)
        ).fetchone()[0]

    # ------------------------------------------------------------ postagem
    def postar(self, conn: sqlite3.Connection, nome: str, corte, horario: datetime | None = None,
               manual: bool = False, chave: str | None = None) -> str:
        # a porta única do envio de vídeo (motor, painel e CLI): rede só de imagens não recebe vídeo
        base = "instagram" if nome == "facebook" else nome
        if not self.cfg[base].get("videos", True):
            return "so_imagens"
        trava = TRAVAS_REDE[nome]
        if not trava.acquire(blocking=False):
            return "ocupado"
        try:
            if nome == "facebook":
                return self._facebook_avulso(conn, corte, manual)
            return self._postar(conn, nome, corte, horario, manual, chave)
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

    def _postar(self, conn, nome: str, corte, horario: datetime | None, manual: bool, chave: str | None) -> str:
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
                    "INSERT INTO postagens (corte_id, plataforma, status, erro, horario, slot, manual, via, criado_em, "
                    "atualizado_em) VALUES (?, 'youtube', 'pulado', ?, ?, ?, ?, ?, ?, ?)",
                    (corte["id"], f"Não é um Short: {motivo}", horario_ts, chave, int(manual), plataforma.via, t, t),
                )
                log.warning("YouTube: pulando %s (não é um Short: %s)", arquivo.name, motivo)
                return "pulado"

        if plataforma.via == "manual":
            return self._criar_tarefa(conn, nome, corte, arquivo, conteudo, horario_ts, chave, manual)

        postagem = conn.execute(
            "INSERT INTO postagens (corte_id, plataforma, status, horario, slot, manual, via, criado_em, atualizado_em) "
            "VALUES (?, ?, 'enviando', ?, ?, ?, ?, ?, ?)",
            (corte["id"], nome, horario_ts, chave, int(manual), plataforma.via, t, t),
        ).lastrowid

        def registrar(status: str, **campos) -> None:
            conn.execute(
                "UPDATE postagens SET status=?, id_remoto=COALESCE(?, id_remoto), url=COALESCE(?, url), erro=?, "
                "atualizado_em=? WHERE id=?",
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

        def anotar(id_remoto, url) -> None:
            # gravado na hora em que a rede aceita: se o AutoCortes fechar depois, o post não sai de novo
            conn.execute("UPDATE postagens SET id_remoto=?, url=?, atualizado_em=? WHERE id=?",
                         (id_remoto, url, agora(), postagem))

        entrega = Entrega(anotar)
        log.info("%s: publicando %s", plataforma.rotulo, arquivo.name)
        ATIVIDADES.definir(f"Enviando '{conteudo.titulo}' para o {plataforma.rotulo}", 0)
        try:
            resultado = plataforma.publicar(arquivo, conteudo, self.parar, entrega)
        except ErroPublicacao as e:
            return self._falhou(conn, nome, plataforma, corte, postagem, registrar, entrega, e)
        except Exception as e:  # erro inesperado: registra e segue o loop
            log.exception("%s: erro inesperado ao publicar", plataforma.rotulo)
            erro = f"{e.__class__.__name__}: {e}"[:500]
            if entrega.aceita:
                registrar("conferir", id_remoto=entrega.id_remoto, url=entrega.url,
                          erro=f"A rede recebeu o vídeo, mas houve um erro inesperado depois: {erro}")
                return "conferir"
            registrar("falhou", erro=erro)
            return self._limitar_falhas(conn, nome, corte, postagem, erro)
        finally:
            ATIVIDADES.limpar()

        self._recusas.pop(nome, None)
        if not resultado.confirmado:
            registrar("conferir", id_remoto=resultado.id_remoto, url=resultado.url,
                      erro=resultado.observacao or "a rede recebeu o vídeo, mas não vi a confirmação da publicação")
            log.warning("%s: %s. Ficou para você conferir no painel", plataforma.rotulo,
                        resultado.observacao or "não vi a confirmação da publicação")
            if nome == "instagram":
                self._postar_facebook(conn, corte, arquivo, conteudo, plataforma, horario_ts, manual)
            return "conferir"
        registrar("publicado", id_remoto=resultado.id_remoto, url=resultado.url, erro=resultado.observacao)
        log.info(
            "%s: publicado %s%s", plataforma.rotulo, resultado.url or resultado.id_remoto,
            f" ({resultado.observacao})" if resultado.observacao else "",
        )
        if nome == "instagram":
            self._postar_facebook(conn, corte, arquivo, conteudo, plataforma, horario_ts, manual)
        return "publicado"

    def _falhou(self, conn, nome: str, plataforma: Plataforma, corte, postagem: int, registrar, entrega: Entrega,
                e: ErroPublicacao) -> str:
        """Registra a falha do jeito que o tipo do erro pede (veja o começo do módulo)."""
        if entrega.aceita:
            registrar("conferir", id_remoto=entrega.id_remoto, url=entrega.url,
                      erro=f"A rede recebeu o vídeo, mas não vi a publicação terminar: {e}")
            log.warning("%s: %s (a rede já tinha recebido o vídeo: confira no painel, eu não posto de novo)",
                        plataforma.rotulo, e)
            self._recusas.pop(nome, None)
            return "conferir"
        if self.parar.is_set():
            registrar("interrompido", erro=str(e))
            return "interrompido"
        if e.tipo == "ensaio":
            registrar("ensaio", erro=str(e))
            log.info("%s", e)
            return "ensaio"
        if e.rede_falhou or e.ambiente:
            # nada saiu e o vídeo não tem culpa: a linha some e a rede espera um pouco
            conn.execute("DELETE FROM postagens WHERE id=?", (postagem,))
            if e.rede_falhou:
                segundos, tipo = float(e.pausa_seg or PAUSA_REDE_SEG), "rede"
            else:
                segundos, tipo = float(e.pausa_seg or PAUSA_COTA_SEG), "cota"
            self._pausar(conn, nome, str(e), tipo, segundos)
            log.warning("%s: %s (tento de novo em %s)", plataforma.rotulo, e, _quando(segundos))
            return "pausado"
        if e.tipo == "bloqueio":
            # login vencido, conta restrita, configuração errada: nada saiu e o vídeo não tem culpa.
            # Como na queda da internet, a linha some (senão um dia de login vencido somava falhas
            # até tirar o corte da rede); o motivo fica na pausa da rede, que o painel mostra.
            conn.execute("DELETE FROM postagens WHERE id=?", (postagem,))
            segundos = None if e.persistente else float(e.pausa_seg or DESBLOQUEIO_SEG)
            self._pausar(conn, nome, str(e), "bloqueio", segundos)
            log.error("%s pausado%s: %s", plataforma.rotulo,
                      " até você testar a conta em Redes sociais" if segundos is None else f" por {_quando(segundos)}", e)
            return "bloqueado"
        if e.tipo == "corte":
            registrar("pulado", erro=str(e))
            log.warning("%s: %s (pulando este corte)", plataforma.rotulo, e)
            recusas = self._recusas[nome] = self._recusas.get(nome, 0) + 1
            if recusas >= RECUSAS_PARA_PAUSAR:
                # dois vídeos recusados em seguida raramente é culpa dos vídeos: segura a fila
                self._recusas.pop(nome, None)
                motivo = (f"{recusas} vídeos recusados em seguida (o último: {e}). Confira a rede e a configuração; "
                          "pausei para não queimar a fila")
                self._pausar(conn, nome, motivo, "recusas", PAUSA_RECUSAS_SEG)
                log.error("%s pausado por %s: %s", plataforma.rotulo, _quando(PAUSA_RECUSAS_SEG), motivo)
            return "pulado"
        registrar("falhou", erro=str(e))
        log.warning("%s: %s (vai tentar de novo)", plataforma.rotulo, e)
        return self._limitar_falhas(conn, nome, corte, postagem, str(e))

    def _limitar_falhas(self, conn, nome: str, corte, postagem: int, erro: str, midia: str = "video",
                        imagem_tipo: str | None = None) -> str:
        """Depois de algumas falhas comuns, o corte sai desta rede (com o motivo no histórico).

        Contam só as falhas da mesma mídia (e, na imagem, do mesmo tipo). O post da gravação marcado
        "não saiu" ("gravação:") e a falha do PC ao gerar a imagem ("geração:") não são culpa do corte.
        """
        falhas = conn.execute(
            "SELECT COUNT(*) FROM postagens WHERE corte_id=? AND plataforma=? AND midia=? AND imagem_tipo IS ? "
            "AND status='falhou' AND COALESCE(erro, '') NOT LIKE 'gravação:%' AND COALESCE(erro, '') NOT LIKE 'geração:%'",
            (corte["id"], nome, midia, imagem_tipo),
        ).fetchone()[0]
        if falhas < FALHAS_POR_CORTE:
            return "falhou"
        conn.execute("UPDATE postagens SET status='pulado', erro=?, atualizado_em=? WHERE id=?",
                     (f"Desisti depois de {falhas} falhas: {erro}"[:600], agora(), postagem))
        log.warning("%s: desisti do corte %s nesta rede depois de %d falhas (%s)",
                    ROTULOS.get(nome, nome), corte["id"], falhas, erro)
        return "pulado"

    # ------------------------------------------------------------ postagem à mão
    def _criar_tarefa(self, conn, nome: str, corte, arquivo: Path, conteudo: Conteudo, horario_ts, chave,
                      manual: bool) -> str:
        """Tarefa para você postar: nada é enviado, o painel mostra o vídeo e os textos da rede."""
        if conn.execute("SELECT 1 FROM postagens WHERE corte_id=? AND plataforma=? AND midia='video' "
                        "AND status='aguardando'", (corte["id"], nome)).fetchone():
            return "aguardando"  # este corte já está esperando você nesta rede
        t = agora()
        tarefa = conn.execute(
            "INSERT INTO postagens (corte_id, plataforma, status, horario, slot, manual, via, criado_em, atualizado_em) "
            "VALUES (?, ?, 'aguardando', ?, ?, ?, 'manual', ?, ?)",
            (corte["id"], nome, horario_ts, chave, int(manual), t, t),
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
        # Instagram só de imagens: não há Reel para repetir na Página
        return bool(ig["ativo"] and ig.get("videos", True) and ig["pagina_facebook"]
                    and ig["envio"] in ("oficial", "upload_post"))

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
                "UPDATE postagens SET status=?, erro=?, id_remoto=COALESCE(?, id_remoto), url=COALESCE(?, url), "
                "atualizado_em=? WHERE id=?",
                (status, erro, id_remoto, url, agora(), postagem),
            )

        if conteudo.duracao > FACEBOOK_MAX_SEG + 0.5:
            registrar("pulado", erro=f"o Reel da Página aceita até {FACEBOOK_MAX_SEG} s")
            return "pulado"
        if self.cfg.simulacao:
            log.info("[SIMULAÇÃO] Página do Facebook receberia %s", arquivo.name)
            registrar("simulado")
            return "simulado"

        def anotar(video_id, url) -> None:
            conn.execute("UPDATE postagens SET id_remoto=?, url=?, atualizado_em=? WHERE id=?",
                         (video_id, url, agora(), postagem))

        entrega = Entrega(anotar)
        log.info("Página do Facebook: publicando %s", arquivo.name)
        ATIVIDADES.definir(f"Enviando '{conteudo.titulo}' para a Página do Facebook", 0)
        try:
            resultado = instagram.publicar_facebook(arquivo, conteudo, self.parar, entrega=entrega)
        except ErroPublicacao as e:
            if entrega.aceita:
                if entrega.url:  # o Facebook aceitou publicar e deu o link: não envia de novo
                    registrar("publicado", erro=f"o Reel foi aceito, mas não vi o processamento terminar ({e})",
                              id_remoto=entrega.id_remoto, url=entrega.url)
                    return "publicado"
                registrar("conferir", erro=f"A Página recebeu o vídeo, mas não vi a publicação terminar: {e}",
                          id_remoto=entrega.id_remoto)
                return "conferir"
            if self.parar.is_set():
                registrar("interrompido", erro=str(e))
                return "interrompido"
            # 'corte' (vídeo recusado) e 'bloqueio' (falta permissão) não adiantam repetir; o painel avisa
            status = "falhou" if e.tipo == "temporario" else "pulado"
            registrar(status, erro=str(e))
            log.warning("Página do Facebook: %s (%s)", e, "vai tentar de novo" if status == "falhou" else "não vai repetir")
            return status
        except Exception as e:
            log.exception("Página do Facebook: erro inesperado ao publicar")
            if entrega.aceita:
                registrar("conferir", erro=f"erro inesperado depois que a Página recebeu o vídeo: {e}"[:500],
                          id_remoto=entrega.id_remoto, url=entrega.url)
                return "conferir"
            registrar("falhou", erro=f"{e.__class__.__name__}: {e}"[:500])
            return "falhou"
        finally:
            ATIVIDADES.limpar()
        status = "publicado" if resultado.confirmado else "conferir"
        registrar(status, erro=resultado.observacao, id_remoto=resultado.id_remoto, url=resultado.url)
        log.info("Página do Facebook: %s %s%s", "publicado" if status == "publicado" else "a conferir",
                 resultado.url or resultado.id_remoto, f" ({resultado.observacao})" if resultado.observacao else "")
        return status

    def _repetir_facebook(self, conn: sqlite3.Connection) -> None:
        """Tenta de novo os Reels que não chegaram à Página (erro temporário ou envio interrompido)."""
        if self.cfg.simulacao or not self.facebook_ligado():
            return
        instagram = self.plataformas.get("instagram")
        if instagram is None or not hasattr(instagram, "publicar_facebook") or not instagram.pronta()[0]:
            return
        if "instagram" in self.bloqueadas:
            return  # a conta do Instagram está pausada: a Página usa a mesma
        linhas = conn.execute(
            "SELECT corte_id, COUNT(*) AS n, MAX(atualizado_em) AS ultima FROM postagens WHERE plataforma='facebook' "
            "GROUP BY corte_id HAVING SUM(status IN ('publicado', 'pulado', 'enviando', 'conferir')) = 0 "
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
        if not estados or any(s in ("publicado", "pulado", "conferir") for s in estados):
            return False
        if "enviando" in estados:
            return True
        return sum(s in ("falhou", "interrompido") for s in estados) < MAX_TENTATIVAS_FACEBOOK

    # ------------------------------------------------------------ ciclo
    def atualizar_concluidos(self, conn: sqlite3.Connection) -> None:
        """Marca como concluídos os cortes já publicados em todas as redes ativas.

        A tarefa à mão ('aguardando') e o post 'a conferir' só contam quando você resolve no painel.
        Só os vídeos contam: o post de imagem nunca segura nem conclui um corte.
        """
        ativas = [nome for nome in self.plataformas if self.cfg[nome].get("videos", True)]
        if self.cfg.simulacao or not ativas:
            return  # em simulação nada é concluído: os cortes continuam valendo para o modo real
        facebook = self.facebook_ligado()
        marc = ",".join("?" * len(ativas))
        for corte in conn.execute("SELECT id, arquivo FROM cortes WHERE status='pronto'").fetchall():
            # a rede está resolvida com o post feito ou pulado (ou, no histórico antigo, com as falhas no limite)
            feitas = conn.execute(
                f"SELECT plataforma FROM postagens WHERE corte_id=? AND plataforma IN ({marc}) AND midia='video' "
                "GROUP BY plataforma HAVING SUM(status IN ('publicado', 'pulado')) > 0 OR SUM(status = 'falhou') >= ?",
                (corte["id"], *ativas, FALHAS_POR_CORTE),
            ).fetchall()
            if len(feitas) < len(ativas):
                continue
            if facebook and self._facebook_pendente(conn, corte["id"]):
                continue  # o Reel da Página ainda vai ser tentado de novo com este arquivo
            if conn.execute("SELECT 1 FROM postagens WHERE corte_id=? AND midia='video' "
                            "AND status IN ('aguardando', 'conferir') LIMIT 1", (corte["id"],)).fetchone():
                continue  # tarefa à mão ou post a conferir: o vídeo fica até você resolver
            conn.execute("UPDATE cortes SET status='concluido' WHERE id=?", (corte["id"],))
            if self.cfg["geral"]["apagar_apos_postar"] and corte["arquivo"]:
                # a miniatura (.jpg) fica para o histórico do painel
                for extensao in (".mp4", ".ass"):
                    Path(corte["arquivo"]).with_suffix(extensao).unlink(missing_ok=True)

    def ciclo(self, conn: sqlite3.Connection) -> None:
        pausas = self._bloqueios_vigentes(conn)
        if self.cfg["agenda"]["pausado"]:
            self.atualizar_concluidos(conn)
            self._coletar_metricas(conn)
            return
        momento = datetime.now()
        for nome, plataforma in self.plataformas.items():
            if self.parar.is_set():
                return
            if nome in pausas and not self.cfg.simulacao:
                # na simulação nada é enviado: a pausa fica guardada para quando a publicação real voltar
                # (como no painel, que só mostra a pausa fora da simulação)
                continue
            if not self.cfg.simulacao:
                pronta, motivo = plataforma.pronta()
                if not pronta:
                    if nome not in self._nao_prontas:
                        log.error("%s não está configurado: %s", plataforma.rotulo, motivo)
                    self._nao_prontas[nome] = {"motivo": motivo, "tipo": "configuracao", "desde": time.time(),
                                               "ate": None}
                    self.bloqueadas[nome] = self._nao_prontas[nome]
                    continue
                if self._nao_prontas.pop(nome, None) is not None:
                    self.bloqueadas.pop(nome, None)
            if self.cfg[nome].get("videos", True):
                self._ciclo_video(conn, nome, plataforma, momento)
        if not self.parar.is_set():
            self._repetir_facebook(conn)
        self.atualizar_concluidos(conn)
        self._coletar_metricas(conn)

    def _ciclo_video(self, conn: sqlite3.Connection, nome: str, plataforma: Plataforma, momento: datetime) -> None:
        """O horário de vídeo da rede que chegou: o próximo corte da fila."""
        if plataforma.via == "manual":
            esperando = self.tarefas_esperando(conn, nome)
            if esperando >= tarefas.MAX_TAREFAS_POR_REDE:
                self._avisar(f"{nome}-tarefas", "%s: %d tarefas de postagem à mão esperando você; os próximos "
                             "horários ficam sem tarefa nova até você postar ou pular", ROTULOS[nome], esperando)
                return
        for _ in range(3):  # até 3 cortes recusados no mesmo horário
            devido = self.horario_devido(conn, nome, momento)
            if devido is None:
                break
            horario, chave = devido
            corte = self.proximo_corte(conn, nome)
            if corte is None:
                self._avisar(f"{nome}-sem-corte", "%s: chegou o horário %s, mas não há corte pronto ainda",
                             plataforma.rotulo, horario.strftime("%H:%M"), intervalo=1800)
                break
            if self.postar(conn, nome, corte, horario=horario, chave=chave) != "pulado":
                break
            if nome in self.bloqueadas:
                break  # a última recusa pausou a rede

    def _coletar_metricas(self, conn: sqlite3.Connection) -> None:
        if self.cfg.simulacao or not self.cfg["metricas"]["ativo"] or time.time() < self._proxima_metrica:
            return
        self._proxima_metrica = time.time() + 1800  # confere a cada 30 min quais posts precisam de dados novos
        try:
            metricas.coletar(self.cfg, conn, {n: p for n, p in self.plataformas.items() if n not in self.bloqueadas})
        except Exception:
            log.exception("Erro ao coletar métricas")
