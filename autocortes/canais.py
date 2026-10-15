"""Canais do YouTube: baixa os vídeos novos dos canais cadastrados para a pasta de filmes.

Os canais ficam na tabela `canais`, e cada vídeo tentado na `downloads`, que também impede baixar
o mesmo vídeo duas vezes. Quem baixa é o yt-dlp oficial (`ferramentas.garantir_ytdlp`), numa pasta
escondida (`.baixando`) dentro da pasta do canal, no mesmo disco: o vídeo só aparece na pasta de
filmes inteiro, com os dados dele (o .json com título, canal e link) já ao lado. Daí em diante é
um filme como os outros: a varredura (`produtor.varrer_biblioteca`) leva para a análise e os cortes.

Direitos: baixar vídeo de terceiros sem permissão viola os termos do YouTube e os direitos de quem
fez. O painel avisa. O caminho é usar canais seus ou que autorizam cortes, com o crédito nos posts
({canal} e {link_original} nos modelos de texto).
"""

from __future__ import annotations

import os
import json
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import unquote

from . import ferramentas
from .config import Config
from .db import agora, gravar_estado, ler_estado
from .midia import ErroMidia, Interrompido, executar
from .util import ATIVIDADES, log, salvar_json, sem_acentos, slug

__all__ = [
    "ErroCanal", "TRAVA", "adicionar", "downloads_recentes", "listar", "na_fila", "na_hora", "normalizar_url",
    "pasta_canais", "pausa", "proxima_verificacao", "titulo_curto", "verificar",
]

# uma verificação por vez neste processo: o motor e o "Verificar agora" do painel não baixam juntos
TRAVA = threading.Lock()
# tentativas de um vídeo que falhou (erro de rede, por exemplo) antes de desistir dele
MAX_TENTATIVAS = 3
# quantos dos vídeos mais novos de cada aba do canal são olhados por vez
VIDEOS_POR_ABA = 60
# o YouTube pediu para confirmar que não é robô (ou limitou os pedidos): para tudo por este tempo
PAUSA_BLOQUEIO_SEG = 12 * 3600
CHAVE_PAUSA = "canais:pausa"
CHAVE_VERIFICADO = "canais:verificado_em"

_URL = re.compile(
    r"^(?:https?://)?(?:(?:www|m)\.)?youtube\.com/"
    r"(@[^/?#\s]{1,100}|channel/UC[\w-]{22}|c/[^/?#\s]{1,100}|user/[^/?#\s]{1,100})(?:[/?#].*)?$",
    re.IGNORECASE,
)
_HANDLE = re.compile(r"^@[^/?#\s]{1,100}$")
# emoji e símbolos que o libass não desenha no título do corte
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")


class ErroCanal(Exception):
    """tipo: video (só este vídeo não serve), bloqueio (o YouTube barrou: para tudo por um tempo),
    ferramenta (falta o yt-dlp ou um JavaScript), desatualizado (o yt-dlp precisa se atualizar),
    espaco (disco cheio) ou temporario (rede, por exemplo)."""

    def __init__(self, mensagem: str, tipo: str = "temporario"):
        super().__init__(mensagem)
        self.tipo = tipo


# ---------------------------------------------------------------- endereços e nomes

def normalizar_url(texto: str) -> str:
    """O endereço do canal no formato que o AutoCortes guarda (sem a aba: /videos, /featured...)."""
    t = str(texto or "").strip()
    if _HANDLE.match(t):
        return f"https://www.youtube.com/{t}"
    achado = _URL.match(t)
    if not achado:
        raise ValueError("Use o endereço do canal no YouTube, como https://www.youtube.com/@canal "
                         "ou https://www.youtube.com/channel/UC...")
    parte = achado.group(1)
    if "/" in parte:  # channel/, c/, user/: o prefixo em minúsculas, o resto como veio (o UC... diferencia)
        prefixo, resto = parte.split("/", 1)
        parte = f"{prefixo.lower()}/{resto}"
    return f"https://www.youtube.com/{parte}"


def _nome_inicial(url: str) -> str:
    """O nome que aparece até a primeira verificação trazer o nome de verdade do canal."""
    return unquote(url.rstrip("/").rsplit("/", 1)[-1])


def titulo_curto(titulo: str, maximo: int = 60) -> str:
    """Título do vídeo bom para o topo do corte: sem hashtags e emoji, e sem o resto depois de um
    separador ("Tema do episódio | Podcast X #123" vira "Tema do episódio")."""
    original = " ".join(str(titulo or "").split())
    t = _EMOJI.sub("", re.sub(r"#\S+", "", original))
    t = " ".join(t.split()).strip(" -|–—:·,")
    for separador in (" | ", " – ", " — ", " - ", " ~ "):
        if separador in t:
            antes = t.split(separador, 1)[0].strip(" -|–—:·,")
            if len(antes) >= 12:
                t = antes
                break
    if len(t) > maximo:
        corte = t[:maximo].rsplit(" ", 1)[0].rstrip(" -|–—:·,")
        t = (corte if len(corte) >= maximo // 2 else t[:maximo]).rstrip() + "…"
    return t or original[:maximo] or "Vídeo do YouTube"


def pasta_canais(cfg: Config) -> Path:
    """Onde ficam os vídeos baixados (uma pasta por canal), dentro da pasta de filmes."""
    return cfg.pasta_filmes / str(cfg["canais"]["pasta"]).strip()


# ---------------------------------------------------------------- lista de canais e downloads

def listar(conn: sqlite3.Connection) -> list[dict]:
    contagem: dict[int, dict] = {}
    for r in conn.execute("SELECT canal_id, status, COUNT(*) AS n FROM downloads GROUP BY canal_id, status"):
        contagem.setdefault(r["canal_id"], {})[r["status"]] = r["n"]
    saida = []
    for c in conn.execute("SELECT * FROM canais ORDER BY id"):
        n = contagem.get(c["id"], {})
        saida.append({
            "id": c["id"], "url": c["url"], "nome": c["nome"] or _nome_inicial(c["url"]), "ativo": bool(c["ativo"]),
            "erro": c["erro"], "verificado_em": c["verificado_em"], "criado_em": c["criado_em"],
            "baixados": n.get("baixado", 0), "erros": n.get("erro", 0), "pulados": n.get("pulado", 0),
            "baixando": n.get("baixando", 0),
        })
    return saida


def adicionar(conn: sqlite3.Connection, texto: str) -> int:
    url = normalizar_url(texto)
    if conn.execute("SELECT 1 FROM canais WHERE lower(url)=lower(?)", (url,)).fetchone():
        raise ValueError("Este canal já está na lista")
    cursor = conn.execute("INSERT INTO canais (url, nome, ativo, criado_em) VALUES (?, ?, 1, ?)",
                          (url, _nome_inicial(url), agora()))
    log.info("Canal do YouTube adicionado: %s", url)
    return int(cursor.lastrowid)


def downloads_recentes(conn: sqlite3.Connection, limite: int = 40) -> list[dict]:
    itens = []
    for d in conn.execute(
        "SELECT d.*, c.nome AS canal_nome, c.url AS canal_url, f.id AS filme_id, f.status AS filme_status "
        "FROM downloads d LEFT JOIN canais c ON c.id = d.canal_id "
        "LEFT JOIN filmes f ON f.caminho = d.arquivo AND f.tipo = 'filme' "
        "ORDER BY d.atualizado_em DESC, d.id DESC LIMIT ?", (limite,),
    ):
        itens.append({
            "id": d["id"], "video_id": d["video_id"], "titulo": d["titulo"], "status": d["status"], "erro": d["erro"],
            "canal": d["canal_nome"] or (_nome_inicial(d["canal_url"]) if d["canal_url"] else ""),
            "duracao": d["duracao"], "publicado_em": d["publicado_em"], "tamanho": d["tamanho"],
            "quando": d["atualizado_em"], "tentativas": d["tentativas"],
            "url": f"https://www.youtube.com/watch?v={d['video_id']}",
            "filme_id": d["filme_id"], "filme_status": d["filme_status"],
        })
    return itens


def na_fila(conn: sqlite3.Connection) -> int:
    """Vídeos baixados que ainda esperam a análise (inclusive os que a varredura não viu ainda)."""
    n = 0
    for d in conn.execute(
        "SELECT d.arquivo, f.status FROM downloads d "
        "LEFT JOIN filmes f ON f.caminho = d.arquivo AND f.tipo = 'filme' WHERE d.status = 'baixado'"
    ):
        if d["status"] in ("novo", "analisando"):
            n += 1
        elif d["status"] is None and d["arquivo"] and Path(d["arquivo"]).is_file():
            n += 1
    return n


def pausa(conn: sqlite3.Connection) -> dict | None:
    """A pausa depois que o YouTube barrou (None = sem pausa)."""
    p = ler_estado(conn, CHAVE_PAUSA)
    if not isinstance(p, dict) or float(p.get("ate") or 0) <= time.time():
        return None
    return p


def ultima_verificacao(conn: sqlite3.Connection) -> float | None:
    valor = ler_estado(conn, CHAVE_VERIFICADO)
    return float(valor) if isinstance(valor, (int, float)) and valor > 0 else None


def proxima_verificacao(cfg: Config, conn: sqlite3.Connection) -> float | None:
    if not cfg["canais"]["ativo"]:
        return None
    ultima = float(ler_estado(conn, CHAVE_VERIFICADO, 0) or 0)
    proxima = ultima + float(cfg["canais"]["verificar_a_cada_horas"]) * 3600
    p = pausa(conn)
    return max(proxima, float(p["ate"])) if p else proxima


def na_hora(cfg: Config, conn: sqlite3.Connection) -> bool:
    proxima = proxima_verificacao(cfg, conn)
    if proxima is None or proxima > time.time():
        return False
    return conn.execute("SELECT 1 FROM canais WHERE ativo = 1 LIMIT 1").fetchone() is not None


# ---------------------------------------------------------------- yt-dlp

def _base(cfg: Config, ytdlp: Path, js: list[str]) -> list[str]:
    # --ignore-config: um yt-dlp.conf do seu usuário (proxy, cookies, formato) não entra no AutoCortes
    cmd = [str(ytdlp), "--ignore-config", "--encoding", "utf-8", *js]
    ffmpeg = str(cfg["ferramentas"]["ffmpeg"]).strip()
    if ffmpeg and ffmpeg.lower() not in ("ffmpeg", "ffmpeg.exe"):
        cmd += ["--ffmpeg-location", str(cfg.caminho_de(ffmpeg))]
    return cmd


_DO_VIDEO = (
    "private video", "video unavailable", "this video is unavailable", "members-only", "members only",
    "join this channel", "this live event will begin", "premieres in", "premiere will begin",
    "has been removed", "account associated with this video has been terminated",
    "not available in your country", "copyright claim", "this video has been removed",
)
_IDADE = ("confirm your age", "age-restricted", "age restricted", "inappropriate for some users")
_DESATUALIZADO = (
    "http error 403", "nsig", "signature extraction", "requested format is not available", "unable to extract",
    "please report this issue", "update to the latest version", "yt-dlp -u", "player response",
)


def _classificar(linhas: list[str]) -> ErroCanal:
    """O erro do yt-dlp, no tipo que diz o que fazer em seguida."""
    uteis = [x.strip() for x in linhas if x.strip()]
    texto = " ".join(uteis[-15:]).lower().replace("’", "'")
    ultimo = next((x for x in reversed(uteis) if x.startswith("ERROR:")), uteis[-1] if uteis else "erro sem detalhes")
    mensagem = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?([\w-]{11}:\s*)?", "", ultimo).strip()[:300] or "erro sem detalhes"
    # a idade vem antes: "Sign in to confirm your age" começa igual ao aviso de robô
    if any(s in texto for s in _IDADE):
        return ErroCanal("o vídeo tem restrição de idade, e eu não uso login para baixar", "video")
    if "not a bot" in texto or ("confirm you" in texto and " bot" in texto):
        return ErroCanal("o YouTube pediu para confirmar que não é um robô. Paro os downloads por 12 h; "
                         "não uso login nem contorno a verificação", "bloqueio")
    if "http error 429" in texto or "too many requests" in texto:
        return ErroCanal("o YouTube limitou os pedidos (muitos seguidos). Paro os downloads por 12 h", "bloqueio")
    if "no supported javascript runtime" in texto:
        return ErroCanal("o yt-dlp precisa do Deno ou do Node.js 22 ou mais novo para baixar do YouTube", "ferramenta")
    # antes do "do vídeo": "Requested format is not available" é yt-dlp velho, não vídeo indisponível
    if any(s in texto for s in _DESATUALIZADO):
        return ErroCanal(mensagem, "desatualizado")
    if any(s in texto for s in _DO_VIDEO):
        return ErroCanal(mensagem, "video")
    return ErroCanal(mensagem, "temporario")


def _listar(cfg: Config, base: list[str], url: str, aba: str, parar: threading.Event) -> tuple[dict, list[dict]]:
    """Os vídeos mais novos de uma aba do canal, sem baixar nada (com a data aproximada de cada um)."""
    cmd = [*base, "--flat-playlist", "--dump-single-json", "--playlist-items", f"1:{VIDEOS_POR_ABA}",
           "--extractor-args", "youtubetab:approximate_date", f"{url}/{aba}"]
    r = executar(cmd, parar=parar, capturar_saida=True, timeout=300,
                 baixa_prioridade=cfg["geral"]["prioridade_baixa"])
    if r.codigo != 0:
        raise _classificar(r.erros)
    try:
        dados = json.loads([x for x in r.saida.splitlines() if x.strip()][-1])
    except (ValueError, IndexError) as e:
        raise ErroCanal("o yt-dlp devolveu uma lista de vídeos que eu não entendi") from e
    itens = [x for x in (dados.get("entries") or []) if isinstance(x, dict) and x.get("id")]
    return dados, itens


def _quando(e: dict) -> float | None:
    for chave in ("timestamp", "release_timestamp"):
        valor = e.get(chave)
        if isinstance(valor, (int, float)) and valor > 0:
            return float(valor)
    data = str(e.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", data):
        try:
            return time.mktime(time.strptime(data, "%Y%m%d"))
        except (OverflowError, ValueError):
            return None
    return None


def _motivo_para_pular(cfg: Config, e: dict) -> str | None:
    """Por que este vídeo não entra agora (None = serve). Não fica gravado: mudar as regras vale na hora."""
    c = cfg["canais"]
    if str(e.get("live_status") or "") in ("is_live", "is_upcoming", "post_live"):
        return "ao vivo ou agendado"
    if str(e.get("availability") or "") in ("subscriber_only", "premium_only", "needs_auth"):
        return "só para membros"
    duracao = e.get("duration")
    if not isinstance(duracao, (int, float)) or duracao <= 0:
        return "sem duração"
    if duracao < float(c["minutos_min"]) * 60:
        return "curto"
    if c["minutos_max"] and duracao > float(c["minutos_max"]) * 60:
        return "longo"
    if c["max_idade_dias"]:
        quando = _quando(e)
        if quando is None or time.time() - quando > float(c["max_idade_dias"]) * 86400:
            return "antigo"
    titulo = sem_acentos(str(e.get("title") or "")).lower()
    for palavra in c["ignorar_palavras"]:
        alvo = sem_acentos(str(palavra)).lower().strip()
        if alvo and alvo in titulo:
            return f"tem '{palavra}' no título"
    return None


def _conferir_espaco(cfg: Config) -> None:
    pasta = pasta_canais(cfg)
    pasta.mkdir(parents=True, exist_ok=True)
    livre = shutil.disk_usage(pasta).free
    minimo = float(cfg["canais"]["espaco_minimo_gb"]) * 1024**3
    if livre < minimo:
        raise ErroCanal(f"o disco da pasta de filmes tem {livre / 1024**3:.1f} GB livres, e o mínimo para baixar é "
                        f"{cfg['canais']['espaco_minimo_gb']} GB (em Configurações > Canais)", "espaco")


def _nome_livre(destino: Path) -> Path:
    for n in range(1, 1000):
        candidato = destino if n == 1 else destino.with_name(f"{destino.stem} ({n}){destino.suffix}")
        if not candidato.exists() and not candidato.with_name(candidato.name + ".json").exists():
            return candidato
    raise ErroCanal("já existem arquivos demais com o mesmo nome na pasta do canal")


def _baixar(cfg: Config, base: list[str], nome_canal: str, e: dict, parar: threading.Event) -> Path:
    """Baixa um vídeo e põe na pasta do canal, com os dados ao lado. Devolve o caminho final."""
    c = cfg["canais"]
    vid = str(e["id"])
    titulo_original = " ".join(str(e.get("title") or vid).split())
    titulo = titulo_curto(titulo_original)
    pasta = pasta_canais(cfg) / (slug(nome_canal, 60) or "canal")
    temporaria = pasta / ".baixando" / vid
    shutil.rmtree(temporaria, ignore_errors=True)
    temporaria.mkdir(parents=True, exist_ok=True)
    resolucao = int(c["resolucao"])
    cmd = [
        *base, "--no-playlist", "--newline", "--progress", "--no-mtime",
        # até a resolução escolhida; H.264 e AAC primeiro (mp4 que qualquer programa abre e decodifica rápido)
        "-f", f"bv*[height<={resolucao}]+ba/b[height<={resolucao}]/bv*+ba/b",
        "-S", "vcodec:h264,acodec:aac", "--merge-output-format", "mp4/mkv",
        "-P", str(temporaria), "-o", "%(id)s.%(ext)s",
        "--progress-template", "download:AC|%(progress._percent_str)s|%(info.vcodec)s",
        "--print", "after_move:AC_ARQUIVO|%(filepath)s",
        "--retries", "5", "--fragment-retries", "10", "--sleep-requests", "1",
    ]
    if c["legendas"]:
        # só as legendas que o canal publicou (sem --write-auto-subs); vão dentro do vídeo
        cmd += ["--write-subs", "--sub-langs", "pt.*", "--embed-subs"]
    cmd.append(f"https://www.youtube.com/watch?v={vid}")

    chave = threading.current_thread().name
    rotulo = f"Baixando '{titulo}' do canal {nome_canal}"
    ATIVIDADES.definir(rotulo, 0, chave=chave)
    finais: list[str] = []
    ultimo = [0.0]

    def ler(linha: str) -> None:  # roda nas threads que leem a saída do yt-dlp
        if linha.startswith("AC_ARQUIVO|"):
            finais.append(linha.split("|", 1)[1].strip())
        elif linha.startswith("AC|"):
            partes = linha.split("|")
            try:
                pct = float(partes[1].strip().rstrip("%"))
            except (ValueError, IndexError):
                return
            if time.monotonic() - ultimo[0] > 0.5:
                ultimo[0] = time.monotonic()
                parte = "o áudio" if len(partes) > 2 and partes[2].strip() == "none" else "o vídeo"
                ATIVIDADES.definir(f"{rotulo} ({parte})", pct, chave=chave)

    try:
        r = executar(cmd, parar=parar, ao_ler_saida=ler, ao_ler_erro=ler, timeout=6 * 3600,
                     baixa_prioridade=cfg["geral"]["prioridade_baixa"])
        if r.codigo != 0:
            raise _classificar(r.erros)
        candidatos = [Path(x) for x in finais if Path(x).is_file()] or sorted(
            p for p in temporaria.iterdir() if p.is_file() and p.stem == vid and p.suffix.lower() in (".mp4", ".mkv"))
        if not candidatos:
            raise ErroCanal("o yt-dlp terminou, mas não achei o vídeo baixado")
        baixado = candidatos[-1]
        destino = _nome_livre(pasta / f"{slug(titulo, 80) or vid} [{vid}]{baixado.suffix.lower()}")
        # os dados chegam antes do vídeo: a varredura lê o título dele no .json ao achar o arquivo
        salvar_json(destino.with_name(destino.name + ".json"), {
            "titulo": titulo,
            "titulo_original": titulo_original,
            "canal": nome_canal,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "video_id": vid,
            "publicado_em": _quando(e),
            "baixado_em": time.time(),
        })
        try:
            os.rename(baixado, destino)  # mesmo disco: o vídeo aparece inteiro de uma vez
        except OSError:
            destino.with_name(destino.name + ".json").unlink(missing_ok=True)
            raise
        antigo = time.time() - 180  # a varredura ignora o que mudou há menos de 1 min
        os.utime(destino, (antigo, antigo))
        return destino
    finally:
        shutil.rmtree(temporaria, ignore_errors=True)


def _baixar_video(cfg: Config, conn: sqlite3.Connection, base: list[str], canal, nome_canal: str, e: dict,
                  linha, parar: threading.Event) -> Path | None:
    """Um vídeo, com o registro na tabela downloads. None = não deu (e o motivo ficou anotado)."""
    vid = str(e["id"])
    titulo = " ".join(str(e.get("title") or vid).split())[:300]
    t = time.time()
    if linha is None:
        conn.execute(
            "INSERT INTO downloads (canal_id, video_id, titulo, publicado_em, duracao, status, criado_em, "
            "atualizado_em) VALUES (?, ?, ?, ?, ?, 'baixando', ?, ?)",
            (canal["id"], vid, titulo, _quando(e), e.get("duration"), t, t),
        )
    else:
        conn.execute("UPDATE downloads SET status='baixando', erro=NULL, titulo=?, canal_id=?, atualizado_em=? "
                     "WHERE video_id=?", (titulo, canal["id"], t, vid))
    tentativas = int(linha["tentativas"] or 0) if linha is not None else 0
    try:
        try:
            caminho = _baixar(cfg, base, nome_canal, e, parar)
        except ErroCanal as primeiro:
            # versão velha do yt-dlp é o motivo mais comum de "não consegui": atualiza e tenta mais uma vez
            if primeiro.tipo != "desatualizado" or not ferramentas.atualizar_ytdlp(cfg, forcar=True):
                raise
            log.info("Canais: o yt-dlp foi atualizado; tentando '%s' de novo", titulo)
            caminho = _baixar(cfg, base, nome_canal, e, parar)
    except Interrompido:
        # a tentativa cortada no meio não conta contra o vídeo
        conn.execute("UPDATE downloads SET status='erro', erro='parei no meio do download', atualizado_em=? "
                     "WHERE video_id=?", (time.time(), vid))
        raise
    except (ErroCanal, ErroMidia, OSError) as erro:
        tipo = erro.tipo if isinstance(erro, ErroCanal) else "temporario"
        # o bloqueio, o disco cheio e a falta de ferramenta não são culpa do vídeo
        if tipo not in ("bloqueio", "espaco", "ferramenta"):
            tentativas += 1
        status = "pulado" if tipo == "video" or tentativas >= MAX_TENTATIVAS else "erro"
        conn.execute("UPDATE downloads SET status=?, erro=?, tentativas=?, atualizado_em=? WHERE video_id=?",
                     (status, str(erro)[:400], tentativas, time.time(), vid))
        if tipo in ("bloqueio", "espaco", "ferramenta"):
            raise erro if isinstance(erro, ErroCanal) else ErroCanal(str(erro), tipo)
        log.warning("Canal %s: '%s' %s: %s", nome_canal, titulo,
                    "não serve" if status == "pulado" and tipo == "video" else
                    ("foi deixado de lado depois de 3 tentativas" if status == "pulado" else "não baixou"), erro)
        return None
    tamanho = caminho.stat().st_size
    conn.execute("UPDATE downloads SET status='baixado', erro=NULL, arquivo=?, tamanho=?, atualizado_em=? "
                 "WHERE video_id=?", (str(caminho.resolve()), tamanho, time.time(), vid))
    log.info("Canal %s: baixei '%s' (%d MB) para %s", nome_canal, titulo, tamanho // 2**20, caminho.parent)
    return caminho


def _verificar_canal(cfg: Config, conn: sqlite3.Connection, base: list[str], canal, parar: threading.Event,
                     ao_baixar) -> tuple[list[str], bool]:
    """Olha um canal e baixa o que serve. Devolve (títulos baixados, fila de análise cheia)."""
    c = cfg["canais"]
    nome = canal["nome"] or _nome_inicial(canal["url"])
    ATIVIDADES.definir(f"Olhando o canal {nome}")
    entradas: list[dict] = []
    info_canal: dict = {}
    for aba in ("videos", "streams") if c["incluir_ao_vivo"] else ("videos",):
        try:
            info, itens = _listar(cfg, base, canal["url"], aba, parar)
        except ErroCanal as e:
            if aba == "streams" and e.tipo not in ("bloqueio", "ferramenta"):
                continue  # canal sem a aba "Ao vivo"
            raise
        info_canal = info_canal or info
        entradas += itens
    nome_real = str(info_canal.get("channel") or info_canal.get("uploader") or "").strip()
    if nome_real and nome_real != canal["nome"]:
        conn.execute("UPDATE canais SET nome=? WHERE id=?", (nome_real[:120], canal["id"]))
        nome = nome_real[:120]
    entradas.sort(key=lambda x: _quando(x) or 0, reverse=True)  # as duas abas juntas, do mais novo

    baixados: list[str] = []
    tentados = 0
    for e in entradas:
        if tentados >= int(c["max_por_canal"]):
            break
        if parar.is_set():
            raise Interrompido()
        if _motivo_para_pular(cfg, e):
            continue
        linha = conn.execute("SELECT * FROM downloads WHERE video_id=?", (str(e["id"]),)).fetchone()
        if linha is not None and (linha["status"] in ("baixado", "pulado", "baixando")
                                  or int(linha["tentativas"] or 0) >= MAX_TENTATIVAS):
            continue
        if na_fila(conn) >= int(c["max_na_fila"]):
            return baixados, True
        _conferir_espaco(cfg)
        tentados += 1
        caminho = _baixar_video(cfg, conn, base, canal, nome, e, linha, parar)
        if caminho is not None:
            baixados.append(titulo_curto(str(e.get("title") or e["id"])))
            if ao_baixar is not None:
                ao_baixar()  # a varredura acha o vídeo na hora, sem esperar a próxima
    return baixados, False


def verificar(cfg: Config, conn: sqlite3.Connection, parar: threading.Event, so_canal: int | None = None,
              forcar: bool = False, ao_baixar=None) -> dict:
    """Olha os canais ativos e baixa os vídeos novos que servem. Chame com a `TRAVA` na mão.

    forcar: o "Verificar agora" do painel, que passa por cima da pausa depois de um bloqueio
    (foi você que pediu). Devolve um resumo para o log e para o painel.
    """
    resumo: dict = {"canais": 0, "baixados": [], "erros": [], "parado": None, "fila_cheia": False}
    gravar_estado(conn, CHAVE_VERIFICADO, time.time())  # mesmo se der erro, a próxima vez é daqui a N horas
    p = pausa(conn)
    if p and not forcar:
        resumo["parado"] = str(p.get("motivo") or "pausado")
        return resumo
    sql = "SELECT * FROM canais WHERE ativo = 1" + (" AND id = ?" if so_canal else "") + " ORDER BY id"
    canais = conn.execute(sql, (so_canal,) if so_canal else ()).fetchall()
    if not canais:
        return resumo
    try:
        ytdlp = ferramentas.garantir_ytdlp(cfg)
        ferramentas.atualizar_ytdlp(cfg)
        js = ferramentas.runtime_js(cfg)
    except ErroMidia as e:  # inclui o ErroDownload: sem internet, o yt-dlp ou o Deno não chegaram
        for canal in canais:
            conn.execute("UPDATE canais SET erro=? WHERE id=?", (str(e)[:300], canal["id"]))
        raise ErroCanal(str(e), "ferramenta") from e
    base = _base(cfg, ytdlp, js)
    try:
        for canal in canais:
            if parar.is_set():
                raise Interrompido()
            resumo["canais"] += 1
            nome = canal["nome"] or _nome_inicial(canal["url"])
            if na_fila(conn) >= int(cfg["canais"]["max_na_fila"]):
                resumo["fila_cheia"] = True
                break
            try:
                baixados, cheia = _verificar_canal(cfg, conn, base, canal, parar, ao_baixar)
            except ErroCanal as e:
                conn.execute("UPDATE canais SET erro=?, verificado_em=? WHERE id=?",
                             (str(e)[:300], time.time(), canal["id"]))
                resumo["erros"].append(f"{nome}: {e}")
                if e.tipo == "bloqueio":
                    gravar_estado(conn, CHAVE_PAUSA, {"ate": time.time() + PAUSA_BLOQUEIO_SEG, "motivo": str(e),
                                                      "desde": time.time()})
                    log.warning("Canais: %s", e)
                    resumo["parado"] = str(e)
                    break
                if e.tipo in ("espaco", "ferramenta"):
                    log.warning("Canais: %s", e)
                    resumo["parado"] = str(e)
                    break
                log.warning("Canal %s: %s", nome, e)
                continue
            conn.execute("UPDATE canais SET erro=NULL, verificado_em=? WHERE id=?", (time.time(), canal["id"]))
            resumo["baixados"] += baixados
            if cheia:
                resumo["fila_cheia"] = True
                break
    finally:
        ATIVIDADES.definir("Canais do YouTube: esperando a próxima olhada", ocioso=True)
    if resumo["fila_cheia"]:
        log.info("Canais: %d vídeo(s) baixados esperam a análise; baixo mais quando a fila andar",
                 na_fila(conn))
    if resumo["baixados"]:
        log.info("Canais: %d vídeo(s) novo(s): %s", len(resumo["baixados"]), "; ".join(resumo["baixados"]))
    return resumo
