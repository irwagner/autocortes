"""Análise de um filme: metadados, tarjas pretas, cenas, volume, fala e legendas.

Cada etapa grava o resultado em dados/analise/<id>/, então uma análise
interrompida continua de onde parou.
"""

from __future__ import annotations

import re
import shutil
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from . import ferramentas
from .config import Config
from .legendas import (
    Frase,
    agrupar_frases,
    ajustar_a_fala,
    carregar_transcricao,
    ler_srt,
    ler_whisper_json,
    ler_whisper_srt,
    salvar_transcricao,
)
from .midia import (
    ErroMidia,
    Interrompido,
    Progresso,
    base_ffmpeg,
    caminho_filtro,
    caminho_relativo,
    executar,
    fracao,
    sondar,
)
from .textos import ler_metadados
from .util import ler_json, log, salvar_json

PASSO_VOLUME = 0.5  # segundos por amostra de volume
LEGENDAS_TEXTO = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}


@dataclass
class Analise:
    info: dict
    cenas: list[float]
    volume: list[float]
    falas: list[list[float]]
    frases: list[Frase]


def pasta_do_filme(cfg: Config, filme_id: int) -> Path:
    return cfg.pasta_analise / str(filme_id)


# ---------------------------------------------------------------- metadados

def _idioma(stream: dict) -> str:
    return str((stream.get("tags") or {}).get("language") or "").lower()


def _idioma_de(s: dict) -> str:
    """Idioma de uma faixa do ffprobe ou de uma já resumida no info.json."""
    return str(s["idioma"]).lower() if "idioma" in s else _idioma(s)


def _casa(lang: str, pref: str) -> bool:
    return bool(lang) and (lang == pref or lang.startswith(pref + "-") or pref.startswith(lang + "-"))


def _por_idioma(streams: list[dict], idiomas: list[str]) -> dict | None:
    """A primeira faixa no idioma preferido; depois compara só a base do idioma ("en" acha "eng")."""
    prefs = [str(i).lower() for i in idiomas]
    for pref in prefs:
        for s in streams:
            if _casa(_idioma_de(s), pref):
                return s
    for pref in prefs:
        base = _idioma_base(pref)
        for s in streams:
            if base and _idioma_base(_idioma_de(s)) == base:
                return s
    return None


def resumo_midia(cfg: Config, caminho: Path) -> dict:
    dados = sondar(cfg["ferramentas"]["ffprobe"], caminho)
    streams = dados.get("streams", [])
    videos = [
        s for s in streams
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
    ]
    if not videos:
        raise ErroMidia("o arquivo não tem faixa de vídeo")
    v = videos[0]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    audio = (
        _por_idioma(audios, cfg["analise"]["idiomas_audio"])
        or next((s for s in audios if (s.get("disposition") or {}).get("default")), None)
        or (audios[0] if audios else None)
    )

    duracao = 0.0
    for fonte in (dados.get("format", {}).get("duration"), v.get("duration")):
        try:
            duracao = float(fonte)
            if duracao > 0:
                break
        except (TypeError, ValueError):
            continue
    if duracao <= 0:
        raise ErroMidia("não foi possível descobrir a duração do vídeo")

    fps = fracao(v.get("avg_frame_rate")) or fracao(v.get("r_frame_rate")) or Fraction(24)
    if not 23 <= fps <= 60:
        fps = Fraction(30)
    sar = fracao(v.get("sample_aspect_ratio"), Fraction(1)) or Fraction(1)

    legendas = []
    for s in streams:
        if s.get("codec_type") != "subtitle":
            continue
        titulo = str((s.get("tags") or {}).get("title") or "")
        legendas.append(
            {
                "indice": s["index"],
                "codec": s.get("codec_name"),
                "idioma": _idioma(s),
                "forcada": bool((s.get("disposition") or {}).get("forced"))
                or bool(re.search(r"forced|for[çc]ad", titulo, re.I)),
                "titulo": titulo[:80],
            }
        )

    return {
        "duracao": duracao,
        "video": {
            "indice": v["index"],
            "largura": int(v.get("width") or 0),
            "altura": int(v.get("height") or 0),
            "sar": [sar.numerator, sar.denominator],
            "fps": f"{fps.numerator}/{fps.denominator}",
        },
        "audio": (
            {"indice": audio["index"], "canais": audio.get("channels"), "idioma": _idioma(audio),
             "original": bool((audio.get("disposition") or {}).get("original")),
             "dub": bool((audio.get("disposition") or {}).get("dub"))}
            if audio
            else None
        ),
        # todas as faixas de áudio: com mais de um idioma, a escolhida pode ser uma dublagem
        "audios": [_resumo_audio(s) for s in audios],
        "legendas": legendas,
    }


def _resumo_audio(s: dict) -> dict:
    d = s.get("disposition") or {}
    return {"indice": s["index"], "idioma": _idioma(s), "original": bool(d.get("original")), "dub": bool(d.get("dub")),
            # comentário e audiodescrição não contam como outro idioma do filme
            "extra": bool(d.get("comment") or d.get("visual_impaired") or d.get("hearing_impaired"))}


def completar_info(cfg: Config, caminho: Path, info: dict) -> bool:
    """Análise antiga, sem a lista de áudios: completa só isso (é um ffprobe) para a regra do dublado."""
    if "audios" in info:
        return False
    novo = resumo_midia(cfg, caminho)
    info["audios"] = novo["audios"]
    audio = info.get("audio")
    escolhido = next((a for a in novo["audios"] if audio and a["indice"] == audio.get("indice")), None)
    if audio and escolhido:  # a faixa escolhida continua a mesma (volume e voz saíram dela)
        audio["original"], audio["dub"] = escolhido["original"], escolhido["dub"]
    titulos = {s["indice"]: s.get("titulo", "") for s in novo["legendas"]}
    for s in info.get("legendas", []):
        s.setdefault("titulo", titulos.get(s["indice"], ""))
    return True


def detectar_tarjas(cfg: Config, caminho: Path, info: dict, parar: threading.Event | None) -> list[int]:
    """Área útil da imagem [w, h, x, y], sem as tarjas pretas embutidas."""
    v = info["video"]
    largura, altura = v["largura"], v["altura"]
    x1, y1, x2, y2 = largura, altura, 0, 0
    achou = False
    for fracao_tempo in (0.12, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.82):
        t = info["duracao"] * fracao_tempo
        r = executar(
            base_ffmpeg(cfg["ferramentas"]["ffmpeg"])
            + ["-ss", f"{t:.2f}", "-i", str(caminho), "-map", f"0:{v['indice']}", "-an", "-sn", "-dn",
               "-frames:v", "24", "-vf", "cropdetect=limit=24:round=2:reset=0", "-f", "null", "-"],
            parar=parar,
            baixa_prioridade=cfg["geral"]["prioridade_baixa"],
            timeout=300,
        )
        achados = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", "\n".join(r.erros))
        if not achados:
            continue
        w, h, x, y = map(int, achados[-1])
        if w < largura * 0.3 or h < altura * 0.3:
            continue  # quadro quase todo preto
        achou = True
        x1, y1, x2, y2 = min(x1, x), min(y1, y), max(x2, x + w), max(y2, y + h)
    if not achou:
        return [largura, altura, 0, 0]
    w, h = x2 - x1, y2 - y1
    if w >= largura * 0.97 and h >= altura * 0.97:
        return [largura, altura, 0, 0]
    return [w - w % 2, h - h % 2, x1, y1]


# ---------------------------------------------------------------- sinais

def _comando_analise(cfg: Config) -> list[str]:
    return base_ffmpeg(cfg["ferramentas"]["ffmpeg"]) + ["-loglevel", "error", "-progress", "pipe:1", "-nostats"]


def detectar_cenas(cfg: Config, caminho: Path, info: dict, pasta: Path, titulo: str, parar) -> list[float]:
    destino = pasta / "cenas.json"
    cache = ler_json(destino)
    if cache is not None:
        return cache
    bruto = pasta / "cenas.txt"
    limiar = cfg["analise"]["limiar_cena"]
    filtro = f"scale=320:-2,scdet=threshold={limiar}:sc_pass=1,metadata=mode=print:file={caminho_filtro(bruto)}"
    hwaccel = str(cfg["ferramentas"]["hwaccel"] or "none")
    tentativas = ([hwaccel] if hwaccel.lower() != "none" else []) + [None]
    for hw in tentativas:
        cmd = _comando_analise(cfg) + (["-hwaccel", hw] if hw else []) + [
            "-i", str(caminho), "-map", f"0:{info['video']['indice']}", "-an", "-sn", "-dn",
            "-vf", filtro, "-f", "null", "-",
        ]
        prog = Progresso(f"Detectando cenas de '{titulo}'", info["duracao"])
        r = executar(cmd, parar=parar, ao_ler_saida=prog.linha_ffmpeg, baixa_prioridade=cfg["geral"]["prioridade_baixa"])
        if r.codigo == 0:
            break
        log.warning("Detecção de cenas falhou%s: %s", " (hwaccel)" if hw else "", r.resumo_erro(3))
    else:
        raise ErroMidia("não foi possível detectar as cenas")
    texto = bruto.read_text(encoding="utf-8", errors="replace") if bruto.exists() else ""
    tempos = sorted({round(float(t), 3) for t in re.findall(r"lavfi\.scd\.time=([\d.]+)", texto)})
    salvar_json(destino, tempos)
    bruto.unlink(missing_ok=True)
    return tempos


def medir_volume(cfg: Config, caminho: Path, info: dict, pasta: Path, parar) -> list[float]:
    """Volume RMS (dBFS) a cada 0,5 s."""
    destino = pasta / "volume.json"
    cache = ler_json(destino)
    if cache is not None:
        return cache
    if not info.get("audio"):
        return []
    bruto = pasta / "volume.txt"
    amostras = int(16000 * PASSO_VOLUME)
    filtro = (
        f"aresample=16000,aformat=channel_layouts=mono,asetnsamples=n={amostras}:p=0,"
        "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,"
        f"ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file={caminho_filtro(bruto)}"
    )
    cmd = _comando_analise(cfg) + [
        "-i", str(caminho), "-map", f"0:{info['audio']['indice']}", "-vn", "-sn", "-dn",
        "-af", filtro, "-f", "null", "-",
    ]
    r = executar(cmd, parar=parar, baixa_prioridade=cfg["geral"]["prioridade_baixa"])
    if r.codigo != 0:
        raise ErroMidia(f"falha ao medir o volume: {r.resumo_erro(3)}")
    n = int(info["duracao"] / PASSO_VOLUME) + 1
    valores = [-90.0] * n
    indice = None
    for linha in bruto.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.search(r"pts_time:([\d.]+)", linha)
        if m:
            indice = int(round(float(m.group(1)) / PASSO_VOLUME))
            continue
        if "RMS_level=" in linha and indice is not None and 0 <= indice < n:
            try:
                valores[indice] = max(-90.0, float(linha.split("=", 1)[1]))
            except ValueError:
                valores[indice] = -90.0  # -inf / nan = silêncio digital
    salvar_json(destino, [round(v, 2) for v in valores])
    bruto.unlink(missing_ok=True)
    return valores


def extrair_wav(cfg: Config, caminho: Path, info: dict, pasta: Path, parar) -> Path:
    wav = pasta / "audio16k.wav"
    if wav.exists() and wav.stat().st_size > 1000:
        return wav
    parcial = pasta / "audio16k.parcial.wav"
    cmd = _comando_analise(cfg) + [
        "-i", str(caminho), "-map", f"0:{info['audio']['indice']}", "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(parcial),
    ]
    r = executar(cmd, parar=parar, baixa_prioridade=cfg["geral"]["prioridade_baixa"])
    if r.codigo != 0:
        parcial.unlink(missing_ok=True)
        raise ErroMidia(f"falha ao extrair o áudio: {r.resumo_erro(3)}")
    parcial.replace(wav)
    return wav


def detectar_fala(cfg: Config, wav: Path, pasta: Path, parar) -> list[list[float]]:
    """Trechos com voz humana (Silero VAD do whisper.cpp)."""
    destino = pasta / "fala.json"
    cache = ler_json(destino)
    if cache is not None:
        return cache
    _, vad_exe = ferramentas.garantir_whisper(cfg)
    modelo_vad = ferramentas.garantir_vad(cfg)
    r = executar(
        [vad_exe, "-vm", caminho_relativo(modelo_vad, pasta), "-f", wav.name,
         "-t", str(ferramentas.threads(cfg)), "-np"],
        cwd=pasta,
        parar=parar,
        capturar_saida=True,
        baixa_prioridade=cfg["geral"]["prioridade_baixa"],
    )
    if r.codigo != 0:
        raise ErroMidia(f"detecção de voz falhou: {r.resumo_erro(3)}")
    falas = [
        [round(float(a) / 100, 2), round(float(b) / 100, 2)]
        for a, b in re.findall(r"start\s*=\s*([\d.]+),\s*end\s*=\s*([\d.]+)", r.saida)
    ]
    salvar_json(destino, falas)
    return falas


# ---------------------------------------------------------------- legendas

# códigos de idioma que aparecem no nome das legendas ("Filme.pt-BR.srt", "Filme.en.srt", "Filme.PTBR.srt")
_PT_BR = {"pb", "ptb", "pt_br", "ptbr", "br", "portugues", "português", "brazilian", "pob"}
_CODIGOS_IDIOMA = _PT_BR | {
    "pt", "por", "pt-br", "pt-pt", "portuguese", "en", "eng", "en-us", "en-gb", "english", "es", "spa", "esp",
    "es-419", "es-la", "lat", "spanish", "espanol", "español", "fr", "fre", "fra", "french", "de", "ger", "deu",
    "german", "it", "ita", "italian", "ja", "jpn", "japanese", "ko", "kor", "korean", "zh", "chi", "zho",
    "chinese", "ru", "rus", "russian", "nl", "dut", "nld", "pl", "pol", "tr", "tur", "ar", "ara", "he", "heb",
}
# o que mais pode vir no nome de uma legenda do próprio vídeo ("Filme.pt-BR.sdh.srt")
_MARCAS_LEGENDA = {"sdh", "cc", "hi", "full", "completa", "completo", "legenda", "legendas", "legendado", "sub", "subs",
                   "subtitle", "subtitles", "default", "normal", "dialogo", "diálogo", "dialog"}


def procurar_srt(caminho_filme: Path, idiomas: list[str]) -> Path | None:
    """A legenda .srt deste vídeo no idioma preferido (ou sem idioma no nome)."""
    raiz = caminho_filme.stem.lower()
    prefs = [str(i).lower() for i in idiomas]
    melhor, nota_melhor = None, -1
    for arquivo in caminho_filme.parent.iterdir():
        nome = arquivo.stem.lower()
        if arquivo.suffix.lower() != ".srt" or not nome.startswith(raiz):
            continue
        resto = nome[len(raiz):]
        if resto and resto[0] not in "._- ":
            continue  # é de outro vídeo: "Toy.Story.2.srt" não serve para "Toy.Story", nem "S01E10" para "S01E1"
        if re.search(r"forced|for[çc]ad", resto):
            continue
        tokens = [t for t in re.split(r"[._\s]+", resto.strip("._- ")) if t]
        if any(t not in _CODIGOS_IDIOMA and t not in _MARCAS_LEGENDA for t in tokens):
            continue  # sobra algo que não é idioma nem marca de legenda: "Toy.Story.2.pt-BR.srt" é de outro filme
        codigos = [("pt-br" if t in _PT_BR else t) for t in tokens if t in _CODIGOS_IDIOMA]
        if not codigos:
            nota = 1  # "Filme.srt": sem idioma no nome
        else:
            nota = -1
            for i, pref in enumerate(prefs):
                if any(_casa(c, pref) or _idioma_base(c) == _idioma_base(pref) for c in codigos):
                    nota = 100 - i
                    break
            if nota < 0:
                continue  # legenda em outro idioma ("Filme.en.srt"): melhor a embutida ou o Whisper
        if nota > nota_melhor:
            melhor, nota_melhor = arquivo, nota
    return melhor


def transcrever(cfg: Config, wav: Path, pasta: Path, idioma: str, falas, titulo: str, parar) -> tuple[list[Frase], str | None]:
    cli, _ = ferramentas.garantir_whisper(cfg)
    modelo = ferramentas.garantir_modelo(cfg)
    modelo_vad = ferramentas.garantir_vad(cfg)
    for antigo in ("whisper.json", "whisper.srt"):
        (pasta / antigo).unlink(missing_ok=True)

    prog = Progresso(f"Transcrevendo '{titulo}'", 100, intervalo_seg=60)
    detectado: list[str] = []

    def ler_erro(linha: str) -> None:
        m = re.search(r"progress\s*=\s*(\d+)%", linha)
        if m:
            prog.percentual(int(m.group(1)))
        m = re.search(r"auto-detected language:\s*([a-z]{2,3})", linha)
        if m:
            detectado.append(m.group(1))

    cmd = [
        cli, "-m", caminho_relativo(modelo, pasta), "-f", wav.name, "-l", idioma,
        "-t", str(ferramentas.threads(cfg)), "--vad", "-vm", caminho_relativo(modelo_vad, pasta),
        "-sns", "-ml", "1", "-sow", "-oj", "-osrt", "-of", "whisper", "-pp",
    ]
    log.info("Transcrevendo '%s' com o modelo %s (pode demorar)", titulo, modelo.stem)
    r = executar(cmd, cwd=pasta, parar=parar, ao_ler_erro=ler_erro, baixa_prioridade=cfg["geral"]["prioridade_baixa"])
    if r.codigo != 0:
        raise ErroMidia(f"whisper falhou: {r.resumo_erro(4)}")
    try:
        palavras = ler_whisper_json(pasta / "whisper.json")
        dados = ler_json(pasta / "whisper.json", {}) or {}
        idioma_final = (dados.get("result") or {}).get("language") or (detectado[-1] if detectado else None)
    except (OSError, ValueError):
        log.warning("JSON do whisper ilegível; usando o SRT")
        palavras = ler_whisper_srt(pasta / "whisper.srt")
        idioma_final = detectado[-1] if detectado else None
    palavras = ajustar_a_fala(palavras, [tuple(f) for f in falas])
    return agrupar_frases(palavras), idioma_final


_IDIOMA_BASE = {"por": "pt", "pob": "pt", "ptb": "pt", "pb": "pt", "eng": "en", "spa": "es", "esp": "es",
                "fre": "fr", "fra": "fr", "ger": "de", "deu": "de", "ita": "it", "jpn": "ja", "kor": "ko", "chi": "zh",
                "zho": "zh", "rus": "ru", "dut": "nl", "nld": "nl", "pol": "pl", "tur": "tr", "ara": "ar", "heb": "he"}


def _idioma_base(codigo) -> str:
    base = str(codigo or "").lower().split("-")[0].split("_")[0]
    return _IDIOMA_BASE.get(base, base)


_DUBLADO_NO_NOME = re.compile(r"(?<![a-z])dub(?:lado|bed)?(?![a-z])", re.I)


def audio_dublado(info: dict, legenda: dict, nome_arquivo: str = "") -> bool:
    """O áudio escolhido parece uma dublagem no mesmo idioma da legenda (ex.: arquivo "Dual" com por e eng)."""
    audio = info.get("audio") or {}
    idioma = _idioma_base(audio.get("idioma"))
    if idioma in ("", "und") or audio.get("original") or _idioma_base(legenda.get("idioma")) != idioma:
        return False
    if audio.get("dub"):
        return True
    # comentário e audiodescrição não contam; "zxx", "mul" e "mis" não são idiomas de verdade
    faixas = [a for a in info.get("audios") or [] if not a.get("extra")]
    idiomas = {_idioma_base(a.get("idioma")) for a in faixas} - {"", "und", "zxx", "mul", "mis"}
    return len(idiomas) >= 2 or bool(_DUBLADO_NO_NOME.search(nome_arquivo))


def _regiao(s: dict) -> int:
    """Desempate entre faixas do mesmo idioma: português do Brasil antes do de Portugal."""
    titulo = str(s.get("titulo") or "").lower()
    if re.search(r"bras|brazil|\bbr\b|pt-br|ptbr", titulo):
        return 0
    return 2 if re.search(r"portugal|europe|pt-pt", titulo) else 1


def _candidatas(faixas: list[dict], idiomas: list[str], idioma_audio: str) -> list[dict]:
    """Legendas de texto na ordem de preferência: os idiomas da lista, depois o do áudio."""
    prefs = [str(i).lower() for i in idiomas]
    if idioma_audio and idioma_audio != "und":
        prefs.append(idioma_audio.lower())
    ordem: list[dict] = []
    for pref in prefs:
        exatas = [s for s in faixas if _casa(_idioma_de(s), pref)]
        mesma_base = [s for s in faixas if _idioma_base(_idioma_de(s)) == _idioma_base(pref)]
        for s in sorted(exatas, key=_regiao) + sorted(mesma_base, key=_regiao):
            if s not in ordem:
                ordem.append(s)
    return ordem


def _completa(frases: list[Frase], falas) -> bool:
    """Tem falas para o filme todo? Uma faixa só com os letreiros (forçada sem a marca) tem poucas."""
    voz = sum(max(0.0, float(f[1]) - float(f[0])) for f in falas or [])
    return voz < 120 or len(frases) >= voz / 60 * 2  # pelo menos 2 falas por minuto de voz


def _extrair_embutida(cfg: Config, caminho: Path, faixa: dict, pasta: Path, parar) -> list[Frase]:
    destino = pasta / "legenda_embutida.srt"
    r = executar(
        base_ffmpeg(cfg["ferramentas"]["ffmpeg"])
        + ["-loglevel", "error", "-i", str(caminho), "-map", f"0:{faixa['indice']}", "-c:s", "srt", str(destino)],
        parar=parar,
        baixa_prioridade=cfg["geral"]["prioridade_baixa"],
    )
    if r.codigo != 0 or not destino.exists():
        log.warning("Não consegui extrair a legenda embutida (faixa %s): %s", faixa["indice"], r.resumo_erro(2))
        return []
    return ler_srt(destino)


def obter_legendas(cfg: Config, caminho: Path, info: dict, pasta: Path, wav, falas, meta: dict,
                   titulo: str, parar) -> tuple[list[Frase], str, str | None]:
    """(frases, de onde vieram, idioma). wav é o áudio em 16 kHz ou uma função que extrai na hora."""
    fonte = cfg["transcricao"]["fonte"]
    if str(meta.get("fonte") or "") in ("auto", "arquivo", "whisper", "nenhuma"):
        fonte = str(meta["fonte"])  # escolhida só para este filme, no <filme>.json
    idiomas = list(cfg["transcricao"]["idiomas_legenda"])
    if fonte == "nenhuma":
        return [], "nenhuma", None

    descartada = None
    if fonte in ("auto", "arquivo"):
        srt = None
        if meta.get("legenda"):
            srt = (caminho.parent / str(meta["legenda"])).resolve()
            if not srt.exists():
                log.warning("Legenda indicada nos metadados não existe: %s", srt)
                srt = None
        srt = srt or procurar_srt(caminho, idiomas)
        if srt:
            frases = ler_srt(srt)
            if frases:
                log.info("Usando a legenda %s", srt.name)
                return frases, f"arquivo:{srt.name}", None

        idioma_audio = (info.get("audio") or {}).get("idioma") or ""
        texto = [s for s in info.get("legendas", []) if s["codec"] in LEGENDAS_TEXTO and not s["forcada"]]
        for faixa in _candidatas(texto, idiomas, idioma_audio):
            if fonte == "auto" and info.get("audio") and audio_dublado(info, faixa, caminho.name):
                # a legenda embutida costuma ser a tradução do áudio original, e a dublagem fala outras palavras
                log.info("Áudio dublado (%s): a legenda embutida não acompanha a dublagem, então a fala vai ser "
                         "transcrita", idioma_audio)
                descartada = faixa
                break
            frases = _extrair_embutida(cfg, caminho, faixa, pasta, parar)
            if frases and _completa(frases, falas):
                log.info("Usando a legenda embutida (faixa %s, %s)", faixa["indice"], faixa["idioma"] or "?")
                return frases, f"embutida:{faixa['indice']}", faixa["idioma"] or None
            if frases:
                log.info("A legenda embutida %s tem só %d falas (parece só os letreiros): procurando outra",
                         faixa["indice"], len(frases))
        if fonte == "arquivo":
            return [], "nenhuma", None

    audio = wav() if callable(wav) else wav
    if audio is None:
        return [], "nenhuma", None
    idioma = str(meta.get("idioma") or cfg["transcricao"]["idioma"] or "auto")
    try:
        frases, idioma_final = transcrever(cfg, audio, pasta, idioma, falas, titulo, parar)
    except ferramentas.ErroDownload:
        raise  # sem internet para baixar o modelo: tenta de novo mais tarde
    except ErroMidia as e:
        if descartada is not None:  # melhor a legenda do original do que nenhuma
            frases = _extrair_embutida(cfg, caminho, descartada, pasta, parar)
            if frases:
                log.warning("A transcrição falhou (%s): usando a legenda embutida, que pode não bater com a "
                            "dublagem", e)
                return frases, f"embutida:{descartada['indice']}", descartada["idioma"] or None
        raise
    return frases, "whisper", idioma_final


# ---------------------------------------------------------------- orquestração

def analisar(cfg: Config, filme, parar: threading.Event | None = None) -> Analise:
    caminho = Path(filme["caminho"])
    if not caminho.exists():
        raise ErroMidia(f"arquivo não encontrado: {caminho}")
    pasta = pasta_do_filme(cfg, filme["id"])
    pasta.mkdir(parents=True, exist_ok=True)
    titulo = filme["titulo"]
    meta = ler_metadados(caminho)
    tamanho = caminho.stat().st_size

    info = ler_json(pasta / "info.json")
    if not info or info.get("tamanho") != tamanho:
        if info:
            log.info("O arquivo de '%s' mudou; refazendo a análise", titulo)
            shutil.rmtree(pasta, ignore_errors=True)
            pasta.mkdir(parents=True, exist_ok=True)
        log.info("Analisando '%s': metadados e tarjas pretas", titulo)
        info = resumo_midia(cfg, caminho)
        info["tamanho"] = tamanho
        info["crop"] = detectar_tarjas(cfg, caminho, info, parar)
        salvar_json(pasta / "info.json", info)
    else:
        try:
            if completar_info(cfg, caminho, info):
                salvar_json(pasta / "info.json", info)
        except ErroMidia as e:
            log.warning("Não consegui completar a análise antiga de '%s': %s", titulo, e)

    cenas = detectar_cenas(cfg, caminho, info, pasta, titulo, parar)
    volume = medir_volume(cfg, caminho, info, pasta, parar)

    wav = None

    def obter_wav() -> Path | None:
        """Extrai o áudio só quando precisa (a legenda embutida não precisa dele)."""
        nonlocal wav
        if wav is None and info.get("audio"):
            wav = extrair_wav(cfg, caminho, info, pasta, parar)
        return wav

    falas: list[list[float]] = ler_json(pasta / "fala.json") or []
    frases, meta_transcricao = carregar_transcricao(pasta / "transcricao.json")
    try:
        precisa_fala = not (pasta / "fala.json").exists()
        precisa_texto = not meta_transcricao
        if info.get("audio") and precisa_fala:
            try:
                falas = detectar_fala(cfg, obter_wav(), pasta, parar)
            except ferramentas.ErroDownload:
                raise  # problema de rede: o filme volta para a fila e tenta mais tarde
            except ErroMidia as e:
                log.warning("Sem detecção de voz para '%s': %s", titulo, e)
                falas = []
        if precisa_texto:
            try:
                frases, fonte, idioma = obter_legendas(cfg, caminho, info, pasta, obter_wav, falas, meta, titulo,
                                                       parar)
            except ferramentas.ErroDownload:
                raise
            except ErroMidia as e:
                log.warning("Sem legendas para '%s': %s", titulo, e)
                frases, fonte, idioma = [], "nenhuma (erro)", None
            salvar_transcricao(pasta / "transcricao.json", frases, fonte, idioma)
            # resumo pequeno para o painel não precisar abrir a transcrição inteira
            salvar_json(pasta / "legenda.json", {"fonte": fonte, "idioma": idioma, "frases": len(frases)})
            log.info("Legendas de '%s': %s, %d frases", titulo, fonte, len(frases))
    finally:
        if wav is not None:
            try:
                wav.unlink(missing_ok=True)
            except OSError:
                pass
    return Analise(info=info, cenas=cenas, volume=volume, falas=falas, frases=frases)


def carregar_para_render(cfg: Config, filme_id: int) -> tuple[dict, list[Frase]]:
    pasta = pasta_do_filme(cfg, filme_id)
    info = ler_json(pasta / "info.json")
    if not info:
        raise ErroMidia("análise do filme não encontrada (rode: python -m autocortes reanalisar)")
    frases, _ = carregar_transcricao(pasta / "transcricao.json")
    return info, frases


__all__ = ["Analise", "Interrompido", "analisar", "carregar_para_render", "pasta_do_filme", "PASSO_VOLUME"]
