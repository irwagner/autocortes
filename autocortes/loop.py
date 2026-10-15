"""Motor contínuo: uma thread produz cortes, outra cuida da agenda de postagens e a terceira
olha os canais do YouTube (quando ligados) e baixa os vídeos novos.

O mesmo motor roda no terminal (python -m autocortes rodar) e dentro do painel,
que pode ligar, desligar e trocar a configuração com ele em funcionamento.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from . import agenda, canais, db
from .config import Config
from .midia import Interrompido
from .produtor import TRAVA_ANALISE, TRAVA_EDICAO, Produtor
from .publicador import TRAVAS_REDE, Publicador, desbloquear
from .util import ATIVIDADES, Trava, impedir_suspensao, log


def recuperar_pendentes(cfg: Config) -> None:
    """Devolve à fila o que ficou "em andamento", sem mexer no que está rodando agora mesmo
    (um "Postar agora" do painel ou uma edição continuam enquanto o motor reinicia)."""
    conn = db.conectar(cfg.banco)
    try:
        db.recuperar_estados_pendentes(
            conn, redes_ocupadas=[rede for rede, trava in TRAVAS_REDE.items() if trava.locked()],
            editando=TRAVA_EDICAO.locked() or TRAVA_ANALISE.locked(),
            baixando=canais.TRAVA.locked(),
        )
    finally:
        conn.close()


def _dormir(parar: threading.Event, segundos: float) -> None:
    """Espera em passos de 1 s para o Ctrl+C responder rápido no Windows."""
    fim = time.monotonic() + segundos
    while not parar.is_set() and time.monotonic() < fim:
        parar.wait(min(1.0, fim - time.monotonic()))


def resumo(cfg: Config, conn, publicador: Publicador | None = None) -> str:
    contagem = dict(conn.execute("SELECT status, COUNT(*) FROM cortes GROUP BY status").fetchall())
    agora = datetime.now()
    proximos = []
    for nome in cfg.plataformas_ativas():
        if publicador and nome in publicador.bloqueadas:
            proximos.append(f"{nome}: pausado")
            continue
        h = agenda.proximo_horario(cfg, nome, agora)
        proximos.append(f"{nome} {h.strftime('%d/%m %H:%M') if h else '-'}")
    return (
        f"Estoque: {contagem.get('pronto', 0)} prontos, {contagem.get('revisao', 0)} em revisão, "
        f"{contagem.get('candidato', 0)} candidatos, {contagem.get('concluido', 0)} concluídos | "
        f"próximos horários: {', '.join(proximos) or 'nenhuma rede ativa'}"
    )


class Motor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._trava = threading.Lock()
        self._parar: threading.Event | None = None
        self._threads: list[threading.Thread] = []
        self.produtor: Produtor | None = None
        self.publicador: Publicador | None = None
        self.iniciado_em: float | None = None

    @property
    def rodando(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    @property
    def parando(self) -> bool:
        return self._parar is not None and self._parar.is_set() and self.rodando

    def iniciar(self) -> bool:
        with self._trava:
            if self.rodando:
                return False
            self._parar = threading.Event()
            recuperar_pendentes(self.cfg)
            self.produtor = Produtor(self.cfg, self._parar)
            self.publicador = Publicador(self.cfg, self._parar)
            self._threads = [
                threading.Thread(target=self._loop_produtor, name="produtor", daemon=True),
                threading.Thread(target=self._loop_publicador, name="publicador", daemon=True),
                threading.Thread(target=self._loop_canais, name="canais", daemon=True),
            ]
            for t in self._threads:
                t.start()
            self.iniciado_em = time.time()
        modo = "SIMULAÇÃO (nada será publicado)" if self.cfg.simulacao else "REAL"
        log.info("Motor ligado em modo %s. Redes ativas: %s", modo, ", ".join(self.cfg.plataformas_ativas()) or "nenhuma")
        return True

    def parar(self, timeout: float = 90) -> None:
        with self._trava:
            if self._parar is None or not self.rodando:
                return
            self._parar.set()
            threads = list(self._threads)
        log.info("Desligando o motor (aguarde o processo atual parar)...")
        for t in threads:
            t.join(timeout)
        vivas = [t.name for t in threads if t.is_alive()]
        if vivas:
            # a thread ainda está no meio de um envio ou de uma edição: devolver o estado agora
            # poria na fila de novo algo que ainda está acontecendo. Fica para a próxima abertura.
            log.warning("O motor não terminou a tempo (%s ainda trabalhando); o estado em andamento fica como está",
                        ", ".join(vivas))
        else:
            recuperar_pendentes(self.cfg)
        self.iniciado_em = None
        log.info("Motor desligado.")

    def aplicar_config(self, novo: Config) -> None:
        """Troca a configuração sem reiniciar (vale a partir do próximo ciclo)."""
        self.cfg.atualizar_de(novo)
        if self.publicador is not None:
            self.publicador.sincronizar()

    def rede_atualizada(self, rede: str, persistente: bool = False) -> None:
        """Depois de um login novo ou de mudar a configuração, a rede volta a ser tentada na hora.

        persistente=True (você testou a conta ou conectou de novo) tira também o bloqueio que não
        passa sozinho, como a verificação de robô ou a conta restrita.
        """
        conn = db.conectar(self.cfg.banco, preparar=False)
        try:
            if self.publicador is not None:
                self.publicador.liberar(conn, rede, persistente)
            else:
                desbloquear(conn, rede, persistente)
        finally:
            conn.close()

    def pedir_varredura(self) -> None:
        if self.produtor is not None:
            self.produtor.pedir_varredura()

    # ------------------------------------------------------------ threads
    def _loop_produtor(self) -> None:
        parar = self._parar
        conn = db.conectar(self.cfg.banco)
        falhas = 0
        try:
            while not parar.is_set():
                try:
                    trabalhou = self.produtor.ciclo(conn)
                    falhas = 0
                except Interrompido:
                    break
                except Exception:
                    falhas += 1
                    log.exception("Erro na produção de cortes (tentando de novo em instantes)")
                    ATIVIDADES.definir("Erro na produção; tentando de novo em instantes", ocioso=True)
                    _dormir(parar, min(900, 30 * falhas))
                    continue
                if not trabalhou:
                    _dormir(parar, 30)
        finally:
            ATIVIDADES.limpar("produtor")
            conn.close()

    def _loop_publicador(self) -> None:
        parar = self._parar
        conn = db.conectar(self.cfg.banco)
        if self.cfg["geral"]["impedir_suspensao"]:
            impedir_suspensao(True)  # vale para esta thread, que vive enquanto o motor estiver ligado
        ultimo_resumo = 0.0
        try:
            while not parar.is_set():
                try:
                    self.publicador.ciclo(conn)
                except Exception:
                    log.exception("Erro no ciclo de publicação")
                if time.time() - ultimo_resumo > 3600:
                    ultimo_resumo = time.time()
                    log.info(resumo(self.cfg, conn, self.publicador))
                _dormir(parar, 15)
        finally:
            impedir_suspensao(False)
            ATIVIDADES.limpar("publicador")
            conn.close()

    def _loop_canais(self) -> None:
        """Olha os canais do YouTube de N em N horas (com [canais].ativo) e baixa os vídeos novos."""
        parar = self._parar
        conn = db.conectar(self.cfg.banco)
        try:
            while not parar.is_set():
                try:
                    # o "Verificar agora" do painel pode estar baixando: este ciclo espera a vez
                    if canais.na_hora(self.cfg, conn) and canais.TRAVA.acquire(blocking=False):
                        try:
                            canais.verificar(self.cfg, conn, parar, ao_baixar=self.pedir_varredura)
                        finally:
                            canais.TRAVA.release()
                except Interrompido:
                    break
                except Exception:
                    # a hora da verificação já foi anotada: a próxima tentativa é na hora marcada
                    log.exception("Erro ao olhar os canais do YouTube")
                _dormir(parar, 60)
        finally:
            ATIVIDADES.limpar("canais")
            conn.close()


def rodar(cfg: Config) -> int:
    """Modo terminal, sem painel."""
    trava = Trava(cfg.pasta_dados / "autocortes.lock")
    if not trava.adquirir(espera=2):
        log.error("Já existe um AutoCortes rodando com esta pasta de dados (feche o painel ou a outra janela).")
        return 1
    motor = Motor(cfg)
    log.info("Pasta de filmes: %s", cfg.pasta_filmes)
    motor.iniciar()
    try:
        while motor.rodando:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        motor.parar()
        trava.liberar()
    return 0
