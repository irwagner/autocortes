"""Execução de FFmpeg/FFprobe/whisper.cpp com leitura de progresso e cancelamento."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable, Sequence

from .util import ATIVIDADES, log


class ErroMidia(Exception):
    pass


class Interrompido(Exception):
    """O loop pediu para parar enquanto um processo externo rodava."""


# ---------------------------------------------------------------- escapes

def escapar_filtro(valor: str) -> str:
    """Escapa um valor usado dentro de um filtergraph do FFmpeg.

    São dois níveis: o valor da opção (\\ ' :) e o filtergraph (\\ ' [ ] , ;).
    Ex.: E:/Pasta X/a.ass -> E\\\\:/Pasta X/a.ass
    """
    nivel_opcao = "".join("\\" + c if c in "\\':" else c for c in valor)
    return "".join("\\" + c if c in "\\'[],;" else c for c in nivel_opcao)


def caminho_filtro(caminho: Path) -> str:
    return escapar_filtro(Path(caminho).resolve().as_posix())


def caminho_relativo(caminho: Path, base: Path) -> str:
    """Caminho relativo quando possível (evita acentos/espaços para ferramentas externas)."""
    try:
        return os.path.relpath(Path(caminho).resolve(), Path(base).resolve())
    except ValueError:  # drives diferentes no Windows
        return str(Path(caminho).resolve())


# ---------------------------------------------------------------- processos

def _flags_criacao(baixa_prioridade: bool) -> int:
    if os.name != "nt":
        return 0
    flags = 0x08000000  # CREATE_NO_WINDOW: sem janelas piscando quando roda via pythonw
    if baixa_prioridade:
        flags |= 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS
    return flags


@dataclass
class ResultadoProcesso:
    codigo: int
    saida: str = ""
    erros: list[str] = field(default_factory=list)

    def resumo_erro(self, linhas: int = 8) -> str:
        uteis = [linha for linha in self.erros if linha.strip()]
        return "\n".join(uteis[-linhas:]) or f"código de saída {self.codigo}"


_JOB: tuple | None = None
_JOB_TRAVA = threading.Lock()


def _criar_job() -> tuple:
    """Job Object do Windows que mata FFmpeg/Whisper se o AutoCortes for fechado de repente."""
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class LimiteBasico(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class LimiteEstendido(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", LimiteBasico),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = LimiteEstendido()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return k32, job


def _anexar_ao_job(proc: subprocess.Popen) -> None:
    global _JOB
    if os.name != "nt":
        return
    with _JOB_TRAVA:
        if _JOB is None:
            try:
                _JOB = _criar_job()
            except OSError as e:
                log.debug("Job Object indisponível: %s", e)
                _JOB = ()
    if not _JOB:
        return
    k32, job = _JOB
    if not k32.AssignProcessToJobObject(job, int(proc._handle)):
        log.debug("Não foi possível anexar o processo %s ao Job Object", proc.pid)


def _encerrar(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def executar(
    cmd: Sequence,
    *,
    cwd: Path | None = None,
    baixa_prioridade: bool = True,
    parar: threading.Event | None = None,
    ao_ler_saida: Callable[[str], None] | None = None,
    ao_ler_erro: Callable[[str], None] | None = None,
    capturar_saida: bool = False,
    timeout: float | None = None,
) -> ResultadoProcesso:
    cmd = [str(c) for c in cmd]
    log.debug("Executando: %s", subprocess.list2cmdline(cmd))
    kwargs = {}
    if os.name != "nt" and baixa_prioridade:
        kwargs["preexec_fn"] = lambda: os.nice(10)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_flags_criacao(baixa_prioridade),
            **kwargs,
        )
    except FileNotFoundError as e:
        raise ErroMidia(f"Programa não encontrado: {cmd[0]}") from e
    _anexar_ao_job(proc)

    erros: deque[str] = deque(maxlen=400)
    saida: list[str] = []

    def ler(fluxo, destino, callback) -> None:
        for bruto in iter(fluxo.readline, b""):
            linha = bruto.decode("utf-8", errors="replace").rstrip("\r\n")
            if destino is not None:
                destino.append(linha)
            if callback is not None:
                try:
                    callback(linha)
                except Exception:  # um callback com defeito não pode travar o processo
                    log.debug("Falha no callback de leitura", exc_info=True)
        fluxo.close()

    t_err = threading.Thread(target=ler, args=(proc.stderr, erros, ao_ler_erro), daemon=True)
    t_out = threading.Thread(
        target=ler, args=(proc.stdout, saida if capturar_saida else None, ao_ler_saida), daemon=True
    )
    t_err.start()
    t_out.start()

    inicio = time.monotonic()
    try:
        while True:
            try:
                proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if parar is not None and parar.is_set():
                    _encerrar(proc)
                    raise Interrompido()
                if timeout and time.monotonic() - inicio > timeout:
                    _encerrar(proc)
                    raise ErroMidia(f"Tempo esgotado ({int(timeout)} s) executando {Path(cmd[0]).name}")
    except KeyboardInterrupt:
        _encerrar(proc)
        raise
    t_err.join(timeout=5)
    t_out.join(timeout=5)
    return ResultadoProcesso(proc.returncode, "\n".join(saida), list(erros))


class Progresso:
    """Converte a saída de '-progress pipe:1' do FFmpeg em log periódico e progresso no painel."""

    def __init__(self, rotulo: str, total_seg: float, intervalo_seg: float = 30):
        self.rotulo = rotulo
        self.total = max(total_seg, 0.001)
        self.intervalo = intervalo_seg
        self._ultimo = time.monotonic()
        # os callbacks rodam nas threads de leitura: guarda a thread dona da tarefa
        self.chave = threading.current_thread().name
        ATIVIDADES.definir(rotulo, 0, self.chave)

    def linha_ffmpeg(self, linha: str) -> None:
        if linha.startswith(("out_time_us=", "out_time_ms=")):
            try:
                segundos = int(linha.split("=", 1)[1]) / 1_000_000
            except ValueError:
                return
            self.percentual(100 * segundos / self.total)

    def percentual(self, pct: float) -> None:
        ATIVIDADES.progresso(pct, self.chave)
        agora = time.monotonic()
        if agora - self._ultimo >= self.intervalo:
            self._ultimo = agora
            log.info("%s: %d%%", self.rotulo, max(0, min(100, int(pct))))


# ---------------------------------------------------------------- ffmpeg/ffprobe

def base_ffmpeg(ffmpeg: str) -> list[str]:
    return [ffmpeg, "-hide_banner", "-nostdin", "-y"]


def saida_binaria(cmd: Sequence, timeout: float = 60) -> bytes:
    """Roda um comando curto e devolve o stdout em bytes (ex.: pixels de uma imagem)."""
    cmd = [str(c) for c in cmd]
    log.debug("Executando: %s", subprocess.list2cmdline(cmd))
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                           creationflags=_flags_criacao(False))
    except FileNotFoundError as e:
        raise ErroMidia(f"Programa não encontrado: {cmd[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise ErroMidia(f"{Path(cmd[0]).name} demorou demais") from e
    if r.returncode != 0:
        erro = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ErroMidia(f"{Path(cmd[0]).name} falhou: {' | '.join(erro[-3:]) or r.returncode}")
    return r.stdout


def sondar(ffprobe: str, caminho: Path) -> dict:
    r = executar(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(caminho)],
        capturar_saida=True,
        baixa_prioridade=False,
        timeout=180,
    )
    if r.codigo != 0:
        raise ErroMidia(f"ffprobe falhou em {caminho.name}: {r.resumo_erro(3)}")
    try:
        return json.loads(r.saida)
    except json.JSONDecodeError as e:
        raise ErroMidia(f"Resposta inválida do ffprobe para {caminho.name}") from e


def fracao(texto: str | None, padrao: Fraction | None = None) -> Fraction | None:
    """'24000/1001' -> Fraction; valores 0, N/A ou inválidos -> padrao."""
    if not texto:
        return padrao
    try:
        valor = Fraction(str(texto).replace(":", "/"))
    except (ValueError, ZeroDivisionError):
        return padrao
    return valor if valor > 0 else padrao


def duracao_arquivo(ffprobe: str, caminho: Path) -> float:
    dados = sondar(ffprobe, caminho)
    try:
        return float(dados.get("format", {}).get("duration") or 0)
    except ValueError:
        return 0.0


def filtros_disponiveis(ffmpeg: str) -> set[str]:
    r = executar([ffmpeg, "-hide_banner", "-filters"], capturar_saida=True, baixa_prioridade=False, timeout=60)
    nomes = set()
    for linha in r.saida.splitlines():
        partes = linha.split()
        if len(partes) >= 3 and len(partes[0]) in (2, 3) and "->" in partes[2]:
            nomes.add(partes[1])
    return nomes


def versao_ffmpeg(ffmpeg: str) -> str:
    r = executar([ffmpeg, "-hide_banner", "-version"], capturar_saida=True, baixa_prioridade=False, timeout=60)
    if r.codigo != 0 or not r.saida:
        raise ErroMidia("FFmpeg não respondeu")
    return r.saida.splitlines()[0]
