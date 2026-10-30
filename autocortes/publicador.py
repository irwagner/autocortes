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
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from . import agenda, db, gravador, imagens, metricas, planejador
from .config import PLATAFORMAS, ROTULOS, Config
from .db import agora
from .midia import ErroMidia, sondar
from .plataformas import Entrega, ErroPublicacao, Plataforma, criar
from .plataformas import manual as tarefas
from .plataformas.youtube import motivo_nao_short
from .textos import Conteudo, montar_conteudo, montar_conteudo_imagem
from .util import ATIVIDADES, log, slug

DESBLOQUEIO_SEG = 6 * 3600     # bloqueio que passa sozinho (login, configuração): tenta de novo depois de 6 h
PAUSA_REDE_SEG = 600           # a internet caiu antes de a rede responder
PAUSA_COTA_SEG = 3600          # limite de uso sem prazo informado pela rede
PAUSA_RECUSAS_SEG = 6 * 3600   # vídeos recusados em seguida: algo mudou na rede ou na configuração
RECUSAS_PARA_PAUSAR = 2
FALHAS_POR_CORTE = planejador.FALHAS_POR_CORTE
# um envio por vez em cada rede, mesmo com o "postar agora" do painel rodando junto com o loop
TRAVAS_REDE = {p: threading.Lock() for p in PLATAFORMAS}


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
        if not self.cfg[nome].get("videos", True):
            return "so_imagens"
        trava = TRAVAS_REDE[nome]
        if not trava.acquire(blocking=False):
            return "ocupado"
        try:
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

        if nome == "facebook":  # a fila já tira os mais longos: aqui só chega o "Postar agora"
            maximo = int(self.cfg["facebook"]["max_segundos"])
            if corte["fim"] - corte["inicio"] > maximo + 0.5:
                conn.execute(
                    "INSERT INTO postagens (corte_id, plataforma, status, erro, horario, slot, manual, via, criado_em, "
                    "atualizado_em) VALUES (?, 'facebook', 'pulado', ?, ?, ?, ?, ?, ?, ?)",
                    (corte["id"], f"o Reel da Página aceita até {maximo} s", horario_ts, chave, int(manual),
                     plataforma.via, t, t),
                )
                log.warning("Facebook: pulando %s (o Reel da Página aceita até %d s)", arquivo.name, maximo)
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
            return "simulado"

        log.info("%s: publicando %s", plataforma.rotulo, arquivo.name)
        ATIVIDADES.definir(f"Enviando '{conteudo.titulo}' para o {plataforma.rotulo}", 0)
        return self._enviar(conn, nome, plataforma, corte, postagem, registrar,
                            lambda entrega: plataforma.publicar(arquivo, conteudo, self.parar, entrega))

    def _enviar(self, conn, nome: str, plataforma: Plataforma, corte, postagem: int, registrar, chamada,
                midia: str = "video", imagem_tipo: str | None = None) -> str:
        """O envio em si, igual para vídeo e imagem: a Entrega, os erros e a confirmação.

        chamada(entrega) publica. Toda rede passa por aqui, inclusive o Facebook: a rede que aceitou o
        post (com o id e até o link) e deu erro depois fica para você conferir, nunca "publicado" calado
        (o antigo Reel da Página dava "publicado" nesse caso; conferir é mais seguro).
        """
        o_post = "a imagem" if midia == "imagem" else "o vídeo"

        def anotar(id_remoto, url) -> None:
            # gravado na hora em que a rede aceita: se o AutoCortes fechar depois, o post não sai de novo
            conn.execute("UPDATE postagens SET id_remoto=?, url=?, atualizado_em=? WHERE id=?",
                         (id_remoto, url, agora(), postagem))

        entrega = Entrega(anotar)
        try:
            resultado = chamada(entrega)
        except ErroPublicacao as e:
            return self._falhou(conn, nome, plataforma, corte, postagem, registrar, entrega, e, midia, imagem_tipo)
        except Exception as e:  # erro inesperado: registra e segue o loop
            log.exception("%s: erro inesperado ao publicar", plataforma.rotulo)
            erro = f"{e.__class__.__name__}: {e}"[:500]
            if entrega.aceita:
                registrar("conferir", id_remoto=entrega.id_remoto, url=entrega.url,
                          erro=f"A rede recebeu {o_post}, mas houve um erro inesperado depois: {erro}")
                return "conferir"
            registrar("falhou", erro=erro)
            return self._limitar_falhas(conn, nome, corte, postagem, erro, midia, imagem_tipo)
        finally:
            ATIVIDADES.limpar()

        self._recusas.pop(nome, None)
        if not resultado.confirmado:
            registrar("conferir", id_remoto=resultado.id_remoto, url=resultado.url,
                      erro=resultado.observacao or f"a rede recebeu {o_post}, mas não vi a confirmação da publicação")
            log.warning("%s: %s. Ficou para você conferir no painel", plataforma.rotulo,
                        resultado.observacao or "não vi a confirmação da publicação")
            return "conferir"
        registrar("publicado", id_remoto=resultado.id_remoto, url=resultado.url, erro=resultado.observacao)
        log.info(
            "%s: publicado %s%s", plataforma.rotulo, resultado.url or resultado.id_remoto,
            f" ({resultado.observacao})" if resultado.observacao else "",
        )
        return "publicado"

    def _falhou(self, conn, nome: str, plataforma: Plataforma, corte, postagem: int, registrar, entrega: Entrega,
                e: ErroPublicacao, midia: str = "video", imagem_tipo: str | None = None) -> str:
        """Registra a falha do jeito que o tipo do erro pede (veja o começo do módulo)."""
        o_post = "a imagem" if midia == "imagem" else "o vídeo"
        if entrega.aceita:
            registrar("conferir", id_remoto=entrega.id_remoto, url=entrega.url,
                      erro=f"A rede recebeu {o_post}, mas não vi a publicação terminar: {e}")
            log.warning("%s: %s (a rede já tinha recebido %s: confira no painel, eu não posto de novo)",
                        plataforma.rotulo, e, o_post)
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
        if e.tipo == "formato":
            # a página não aceita este post (ou o roteiro de imagem sumiu): nada saiu, o corte não tem
            # culpa e os vídeos seguem. Só o carrossel num campo sem "multiple" tira só o carrossel;
            # o resto para as imagens da rede até você gravar o roteiro de imagem de novo
            conn.execute("DELETE FROM postagens WHERE id=?", (postagem,))
            motivo = str(e)[:600]
            if imagem_tipo == "carrossel" and not e.parado:
                planejador.gravar_estado_imagens(conn, nome, {"sem_carrossel": motivo})
                log.warning("%s: %s (o carrossel sai desta rede; as outras imagens continuam)", plataforma.rotulo, e)
            else:
                planejador.gravar_estado_imagens(conn, nome, {"parado": motivo})
                log.error("%s: %s (as imagens desta rede param até você gravar o roteiro de imagem de novo; "
                          "os vídeos continuam)", plataforma.rotulo, e)
            return "formato"
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
                # dois posts recusados em seguida (vídeo ou imagem) raramente é culpa deles: segura a fila
                self._recusas.pop(nome, None)
                motivo = (f"{recusas} posts recusados em seguida (o último: {e}). Confira a rede e a configuração; "
                          "pausei para não queimar a fila")
                self._pausar(conn, nome, motivo, "recusas", PAUSA_RECUSAS_SEG)
                log.error("%s pausado por %s: %s", plataforma.rotulo, _quando(PAUSA_RECUSAS_SEG), motivo)
            return "pulado"
        registrar("falhou", erro=str(e))
        log.warning("%s: %s (vai tentar de novo)", plataforma.rotulo, e)
        return self._limitar_falhas(conn, nome, corte, postagem, str(e), midia, imagem_tipo)

    # ------------------------------------------------------------ posts de imagem
    def postar_imagem(self, conn: sqlite3.Connection, nome: str, corte, tipo: str, horario: datetime | None = None,
                      manual: bool = False, chave: str | None = None) -> str:
        """Gera (ou reaproveita) as imagens do corte e posta pelo roteiro de imagem da rede.

        Devolve o status da postagem ou: "ocupado" (outro envio na rede), "bloqueado" (as imagens da
        rede não estão prontas, sem linha), "sem_imagem" (o filme saiu da pasta, sem linha) e
        "formato" (a página não aceitou este post, sem linha).
        """
        trava = TRAVAS_REDE[nome]
        if not trava.acquire(blocking=False):
            return "ocupado"
        try:
            return self._postar_imagem(conn, nome, corte, tipo, horario, manual, chave)
        finally:
            trava.release()

    def _pasta_envio(self, corte_id: int, nome: str) -> Path:
        # uma por rede: dois envios do mesmo corte em redes diferentes não apagam as cópias um do outro
        return imagens.pasta_imagens(self.cfg, corte_id) / "envio" / nome

    def _copias_de_envio(self, pasta: Path, arquivos: list[Path], titulo: str) -> list[Path]:
        """Cópias com o nome do filme: um campo que a página preenche com o nome do arquivo mostra o filme."""
        if pasta.exists():
            shutil.rmtree(pasta)
        pasta.mkdir(parents=True)
        base = slug(titulo, 40)
        if not base or base == "video" or gravador._tem_nome_de_exemplo(base):
            base = "imagem"
        copias = []
        for i, arquivo in enumerate(arquivos, 1):
            copia = pasta / f"{base}-{i}{arquivo.suffix.lower() or '.jpg'}"
            shutil.copyfile(arquivo, copia)
            copias.append(copia)
        return copias

    def _postar_imagem(self, conn, nome: str, corte, tipo: str, horario: datetime | None, manual: bool,
                       chave: str | None) -> str:
        if not planejador.imagens_prontas(self.cfg, conn, nome)[0]:
            return "bloqueado"  # o painel já mostra o motivo
        plataforma = self.plataformas.get(nome) or criar(nome, self.cfg)
        rotulo = ROTULOS[nome]
        filme = conn.execute("SELECT * FROM filmes WHERE id=?", (corte["filme_id"],)).fetchone()
        horario_ts = horario.timestamp() if horario else None
        pasta = self._pasta_envio(corte["id"], nome)

        def inserir(status: str, erro: str | None = None, imagem_id: int | None = None) -> int:
            t = agora()
            return conn.execute(
                "INSERT INTO postagens (corte_id, plataforma, status, midia, imagem_tipo, imagem_id, erro, horario, "
                "slot, manual, via, criado_em, atualizado_em) VALUES (?, ?, ?, 'imagem', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (corte["id"], nome, status, tipo, imagem_id, erro, horario_ts, chave, int(manual), plataforma.via,
                 t, t),
            ).lastrowid

        imagens.TRAVA.acquire()  # a gravação e a prévia desistem em 5 s; o motor espera a vez
        try:
            try:
                linha = imagens.preparar(self.cfg, conn, corte, tipo, nome, self.parar)
                arquivos = [Path(a) for a in json.loads(linha["arquivos"] or "[]")]
                conteudo = montar_conteudo_imagem(self.cfg, filme, corte, tipo, len(arquivos), nome,
                                                  linha["texto"] or "", com_chamada=bool(linha["com_chamada"]))
                copias = self._copias_de_envio(pasta, arquivos, filme["titulo"] if filme else "")
            except Exception as e:
                self._apagar_envio(pasta, rotulo)
                if self.parar.is_set():
                    return "interrompido"  # parou no meio: nada foi gravado
                if isinstance(e, imagens.ErroImagem) and e.sem_linha:
                    # o filme saiu da pasta entre a fila e a geração: o horário fica para a próxima volta
                    self._avisar(f"{nome}-filme-ausente", "%s: %s (a imagem do corte %s fica para depois)",
                                 rotulo, e, corte["id"], intervalo=1800)
                    return "sem_imagem"
                if isinstance(e, imagens.ErroImagem) and e.do_corte:
                    inserir("pulado", str(e)[:600])
                    log.warning("%s: %s (a imagem do tipo %s do corte %s sai desta rede)", rotulo, e, tipo,
                                corte["id"])
                    return "pulado"
                # falha do PC (FFmpeg, disco, prazo): não gasta o corte, mas conta nas tentativas do horário
                if not isinstance(e, (imagens.ErroImagem, OSError)):
                    log.exception("%s: erro inesperado ao gerar a imagem", rotulo)
                postagem = inserir("falhou", f"geração: {e}"[:600])
                self._avisar(f"{nome}-geracao", "%s: não consegui gerar a imagem (%s); tento de novo no próximo "
                             "horário", rotulo, e, intervalo=1800)
                return self._limitar_falhas(conn, nome, corte, postagem, f"geração: {e}", "imagem", tipo)
            # a linha nasce antes de soltar a TRAVA: a gravação não redesenha uma imagem que está saindo
            postagem = inserir("enviando", imagem_id=linha["id"])
        finally:
            imagens.TRAVA.release()

        def registrar(status: str, **campos) -> None:
            conn.execute(
                "UPDATE postagens SET status=?, id_remoto=COALESCE(?, id_remoto), url=COALESCE(?, url), erro=?, "
                "atualizado_em=? WHERE id=?",
                (status, campos.get("id_remoto"), campos.get("url"), campos.get("erro"), agora(), postagem),
            )

        descricao = f"{len(copias)} imagens ({tipo})" if len(copias) > 1 else f"1 imagem ({tipo})"
        try:
            if self.cfg.simulacao:
                log.info("[SIMULAÇÃO] %s receberia %s de '%s'\n    texto: %s", rotulo, descricao, conteudo.titulo,
                         conteudo.descricao.replace("\n", " / "))
                registrar("simulado")
                return "simulado"
            log.info("%s: publicando %s de '%s'", plataforma.rotulo, descricao, conteudo.titulo)
            ATIVIDADES.definir(f"Enviando a imagem de '{conteudo.titulo}' para o {rotulo}", 0)
            return self._enviar(conn, nome, plataforma, corte, postagem, registrar,
                                lambda entrega: plataforma.publicar_imagens(copias, conteudo, self.parar, entrega),
                                midia="imagem", imagem_tipo=tipo)
        finally:
            self._apagar_envio(pasta, rotulo)

    @staticmethod
    def _apagar_envio(pasta: Path, rotulo: str) -> None:
        try:
            shutil.rmtree(pasta)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("%s: não consegui apagar as cópias de envio em %s (%s); saem na próxima vez", rotulo, pasta, e)

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

    # ------------------------------------------------------------ ciclo
    def atualizar_concluidos(self, conn: sqlite3.Connection) -> None:
        """Marca como concluídos os cortes já publicados em todas as redes ativas.

        A tarefa à mão ('aguardando') e o post 'a conferir' só contam quando você resolve no painel.
        Só os vídeos contam: o post de imagem nunca segura nem conclui um corte.
        """
        ativas = [nome for nome in self.plataformas if self.cfg[nome].get("videos", True)]
        if self.cfg.simulacao or not ativas:
            return  # em simulação nada é concluído: os cortes continuam valendo para o modo real
        maximos = {nome: planejador.duracao_maxima(self.cfg, nome) for nome in ativas}
        for corte in conn.execute("SELECT id, arquivo, inicio, fim FROM cortes WHERE status='pronto'").fetchall():
            # a rede cujo limite de duração o corte passa está resolvida: a fila nunca o manda para ela
            duracao = corte["fim"] - corte["inicio"]
            redes = [nome for nome in ativas if maximos[nome] is None or duracao <= maximos[nome] + 0.5]
            if not redes:
                continue  # não cabe em nenhuma rede ativa: não saiu em lugar nenhum, então não conclui
            marc = ",".join("?" * len(redes))
            # a rede está resolvida com o post feito ou pulado (ou, no histórico antigo, com as falhas no limite)
            feitas = conn.execute(
                f"SELECT plataforma FROM postagens WHERE corte_id=? AND plataforma IN ({marc}) AND midia='video' "
                "GROUP BY plataforma HAVING SUM(status IN ('publicado', 'pulado')) > 0 OR SUM(status = 'falhou') >= ?",
                (corte["id"], *redes, FALHAS_POR_CORTE),
            ).fetchall()
            if len(feitas) < len(redes):
                continue
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
        # antes do teste da pausa da agenda: a marcação vale uma vez por perfil, e a conclusão dos cortes
        # (que roda mesmo com a agenda pausada) já precisa dela para não esperar um Facebook que não vem
        if "facebook" in self.plataformas and not self.cfg.simulacao:
            try:
                planejador.preparar_facebook(self.cfg, conn)
            except sqlite3.Error:
                log.exception("Facebook: não consegui separar os cortes que já saíram no Instagram; "
                              "o ciclo segue sem o Facebook nesta volta")
                pausas = {**pausas, "facebook": {"tipo": "rede"}}
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
            pausou = nome in self.bloqueadas and not self.cfg.simulacao  # o bloco de vídeo pode ter pausado a rede
            if nome in self.cfg.redes_de_imagem() and not pausou and not self.parar.is_set():
                self._ciclo_imagem(conn, nome, plataforma, momento)
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

    def _ciclo_imagem(self, conn: sqlite3.Connection, nome: str, plataforma: Plataforma, momento: datetime) -> None:
        """O horário de imagem da rede que chegou: o próximo corte e tipo da fila de imagens."""
        pronta, motivo = planejador.imagens_prontas(self.cfg, conn, nome)
        if not pronta:
            self._avisar(f"{nome}-imagens", "%s: as imagens estão paradas: %s", plataforma.rotulo, motivo)
            return
        for _ in range(3):  # o pulado e o formato passam a vez ao tipo ou corte seguinte no mesmo horário
            devido = self.horario_devido(conn, nome, momento, midia="imagem")
            if devido is None:
                break
            horario, chave = devido
            fila = planejador.fila_imagens(self.cfg, conn, nome, 1)
            if not fila:
                self._avisar(f"{nome}-sem-imagem", "%s: chegou o horário de imagem %s, mas nenhum corte serve "
                             "para imagem agora", plataforma.rotulo, horario.strftime("%H:%M"), intervalo=1800)
                break
            resultado = self.postar_imagem(conn, nome, fila[0]["corte"], fila[0]["tipo"], horario=horario,
                                           chave=chave)
            if resultado not in ("pulado", "formato") or self.parar.is_set():
                break
            if nome in self.bloqueadas or not planejador.imagens_prontas(self.cfg, conn, nome)[0]:
                break  # a recusa pausou a rede, ou o formato parou as imagens dela

    def _coletar_metricas(self, conn: sqlite3.Connection) -> None:
        if self.cfg.simulacao or not self.cfg["metricas"]["ativo"] or time.time() < self._proxima_metrica:
            return
        self._proxima_metrica = time.time() + 1800  # confere a cada 30 min quais posts precisam de dados novos
        try:
            metricas.coletar(self.cfg, conn, {n: p for n, p in self.plataformas.items() if n not in self.bloqueadas})
        except Exception:
            log.exception("Erro ao coletar métricas")
