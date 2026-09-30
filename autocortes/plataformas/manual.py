"""Postagem à mão: Kwai e Bilibili (sem API aberta) e qualquer rede com envio = "manual".

No horário da agenda nada é enviado: o AutoCortes cria uma tarefa (postagem com status
'aguardando') com o vídeo e os textos no formato de cada rede. Você posta pelo app ou pelo site
e marca no painel como feito, ou pula. Com [manual].pasta, o vídeo e um .txt com os textos
também vão para uma pasta sincronizada, para pegar no celular.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
from pathlib import Path

from ..config import ROTULOS, Config
from ..textos import Conteudo, limitar, limitar_hashtags, sem_hashtags, tamanho_utf16
from ..util import slug
from .base import ErroPublicacao, Plataforma, Resultado

# tarefas esperando numa rede: passando disso, os próximos horários ficam sem tarefa nova
MAX_TAREFAS_POR_REDE = 5
# onde postar pelo computador (o Kwai só tem o app)
SITES = {
    "youtube": ("https://studio.youtube.com/", "Abrir o YouTube Studio"),
    "tiktok": ("https://www.tiktok.com/tiktokstudio/upload", "Abrir o TikTok Studio"),
    "instagram": ("https://www.instagram.com/", "Abrir o Instagram"),
    "bilibili": ("https://member.bilibili.com/platform/upload/video/frame", "Abrir o envio do Bilibili"),
}
# limites dos textos do Bilibili (formulário de envio do site; a descrição aceita mais, 250 vale sempre)
BILIBILI_TITULO = 80
BILIBILI_DESCRICAO = 250
BILIBILI_TAGS = 10
BILIBILI_TAG = 20


class ViaManual(Plataforma):
    """Rede postada por você: o AutoCortes só separa o vídeo e os textos."""

    via = "manual"

    def __init__(self, nome: str, cfg: Config):
        self.nome = nome
        self.rotulo = f"{ROTULOS[nome]} (à mão)"
        super().__init__(cfg)

    def pronta(self) -> tuple[bool, str]:
        return True, "ok"

    def conta(self) -> str | None:
        return None

    def lembrar_conta(self, descricao: str) -> None:
        pass

    def desconectar(self) -> None:
        pass

    def autorizar(self) -> None:
        print(f"\nO {ROTULOS[self.nome]} está com envio à mão: não há conta para conectar.\n"
              "No horário da agenda, a tarefa aparece no Início do painel com o vídeo e os textos.")

    def verificar(self) -> str:
        return "postagem à mão (as tarefas aparecem no Início do painel)"

    def publicar(self, arquivo: Path, conteudo: Conteudo, parar: threading.Event | None = None) -> Resultado:
        raise ErroPublicacao(f"O {ROTULOS[self.nome]} é postado à mão: use a tarefa do painel", "bloqueio")


# ---------------------------------------------------------------- textos de cada rede

def fonte(conteudo: Conteudo) -> str:
    """De onde vem o trecho (o Bilibili pede a fonte de todo vídeo 转载)."""
    return f"{conteudo.filme} ({conteudo.ano})" if conteudo.ano else conteudo.filme


def _com_texto(conteudo: Conteudo, legenda: str) -> str:
    """Legenda nunca feita só de hashtags (o Kwai pede texto junto)."""
    return legenda if sem_hashtags(legenda) else f"{conteudo.titulo}\n\n{legenda}".strip()


def _tags_bilibili(cfg: Config, conteudo: Conteudo) -> list[str]:
    tags: list[str] = []
    for bruto in list(cfg["bilibili"]["tags"]) + [h.lstrip("#") for h in conteudo.hashtags]:
        tag = re.sub(r"[#,，\s]+", "", str(bruto))[:BILIBILI_TAG]
        if tag and tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
        if len(tags) >= BILIBILI_TAGS:
            break
    return tags


def textos(cfg: Config, rede: str, conteudo: Conteudo) -> dict:
    """Os textos do post já no formato e nos limites da rede.

    Fonte única: usada pela tarefa à mão e pelo roteiro do envio pelo navegador.
    """
    legenda = (conteudo.descricao or conteudo.titulo).strip()
    if rede == "youtube":
        from .youtube import metadados

        meta = metadados(cfg["youtube"], conteudo)["snippet"]
        return {"titulo": meta["title"], "descricao": meta["description"], "tags": list(meta["tags"])}
    if rede == "tiktok":
        texto = legenda
        while tamanho_utf16(texto) > 2200:
            texto = limitar(texto, len(texto) - 20)
        return {"legenda": texto}
    if rede == "instagram":
        from .instagram import MAX_HASHTAGS

        return {"legenda": limitar(limitar_hashtags(legenda, MAX_HASHTAGS), 2200)}
    if rede == "kwai":
        return {"legenda": _com_texto(conteudo, legenda)}
    if rede == "bilibili":
        return {
            "titulo": limitar(conteudo.titulo, BILIBILI_TITULO),
            "descricao": limitar(sem_hashtags(legenda) or conteudo.titulo, BILIBILI_DESCRICAO),
            "tags": _tags_bilibili(cfg, conteudo),
            "fonte": fonte(conteudo),
        }
    raise ValueError(f"rede desconhecida: {rede}")


def pacote(cfg: Config, rede: str, conteudo: Conteudo) -> dict:
    """Textos da tarefa no formato da rede, os passos para postar e o site de envio."""
    campos: list[dict] = []
    t = textos(cfg, rede, conteudo)

    def campo(rotulo: str, texto: str, limite: int | None = None, utf16: bool = False, nota: str = "",
              lista: list[str] | None = None) -> None:
        campos.append({"rotulo": rotulo, "texto": texto, "limite": limite, "nota": nota, "lista": lista,
                       "tamanho": tamanho_utf16(texto) if utf16 else len(texto)})

    if rede == "youtube":
        campo("Título", t["titulo"], 100)
        campo("Descrição", t["descricao"])
        campo("Tags", ", ".join(t["tags"]), nota="Ficam em Mostrar mais > Tags.")
        passos = [
            "No YouTube Studio, clique em Criar > Enviar vídeos e escolha o vídeo.",
            "Cole o título, a descrição e as tags.",
            "Em Público-alvo, marque que o vídeo não é para crianças.",
            "Publique agora ou use Programar para escolher o dia e a hora.",
        ]
    elif rede == "tiktok":
        campo("Legenda", t["legenda"], 2200, utf16=True)
        passos = [
            "No celular, toque em + no app do TikTok e escolha o vídeo. No computador, use o TikTok Studio.",
            "Cole a legenda.",
            "Publique agora ou agende pelo TikTok Studio.",
        ]
    elif rede == "instagram":
        from .instagram import MAX_HASHTAGS

        campo("Legenda", t["legenda"], 2200, nota=f"No máximo {MAX_HASHTAGS} hashtags.")
        passos = ["No app do Instagram, toque em + > Reel e escolha o vídeo.", "Cole a legenda."]
        if cfg["instagram"]["pagina_facebook"]:
            passos.append("Para sair também na Página do Facebook, ligue \"Compartilhar no Facebook\" antes de publicar.")
        passos.append("Publique agora ou agende pelo próprio app.")
    elif rede == "kwai":
        campo("Legenda", t["legenda"])
        passos = [
            "Passe o vídeo para o celular: baixe aqui ou pegue na pasta sincronizada.",
            "No app do Kwai, toque na câmera, depois em Álbum, e escolha o vídeo.",
            "Cole a legenda e publique.",
        ]
    elif rede == "bilibili":
        campo("标题 (título)", t["titulo"], BILIBILI_TITULO)
        campo("简介 (descrição)", t["descricao"], BILIBILI_DESCRICAO)
        campo("标签 (tags)", "\n".join(t["tags"]), nota="No Bilibili, cole uma tag por vez e tecle Enter.",
              lista=t["tags"])
        campo("转载来源 (fonte)", t["fonte"], nota="O trecho é de um filme de terceiros: marque 转载.")
        passos = [
            "Na página de envio do Bilibili, escolha o vídeo.",
            "Em 类型 (tipo), marque 转载 (repost) e cole a fonte em 转载来源.",
            "Em 分区 (zona), escolha 影视 > 影视剪辑.",
            "Cole o título, a descrição e as tags e clique em 立即投稿.",
        ]
    else:
        raise ValueError(f"rede desconhecida: {rede}")
    site = SITES.get(rede)
    return {"campos": campos, "passos": passos, "site": {"url": site[0], "rotulo": site[1]} if site else None}


def texto_do_pacote(rede: str, dados: dict) -> str:
    """O .txt que acompanha o vídeo na pasta sincronizada."""
    linhas = [ROTULOS[rede]]
    for c in dados["campos"]:
        linhas += ["", f"{c['rotulo']}:", c["texto"]]
    return "\n".join(linhas).strip() + "\n"


# ---------------------------------------------------------------- pasta sincronizada

def pasta_sincronizada(cfg: Config) -> Path | None:
    texto = str(cfg["manual"]["pasta"]).strip()
    return cfg.caminho_de(texto) if texto else None


def copiar_para_pasta(cfg: Config, rede: str, tarefa_id: int, arquivo: Path, conteudo: Conteudo, parte) -> Path | None:
    """Copia o vídeo e um .txt com os textos para <pasta>/<rede>/ (lança OSError)."""
    base = pasta_sincronizada(cfg)
    if base is None:
        return None
    destino = base / ROTULOS[rede]
    destino.mkdir(parents=True, exist_ok=True)
    nome = f"{slug(conteudo.filme or 'corte')}-parte{int(parte or 0):02d}-tarefa{int(tarefa_id)}"
    video = destino / f"{nome}.mp4"
    parcial = destino / f"{nome}.mp4.parcial"
    shutil.copyfile(arquivo, parcial)  # o app de sincronização não pega o vídeo pela metade
    os.replace(parcial, video)
    (destino / f"{nome}.txt").write_text(texto_do_pacote(rede, pacote(cfg, rede, conteudo)), encoding="utf-8")
    return video


def copia_da_tarefa(cfg: Config, rede: str, tarefa_id: int) -> Path | None:
    base = pasta_sincronizada(cfg)
    if base is None:
        return None
    try:
        return next(iter(sorted((base / ROTULOS[rede]).glob(f"*-tarefa{int(tarefa_id)}.mp4"))), None)
    except OSError:
        return None


def apagar_copias(cfg: Config, rede: str, tarefa_id: int) -> None:
    """Apaga o vídeo e o .txt da tarefa na pasta sincronizada (só os arquivos com o número dela)."""
    base = pasta_sincronizada(cfg)
    if base is None or rede not in ROTULOS:
        return
    pasta = base / ROTULOS[rede]
    try:
        arquivos = list(pasta.glob(f"*-tarefa{int(tarefa_id)}.*")) if pasta.is_dir() else []
    except OSError:
        return
    for arquivo in arquivos:
        if arquivo.name.lower().endswith((".mp4", ".txt", ".mp4.parcial")):
            try:
                arquivo.unlink(missing_ok=True)
            except OSError:
                pass  # aberto no app de sincronização: fica para você apagar
