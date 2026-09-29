"""Títulos a partir do nome do arquivo, metadados opcionais e textos das postagens."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .util import log, sem_acentos

_ANO = re.compile(r"^\(?((?:19|20)\d{2})\)?$")
_LIXO = {
    "480p", "576p", "720p", "1080p", "1080i", "2160p", "4k", "uhd", "hdr", "hdr10", "sdr",
    "bluray", "blu-ray", "bdrip", "brrip", "bdremux", "remux", "webrip", "web-dl", "webdl",
    "hdrip", "dvdrip", "dvdscr", "hdtv", "hdcam", "x264", "x265", "h264", "h265", "h.264",
    "h.265", "hevc", "avc", "xvid", "divx", "aac", "ac3", "eac3", "dts", "dts-hd", "truehd", "atmos",
    "10bit", "8bit", "dublado", "legendado", "dual-audio", "audio", "áudio",
}
# palavras que também aparecem em títulos ("Charlotte's Web", "Dual", "A Complete Unknown"): só contam
# como lixo quando vêm logo antes de outra palavra de lixo, ou no fim do nome
_LIXO_AMBIGUO = {
    "web", "cam", "dual", "multi", "complete", "extended", "proper", "repack", "unrated", "imax", "dv",
    "remastered", "nacional",
}
_RESOLUCAO = re.compile(r"^(\d{3,4}p|\d{3,4}x\d{3,4})$", re.I)

_EPISODIO = re.compile(r"^[ST](\d{1,2})[ .]?E(\d{1,3})(?:-?E?\d{1,3})*$", re.I)
_EPISODIO_X = re.compile(r"^(\d{1,2})x(\d{2,3})$", re.I)
# o "T1:E1" no fim do título de uma série (fica fora da hashtag)
_MARCA_EPISODIO = re.compile(r"\s+T(\d{1,2}):E(\d{1,3})$")


def _episodio(tok: str) -> str | None:
    ep = _EPISODIO.match(tok.strip("-{}()")) or _EPISODIO_X.match(tok.strip("-{}()"))
    return f"T{int(ep.group(1))}:E{int(ep.group(2))}" if ep else None


def _eh_lixo(tokens: list[str], i: int) -> bool:
    limpo = tokens[i].strip("-{}()").lower()
    if limpo in _LIXO or _RESOLUCAO.match(limpo):
        return True
    if limpo in _LIXO_AMBIGUO:
        if i + 1 >= len(tokens):
            return True
        seguinte = tokens[i + 1].strip("-{}()").lower()
        return seguinte in _LIXO or seguinte in _LIXO_AMBIGUO or bool(_RESOLUCAO.match(seguinte))
    return False


def titulo_do_arquivo(nome: str) -> tuple[str, int | None]:
    """'O.Poderoso.Chefao.1972.1080p.BluRay.mkv' -> ('O Poderoso Chefao', 1972).

    Séries: 'The.Great.S01E01.1080p.WEB-DL.mkv' -> ('The Great T1:E1', None).
    """
    base = Path(nome).stem
    base = re.sub(r"\[[^\]]*\]", " ", base)
    base = re.sub(r"[._]+", " ", base)
    base = re.sub(r"\b([ST]\d{1,2})\s+(E\d{1,3})\b", r"\1\2", base, flags=re.I)  # "S01 E01" -> "S01E01"
    tokens = base.split()
    limite_ano = time.localtime().tm_year + 1
    palavras: list[str] = []
    ano = None
    episodio = None
    for i, tok in enumerate(tokens):
        m = _ANO.match(tok.strip("-{}"))
        if m and i > 0 and int(m.group(1)) <= limite_ano:
            proximo = tokens[i + 1].strip("-{}") if i + 1 < len(tokens) else ""
            seguinte = _ANO.match(proximo)
            if seguinte and int(seguinte.group(1)) <= limite_ano:
                palavras.append(tok)  # "Blade.Runner.2049.2017": o primeiro número é do título
                continue
            ano = int(m.group(1))
            # "Show (2019) - S01E01": o episódio ainda vem depois do ano
            for tok2 in [t for t in tokens[i + 1:i + 4] if t.strip("-")][:2]:
                episodio = _episodio(tok2)
                if episodio:
                    break
            break
        episodio = _episodio(tok) if i > 0 else None
        if episodio:
            break
        if i > 0 and _eh_lixo(tokens, i):  # o primeiro token é sempre do título ("Cam", "Dual")
            break
        palavras.append(tok)
    titulo = " ".join(palavras).strip(" -()") or base.strip()
    if titulo == titulo.lower() or titulo == titulo.upper():
        # maiúscula no começo de cada palavra, sem estragar "Se7en" nem "Ocean's"
        titulo = re.sub(r"(^|[\s-])(\w)", lambda m: m.group(1) + m.group(2).upper(), titulo.lower())
    if episodio:
        titulo = f"{titulo} {episodio}"
    return titulo, ano


def episodio_de(titulo: str) -> tuple[str, int | None, int | None]:
    """'The Great T1:E1' -> ('The Great', 1, 1); filmes: (título, None, None)."""
    m = _MARCA_EPISODIO.search(str(titulo))
    if not m:
        return str(titulo), None, None
    return str(titulo)[:m.start()].strip() or str(titulo), int(m.group(1)), int(m.group(2))


def nome_da_serie(titulo: str) -> str:
    """'The Great T1:E1' -> 'The Great' (filmes ficam como estão)."""
    return _MARCA_EPISODIO.sub("", str(titulo)).strip() or str(titulo)


def _arquivos_meta(caminho_filme: Path) -> tuple[Path, Path]:
    return caminho_filme.with_name(caminho_filme.name + ".json"), caminho_filme.with_suffix(".json")


def tem_arquivo_meta(caminho_filme: Path) -> bool:
    return any(p.exists() for p in _arquivos_meta(caminho_filme))


def ler_metadados(caminho_filme: Path) -> dict:
    """Metadados opcionais em '<filme>.json' (ou '<filme sem extensão>.json').

    Chaves aceitas: titulo, ano, hashtags (lista), idioma (fala, para o Whisper),
    legenda (caminho de um .srt), fonte (de onde vêm as legendas deste filme), ignorar (true/false).
    """
    for candidato in _arquivos_meta(caminho_filme):
        if candidato.exists():
            try:
                dados = json.loads(candidato.read_text(encoding="utf-8-sig"))
                return dados if isinstance(dados, dict) else {}
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                log.warning("Metadados inválidos em %s: %s", candidato.name, e)
    return {}


def normalizar(texto: str) -> str:
    return sem_acentos(texto).lower()


# letras que o sem_acentos não converte
_TRANSLITERAR = str.maketrans({"ß": "ss", "æ": "ae", "Æ": "Ae", "ø": "o", "Ø": "O", "œ": "oe", "Œ": "Oe",
                               "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "þ": "th", "Þ": "Th"})


def hashtag_de(texto: str) -> str:
    # "Ocean's Eleven" vira #OceansEleven (e não #OceanSEleven)
    base = re.sub(r"['’‘´`]", "", nome_da_serie(texto)).translate(_TRANSLITERAR)
    palavras = re.findall(r"[A-Za-z0-9]+", sem_acentos(base))
    if not palavras:
        return ""
    return "#" + "".join(p[:1].upper() + p[1:] for p in palavras)


class _Variaveis(dict):
    def __missing__(self, chave):
        return ""


def aplicar_modelo(modelo: str, variaveis: dict) -> str:
    try:
        texto = modelo.format_map(_Variaveis(variaveis))
    except (ValueError, IndexError, AttributeError, KeyError) as e:
        log.warning("Modelo de texto inválido (%s): %r", e, modelo)
        texto = modelo
    texto = re.sub(r"[ \t]+\n", "\n", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


@dataclass
class Conteudo:
    titulo: str
    descricao: str
    hashtags: list[str] = field(default_factory=list)
    duracao: float = 0.0
    # nome e ano do filme como aparecem nos textos (a fonte do trecho nas redes que pedem)
    filme: str = ""
    ano: int | str | None = None


def textos_ia(corte) -> dict:
    """Textos que a IA escreveu para o corte ({} se não houver)."""
    if "ia_textos" not in set(corte.keys()) or not corte["ia_textos"]:
        return {}
    try:
        dados = json.loads(corte["ia_textos"])
    except (TypeError, ValueError):
        return {}
    return dados if isinstance(dados, dict) and dados.get("titulo") else {}


def fonte_dos_textos(corte) -> str:
    """De onde vêm os textos do post: "manual" (painel), "ia" ou "modelo"."""
    chaves = set(corte.keys())
    if any((corte[k] or "").strip() for k in ("titulo_custom", "descricao_custom") if k in chaves):
        return "manual"
    return "ia" if textos_ia(corte) else "modelo"


def montar_conteudo(cfg: Config, filme: dict, corte: dict, usar_ia: bool = True) -> Conteudo:
    meta = ler_metadados(Path(filme["caminho"]))
    titulo_filme = str(meta.get("titulo") or filme["titulo"])
    ano = meta.get("ano") or filme["ano"]
    t = cfg["textos"]
    ia = textos_ia(corte) if usar_ia else {}

    tags: list[str] = []
    vistos: set[str] = set()
    extras = meta.get("hashtags") if isinstance(meta.get("hashtags"), list) else []
    automatica = [hashtag_de(titulo_filme)] if t["hashtag_do_filme"] else []
    # da mais específica para a mais genérica: o YouTube mostra só as 3 primeiras acima
    # do título e o Instagram aceita no máximo 5
    for tag in automatica + extras + list(ia.get("hashtags") or []) + list(t["hashtags"]):
        tag = str(tag).strip()
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = "#" + tag
        chave = tag.lower()
        if chave not in vistos:
            vistos.add(chave)
            tags.append(tag)
    tags = tags[: int(t["max_hashtags"])]

    variaveis = {
        "filme": titulo_filme,
        "ano": ano or "",
        "ano_parenteses": f" ({ano})" if ano else "",
        "parte": corte["parte"] or "",
        "frase": (corte["frase"] or "").strip(),
        "hashtags": " ".join(tags),
        "titulo_ia": ia.get("titulo", ""),
        "descricao_ia": ia.get("descricao", ""),
    }
    # ordem: texto escrito à mão no painel > texto da IA > modelos do [textos]
    chaves = set(corte.keys())
    titulo_custom = (corte["titulo_custom"] or "").strip() if "titulo_custom" in chaves else ""
    descricao_custom = (corte["descricao_custom"] or "").strip() if "descricao_custom" in chaves else ""
    modelo_titulo = t["titulo_ia"] if ia else t["titulo"]
    modelo_descricao = t["descricao_ia"] if ia else t["descricao"]
    return Conteudo(
        titulo=aplicar_modelo(titulo_custom or modelo_titulo, variaveis),
        descricao=aplicar_modelo(descricao_custom or modelo_descricao, variaveis),
        hashtags=tags,
        duracao=float(corte["fim"]) - float(corte["inicio"]),
        filme=titulo_filme,
        ano=ano or None,
    )


def texto_topo(cfg: Config, filme: dict, parte: int) -> str:
    meta = ler_metadados(Path(filme["caminho"]))
    variaveis = {
        "filme": str(meta.get("titulo") or filme["titulo"]),
        "ano": meta.get("ano") or filme["ano"] or "",
        "parte": parte,
    }
    return aplicar_modelo(str(cfg["edicao"]["texto_topo"]), variaveis)


def limitar(texto: str, maximo: int) -> str:
    """Corta em até 'maximo' caracteres sem quebrar palavras quando possível."""
    if len(texto) <= maximo:
        return texto
    corte = texto[: maximo - 1]
    espaco = corte.rfind(" ")
    if espaco > maximo * 0.6:
        corte = corte[:espaco]
    return corte.rstrip(" ,.;:-") + "…"


def limitar_hashtags(texto: str, maximo: int) -> str:
    """Mantém só as primeiras 'maximo' hashtags do texto (vale também para texto escrito à mão)."""
    contagem = 0

    def trocar(m: re.Match) -> str:
        nonlocal contagem
        contagem += 1
        return m.group(0) if contagem <= maximo else ""

    novo = re.sub(r"(?<![\w#])#\w+", trocar, texto)
    if contagem <= maximo:
        return texto
    novo = re.sub(r"[ \t]{2,}", " ", novo)
    novo = re.sub(r"[ \t]+\n", "\n", novo)
    return re.sub(r"\n{3,}", "\n\n", novo).strip()


def sem_hashtags(texto: str) -> str:
    """O texto sem as hashtags (para redes que usam tags à parte, como o Bilibili)."""
    novo = re.sub(r"(?<![\w#])#\w+", "", texto)
    novo = re.sub(r"[ \t]{2,}", " ", novo)
    novo = re.sub(r"[ \t]+\n", "\n", novo)
    return re.sub(r"\n{3,}", "\n\n", novo).strip()


def tamanho_utf16(texto: str) -> int:
    return len(texto.encode("utf-16-le")) // 2
