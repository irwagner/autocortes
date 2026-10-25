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
import random
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from . import canais_conta, ferramentas
from .config import Config
from .db import agora, gravar_estado, ler_estado
from .midia import ErroMidia, Interrompido, executar
from .util import ATIVIDADES, log, salvar_json, sem_acentos, slug

__all__ = [
    "ErroCanal", "TRAVA", "adicionar", "conta_reconectada", "downloads_recentes", "janela_so_conta", "listar",
    "na_fila", "na_hora", "normalizar_url", "pasta_canais", "pausa", "proxima_verificacao", "titulo_curto",
    "verificar",
]

# uma verificação por vez neste processo: o motor e o "Verificar agora" do painel não baixam juntos
TRAVA = threading.Lock()
# tentativas de um vídeo que falhou (erro de rede, por exemplo) antes de desistir dele
MAX_TENTATIVAS = 3
# quantos dos vídeos mais novos de cada aba do canal são olhados por vez
VIDEOS_POR_ABA = 60
# espera sorteada entre uma chamada do yt-dlp e a seguinte (listar, baixar, passar ao próximo canal):
# pedidos colados um no outro são o que leva o YouTube a pedir para confirmar que não é robô
RITMO_SEG = (10.0, 20.0)
# o YouTube pediu para confirmar que não é robô (ou limitou os pedidos): para tudo por este tempo,
# que cresce se o aviso voltar logo (ou se você insistir durante a pausa)
PAUSAS_SEG = (12 * 3600, 24 * 3600, 48 * 3600)
# aviso até este tempo depois do fim da pausa conta como reincidência (a pausa seguinte é maior)
REINCIDENCIA_SEG = 24 * 3600
# este tempo sem aviso zera a conta (a pausa seguinte volta a ser a menor)
ZERAR_SEG = 3 * 86400
CHAVE_PAUSA = "canais:pausa"
CHAVE_VERIFICADO = "canais:verificado_em"
# o texto curto de cada pausa, sem a hora (a hora entra na leitura, em `_mensagem_pausa`)
MOTIVOS = {
    "robo": "o YouTube pediu para confirmar que não é um robô",
    "limite": "o YouTube limitou os pedidos (muitos seguidos)",
}
# valor de cookie, token ou cabeçalho de login que o yt-dlp possa repetir na linha de erro
_SEGREDO = re.compile(
    r"\b(cookies?|po_token|token|visitor_data|authorization|bearer|sapisid|apisid|hsid|ssid|sid|login_info"
    r"|__Secure-[\w-]+)(\s*[=:]\s*)((?:(?:bearer|basic|oauth)\s+)?[^\s;,&)\]\"']+)",
    re.IGNORECASE,
)
# os cookies da conta separada que o YouTube não aceita mais (usada quando houver a conta)
_COOKIES_INVALIDOS = ("cookies are no longer valid", "cookies are invalid")

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
    espaco (disco cheio) ou temporario (rede, por exemplo).

    causa: robo, limite, idade, js, desatualizado, video ou None. detalhe: a última linha ERROR: do
    yt-dlp, já sem segredo (`_limpar_detalhe`). com_conta: o erro veio da chamada com a conta separada."""

    def __init__(self, mensagem: str, tipo: str = "temporario", *, causa: str | None = None,
                 detalhe: str | None = None, com_conta: bool = False):
        super().__init__(mensagem)
        self.tipo = tipo
        self.causa = causa
        self.detalhe = detalhe
        self.com_conta = com_conta


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


def _hora_texto(ts: float) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%d/%m às %H:%M")


def _normalizar_pausa(p) -> dict | None:
    """O registro de `canais:pausa` com todos os campos. Aceita o formato antigo (só ate, motivo e
    desde, com a mensagem inteira no motivo). None = não há registro que sirva."""
    if not isinstance(p, dict):
        return None
    try:
        ate = float(p.get("ate") or 0)
        desde = float(p.get("desde") or 0) or ate - PAUSAS_SEG[0]
        ultimo = float(p.get("ultimo_aviso") or 0) or desde
        vezes = max(1, int(p.get("vezes") or 1))
    except (TypeError, ValueError):
        return None
    if ate <= 0:
        return None
    tipo = p.get("tipo")
    if tipo not in MOTIVOS:
        tipo = "limite" if "limitou" in str(p.get("motivo") or "") else "robo"
    detalhe = p.get("detalhe")
    return {
        "ate": ate, "desde": desde, "ultimo_aviso": ultimo, "vezes": vezes, "tipo": tipo, "motivo": MOTIVOS[tipo],
        "detalhe": str(detalhe) if detalhe else None, "com_conta": bool(p.get("com_conta")),
        "so_conta": bool(p.get("so_conta")),
    }


def _proxima_pausa(anterior: dict | None, agora: float) -> tuple[int, int]:
    """(vezes, duração em segundos) do aviso que acabou de chegar. `anterior` já normalizado."""
    if not anterior or agora - float(anterior["ultimo_aviso"]) >= ZERAR_SEG:
        vezes = 1                                   # três dias sem aviso zeram a conta
    elif agora <= float(anterior["ate"]) + REINCIDENCIA_SEG:
        vezes = int(anterior["vezes"]) + 1           # durante a pausa (Olhar agora) ou até 24 h depois do fim
    else:
        vezes = int(anterior["vezes"])               # passou das 24 h, mas não dos 3 dias: repete
    return vezes, PAUSAS_SEG[min(vezes, len(PAUSAS_SEG)) - 1]


def _situacao_conta(cfg: Config) -> str:
    """A conta separada para baixar (`canais_conta`): nenhuma, ok ou caiu."""
    return canais_conta.situacao(cfg)


def _mensagem_pausa(p: dict, conta: str | None = None) -> str:
    """O texto da pausa, com a hora do fim (calculado na leitura, nunca gravado). `conta` é a
    situação da conta separada naquela hora: muda o complemento, e com ela a janela "só com a conta"
    não é pausa."""
    ate = _hora_texto(p["ate"])
    if p.get("so_conta") and conta == "ok":
        return f"{MOTIVOS['robo']} sem a conta; até {ate} os downloads vão direto com a conta separada"
    texto = f"{MOTIVOS[p['tipo']]}. Paro os downloads até {ate}"
    if p["tipo"] == "robo" and conta == "nenhuma":
        texto += "; para não depender disso, conecte uma conta separada em Filmes > Canais do YouTube (opcional)"
    elif p["tipo"] == "robo" and conta == "caiu":
        texto += "; a conta separada precisa entrar de novo em Filmes > Canais do YouTube"
    return texto


def _registrar_aviso(conn: sqlite3.Connection, erro: ErroCanal, *, so_conta: bool = False,
                     agora: float | None = None) -> dict:
    """Grava a pausa do aviso que acabou de chegar (robô ou 429), já com a escalada. O registro fica
    depois do fim da pausa: é por ele que o aviso seguinte sabe se é reincidência."""
    t = time.time() if agora is None else float(agora)
    vezes, duracao = _proxima_pausa(_normalizar_pausa(ler_estado(conn, CHAVE_PAUSA)), t)
    tipo = erro.causa if erro.causa in MOTIVOS else "robo"
    p = {
        "ate": t + duracao, "desde": t, "ultimo_aviso": t, "vezes": vezes, "tipo": tipo, "motivo": MOTIVOS[tipo],
        "detalhe": erro.detalhe, "com_conta": bool(erro.com_conta), "so_conta": bool(so_conta),
    }
    gravar_estado(conn, CHAVE_PAUSA, p)
    return p


def pausa(conn: sqlite3.Connection, cfg: Config) -> dict | None:
    """A pausa depois que o YouTube barrou (None = sem pausa), com a `mensagem` pronta. A janela "só
    com a conta" não para nada enquanto a conta separada puder ser usada; sem ela, vale como pausa."""
    p = _normalizar_pausa(ler_estado(conn, CHAVE_PAUSA))
    if p is None or p["ate"] <= time.time():
        return None
    conta = _situacao_conta(cfg)
    if p["so_conta"] and conta == "ok":
        return None
    return {**p, "mensagem": _mensagem_pausa(p, conta)}


def janela_so_conta(conn: sqlite3.Connection, cfg: Config) -> dict | None:
    """O aviso foi sem a conta e a conta passou: até o fim, as olhadas vão direto com ela (None = não
    há janela, ou a conta não pode ser usada)."""
    p = _normalizar_pausa(ler_estado(conn, CHAVE_PAUSA))
    if p is None or not p["so_conta"] or p["ate"] <= time.time() or not canais_conta.utilizavel(cfg):
        return None
    return {**p, "mensagem": _mensagem_pausa(p, "ok")}


def usar_conta_direto(conn: sqlite3.Connection, cfg: Config) -> bool:
    return janela_so_conta(conn, cfg) is not None


def conta_reconectada(conn: sqlite3.Connection) -> bool:
    """Depois de conectar a conta separada: a pausa do aviso de robô sem a conta vira a janela "só
    com a conta", com o mesmo fim e a mesma contagem (com a conta, o aviso sem ela não para). A do
    429 e a do robô que a própria conta levou (`com_conta`) continuam."""
    p = _normalizar_pausa(ler_estado(conn, CHAVE_PAUSA))
    if p is None or p["ate"] <= time.time() or p["tipo"] != "robo" or p["so_conta"] or p["com_conta"]:
        return False
    p.update(so_conta=True, com_conta=False)
    gravar_estado(conn, CHAVE_PAUSA, p)
    return True


def ultima_verificacao(conn: sqlite3.Connection) -> float | None:
    valor = ler_estado(conn, CHAVE_VERIFICADO)
    return float(valor) if isinstance(valor, (int, float)) and valor > 0 else None


def proxima_verificacao(cfg: Config, conn: sqlite3.Connection) -> float | None:
    if not cfg["canais"]["ativo"]:
        return None
    ultima = float(ler_estado(conn, CHAVE_VERIFICADO, 0) or 0)
    proxima = ultima + float(cfg["canais"]["verificar_a_cada_horas"]) * 3600
    p = pausa(conn, cfg)
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


def _limpar_detalhe(linha: str, extras=()) -> str:
    """A linha original do yt-dlp boa para o registro e o painel: espaços juntos, o caminho do arquivo
    de cookies (`extras`) trocado, nenhum valor de cookie ou token, e no máximo 300 caracteres."""
    t = str(linha or "")
    for extra in extras:
        if extra:
            t = t.replace(str(extra), _MARCA_COOKIES)
    t = " ".join(t.split())
    for extra in extras:
        if extra:
            t = t.replace(" ".join(str(extra).split()), _MARCA_COOKIES)
    t = _SEGREDO.sub(_esconder, t)
    return t[:300]


# o que fica no lugar do caminho do arquivo temporário de cookies da conta separada
_MARCA_COOKIES = "<cookies da conta>"


def _esconder(m: re.Match) -> str:
    nome, separador, valor = m.group(1), m.group(2), m.group(3)
    if m.string.startswith(_MARCA_COOKIES, m.start(3)):
        return m.group(0)  # "cookies: <cookies da conta>" já não tem o caminho
    if nome.lower() in ("cookie", "cookies") and "=" in valor:
        # "cookie: SAPISID=..." fica "cookie: SAPISID=***": o nome ajuda, o valor nunca sai
        return f"{nome}{separador}{valor.split('=', 1)[0]}=***"
    return f"{nome}{separador}***"


def _classificar(linhas: list[str], extras=()) -> ErroCanal:
    """O erro do yt-dlp, no tipo que diz o que fazer em seguida (com a linha original em `detalhe`)."""
    uteis = [x.strip() for x in linhas if x.strip()]
    texto = " ".join(uteis[-15:]).lower().replace("’", "'")
    ultimo = next((x for x in reversed(uteis) if x.startswith("ERROR:")), uteis[-1] if uteis else "")
    detalhe = _limpar_detalhe(ultimo, extras) or None
    mensagem = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?([\w-]{11}:\s*)?", "", detalhe or "").strip() or "erro sem detalhes"
    # a idade vem antes: "Sign in to confirm your age" começa igual ao aviso de robô
    if any(s in texto for s in _IDADE):
        return ErroCanal("o vídeo tem restrição de idade", "video", causa="idade", detalhe=detalhe)
    if "not a bot" in texto or ("confirm you" in texto and " bot" in texto):
        return ErroCanal(MOTIVOS["robo"], "bloqueio", causa="robo", detalhe=detalhe)
    if "http error 429" in texto or "too many requests" in texto:
        return ErroCanal(MOTIVOS["limite"], "bloqueio", causa="limite", detalhe=detalhe)
    if "no supported javascript runtime" in texto:
        return ErroCanal("o yt-dlp precisa do Deno ou do Node.js 22 ou mais novo para baixar do YouTube", "ferramenta",
                         causa="js", detalhe=detalhe)
    # antes do "do vídeo": "Requested format is not available" é yt-dlp velho, não vídeo indisponível
    if any(s in texto for s in _DESATUALIZADO):
        return ErroCanal(mensagem, "desatualizado", causa="desatualizado", detalhe=detalhe)
    if any(s in texto for s in _DO_VIDEO):
        return ErroCanal(mensagem, "video", causa="video", detalhe=detalhe)
    return ErroCanal(mensagem, "temporario", detalhe=detalhe)


def _com_detalhe(erro: Exception) -> str:
    """O pedaço " | yt-dlp: <linha original>" do registro, quando a mensagem não é a própria linha."""
    detalhe = getattr(erro, "detalhe", None)
    return f" | yt-dlp: {detalhe}" if detalhe and str(erro) not in detalhe else ""


class _Olhada:
    """Uma olhada nos canais: as chamadas do yt-dlp, com o ritmo entre elas, a conta separada (só
    quando o YouTube pede) e a pausa quando o YouTube barra (robô ou 429)."""

    def __init__(self, cfg: Config, conn: sqlite3.Connection, base: list[str], parar: threading.Event):
        self.cfg = cfg
        self.conn = conn
        self.base = base
        self.parar = parar
        self._ultima: float | None = None  # time.monotonic() do fim da última chamada
        # direto com a conta: na janela "só com a conta", e no resto da olhada depois que ela passou
        self.com_conta = usar_conta_direto(conn, cfg)
        self.usou_conta = False

    def _esperar_vez(self, rotulo: str | None) -> None:
        """A partir da segunda chamada, espera de RITMO_SEG[0] a RITMO_SEG[1] s (o motor e o
        fechamento cortam a espera na hora)."""
        if self._ultima is None:
            return
        s = random.uniform(*RITMO_SEG)
        ATIVIDADES.definir(f"Canais do YouTube: espero {round(s)} s entre um pedido e outro")
        if self.parar.wait(s):
            raise Interrompido()
        if rotulo:
            ATIVIDADES.definir(rotulo)

    def _executar(self, argumentos: list[str], rotulo: str | None, opcoes: dict):
        self._esperar_vez(rotulo)
        try:
            return executar([*self.base, *argumentos], parar=self.parar,
                            baixa_prioridade=self.cfg["geral"]["prioridade_baixa"], **opcoes)
        finally:
            self._ultima = time.monotonic()

    def rodar(self, argumentos: list[str], rotulo: str | None = None, **opcoes):
        """Uma chamada do yt-dlp (sem o começo do comando). Erro vira ErroCanal; o bloqueio já sai
        com a pausa gravada.

        Cada pedido vai primeiro sem a conta. Só o aviso de robô e a restrição de idade repetem o
        mesmo pedido com a conta separada (se houver uma que sirva); depois disso, o resto da olhada
        vai direto com ela."""
        erro_sem_conta: ErroCanal | None = None
        aviso_robo: ErroCanal | None = None
        if not self.com_conta:
            r = self._executar(argumentos, rotulo, opcoes)
            if r.codigo == 0:
                return r
            erro_sem_conta = _classificar(r.erros)
            if erro_sem_conta.causa not in ("robo", "idade") or not canais_conta.utilizavel(self.cfg):
                raise self._finalizar(erro_sem_conta, _situacao_conta(self.cfg))
            log.info("Canais: %s; repito o mesmo pedido com a conta separada%s", erro_sem_conta,
                     _com_detalhe(erro_sem_conta))
            if erro_sem_conta.causa == "robo":
                aviso_robo = erro_sem_conta
        try:
            with canais_conta.cookies_temporarios(self.cfg) as arq:
                r = self._executar(["--cookies", str(arq.caminho), *argumentos], rotulo, opcoes)
        except canais_conta.ErroConta:
            # a conta foi esquecida (ou caiu) no meio da olhada
            self.com_conta = False
            if erro_sem_conta is not None:
                raise self._finalizar(erro_sem_conta, _situacao_conta(self.cfg)) from None
            self._parar_se_em_pausa()
            return self.rodar(argumentos, rotulo, **opcoes)
        invalidos = any(s in " ".join(r.erros).lower() for s in _COOKIES_INVALIDOS)
        erro = None if r.codigo == 0 else _classificar(r.erros, extras=(str(arq.caminho),))
        if erro is not None:
            erro.com_conta = True
        if r.codigo == 0:
            self.usou_conta = True
        if invalidos:
            canais_conta.marcar_caiu(self.cfg, "o YouTube não aceita mais os cookies da conta (ela saiu ou foi trocada)",
                                     arq.id)
            self.com_conta = False  # o resto da olhada vai sem ela, mas só se nada mandar parar
            if erro is not None and erro.tipo == "bloqueio":
                raise self._finalizar(erro, _situacao_conta(self.cfg))  # robô ou 429 também com a conta
            if aviso_robo is not None:
                # o aviso sem a conta vale como pausa normal: nada de seguir sem ela logo depois do robô
                raise self._finalizar(aviso_robo, _situacao_conta(self.cfg))
            self._parar_se_em_pausa()  # a janela "só com a conta" sem a conta vira pausa
            if erro is None:
                return r
            raise self._finalizar(erro, "tentada" if erro.causa == "idade" else _situacao_conta(self.cfg))
        if erro is None:
            self.com_conta = True
            if aviso_robo is not None:
                p = _registrar_aviso(self.conn, aviso_robo, so_conta=True)
                log.info("Canais: a conta separada passou; até %s as olhadas vão direto com ela",
                         _hora_texto(p["ate"]))
            return r
        if erro.causa == "robo":
            canais_conta.marcar_caiu(self.cfg, "o YouTube pediu verificação mesmo com a conta", arq.id)
        elif erro.causa != "limite" and aviso_robo is not None:
            # a conta passou do aviso de robô (o erro é outro): o resto da olhada vai direto com ela
            self.com_conta = True
            _registrar_aviso(self.conn, aviso_robo, so_conta=True)
        raise self._finalizar(erro, "tentada" if erro.causa == "idade" else _situacao_conta(self.cfg))

    def _parar_se_em_pausa(self) -> None:
        """Depois de perder a conta no meio da olhada: com a pausa valendo (inclusive a janela "só com
        a conta", que sem ela é pausa), para aqui em vez de seguir sem a conta. Não grava de novo."""
        p = pausa(self.conn, self.cfg)
        if p is not None:
            raise ErroCanal(p["mensagem"], "bloqueio", causa=p["tipo"], detalhe=p["detalhe"])

    def _finalizar(self, erro: ErroCanal, conta: str) -> ErroCanal:
        """O erro que sobe: a idade com o texto da conta, e o bloqueio com a pausa já gravada.
        `conta`: nenhuma, ok, caiu ou tentada (a conta foi usada e também não pôde ver o vídeo)."""
        if erro.causa == "idade":
            texto = {
                "tentada": "o vídeo tem restrição de idade, e a conta separada também não pôde ver (confirme a "
                           "idade dela no YouTube)",
                "caiu": "o vídeo tem restrição de idade, e a conta separada precisa entrar de novo",
            }.get(conta, "o vídeo tem restrição de idade: só dá para baixar com a conta separada "
                         "(Filmes > Canais do YouTube)")
            return ErroCanal(texto, erro.tipo, causa=erro.causa, detalhe=erro.detalhe, com_conta=erro.com_conta)
        if erro.tipo != "bloqueio":
            return erro
        p = _registrar_aviso(self.conn, erro)
        return ErroCanal(_mensagem_pausa(p, conta), "bloqueio", causa=erro.causa, detalhe=erro.detalhe,
                         com_conta=erro.com_conta)


def _listar(cfg: Config, olhada: _Olhada, url: str, aba: str, rotulo: str | None = None) -> tuple[dict, list[dict]]:
    """Os vídeos mais novos de uma aba do canal, sem baixar nada (com a data aproximada de cada um)."""
    argumentos = ["--flat-playlist", "--dump-single-json", "--playlist-items", f"1:{VIDEOS_POR_ABA}",
                  "--extractor-args", "youtubetab:approximate_date", f"{url}/{aba}"]
    r = olhada.rodar(argumentos, rotulo=rotulo, capturar_saida=True, timeout=300)
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


def _baixar(cfg: Config, olhada: _Olhada, nome_canal: str, e: dict) -> Path:
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
    argumentos = [
        "--no-playlist", "--newline", "--progress", "--no-mtime",
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
        argumentos += ["--write-subs", "--sub-langs", "pt.*", "--embed-subs"]
    argumentos.append(f"https://www.youtube.com/watch?v={vid}")

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
        olhada.rodar(argumentos, rotulo=rotulo, ao_ler_saida=ler, ao_ler_erro=ler, timeout=6 * 3600)
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


def _baixar_video(cfg: Config, conn: sqlite3.Connection, olhada: _Olhada, canal, nome_canal: str, e: dict,
                  linha) -> Path | None:
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
            caminho = _baixar(cfg, olhada, nome_canal, e)
        except ErroCanal as primeiro:
            # versão velha do yt-dlp é o motivo mais comum de "não consegui": atualiza e tenta mais uma vez
            if primeiro.tipo != "desatualizado" or not ferramentas.atualizar_ytdlp(cfg, forcar=True):
                raise
            log.info("Canais: o yt-dlp foi atualizado; tentando '%s' de novo", titulo)
            caminho = _baixar(cfg, olhada, nome_canal, e)  # também espera a vez
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
        log.warning("Canal %s: '%s' %s: %s%s", nome_canal, titulo,
                    "não serve" if status == "pulado" and tipo == "video" else
                    ("foi deixado de lado depois de 3 tentativas" if status == "pulado" else "não baixou"), erro,
                    _com_detalhe(erro))
        return None
    tamanho = caminho.stat().st_size
    conn.execute("UPDATE downloads SET status='baixado', erro=NULL, arquivo=?, tamanho=?, atualizado_em=? "
                 "WHERE video_id=?", (str(caminho.resolve()), tamanho, time.time(), vid))
    log.info("Canal %s: baixei '%s' (%d MB) para %s", nome_canal, titulo, tamanho // 2**20, caminho.parent)
    return caminho


def _verificar_canal(cfg: Config, conn: sqlite3.Connection, olhada: _Olhada, canal,
                     ao_baixar) -> tuple[list[str], bool]:
    """Olha um canal e baixa o que serve. Devolve (títulos baixados, fila de análise cheia)."""
    c = cfg["canais"]
    parar = olhada.parar
    nome = canal["nome"] or _nome_inicial(canal["url"])
    rotulo = f"Olhando o canal {nome}"
    ATIVIDADES.definir(rotulo)
    entradas: list[dict] = []
    info_canal: dict = {}
    for aba in ("videos", "streams") if c["incluir_ao_vivo"] else ("videos",):
        try:
            info, itens = _listar(cfg, olhada, canal["url"], aba, rotulo)
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
        caminho = _baixar_video(cfg, conn, olhada, canal, nome, e, linha)
        if caminho is not None:
            baixados.append(titulo_curto(str(e.get("title") or e["id"])))
            if ao_baixar is not None:
                ao_baixar()  # a varredura acha o vídeo na hora, sem esperar a próxima
    return baixados, False


def verificar(cfg: Config, conn: sqlite3.Connection, parar: threading.Event, so_canal: int | None = None,
              forcar: bool = False, ao_baixar=None) -> dict:
    """Olha os canais ativos e baixa os vídeos novos que servem. Chame com a `TRAVA` na mão.

    forcar: o "Olhar agora" do painel que você confirmou durante a pausa depois de um bloqueio. Se o
    YouTube barrar de novo, a pausa seguinte é maior. Devolve um resumo para o log e para o painel.
    """
    resumo: dict = {"canais": 0, "baixados": [], "erros": [], "parado": None, "fila_cheia": False, "conta": False}
    gravar_estado(conn, CHAVE_VERIFICADO, time.time())  # mesmo se der erro, a próxima vez é daqui a N horas
    p = pausa(conn, cfg)
    if p and not forcar:
        resumo["parado"] = p["mensagem"]
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
    canais_conta.limpar_sobras(cfg)  # arquivo temporário de cookies de uma olhada que caiu no meio
    olhada = _Olhada(cfg, conn, _base(cfg, ytdlp, js), parar)
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
                baixados, cheia = _verificar_canal(cfg, conn, olhada, canal, ao_baixar)
            except ErroCanal as e:
                conn.execute("UPDATE canais SET erro=?, verificado_em=? WHERE id=?",
                             (str(e)[:300], time.time(), canal["id"]))
                resumo["erros"].append(f"{nome}: {e}")
                # o bloqueio já deixou a pausa gravada (`_Olhada._finalizar`)
                if e.tipo in ("bloqueio", "espaco", "ferramenta"):
                    log.warning("Canais: %s%s", e, _com_detalhe(e))
                    resumo["parado"] = str(e)
                    break
                log.warning("Canal %s: %s%s", nome, e, _com_detalhe(e))
                continue
            conn.execute("UPDATE canais SET erro=NULL, verificado_em=? WHERE id=?", (time.time(), canal["id"]))
            resumo["baixados"] += baixados
            if cheia:
                resumo["fila_cheia"] = True
                break
    finally:
        ATIVIDADES.definir("Canais do YouTube: esperando a próxima olhada", ocioso=True)
    resumo["conta"] = olhada.usou_conta
    if resumo["fila_cheia"]:
        log.info("Canais: %d vídeo(s) baixados esperam a análise; baixo mais quando a fila andar",
                 na_fila(conn))
    if resumo["baixados"]:
        log.info("Canais: %d vídeo(s) novo(s)%s: %s", len(resumo["baixados"]),
                 " (com a conta separada)" if olhada.usou_conta else "", "; ".join(resumo["baixados"]))
    return resumo
