"""Leitura, validação e gravação do config.toml (com valores padrão para tudo)."""

from __future__ import annotations

import copy
import os
import re
import shutil
import threading
import tomllib
from pathlib import Path

from .util import log

RAIZ = Path(__file__).resolve().parent.parent
PLATAFORMAS = ("youtube", "tiktok", "instagram", "kwai", "bilibili")
# nomes nos textos e no log (o Facebook não tem agenda própria: vai junto com o Instagram)
ROTULOS = {
    "youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram", "kwai": "Kwai", "bilibili": "Bilibili",
    "facebook": "Facebook",
}
# formas de envio de cada rede: "oficial" (API da rede), "upload_post" (serviço já auditado) ou
# "manual" (o AutoCortes prepara o vídeo e os textos e você posta). Kwai e Bilibili não têm API aberta.
# "navegador" = o AutoCortes posta clicando na página da rede, no Chrome com a sua sessão.
# O Kwai não entra: o upload dele é só pelo app do celular, não existe envio pelo site.
ENVIOS = {
    "youtube": ("oficial", "upload_post", "navegador", "manual"),
    "tiktok": ("oficial", "upload_post", "navegador", "manual"),
    "instagram": ("oficial", "upload_post", "navegador", "manual"),
    "kwai": ("manual",),
    "bilibili": ("navegador", "manual"),
}
DIAS_SEMANA = ("seg", "ter", "qua", "qui", "sex", "sab", "dom")
# campos que nunca voltam para o painel em texto puro
SEGREDOS = (
    ("youtube", "client_secret"), ("tiktok", "client_secret"), ("instagram", "app_secret"),
    ("upload_post", "api_key"), ("ia", "api_key"),
)
LIMPAR = "__limpar__"
# atualizar_de (painel salvando) e copia (edição começando) não se misturam
_TRAVA_DADOS = threading.RLock()

PADRAO: dict = {
    "geral": {
        # nome deste perfil (nicho) no painel; vazio = "Principal"
        "perfil": "",
        # abrir este perfil junto com o principal (só vale nos perfis de perfis/)
        "autoiniciar": False,
        "simulacao": True,
        "pasta_filmes": "filmes",
        "pasta_dados": "dados",
        "buffer_cortes": 3,
        "analisar_antecipado": True,
        "exigir_aprovacao": False,
        "prioridade_baixa": True,
        "impedir_suspensao": True,
        "apagar_apos_postar": False,
        "varrer_a_cada_min": 5,
    },
    "painel": {
        "porta": 8777,
        "abrir_navegador": True,
    },
    "ferramentas": {
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffprobe",
        "whisper_cli": "",
        "whisper_vad": "",
        "whisper_versao": "b5130",
        "threads": 0,
        "hwaccel": "auto",
    },
    "analise": {
        "ignorar_inicio_seg": 90,
        "ignorar_fim_seg": 420,
        "limiar_cena": 10,
        "idiomas_audio": ["por", "pt"],
    },
    "transcricao": {
        "fonte": "auto",
        "idiomas_legenda": ["pt-BR", "pt", "por", "pob"],
        "modelo": "small",
        "idioma": "auto",
    },
    "cortes": {
        "duracao_min": 30,
        "duracao_max": 60,
        "duracao_alvo": 45,
        "max_por_filme": 40,
        "espaco_minimo_seg": 5,
        "palavras_chave": [],
        "ordem_filmes": "intercalar",
        "ordem_cortes": "melhores",
        "pesos": {
            "volume": 1.0,
            "picos": 1.5,
            "fala": 1.2,
            "cenas": 0.8,
            "gancho": 1.0,
            "texto": 0.6,
            "duracao": 0.5,
            "abertura": 0.6,
            "silencio": 2.0,
        },
    },
    "edicao": {
        # modelo visual em uso (os modelos ficam salvos em dados/modelos_visuais.json)
        "modelo": "Padrão",
        "largura": 1080,
        "altura": 1920,
        # imagem PNG por cima do vídeo, com a área do vídeo transparente ("" = sem moldura)
        "moldura": "",
        # desfocado | preto | preencher
        "layout": "desfocado",
        "zoom": 1.25,
        # centro do vídeo na altura, em px (0 = automático)
        "posicao_video": 0,
        "desfoque_fundo": 20,
        "escurecer_fundo": 0.12,
        "legendas": True,
        "palavras_por_bloco": 3,
        "max_caracteres_bloco": 18,
        "maiusculas": True,
        "fonte_arquivo": "C:/Windows/Fonts/ariblk.ttf",
        "fonte_nome": "Arial Black",
        "tamanho_legenda": 76,
        "cor_legenda": "FFFFFF",
        "cor_destaque": "FFD400",
        "contorno_legenda": 6,
        # automatico (abaixo do vídeo se couber na área segura) | abaixo (sempre abaixo do vídeo) | sobre
        "legenda_lugar": "automatico",
        "posicao_legenda": 0,
        "texto_topo": "{filme}\nParte {parte}",
        "tamanho_topo": 60,
        "cor_topo": "FFFFFF",
        # fonte do título ("" = a mesma da legenda)
        "fonte_topo_arquivo": "",
        "fonte_topo_nome": "",
        # base do título, em px (0 = automático)
        "posicao_topo": 0,
        "barra_progresso": True,
        "cor_barra": "FFD400",
        "fade": True,
        "normalizar_audio": True,
        "codec": "libx264",
        "preset": "medium",
        "crf": 20,
    },
    "textos": {
        "titulo": "{filme} - Parte {parte}",
        "descricao": "{frase}\n\n{filme}{ano_parenteses} - Parte {parte}\n\n{hashtags}",
        # usados quando a IA escreveu os textos do corte
        "titulo_ia": "{titulo_ia} | {filme} - Parte {parte}",
        "descricao_ia": "{descricao_ia}\n\n{filme}{ano_parenteses} - Parte {parte}\n\n{hashtags}",
        "hashtags": ["#filmes", "#cinema", "#cenasdefilmes", "#filme"],
        "hashtag_do_filme": True,
        "max_hashtags": 8,
    },
    "ia": {
        # textos das postagens escritos por IA (qualquer API compatível com a da OpenAI)
        "ativo": False,
        "url": "http://127.0.0.1:11434/v1",
        "modelo": "qwen3:4b-instruct-2507-q4_K_M",
        "api_key": "",
        "temperatura": 0.7,
        "tempo_limite_seg": 180,
    },
    "upload_post": {
        # serviço já auditado pelas redes; usado pelas redes com envio = "upload_post"
        "api_key": "",
        "perfil": "",
    },
    "manual": {
        # pasta sincronizada (OneDrive, Google Drive...) que recebe o vídeo e os textos de cada
        # tarefa de postagem à mão, numa subpasta por rede ("" = não copia)
        "pasta": "",
    },
    "navegador": {
        # usado pelas redes com envio = "navegador": o AutoCortes posta pela página da rede,
        # no Chrome com um perfil separado (dados/chrome), onde você loga uma vez
        "programa": "",  # "" = acha o Chrome e, se não houver, o Edge
        "porta": 9222,
        # false esconde a janela; para logar e para ver o que aconteceu, deixe true
        "visivel": True,
        # pausa aleatória entre os passos, em segundos (ritmo humano)
        "pausa_min_seg": 0.4,
        "pausa_max_seg": 1.6,
        "tempo_limite_seg": 180,
        # true preenche tudo e para antes de publicar (para você conferir)
        "ensaio": False,
    },
    "criacao": {
        # vídeos gerados do zero (motivacional, frases, curiosidades), sem partir de um filme
        "ativo": False,
        "fps": 30,
        # segundos de imagem depois da última palavra (dá um respiro antes de cortar)
        "cauda_seg": 0.6,
        # troca a ordem dos clipes em cada vídeo
        "embaralhar_clipes": True,
        # zoom lento nas imagens paradas (sem isso parece apresentação de slides)
        "ken_burns": True,
        # pasta com músicas de fundo ("" = sem música). A música abaixa sozinha sob a voz.
        "pasta_musicas": "",
        "volume_musica": 0.18,
        "fade_audio_seg": 0.6,
    },
    "estoque": {
        # de onde vêm as imagens de fundo dos vídeos gerados: "pasta" (seus arquivos, sem chave),
        # "pexels" ou "pixabay" (bancos gratuitos, com chave de API própria)
        "fonte": "pasta",
        # pasta com seus vídeos e imagens ("" = a pasta "material" ao lado do config)
        "pasta": "",
        # chaves da API (grátis). Várias chaves são alternadas, porque cada uma tem limite por hora.
        "pexels_chaves": [],
        "pixabay_chaves": [],
        # clipe mais curto que isto não entra
        "duracao_min_seg": 3.0,
        # não repetir num vídeo novo o material que já saiu antes (as redes tratam repetição
        # como conteúdo não original)
        "evitar_repetidos": True,
    },
    "voz": {
        # narração dos vídeos gerados do zero. "edge" = serviço de leitura em voz alta do
        # Microsoft Edge (gratuito, vozes neurais, devolve o tempo de cada palavra).
        # O texto do roteiro sai do computador.
        "motor": "edge",
        "voz": "pt-BR-AntonioNeural",
        # velocidade, tom e volume da fala (ex.: "+10%", "-2Hz")
        "ritmo": "+0%",
        "tom": "+0Hz",
        "volume": "+0%",
        # silêncio entre as linhas do roteiro, para dar respiro (use [pausa: 2s] no texto
        # quando quiser uma pausa maior num ponto exato)
        "pausa_linha_seg": 0.35,
        "tempo_limite_seg": 120,
    },
    "metricas": {
        "ativo": True,
        "intervalo_horas": 6,
        "janela_dias": 14,
    },
    "agenda": {
        # usado pelas redes que não têm horários próprios
        "horarios": ["12:00", "18:00", "21:00"],
        "dias": list(DIAS_SEMANA),
        "variacao_minutos": 10,
        "tolerancia_minutos": 90,
        "intervalo_minimo_min": 60,
        "maximo_por_dia": 6,
        "max_tentativas": 3,
        "espera_erro_min": 10,
        "pausado": False,
    },
    "youtube": {
        "ativo": True,
        # "oficial" (API do YouTube), "upload_post" ou "manual"
        "envio": "oficial",
        "client_id": "",
        "client_secret": "",
        "privacidade": "public",
        "categoria": "1",
        "tags": ["filmes", "cinema", "shorts"],
        "adicionar_shorts": True,
        # o YouTube só recebe Shorts: vídeo vertical ou quadrado de até esta duração (o Short vai até 180 s)
        "max_segundos": 180,
        "porta_redirect": 0,
        "horarios": ["12:30", "15:00", "20:00"],
        "dias": [],
    },
    "tiktok": {
        "ativo": True,
        "envio": "oficial",
        "client_key": "",
        "client_secret": "",
        "porta_redirect": 8765,
        "modo": "direto",
        "privacidade": "SELF_ONLY",
        "permitir_comentarios": True,
        "permitir_duet": False,
        "permitir_stitch": False,
        "capa_ms": 1000,
        "horarios": ["12:00", "18:30", "21:00"],
        "dias": [],
    },
    "instagram": {
        "ativo": True,
        "envio": "oficial",
        "app_id": "",
        "app_secret": "",
        "versao_api": "v26.0",
        "compartilhar_no_feed": True,
        "capa_ms": 1000,
        # reel de teste: mostrado primeiro só para quem não segue a conta
        "trial_reels": False,
        # MANUAL (você libera no app) ou SS_PERFORMANCE (libera sozinho se for bem)
        "trial_graduacao": "SS_PERFORMANCE",
        # posta o mesmo Reel na Página do Facebook ligada à conta (API oficial ou Upload-Post)
        "pagina_facebook": False,
        # Upload-Post com mais de uma Página conectada: o ID da Página que recebe o Reel
        "facebook_pagina_id": "",
        "horarios": ["12:15", "19:00"],
        "dias": [],
    },
    "kwai": {
        # sem API de postagem para criadores: no horário, vira uma tarefa para você postar pelo app
        "ativo": False,
        "envio": "manual",
        "horarios": ["12:00", "19:30"],
        "dias": [],
    },
    "bilibili": {
        # a API de envio é só para empresas chinesas: no horário, vira uma tarefa para você enviar pelo site
        "ativo": False,
        "envio": "manual",
        # 08:00 em Brasília = 19:00 em Pequim
        "horarios": ["08:00"],
        "dias": [],
        # tags fixas (sem #), somadas às hashtags do post
        "tags": ["电影", "影视剪辑", "电影片段"],
    },
}


class ErroConfig(Exception):
    def __init__(self, mensagem: str, erros: list[str] | None = None):
        super().__init__(mensagem)
        self.erros = erros or [mensagem]


class Config:
    def __init__(self, dados: dict, caminho: Path):
        self.dados = dados
        self.caminho = caminho
        self.raiz = caminho.parent

    def __getitem__(self, secao: str) -> dict:
        return self.dados[secao]

    def caminho_de(self, valor: str) -> Path:
        p = Path(valor).expanduser()
        return p if p.is_absolute() else (self.raiz / p)

    @property
    def simulacao(self) -> bool:
        return bool(self["geral"]["simulacao"])

    @property
    def pasta_filmes(self) -> Path:
        return self.caminho_de(self["geral"]["pasta_filmes"])

    @property
    def pasta_dados(self) -> Path:
        return self.caminho_de(self["geral"]["pasta_dados"])

    # modelos e ferramentas pertencem à instalação, não ao arquivo de config
    @property
    def pasta_modelos(self) -> Path:
        return RAIZ / "modelos"

    @property
    def pasta_ferramentas(self) -> Path:
        return RAIZ / "ferramentas"

    @property
    def banco(self) -> Path:
        return self.pasta_dados / "autocortes.db"

    @property
    def pasta_tokens(self) -> Path:
        return self.pasta_dados / "tokens"

    @property
    def pasta_logs(self) -> Path:
        return self.pasta_dados / "logs"

    @property
    def pasta_analise(self) -> Path:
        return self.pasta_dados / "analise"

    @property
    def pasta_cortes(self) -> Path:
        return self.pasta_dados / "cortes"

    @property
    def pasta_fontes(self) -> Path:
        return self.pasta_dados / "fontes"

    @property
    def pasta_previa(self) -> Path:
        return self.pasta_dados / "previa"

    @property
    def pasta_molduras(self) -> Path:
        """Molduras (PNG) disponíveis no Estúdio, ao lado da pasta de filmes."""
        return self.raiz / "molduras"

    def plataformas_ativas(self) -> list[str]:
        return [p for p in PLATAFORMAS if self[p]["ativo"]]

    def horarios(self, plataforma: str) -> list[str]:
        return list(self[plataforma].get("horarios") or self["agenda"]["horarios"])

    def dias(self, plataforma: str) -> list[str]:
        return list(self[plataforma].get("dias") or self["agenda"]["dias"])

    def segredo_definido(self, secao: str, chave: str) -> bool:
        return bool(str(self[secao].get(chave) or "").strip())

    def atualizar_de(self, outro: "Config") -> None:
        """Copia os valores de outro Config para dentro dos mesmos dicionários.

        Quem guardou uma referência a uma seção (ex.: as plataformas) passa a ver os
        valores novos sem precisar reiniciar o loop.
        """
        def copiar(destino: dict, origem: dict) -> None:
            for chave, valor in origem.items():
                if isinstance(valor, dict) and isinstance(destino.get(chave), dict):
                    copiar(destino[chave], valor)
                else:
                    destino[chave] = copy.deepcopy(valor)

        with _TRAVA_DADOS:
            copiar(self.dados, outro.dados)

    def copia(self) -> "Config":
        """Retrato da configuração: nada muda nele se o painel salvar no meio de um trabalho."""
        with _TRAVA_DADOS:
            return Config(copy.deepcopy(self.dados), self.caminho)

    def para_painel(self) -> dict:
        """Cópia dos dados sem os segredos (o painel só sabe se estão preenchidos)."""
        dados = copy.deepcopy(self.dados)
        dados["_segredos"] = {}
        for secao, chave in SEGREDOS:
            dados["_segredos"][f"{secao}.{chave}"] = self.segredo_definido(secao, chave)
            dados[secao][chave] = ""
        return dados


# ---------------------------------------------------------------- leitura

def _mesclar(base: dict, novo: dict, prefixo: str, avisos: list[str]) -> None:
    for chave, valor in novo.items():
        if chave not in base:
            avisos.append(prefixo + chave)
            base[chave] = valor
        elif isinstance(base[chave], dict) and isinstance(valor, dict):
            _mesclar(base[chave], valor, f"{prefixo}{chave}.", avisos)
        else:
            base[chave] = valor


def _normalizar(padrao: dict, dados: dict) -> None:
    """Conversões óbvias: 1 -> 1.0 em campos decimais, 3 -> "3" em campos de texto..."""
    for chave, base in padrao.items():
        if chave not in dados:
            continue
        valor = dados[chave]
        if isinstance(base, dict):
            if isinstance(valor, dict):
                _normalizar(base, valor)
        elif isinstance(base, bool) or isinstance(valor, bool):
            continue
        elif isinstance(base, float) and isinstance(valor, int):
            dados[chave] = float(valor)
        elif isinstance(base, int) and isinstance(valor, float) and valor.is_integer():
            dados[chave] = int(valor)
        elif isinstance(base, str) and isinstance(valor, (int, float)):
            dados[chave] = str(valor)


def criar_config_se_faltar(caminho: Path) -> bool:
    """Copia o config.example.toml para config.toml na primeira execução."""
    if caminho.exists():
        return False
    exemplo = RAIZ / "config.example.toml"
    if not exemplo.exists():
        raise ErroConfig(f"{caminho.name} não existe e config.example.toml também não foi encontrado")
    shutil.copyfile(exemplo, caminho)
    return True


def carregar(caminho: Path | None = None) -> Config:
    caminho = (caminho or RAIZ / "config.toml").resolve()
    dados = copy.deepcopy(PADRAO)
    if caminho.exists():
        try:
            bruto = tomllib.loads(caminho.read_text(encoding="utf-8-sig"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            raise ErroConfig(f"Erro de sintaxe em {caminho.name}: {e}") from e
        avisos: list[str] = []
        _mesclar(dados, bruto, "", avisos)
        for chave in avisos:
            log.warning("Opção desconhecida em %s: %s", caminho.name, chave)
    _normalizar(PADRAO, dados)
    cfg = Config(dados, caminho)
    validar(cfg)
    return cfg


# ---------------------------------------------------------------- validação

_HORA = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _checar_tipos(padrao: dict, dados: dict, prefixo: str, erros: list[str]) -> None:
    for chave, base in padrao.items():
        if chave not in dados:
            continue
        valor, nome = dados[chave], f"{prefixo}{chave}"
        if isinstance(base, dict):
            if not isinstance(valor, dict):
                erros.append(f"[{nome}] deveria ser uma seção")
            else:
                _checar_tipos(base, valor, nome + ".", erros)
        elif isinstance(base, bool):
            if not isinstance(valor, bool):
                erros.append(f"{nome} deve ser true ou false")
        elif isinstance(base, (int, float)):
            if isinstance(valor, bool) or not isinstance(valor, (int, float)):
                erros.append(f"{nome} deve ser um número")
            elif isinstance(base, int) and not isinstance(valor, int):
                erros.append(f"{nome} deve ser um número inteiro")
        elif isinstance(base, str):
            if not isinstance(valor, str):
                erros.append(f"{nome} deve ser um texto")
        elif isinstance(base, list):
            if not isinstance(valor, list):
                erros.append(f"{nome} deve ser uma lista")


def validar(cfg: Config) -> None:
    erros: list[str] = []
    _checar_tipos(PADRAO, cfg.dados, "", erros)
    if erros:
        raise ErroConfig("Problemas no config:\n  - " + "\n  - ".join(erros), erros)

    def numero(secao: str, chave: str, minimo: float, maximo: float) -> None:
        valor = cfg[secao][chave]
        if not minimo <= valor <= maximo:
            erros.append(f"[{secao}].{chave} deve ficar entre {minimo:g} e {maximo:g} (atual: {valor!r})")

    def opcao(secao: str, chave: str, validas: tuple[str, ...]) -> None:
        if cfg[secao][chave] not in validas:
            erros.append(f"[{secao}].{chave} deve ser um de {validas} (atual: {cfg[secao][chave]!r})")

    numero("geral", "buffer_cortes", 1, 50)
    numero("geral", "varrer_a_cada_min", 1, 1440)
    numero("painel", "porta", 1024, 65535)
    numero("cortes", "duracao_min", 5, 600)
    numero("cortes", "duracao_max", 5, 600)
    numero("cortes", "duracao_alvo", 5, 600)
    numero("cortes", "max_por_filme", 1, 1000)
    numero("cortes", "espaco_minimo_seg", 0, 600)
    numero("edicao", "zoom", 0.3, 3.0)
    numero("edicao", "palavras_por_bloco", 1, 12)
    numero("edicao", "max_caracteres_bloco", 4, 60)
    numero("edicao", "tamanho_legenda", 20, 200)
    numero("edicao", "tamanho_topo", 20, 200)
    numero("edicao", "desfoque_fundo", 1, 60)
    numero("edicao", "escurecer_fundo", 0, 0.9)
    numero("edicao", "contorno_legenda", 0, 20)
    numero("edicao", "crf", 0, 51)
    numero("textos", "max_hashtags", 0, 30)
    numero("agenda", "maximo_por_dia", 1, 100)
    numero("agenda", "tolerancia_minutos", 0, 720)
    numero("agenda", "variacao_minutos", 0, 120)
    numero("agenda", "intervalo_minimo_min", 0, 1440)
    numero("agenda", "max_tentativas", 1, 20)
    numero("agenda", "espera_erro_min", 1, 1440)
    opcao("transcricao", "fonte", ("auto", "whisper", "arquivo", "nenhuma"))
    opcao("cortes", "ordem_filmes", ("intercalar", "sequencial"))
    opcao("cortes", "ordem_cortes", ("melhores", "cronologica"))
    opcao("edicao", "layout", ("desfocado", "preto", "preencher"))
    opcao("edicao", "legenda_lugar", ("automatico", "abaixo", "sobre"))
    opcao("youtube", "privacidade", ("public", "unlisted", "private"))
    opcao("tiktok", "modo", ("direto", "rascunho"))
    opcao("instagram", "trial_graduacao", ("MANUAL", "SS_PERFORMANCE"))
    for rede in PLATAFORMAS:
        opcao(rede, "envio", ENVIOS[rede])
    numero("youtube", "max_segundos", 15, 180)
    numero("navegador", "porta", 1024, 65535)
    numero("navegador", "pausa_min_seg", 0, 30)
    numero("navegador", "pausa_max_seg", 0, 60)
    numero("navegador", "tempo_limite_seg", 30, 1800)
    if cfg["navegador"]["pausa_min_seg"] > cfg["navegador"]["pausa_max_seg"]:
        erros.append("[navegador].pausa_min_seg não pode ser maior que pausa_max_seg")
    if cfg["navegador"]["porta"] == cfg["painel"]["porta"]:
        erros.append("[navegador].porta não pode ser a mesma do painel")
    if not re.fullmatch(r"\d{0,30}", str(cfg["instagram"]["facebook_pagina_id"]).strip()):
        erros.append("[instagram].facebook_pagina_id deve ter só os números do ID da Página")
    if re.search(r"[\x00-\x1f*?\"<>|]", str(cfg["manual"]["pasta"])):
        erros.append("[manual].pasta tem caracteres que não valem num caminho do Windows")
    numero("ia", "temperatura", 0, 2)
    numero("ia", "tempo_limite_seg", 10, 1800)
    numero("criacao", "fps", 24, 60)
    numero("criacao", "cauda_seg", 0, 5)
    numero("criacao", "volume_musica", 0, 1)
    numero("criacao", "fade_audio_seg", 0, 3)
    if re.search(r"[\x00-\x1f*?\"<>|]", str(cfg["criacao"]["pasta_musicas"])):
        erros.append("[criacao].pasta_musicas tem caracteres que não valem num caminho do Windows")
    opcao("estoque", "fonte", ("pasta", "pexels", "pixabay"))
    numero("estoque", "duracao_min_seg", 0, 60)
    if re.search(r"[\x00-\x1f*?\"<>|]", str(cfg["estoque"]["pasta"])):
        erros.append("[estoque].pasta tem caracteres que não valem num caminho do Windows")
    for fonte in ("pexels", "pixabay"):
        if cfg["estoque"]["fonte"] == fonte and not [c for c in cfg["estoque"][f"{fonte}_chaves"] if str(c).strip()]:
            erros.append(f"[estoque].fonte = \"{fonte}\" precisa de pelo menos uma chave em {fonte}_chaves")
        for chave in cfg["estoque"][f"{fonte}_chaves"]:
            if not re.fullmatch(r"[\w-]{10,120}", str(chave).strip()):
                erros.append(f"[estoque].{fonte}_chaves tem uma chave com caracteres inválidos")
    opcao("voz", "motor", ("edge",))
    numero("voz", "tempo_limite_seg", 10, 900)
    numero("voz", "pausa_linha_seg", 0, 5)
    if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}-[A-Za-z]+", str(cfg["voz"]["voz"]).strip()):
        erros.append("[voz].voz deve ser um nome de voz como pt-BR-AntonioNeural")
    for chave, padrao_valor in (("ritmo", r"[+-]\d{1,3}%"), ("volume", r"[+-]\d{1,3}%"), ("tom", r"[+-]\d{1,3}Hz")):
        if not re.fullmatch(padrao_valor, str(cfg["voz"][chave]).strip()):
            exemplo = "+10%" if chave != "tom" else "+2Hz"
            erros.append(f"[voz].{chave} deve ser algo como {exemplo}")
    numero("metricas", "intervalo_horas", 1, 168)
    numero("metricas", "janela_dias", 1, 90)
    if not re.fullmatch(r"https?://[^\s]+", str(cfg["ia"]["url"]).strip()):
        erros.append("[ia].url deve começar com http:// ou https://")
    if cfg["upload_post"]["perfil"] and not re.fullmatch(r"[\w.@+-]{1,100}", str(cfg["upload_post"]["perfil"])):
        erros.append("[upload_post].perfil tem caracteres inválidos")
    opcao(
        "tiktok",
        "privacidade",
        ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"),
    )

    c = cfg["cortes"]
    if c["duracao_min"] > c["duracao_max"]:
        erros.append("[cortes].duracao_min não pode ser maior que duracao_max")
    for dim in ("largura", "altura"):
        v = cfg["edicao"][dim]
        if v < 240 or v % 2:
            erros.append(f"[edicao].{dim} deve ser um inteiro par >= 240")
    for cor in ("cor_legenda", "cor_destaque", "cor_barra", "cor_topo"):
        if not re.fullmatch(r"#?[0-9A-Fa-f]{6}", str(cfg["edicao"][cor])):
            erros.append(f"[edicao].{cor} deve ser uma cor RRGGBB (ex.: FFD400)")
    ed = cfg["edicao"]
    for pos in ("posicao_video", "posicao_topo", "posicao_legenda"):
        if isinstance(ed[pos], int) and not 0 <= ed[pos] <= ed["altura"]:
            erros.append(f"[edicao].{pos} deve ficar entre 0 (automático) e {ed['altura']}")
    if str(ed["moldura"]).strip() and not str(ed["moldura"]).strip().lower().endswith(".png"):
        erros.append("[edicao].moldura deve ser uma imagem .png (com a área do vídeo transparente)")
    for fonte in ("fonte_nome", "fonte_topo_nome"):
        if re.search(r"[,\x00-\x1f{}\\]", str(ed[fonte])):  # quebraria a linha do estilo na legenda (ASS)
            erros.append(f"[edicao].{fonte} não pode ter vírgula, chaves ou barra invertida")
    if not re.fullmatch(r"[^\\/:*?\"<>|\x00-\x1f]{1,60}", str(ed["modelo"]).strip()):
        erros.append("[edicao].modelo deve ter de 1 a 60 caracteres, sem \\ / : * ? \" < > |")
    if str(cfg["geral"]["perfil"]).strip() and not re.fullmatch(
            r"[^\\/:*?\"<>|\x00-\x1f]{1,60}", str(cfg["geral"]["perfil"]).strip()):
        erros.append("[geral].perfil deve ter de 1 a 60 caracteres, sem \\ / : * ? \" < > |")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", str(cfg["transcricao"]["modelo"])):
        erros.append("[transcricao].modelo inválido")

    for nome in ("agenda",) + PLATAFORMAS:
        for h in cfg[nome].get("horarios", []):
            if not isinstance(h, str) or not _HORA.match(h.strip()):
                erros.append(f"[{nome}].horarios tem um horário inválido: {h!r} (use HH:MM)")
        for d in cfg[nome].get("dias", []):
            if d not in DIAS_SEMANA:
                erros.append(f"[{nome}].dias tem um dia inválido: {d!r} (use {', '.join(DIAS_SEMANA)})")
    if not cfg["agenda"]["horarios"]:
        erros.append("[agenda].horarios não pode ficar vazio")
    if not cfg["agenda"]["dias"]:
        erros.append("[agenda].dias precisa de pelo menos um dia")

    if erros:
        raise ErroConfig("Problemas no config:\n  - " + "\n  - ".join(erros), erros)


# ---------------------------------------------------------------- gravação

def _coagir(valor, base):
    """Converte o que vem do painel (texto de formulário) para o tipo do valor padrão."""
    try:
        if isinstance(base, bool):
            if isinstance(valor, str):
                return valor.strip().lower() in ("true", "1", "sim", "on", "yes")
            return bool(valor)
        if isinstance(base, int):
            if isinstance(valor, str):
                valor = float(valor.replace(",", ".").strip())
            if isinstance(valor, float) and valor.is_integer():
                return int(valor)
            return valor
        if isinstance(base, float):
            if isinstance(valor, str):
                return float(valor.replace(",", ".").strip())
            if isinstance(valor, int) and not isinstance(valor, bool):
                return float(valor)
            return valor
        if isinstance(base, str):
            return "" if valor is None else str(valor)
        if isinstance(base, list):
            if isinstance(valor, str):
                valor = [p for p in re.split(r"[,\n]", valor)]
            if isinstance(valor, list):
                return [str(v).strip() for v in valor if str(v).strip()]
    except (TypeError, ValueError):
        return valor
    return valor


def _filtrar(padrao: dict, alteracoes: dict, prefixo: tuple = ()) -> dict:
    saida: dict = {}
    for chave, valor in alteracoes.items():
        if chave not in padrao:
            continue
        base, caminho = padrao[chave], prefixo + (chave,)
        if isinstance(base, dict):
            if isinstance(valor, dict):
                sub = _filtrar(base, valor, caminho)
                if sub:
                    saida[chave] = sub
            continue
        if caminho in SEGREDOS:
            if valor in (None, ""):
                continue  # vazio = manter o segredo que já está salvo
            if valor == LIMPAR:
                valor = ""
        if caminho[-1] == "horarios" and isinstance(valor, list):
            valor = sorted({_hora_normal(v) for v in valor if str(v).strip()})
        saida[chave] = _coagir(valor, base)
    return saida


def _hora_normal(valor) -> str:
    texto = str(valor).strip()
    m = _HORA.match(texto)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else texto


def _mesclar_valores(destino: dict, alteracoes: dict) -> None:
    for chave, valor in alteracoes.items():
        if isinstance(valor, dict) and isinstance(destino.get(chave), dict):
            _mesclar_valores(destino[chave], valor)
        else:
            destino[chave] = valor


def _gravar_doc(tabela, alteracoes: dict) -> None:
    import tomlkit

    for chave, valor in alteracoes.items():
        if isinstance(valor, dict):
            if not isinstance(tabela.get(chave), dict):
                tabela[chave] = tomlkit.table()
            _gravar_doc(tabela[chave], valor)
        elif isinstance(valor, str) and "\n" in valor:
            tabela[chave] = tomlkit.string(valor, multiline=True)
        else:
            tabela[chave] = valor


def _aplicar(cfg: Config, alteracoes: dict) -> tuple[Config, dict]:
    limpo = _filtrar(PADRAO, alteracoes if isinstance(alteracoes, dict) else {})
    novos = copy.deepcopy(cfg.dados)
    _mesclar_valores(novos, limpo)
    _normalizar(PADRAO, novos)
    novo = Config(novos, cfg.caminho)
    validar(novo)  # lança ErroConfig com a lista de problemas
    return novo, limpo


def simular(cfg: Config, alteracoes: dict) -> Config:
    """Config com as alterações aplicadas, sem gravar nada (usado na prévia do painel)."""
    return _aplicar(cfg, alteracoes)[0]


def salvar(cfg: Config, alteracoes: dict) -> Config:
    """Aplica alterações vindas do painel, valida e grava o config.toml mantendo os comentários."""
    import tomlkit

    novo, limpo = _aplicar(cfg, alteracoes)
    if not limpo:
        return novo

    texto = cfg.caminho.read_text(encoding="utf-8-sig") if cfg.caminho.exists() else ""
    doc = tomlkit.parse(texto)
    _gravar_doc(doc, limpo)
    temporario = cfg.caminho.with_name(cfg.caminho.name + ".tmp")
    temporario.write_text(tomlkit.dumps(doc), encoding="utf-8")
    os.replace(temporario, cfg.caminho)
    return novo
