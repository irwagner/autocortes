"""Utilidades gerais: log, arquivos JSON, trava de instância única e energia."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger("autocortes")


class LogMemoria(logging.Handler):
    """Guarda as últimas mensagens do log para o painel mostrar ao vivo."""

    def __init__(self, capacidade: int = 1500):
        super().__init__()
        self.registros: deque[dict] = deque(maxlen=capacidade)
        self._seq = 0
        self._trava = threading.Lock()
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            mensagem = self.format(record)
        except Exception:
            mensagem = record.getMessage()
        with self._trava:
            self._seq += 1
            self.registros.append(
                {"id": self._seq, "ts": record.created, "nivel": record.levelname,
                 "origem": record.threadName, "msg": mensagem}
            )

    @property
    def ultimo_id(self) -> int:
        return self._seq

    def desde(self, ultimo: int = 0, limite: int = 500) -> list[dict]:
        with self._trava:
            itens = [r for r in self.registros if r["id"] > ultimo]
        return itens[-limite:]


LOG_MEMORIA = LogMemoria()


class Atividades:
    """O que cada parte do sistema está fazendo agora (produtor, publicador, downloads...)."""

    def __init__(self):
        self._trava = threading.Lock()
        self._itens: dict[str, dict] = {}

    def definir(self, texto: str, pct: float | None = None, chave: str | None = None) -> None:
        chave = chave or threading.current_thread().name
        with self._trava:
            atual = self._itens.get(chave)
            desde = atual["desde"] if atual and atual["texto"] == texto else time.time()
            self._itens[chave] = {"chave": chave, "texto": texto, "pct": _pct(pct), "desde": desde}

    def progresso(self, pct: float, chave: str | None = None) -> None:
        chave = chave or threading.current_thread().name
        with self._trava:
            if chave in self._itens:
                self._itens[chave]["pct"] = _pct(pct)

    def limpar(self, chave: str | None = None) -> None:
        with self._trava:
            self._itens.pop(chave or threading.current_thread().name, None)

    def listar(self) -> list[dict]:
        with self._trava:
            return [dict(v) for v in self._itens.values()]


def _pct(valor: float | None) -> float | None:
    return None if valor is None else round(max(0.0, min(100.0, float(valor))), 1)


ATIVIDADES = Atividades()


def preparar_console() -> None:
    """Evita que caracteres fora da página de código derrubem o programa."""
    for fluxo in (sys.stdout, sys.stderr):
        if fluxo is not None and hasattr(fluxo, "reconfigure"):
            try:
                fluxo.reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


def configurar_log(pasta: Path, detalhado: bool = False) -> None:
    if log.handlers:
        return
    pasta.mkdir(parents=True, exist_ok=True)
    nivel = logging.DEBUG if detalhado else logging.INFO
    log.setLevel(nivel)
    log.propagate = False
    LOG_MEMORIA.setLevel(logging.INFO)
    log.addHandler(LOG_MEMORIA)

    arquivo = RotatingFileHandler(
        pasta / "autocortes.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    arquivo.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s", "%Y-%m-%d %H:%M:%S"
        )
    )
    log.addHandler(arquivo)

    # pythonw (sem console) deixa sys.stderr = None
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        log.addHandler(console)


def salvar_json(caminho: Path, dados) -> None:
    """Grava de forma atômica (arquivo temporário + replace)."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_name(caminho.name + ".tmp")
    temporario.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporario, caminho)


def ler_json(caminho: Path, padrao=None):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return padrao
    except (json.JSONDecodeError, UnicodeDecodeError):
        log.warning("Arquivo JSON corrompido, ignorando: %s", caminho)
        return padrao


def sem_acentos(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")


def slug(texto: str, maximo: int = 40) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "-", sem_acentos(texto)).strip("-").lower()
    return (base[:maximo].strip("-")) or "video"


def fmt_tempo(segundos: float) -> str:
    segundos = max(0, int(round(segundos)))
    h, resto = divmod(segundos, 3600)
    m, s = divmod(resto, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_data(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%d/%m %H:%M")


class Trava:
    """Impede duas instâncias do loop rodando ao mesmo tempo."""

    def __init__(self, caminho: Path):
        self.caminho = caminho
        self._arquivo = None

    def adquirir(self) -> bool:
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        arquivo = open(self.caminho, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                arquivo.seek(0)
                msvcrt.locking(arquivo.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(arquivo.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            arquivo.close()
            return False
        self._arquivo = arquivo
        return True

    def liberar(self) -> None:
        if self._arquivo is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._arquivo.seek(0)
                msvcrt.locking(self._arquivo.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._arquivo.close()
        self._arquivo = None


def impedir_suspensao(ativo: bool) -> None:
    """Pede ao Windows para não suspender o PC enquanto o loop estiver ativo."""
    if os.name != "nt":
        return
    import ctypes

    es_continuous = 0x80000000
    es_system_required = 0x00000001
    flags = es_continuous | (es_system_required if ativo else 0)
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except (AttributeError, OSError):
        log.debug("SetThreadExecutionState indisponível")
