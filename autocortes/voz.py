"""Narração: transforma texto em áudio, com o tempo de cada palavra.

O motor padrão é o "Read Aloud" do Microsoft Edge, o mesmo serviço que o navegador usa para
ler páginas em voz alta. Ele é gratuito, tem vozes neurais boas em português e, o mais útil
aqui, devolve o **tempo exato de cada palavra** (WordBoundary). Com isso a legenda palavra
por palavra do AutoCortes encaixa na voz sem precisar transcrever de volta.

Dois avisos honestos:
- Não é uma API pública da Microsoft, e sim o endpoint do navegador. A Microsoft já quebrou
  clientes não oficiais dele antes (o token `Sec-MS-GEC` existe para isso). Se parar de
  funcionar, o jeito é trocar de motor, não insistir.
- O texto sai do computador (vai para o serviço). Para narração de roteiro próprio isso é o
  esperado, mas está dito.

A interface é uma só (`falar`), para um motor offline entrar depois sem mexer em quem usa.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr, unescape

from .config import Config
from .legendas import Frase, Palavra, agrupar_frases
from .midia import ErroMidia, base_ffmpeg, duracao_arquivo, executar
from .util import log
from .websocket import ErroWebsocket, Websocket

__all__ = ["ErroVoz", "Narracao", "VOZES_SUGERIDAS", "falar", "validar_voz"]

# ---- constantes do serviço do Edge (o navegador usa exatamente estas)
TOKEN_CONFIAVEL = "6A5AA1D4EAFF4E9FB37E23D68491D6F4"
BASE = "speech.platform.bing.com/consumer/speech/synthesize/readaloud"
URL_WSS = f"wss://{BASE}/edge/v1?TrustedClientToken={TOKEN_CONFIAVEL}"
VERSAO_EDGE = "143.0.3650.75"
VERSAO_GEC = f"1-{VERSAO_EDGE}"
AGENTE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{VERSAO_EDGE.split('.')[0]}.0.0.0 Safari/537.36 Edg/{VERSAO_EDGE.split('.')[0]}.0.0.0"
)
ORIGEM = "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold"
FORMATO_AUDIO = "audio-24khz-48kbitrate-mono-mp3"
# o formato acima é MP3 de taxa constante: dá para converter bytes em tempo com conta exata
BITS_POR_SEGUNDO = 48_000
TICKS_POR_SEGUNDO = 10_000_000
EPOCA_WINDOWS = 11_644_473_600
LIMITE_PEDACO = 4096  # bytes de texto por conexão, como o serviço espera

# vozes neurais de pt-BR que o serviço oferece
VOZES_SUGERIDAS = (
    ("pt-BR-AntonioNeural", "Antônio (masculina)"),
    ("pt-BR-FranciscaNeural", "Francisca (feminina)"),
    ("pt-BR-ThalitaMultilingualNeural", "Thalita (feminina, multilíngue)"),
)

_CONTROLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_ESPACOS = re.compile(r"[ \t]+")
# [pausa: 2s], [pausa 1,5 s], [pausa: 500ms], [Pausa: 2 segundos], [pause]... (sem número = 1 s)
_PAUSA = re.compile(
    r"\[\s*(?:pausa|pause)\s*:?\s*(\d+(?:[.,]\d+)?)?\s*(ms|milissegundos?|s|seg|segs|segundos?|sec|secs|seconds?)?\s*\]",
    re.I,
)
# o que sobrar com cara de pausa (escrito de um jeito que eu não li) não pode ser falado em voz alta
_PAUSA_SOBRANDO = re.compile(r"\[\s*paus[ae][^\]]{0,30}\]", re.I)
# nome das vozes do serviço: pt-BR-AntonioNeural, en-US-AvaMultilingualNeural, sr-Latn-RS-NicholasNeural
_NOME_VOZ = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z]{2,4}){1,2}-[A-Za-z]+Neural$")
_PROSODIA = {"ritmo": re.compile(r"^[+-]\d{1,3}%$"), "tom": re.compile(r"^[+-]\d{1,3}Hz$"),
             "volume": re.compile(r"^[+-]\d{1,3}%$")}
# o relógio do PC pode estar errado; o serviço recusa com 403 e o ajuste vem do cabeçalho Date
_desvio_relogio = 0.0


class ErroVoz(Exception):
    def __init__(self, mensagem: str, tipo: str = "temporario"):
        super().__init__(mensagem)
        self.tipo = tipo  # temporario | config | conteudo


@dataclass
class Narracao:
    arquivo: Path
    duracao: float
    frases: list[Frase]
    voz: str

    @property
    def texto(self) -> str:
        return " ".join(f.txt for f in self.frases)


# ---------------------------------------------------------------- token e cabeçalhos

def _agora() -> float:
    return datetime.now(timezone.utc).timestamp() + _desvio_relogio


def _sec_ms_gec() -> str:
    """Token anti-abuso: hora em formato Windows, arredondada para 5 min, com SHA-256."""
    ticks = _agora() + EPOCA_WINDOWS
    ticks -= ticks % 300           # arredonda para baixo, de 5 em 5 minutos
    ticks *= TICKS_POR_SEGUNDO     # segundos -> intervalos de 100 ns
    return hashlib.sha256(f"{ticks:.0f}{TOKEN_CONFIAVEL}".encode("ascii")).hexdigest().upper()


def _acertar_relogio(cabecalhos: dict) -> bool:
    """Usa o Date do servidor para corrigir o relógio local. True se deu para acertar."""
    global _desvio_relogio

    bruto = cabecalhos.get("date")
    if not bruto:
        return False
    try:
        servidor = datetime.strptime(bruto, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return False
    _desvio_relogio += servidor - _agora()
    log.warning("O relógio do PC está %.0f s fora do horário do serviço de voz; ajustando só para esta conexão.",
                abs(_desvio_relogio))
    return True


def _data_estilo_js() -> str:
    return time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime())


def _cabecalhos_wss() -> dict:
    return {
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "Origin": ORIGEM,
        "User-Agent": AGENTE,
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": f"muid={uuid.uuid4().hex.upper()};",
    }


# ---------------------------------------------------------------- texto

def limpar(texto: str) -> str:
    """Tira o que o serviço recusa e normaliza os espaços."""
    texto = unicodedata.normalize("NFC", str(texto or ""))
    texto = _CONTROLE.sub(" ", texto)
    texto = _ESPACOS.sub(" ", texto)
    linhas = [linha.strip() for linha in texto.split("\n")]
    return "\n".join(linha for linha in linhas if linha).strip()


def validar_voz(voz: str) -> str | None:
    """Motivo para o nome da voz não servir (None = serve). Vem da pauta, escrito à mão."""
    if not _NOME_VOZ.match(str(voz or "")):
        return (f"a voz \"{voz}\" não tem o formato das vozes do serviço (ex.: pt-BR-AntonioNeural, "
                "pt-BR-FranciscaNeural)")
    return None


def _para_ssml(texto: str, voz: str, ritmo: str, tom: str, volume: str) -> str:
    """Um trecho de texto em SSML.

    Sem `<break>`: medido em set/2026, o endpoint do Read Aloud recusa o SSML inteiro com
    "SSML is invalid" quando ele aparece. As pausas são silêncio de verdade entre trechos,
    montado depois (`_juntar`). Os atributos vão escapados: o nome da voz vem da pauta.
    """
    corpo = escape(texto.replace("\n", " ").strip())
    return (
        "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='pt-BR'>"
        f"<voice name={quoteattr(voz)}>"
        f"<prosody pitch={quoteattr(tom)} rate={quoteattr(ritmo)} volume={quoteattr(volume)}>{corpo}</prosody>"
        "</voice></speak>"
    )


def _segundos_da_pausa(numero: str | None, unidade: str | None) -> float:
    if not numero:
        return 1.0
    try:
        valor = float(numero.replace(",", "."))
    except ValueError:
        return 1.0
    if unidade and unidade.lower().startswith(("ms", "mili")):
        valor /= 1000
    return min(10.0, max(0.0, valor))


def _dividir_no_limite(texto: str, limite: int = LIMITE_PEDACO) -> list[str]:
    """Quebra um trecho grande em partes que cabem numa conexão (sem pausa entre elas)."""
    restante = texto.strip()
    saida: list[str] = []
    while len(restante.encode("utf-8")) > limite:
        corte = len(restante)
        while len(restante[:corte].encode("utf-8")) > limite:
            corte -= 1
        quebra = max(restante.rfind("\n", 0, corte), restante.rfind(" ", 0, corte))
        if quebra <= 0:
            quebra = corte
        parte = restante[:quebra].strip()
        if parte:
            saida.append(parte)
        restante = restante[quebra:].lstrip()
    if restante:
        saida.append(restante)
    return saida


def blocos(texto: str, pausa_linha: float = 0.35) -> list[tuple[str, float]]:
    """O texto em trechos, com o silêncio que vem depois de cada um.

    Separadores: `[pausa: 2s]` (o silêncio que você pediu) e quebra de linha (`pausa_linha`,
    que dá o respiro entre as frases). Trecho grande é dividido sem pausa no meio.
    """
    saida: list[tuple[str, float]] = []
    for parte in re.split(r"\n+", texto):
        parte = _PAUSA_SOBRANDO.sub(lambda m: m.group(0) if _PAUSA.fullmatch(m.group(0)) else "[pausa]", parte)
        pedacos_pausa = _PAUSA.split(parte)
        # split com 2 grupos devolve [texto, número, unidade, texto, número, unidade, ...]
        for i in range(0, len(pedacos_pausa), 3):
            trecho = pedacos_pausa[i].strip()
            silencio = 0.0
            if i + 2 < len(pedacos_pausa):
                silencio = _segundos_da_pausa(pedacos_pausa[i + 1], pedacos_pausa[i + 2])
            if not trecho:
                if silencio and saida:  # pausa sozinha na linha: soma na anterior
                    saida[-1] = (saida[-1][0], saida[-1][1] + silencio)
                continue
            partes = _dividir_no_limite(trecho)
            for n, p in enumerate(partes):
                saida.append((p, silencio if n == len(partes) - 1 else 0.0))
        if saida:  # fim de linha: respiro
            saida[-1] = (saida[-1][0], max(saida[-1][1], pausa_linha))
    if saida:  # a última não precisa de silêncio no fim
        saida[-1] = (saida[-1][0], 0.0)
    return saida


# ---------------------------------------------------------------- Edge

def _conf(cfg: Config) -> dict:
    return cfg["voz"]


def _cabecalho_e_corpo(dados: bytes, tamanho: int) -> tuple[dict, bytes]:
    cabecalhos = {}
    for linha in dados[:tamanho].split(b"\r\n"):
        nome, sep, valor = linha.partition(b":")
        if sep:
            cabecalhos[nome.strip().lower()] = valor.strip()
    return cabecalhos, dados[tamanho + 2:]


def _falar_pedaco(texto: str, voz: str, ritmo: str, tom: str, volume: str,
                  tempo_limite: float) -> tuple[bytes, list[Palavra]]:
    """Uma conexão: devolve o MP3 e as palavras com tempo (em segundos, desde o começo do trecho)."""
    url = (f"{URL_WSS}&ConnectionId={uuid.uuid4().hex}"
           f"&Sec-MS-GEC={_sec_ms_gec()}&Sec-MS-GEC-Version={VERSAO_GEC}")
    ws = Websocket(url, timeout=tempo_limite, cabecalhos=_cabecalhos_wss(), rotulo="o serviço de voz")
    audio = bytearray()
    palavras: list[Palavra] = []
    try:
        ws.enviar(
            f"X-Timestamp:{_data_estilo_js()}\r\n"
            "Content-Type:application/json; charset=utf-8\r\n"
            "Path:speech.config\r\n\r\n"
            '{"context":{"synthesis":{"audio":{"metadataoptions":{'
            '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"true"},'
            f'"outputFormat":"{FORMATO_AUDIO}"' "}}}}\r\n"
        )
        ws.enviar(
            f"X-RequestId:{uuid.uuid4().hex}\r\n"
            "Content-Type:application/ssml+xml\r\n"
            f"X-Timestamp:{_data_estilo_js()}Z\r\n"  # o Z sobrando é como o Edge manda
            "Path:ssml\r\n\r\n"
            f"{_para_ssml(texto, voz, ritmo, tom, volume)}"
        )
        while True:
            tipo, dados = ws.receber_quadro()
            if tipo == 0x1:  # texto: metadados e controle
                cabeca, _, corpo = dados.partition(b"\r\n\r\n")
                caminho = b""
                for linha in cabeca.split(b"\r\n"):
                    if linha.lower().startswith(b"path:"):
                        caminho = linha.partition(b":")[2].strip()
                if caminho == b"audio.metadata":
                    for meta in json.loads(corpo).get("Metadata") or []:
                        if meta.get("Type") != "WordBoundary":
                            continue
                        d = meta.get("Data") or {}
                        ini = int(d.get("Offset") or 0) / TICKS_POR_SEGUNDO
                        dur = int(d.get("Duration") or 0) / TICKS_POR_SEGUNDO
                        txt = unescape(str(((d.get("text") or {}).get("Text")) or "")).strip()
                        if txt:
                            palavras.append(Palavra(round(ini, 3), round(ini + dur, 3), txt))
                elif caminho == b"turn.end":
                    break
            else:  # binário: pedaço de MP3
                if len(dados) < 2:
                    continue
                tamanho = int.from_bytes(dados[:2], "big")
                if tamanho > len(dados):
                    continue
                cabecalhos, corpo = _cabecalho_e_corpo(dados, tamanho)
                if cabecalhos.get(b"path") == b"audio" and corpo:
                    audio += corpo
    finally:
        ws.fechar()
    return bytes(audio), palavras


def _juntar(cfg: Config, partes: list[Path], silencios: list[float], destino: Path) -> None:
    """Junta os trechos num MP3 só, com o silêncio pedido entre eles."""
    entradas: list[str] = []
    filtros: list[str] = []
    rotulos: list[str] = []
    n = 0
    for i, arquivo in enumerate(partes):
        entradas += ["-i", str(arquivo)]
        filtros.append(f"[{i}:a]aresample=24000,aformat=sample_fmts=fltp:channel_layouts=mono[a{i}]")
        rotulos.append(f"[a{i}]")
        espera = silencios[i] if i < len(silencios) else 0.0
        if espera > 0.001 and i < len(partes) - 1:
            filtros.append(f"anullsrc=r=24000:cl=mono,atrim=duration={espera:.3f},"
                           f"aformat=sample_fmts=fltp:channel_layouts=mono[s{n}]")
            rotulos.append(f"[s{n}]")
            n += 1
    filtros.append("".join(rotulos) + f"concat=n={len(rotulos)}:v=0:a=1[saida]")
    parcial = destino.with_name(destino.stem + ".parcial.mp3")
    r = executar(
        base_ffmpeg(cfg["ferramentas"]["ffmpeg"]) + ["-loglevel", "error", *entradas,
                                                     "-filter_complex", ";".join(filtros),
                                                     "-map", "[saida]", "-c:a", "libmp3lame", "-b:a", "48k",
                                                     "-ar", "24000", "-ac", "1", str(parcial)],
        timeout=300,
    )
    if r.codigo != 0 or not parcial.exists():
        parcial.unlink(missing_ok=True)
        raise ErroVoz(f"não consegui juntar os trechos da narração: {r.resumo_erro(3)}")
    parcial.replace(destino)


def falar(cfg: Config, texto: str, destino: Path, voz: str | None = None,
          ritmo: str | None = None) -> Narracao:
    """Grava a narração em `destino` (.mp3) e devolve as frases com o tempo de cada palavra."""
    conf = _conf(cfg)
    motor = str(conf.get("motor") or "edge")
    if motor != "edge":
        raise ErroVoz(f"motor de voz '{motor}' ainda não existe (use 'edge')", "config")

    limpo = limpar(texto)
    if not limpo:
        raise ErroVoz("não há texto para narrar", "conteudo")
    voz = str(voz or conf.get("voz") or VOZES_SUGERIDAS[0][0]).strip()
    ritmo = str(ritmo if ritmo is not None else conf.get("ritmo") or "+0%").strip()
    tom = str(conf.get("tom") or "+0Hz").strip()
    volume = str(conf.get("volume") or "+0%").strip()
    problema = validar_voz(voz)
    if problema:
        raise ErroVoz(problema, "conteudo")
    for nome, valor in (("ritmo", ritmo), ("tom", tom), ("volume", volume)):
        if not _PROSODIA[nome].match(valor):
            raise ErroVoz(f"[voz].{nome} = \"{valor}\" não vale (ex.: {'+0Hz' if nome == 'tom' else '+10%'})",
                          "config")
    tempo_limite = float(conf.get("tempo_limite_seg") or 120)
    pausa_linha = float(conf.get("pausa_linha_seg") or 0.35)

    destino.parent.mkdir(parents=True, exist_ok=True)
    trechos = blocos(limpo, pausa_linha)
    inicio = time.monotonic()
    temporarios: list[Path] = []
    silencios: list[float] = []
    palavras: list[Palavra] = []
    deslocamento = 0.0
    ffprobe = cfg["ferramentas"]["ffprobe"]

    def pedaco(trecho: str) -> tuple[bytes, list[Palavra]]:
        """Um trecho, com uma nova tentativa quando o relógio do PC estava fora de hora."""
        try:
            try:
                return _falar_pedaco(trecho, voz, ritmo, tom, volume, tempo_limite)
            except ErroWebsocket as e:
                if e.status == 403 and _acertar_relogio(e.cabecalhos):
                    return _falar_pedaco(trecho, voz, ritmo, tom, volume, tempo_limite)
                raise
        except ErroWebsocket as e:
            if e.status == 401:
                raise ErroVoz("o serviço de voz recusou o pedido (401). Se continuar, troque de motor.",
                              "config") from e
            raise ErroVoz(f"falha na narração: {e}") from e
        except (ValueError, OSError) as e:  # resposta torta ou conexão caída no meio
            raise ErroVoz(f"falha na narração ({e.__class__.__name__}: {e})") from e

    def medir(arquivo: Path, reserva: float) -> float:
        try:
            return duracao_arquivo(ffprobe, arquivo) or reserva
        except ErroMidia:
            return reserva

    try:
        for n, (trecho, silencio) in enumerate(trechos):
            audio, parte_palavras = pedaco(trecho)
            if not audio:
                raise ErroVoz("o serviço de voz não devolveu áudio")
            parte = destino.with_name(f"{destino.stem}.parte{n:03d}.mp3")
            try:
                parte.write_bytes(audio)
            except OSError as e:
                raise ErroVoz(f"não consegui gravar a narração ({e})") from e
            temporarios.append(parte)
            silencios.append(silencio)
            # MP3 de taxa constante: os bytes dão o tempo quando o ffprobe não consegue
            duracao_parte = medir(parte, len(audio) * 8 / BITS_POR_SEGUNDO)
            for p in parte_palavras:
                palavras.append(Palavra(round(p.ini + deslocamento, 3), round(p.fim + deslocamento, 3), p.txt))
            deslocamento += duracao_parte + silencio

        if len(temporarios) == 1 and silencios[0] <= 0.001:
            temporarios[0].replace(destino)
            temporarios.clear()
        else:
            _juntar(cfg, temporarios, silencios, destino)
    finally:
        for p in temporarios:
            p.unlink(missing_ok=True)

    duracao = medir(destino, deslocamento)
    frases = agrupar_frases(palavras) if palavras else []
    log.info("Narração de %.1f s em %.0f s (%d palavras em %d trecho(s), voz %s)", duracao,
             time.monotonic() - inicio, len(palavras), len(trechos), voz)
    return Narracao(arquivo=destino, duracao=round(duracao, 3), frases=frases, voz=voz)
