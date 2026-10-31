"""Leitura, validação e gravação do config.toml (com valores padrão para tudo)."""

from __future__ import annotations

import copy
import re
import shutil
import threading
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from .util import escrever_atomico, log

RAIZ = Path(__file__).resolve().parent.parent
PLATAFORMAS = ("youtube", "tiktok", "instagram", "facebook", "kwai", "bilibili")
# nomes nos textos e no log
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
    "facebook": ("oficial", "upload_post", "navegador", "manual"),
    "kwai": ("manual",),
    "bilibili": ("navegador", "manual"),
}
DIAS_SEMANA = ("seg", "ter", "qua", "qui", "sex", "sab", "dom")
# posts de imagem tirados dos cortes: só estas redes, e só pelo roteiro do navegador
REDES_IMAGEM = ("tiktok", "instagram")
TIPOS_IMAGEM = ("momento", "frase", "carrossel")
FORMATOS_IMAGEM = {
    "instagram": {"nome": "4x5", "largura": 1080, "altura": 1350},
    "tiktok": {"nome": "9x16", "largura": 1080, "altura": 1920},
}
# por que a rede ainda não posta imagens (o painel mostra a caixa Imagens desligada com este texto)
IMAGEM_INDISPONIVEL = {
    "youtube": "As imagens do YouTube (posts da Comunidade) ficam para uma próxima etapa: a página de post da "
               "Comunidade ainda não foi verificada, e a API do YouTube não posta na Comunidade.",
    "facebook": "As imagens da Página do Facebook ficam para uma próxima etapa: a API de fotos da Página aceita "
                "o arquivo direto, mas o envio e a conferência da Página no post de foto ainda não foram feitos.",
    "kwai": "O Kwai não tem página de envio no computador, e a imagem como tarefa à mão fica para uma "
            "próxima etapa.",
    "bilibili": "As imagens do Bilibili ficam para uma próxima etapa: a página de envio de fotos dele ainda "
                "não foi verificada.",
}
SO_PELO_NAVEGADOR = "as imagens saem só pelo navegador: troque o envio para Navegador"
# campos que nunca voltam para o painel em texto puro
SEGREDOS = (
    ("youtube", "client_secret"), ("tiktok", "client_secret"), ("instagram", "app_secret"),
    ("facebook", "app_secret"), ("upload_post", "api_key"), ("ia", "api_key"),
    # listas de chaves: também nunca voltam para o painel
    ("estoque", "pexels_chaves"), ("estoque", "pixabay_chaves"),
)
# codificadores de vídeo que o painel oferece (o da placa cai para o libx264 quando falha)
CODECS = ("libx264", "h264_amf", "h264_nvenc", "h264_qsv")
PRESETS_X264 = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
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
        # programa que baixa os vídeos dos canais ("" = o yt-dlp oficial, baixado em ferramentas/yt-dlp)
        "ytdlp": "",
        # o yt-dlp oficial se atualiza sozinho (o YouTube muda e versões antigas param de baixar)
        "ytdlp_atualizar": True,
    },
    "canais": {
        # baixa sozinho os vídeos novos dos canais do YouTube cadastrados na tela Filmes.
        # Só canais seus ou que autorizam cortes: vídeo de terceiros sem permissão viola os termos
        # do YouTube e os direitos de quem fez.
        "ativo": False,
        # de quantas em quantas horas os canais são olhados
        "verificar_a_cada_horas": 6,
        # só vídeos publicados nos últimos N dias (0 = qualquer data)
        "max_idade_dias": 30,
        # duração do vídeo, em minutos (0 no máximo = sem limite)
        "minutos_min": 5,
        "minutos_max": 240,
        # transmissões ao vivo que já terminaram (a aba "Ao vivo" do canal)
        "incluir_ao_vivo": False,
        # vídeos novos de cada canal por vez
        "max_por_canal": 2,
        # não baixa mais enquanto houver estes vídeos baixados esperando a análise
        "max_na_fila": 3,
        "resolucao": 1080,
        # pula vídeos com estas palavras no título (maiúscula e acento não importam)
        "ignorar_palavras": [],
        # legendas que o canal publicou (as automáticas não entram): poupam a transcrição
        "legendas": True,
        # espaço livre mínimo no disco da pasta de filmes, em GB
        "espaco_minimo_gb": 20,
        # subpasta da pasta de filmes que recebe os vídeos (uma pasta por canal)
        "pasta": "YouTube",
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
        # quem sai primeiro quando falta estoque: "filmes" (os cortes) ou "criacao" (as pautas)
        "prioridade": "filmes",
        # duração que a IA tenta atingir no roteiro que ela escreve
        "duracao_alvo_seg": 40,
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
    "imagens": {
        # posts de imagem tirados dos cortes (ligados por rede, em [tiktok] e [instagram]):
        # momento (um quadro com o nome e a parte), frase (a fala real do trecho) e carrossel
        "tipos": list(TIPOS_IMAGEM),
        # quadros do carrossel (de 3 a 5)
        "quadros_carrossel": 4,
        # texto desenhado em cima do quadro
        "topo": "{filme}\nParte {parte}",
        # chamada para o vídeo, desenhada embaixo (só quando o vídeo daquela parte já saiu na rede)
        "chamada": "A Parte {parte} completa está no perfil",
        # legenda do post, por tipo
        "legenda_momento": "{titulo_ia}\n\n{filme}{ano_parenteses} - Parte {parte}\n{chamada_perfil}\n\n{hashtags}",
        "legenda_frase": "“{frase}”\n\n{filme}{ano_parenteses}\n\n{hashtags}",
        "legenda_carrossel": "{filme}{ano_parenteses} - Parte {parte}, em {quadros} cenas.\n{chamada_perfil}"
                             "\n\n{hashtags}",
        # usa o texto que a IA já escreveu para o corte ({titulo_ia} e {descricao_ia}), sem chamar a IA de novo
        "usar_ia": True,
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
        # posta os cortes em vídeo nesta rede
        "videos": True,
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
        "videos": True,
        # posts de imagem (só com envio = "navegador" e o roteiro de imagem gravado)
        "imagens": False,
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
        # horários dos posts de imagem (longe dos vídeos: perto demais, a imagem sai do dia)
        "horarios_imagens": ["10:30", "15:30"],
        # vazio = os mesmos dias dos vídeos desta rede
        "dias_imagens": [],
    },
    "instagram": {
        "ativo": True,
        "videos": True,
        # posts de imagem (só com envio = "navegador" e o roteiro de imagem gravado)
        "imagens": False,
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
        "horarios": ["12:15", "19:00"],
        "dias": [],
        "horarios_imagens": ["10:00", "16:00"],
        "dias_imagens": [],
    },
    "facebook": {
        # Reels na Página do Facebook (nunca no perfil pessoal)
        "ativo": False,
        "videos": True,
        "envio": "navegador",
        # o link da Página (https://www.facebook.com/SuaPagina ou profile.php?id=...) ou o nome exato dela
        "pagina": "",
        # preenchidos pelo AutoCortes quando ele confirma a Página
        "pagina_id": "",
        "pagina_nome": "",
        # API oficial: vazios = o app do [instagram]
        "app_id": "",
        "app_secret": "",
        "versao_api": "v26.0",
        # a API de Reels da Página aceita de 3 a 90 s
        "max_segundos": 90,
        "horarios": ["13:30", "21:45"],
        "dias": [],
    },
    "kwai": {
        # sem API de postagem para criadores: no horário, vira uma tarefa para você postar pelo app
        "ativo": False,
        "videos": True,
        "envio": "manual",
        "horarios": ["12:00", "19:30"],
        "dias": [],
    },
    "bilibili": {
        # a API de envio é só para empresas chinesas: no horário, vira uma tarefa para você enviar pelo site
        "ativo": False,
        "videos": True,
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
        # o [facebook] veio da opção antiga do Instagram e ainda não está no arquivo (o salvar grava)
        self.migrado_facebook = False

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

    def redes_de_video(self) -> list[str]:
        """Redes ligadas que postam os cortes em vídeo."""
        return [p for p in self.plataformas_ativas() if self[p].get("videos", True)]

    def redes_de_imagem(self) -> list[str]:
        """Redes ligadas que postam imagens: só as de REDES_IMAGEM, e só pelo navegador."""
        return [p for p in self.plataformas_ativas()
                if p in REDES_IMAGEM and self[p].get("imagens") and self[p].get("envio") == "navegador"]

    def horarios(self, plataforma: str, midia: str = "video") -> list[str]:
        if midia == "imagem":  # sem lista geral: a rede sem horários de imagem não ganha imagens
            return list(self[plataforma].get("horarios_imagens") or [])
        return list(self[plataforma].get("horarios") or self["agenda"]["horarios"])

    def dias(self, plataforma: str, midia: str = "video") -> list[str]:
        if midia == "imagem":
            return list(self[plataforma].get("dias_imagens") or self.dias(plataforma))
        return list(self[plataforma].get("dias") or self["agenda"]["dias"])

    def segredo_definido(self, secao: str, chave: str) -> bool:
        valor = self[secao].get(chave)
        if isinstance(valor, list):
            return any(str(v).strip() for v in valor)
        return bool(str(valor or "").strip())

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
            self.migrado_facebook = outro.migrado_facebook

    def copia(self) -> "Config":
        """Retrato da configuração: nada muda nele se o painel salvar no meio de um trabalho."""
        with _TRAVA_DADOS:
            novo = Config(copy.deepcopy(self.dados), self.caminho)
            novo.migrado_facebook = self.migrado_facebook
            return novo

    def para_painel(self) -> dict:
        """Cópia dos dados sem os segredos (o painel só sabe se estão preenchidos)."""
        dados = copy.deepcopy(self.dados)
        dados["_segredos"] = {}
        for secao, chave in SEGREDOS:
            dados["_segredos"][f"{secao}.{chave}"] = self.segredo_definido(secao, chave)
            dados[secao][chave] = [] if isinstance(dados[secao][chave], list) else ""
        return dados


def motivo_sem_imagens(cfg: Config, rede: str) -> str | None:
    """Por que a rede não pode postar imagens (None quando pode)."""
    if rede not in REDES_IMAGEM:
        return IMAGEM_INDISPONIVEL.get(rede, "esta rede não posta imagens")
    if cfg[rede].get("envio") != "navegador":
        return SO_PELO_NAVEGADOR
    return None


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
        migrado = _migrar_legado(bruto)
        if migrado is not None:
            log.info("A Página do Facebook agora é a rede Facebook, com o mesmo envio e os horários do Instagram")
        avisos: list[str] = []
        _mesclar(dados, bruto, "", avisos)
        for chave in avisos:
            log.warning("Opção desconhecida em %s: %s", caminho.name, chave)
    else:
        migrado = None
    _normalizar(PADRAO, dados)
    cfg = Config(dados, caminho)
    cfg.migrado_facebook = migrado is not None
    validar(cfg)
    return cfg


def _migrar_legado(bruto: dict) -> dict | None:
    """O antigo "Reel do Instagram repetido na Página" ([instagram].pagina_facebook) vira a rede [facebook].

    Só lê: o arquivo não muda ao carregar, e a seção entra nele na primeira gravação (salvar). Devolve a
    seção montada, ou None quando não há o que migrar."""
    ig = bruto.get("instagram")
    if not isinstance(ig, dict):
        return None
    ligada = ig.pop("pagina_facebook", None)
    pagina_id = ig.pop("facebook_pagina_id", None)
    if "facebook" in bruto or ligada is not True:
        return None
    envio = ig.get("envio", PADRAO["instagram"]["envio"])
    if envio not in ("oficial", "upload_post"):
        return None  # pelo navegador ou à mão, a opção antiga não fazia nada
    fb = {
        "ativo": bool(ig.get("ativo", True) and ig.get("videos", True)),
        "envio": envio,
        "horarios": list(ig.get("horarios", PADRAO["instagram"]["horarios"])),
        "dias": list(ig.get("dias", [])),
    }
    if envio == "oficial":
        fb["versao_api"] = ig.get("versao_api", PADRAO["instagram"]["versao_api"])
    if isinstance(pagina_id, str) and pagina_id.strip().isdigit():
        fb["pagina_id"] = pagina_id.strip()
    bruto["facebook"] = fb
    return fb


def _limpar_legado(doc) -> None:
    """Tira do arquivo as chaves antigas da Página no [instagram] (a migração já as leu)."""
    ig = doc.get("instagram")
    if isinstance(ig, dict):
        for chave in ("pagina_facebook", "facebook_pagina_id"):
            if chave in ig:
                del ig[chave]


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


def _resolvido(caminho: Path) -> Path:
    try:
        return caminho.resolve()
    except OSError:
        return caminho.absolute()


def _dentro_de(caminho: Path, pasta: Path) -> bool:
    try:
        return caminho.is_relative_to(pasta)
    except ValueError:
        return False


def _validar_pastas(cfg: Config, erros: list[str]) -> None:
    """Vazia, a pasta vira a da instalação; uma dentro da outra, a varredura dos filmes entra nos
    próprios cortes (dados/cortes) e o AutoCortes recorta o que ele mesmo editou."""
    pastas: dict[str, Path] = {}
    for chave in ("pasta_filmes", "pasta_dados"):
        bruto = str(cfg["geral"][chave]).strip()
        if not bruto:
            erros.append(f"[geral].{chave} não pode ficar vazio")
        elif re.search(r"[\x00-\x1f*?\"<>|]", bruto):
            erros.append(f"[geral].{chave} tem caracteres que não valem num caminho do Windows")
        else:
            pastas[chave] = _resolvido(cfg.caminho_de(bruto))
    if len(pastas) < 2:
        return
    filmes, dados = pastas["pasta_filmes"], pastas["pasta_dados"]
    if filmes == dados or _dentro_de(dados, filmes) or _dentro_de(filmes, dados):
        erros.append("[geral].pasta_filmes e pasta_dados não podem ser a mesma pasta, nem uma dentro da outra")
    instalacao = _resolvido(RAIZ)
    if filmes == instalacao or _dentro_de(instalacao, filmes):
        erros.append("[geral].pasta_filmes não pode ser a pasta do AutoCortes (nem uma pasta acima dela): "
                     "a varredura entraria nos cortes de todos os perfis")


def _validar_imagens(cfg: Config, erros: list[str]) -> None:
    """Caixas Vídeos e Imagens de cada rede e a seção [imagens].

    O envio diferente de Navegador com imagens ligadas não é recusado (quem troca o envio no painel
    salva só aquela rede): redes_de_imagem() deixa a rede de fora e o painel diz por quê."""
    com_imagens = False
    for rede in PLATAFORMAS:
        secao = cfg[rede]
        imagens = secao.get("imagens", False)
        if rede not in REDES_IMAGEM and imagens is not False:
            erros.append(f"[{rede}].imagens não vale nesta rede: {IMAGEM_INDISPONIVEL[rede]}")
            imagens = False
        imagens = imagens is True
        com_imagens = com_imagens or imagens
        if secao["ativo"] and not secao["videos"] and not imagens:
            erros.append(f"[{rede}] ligue Vídeos ou Imagens, ou desative a rede")
        if imagens and not secao.get("horarios_imagens"):
            erros.append(f"[{rede}].horarios_imagens precisa de pelo menos um horário com as imagens ligadas")
    im = cfg["imagens"]
    if not 3 <= im["quadros_carrossel"] <= 5:
        erros.append(f"[imagens].quadros_carrossel deve ficar entre 3 e 5 (atual: {im['quadros_carrossel']!r})")
    desconhecidos = [t for t in im["tipos"] if t not in TIPOS_IMAGEM]
    if desconhecidos:
        erros.append(f"[imagens].tipos tem um tipo desconhecido: {desconhecidos[0]!r} "
                     f"(use {', '.join(TIPOS_IMAGEM)})")
    elif com_imagens and not im["tipos"]:
        erros.append("[imagens].tipos precisa de pelo menos um tipo com as imagens ligadas em alguma rede")


_LINK_FACEBOOK = re.compile(r"^https?://(www\.|m\.|web\.)?facebook\.com/\S+$", re.I)
# um link do facebook.com, com ou sem https:// (o mesmo que a conferência da Página reconhece)
LINK_FACEBOOK_SOLTO = re.compile(r"^(?:https?://)?(?:[a-z0-9-]+\.)*(?:facebook|fb)\.com(?:[/?#]|$)", re.I)
_ID_NO_LINK_FACEBOOK = re.compile(r"[?&]id=\d{1,30}(?:&|#|$)|/people/[^/?#]+/\d{1,30}(?:[/?#]|$)")
# Caminhos do facebook.com que não são de uma Página: o /me leva a qualquer perfil que estiver em uso
# (e ensinaria o id da Página errada), o resto é do próprio site. O profile.php só vale com o id.
CAMINHOS_FACEBOOK_SEM_PAGINA = frozenset({
    "me", "home.php", "index.php", "profile.php", "login", "login.php", "checkpoint", "settings", "pages",
    "bookmarks", "notifications", "messages", "friends", "groups", "watch", "reel", "reels", "marketplace",
    "search", "events", "gaming", "stories", "photo.php", "permalink.php", "story.php",
})


def link_facebook_sem_pagina(pagina) -> str:
    """O motivo de recusar [facebook].pagina quando é um link do facebook.com que não aponta uma Página
    (o /me, o /home.php...), ou "". Link com o id (profile.php?id=N, /people/<nome>/N) sempre vale."""
    texto = str(pagina or "").strip()
    if not LINK_FACEBOOK_SOLTO.match(texto) or _ID_NO_LINK_FACEBOOK.search(texto):
        return ""
    if not re.match(r"^https?://", texto, re.I):
        texto = "https://" + texto
    try:
        primeiro = urlsplit(texto).path.strip("/").split("/")[0].lower()
    except ValueError:
        return ""
    if primeiro not in CAMINHOS_FACEBOOK_SEM_PAGINA:
        return ""
    errado = "facebook.com/profile.php sem o id" if primeiro == "profile.php" else f"facebook.com/{primeiro}"
    return f"use o link da Página (facebook.com/SuaPagina ou profile.php?id=...), não {errado}"


def id_do_link_facebook(pagina: str) -> str:
    """O id numérico que o link da Página traz (profile.php?id=N ou /people/<nome>/N), ou ""."""
    texto = str(pagina or "").strip()
    if not _LINK_FACEBOOK.match(texto):
        return ""
    m = re.search(r"[?&]id=(\d{1,30})(?:&|#|$)", texto) or re.search(r"/people/[^/?#]+/(\d{1,30})(?:[/?#]|$)", texto)
    return m.group(1) if m else ""


def _validar_facebook(cfg: Config, erros: list[str]) -> None:
    fb = cfg["facebook"]
    if not 5 <= fb["max_segundos"] <= 90:
        erros.append(f"[facebook].max_segundos deve ficar entre 5 e 90 (atual: {fb['max_segundos']!r})")
    if not re.fullmatch(r"v\d{1,3}\.\d", str(fb["versao_api"]).strip()):
        erros.append("[facebook].versao_api deve ser como v26.0")
    pagina = str(fb["pagina"]).strip()
    if len(pagina) > 300 or re.search(r"[\x00-\x1f\x7f]", pagina):
        erros.append("[facebook].pagina deve ter até 300 caracteres, sem quebra de linha")
    elif link_facebook_sem_pagina(pagina):
        erros.append(f"[facebook].pagina: {link_facebook_sem_pagina(pagina)}")
    elif pagina.lower().startswith("http") and not _LINK_FACEBOOK.match(pagina):
        erros.append("[facebook].pagina deve ser o link da Página no facebook.com ou o nome exato dela")
    pagina_id = str(fb["pagina_id"]).strip()
    if not re.fullmatch(r"\d{0,30}", pagina_id):
        erros.append("[facebook].pagina_id deve ter só os números do ID da Página")
    if len(str(fb["pagina_nome"])) > 200 or re.search(r"[\x00-\x1f\x7f]", str(fb["pagina_nome"])):
        erros.append("[facebook].pagina_nome deve ter até 200 caracteres, sem quebra de linha")
    if not re.fullmatch(r"\d{0,30}", str(fb["app_id"]).strip()):
        erros.append("[facebook].app_id deve ter só os números do App ID da Meta")
    do_link = id_do_link_facebook(pagina)
    if do_link and pagina_id and re.fullmatch(r"\d+", pagina_id) and do_link != pagina_id:
        erros.append(f"[facebook]: o link da Página tem o id {do_link}, mas pagina_id é {pagina_id}")


def validar(cfg: Config) -> None:
    erros: list[str] = []
    _checar_tipos(PADRAO, cfg.dados, "", erros)
    if erros:
        raise ErroConfig("Problemas no config:\n  - " + "\n  - ".join(erros), erros)
    _validar_pastas(cfg, erros)

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
    opcao("edicao", "codec", CODECS)
    opcao("edicao", "preset", PRESETS_X264)
    numero("analise", "limiar_cena", 1, 100)
    numero("analise", "ignorar_inicio_seg", 0, 3600)
    numero("analise", "ignorar_fim_seg", 0, 3600)
    numero("ferramentas", "threads", 0, 64)
    f = cfg["ferramentas"]
    for chave in ("ffmpeg", "ffprobe"):
        if not str(f[chave]).strip() or re.search(r"[\x00-\x1f*?\"<>|]", str(f[chave])):
            erros.append(f"[ferramentas].{chave} deve ser o caminho do programa (ou só \"{chave}\", se estiver no PATH)")
    for chave in ("whisper_cli", "whisper_vad"):
        if re.search(r"[\x00-\x1f*?\"<>|]", str(f[chave])):
            erros.append(f"[ferramentas].{chave} tem caracteres que não valem num caminho do Windows")
    if not re.fullmatch(r"[a-z0-9_]{0,20}", str(f["hwaccel"]).strip().lower()):
        erros.append("[ferramentas].hwaccel deve ser auto, none ou o nome de um acelerador do FFmpeg (ex.: d3d11va)")
    if re.search(r"[\x00-\x1f*?\"<>|]", str(f["ytdlp"])):
        erros.append("[ferramentas].ytdlp tem caracteres que não valem num caminho do Windows")
    numero("canais", "verificar_a_cada_horas", 1, 168)
    numero("canais", "max_idade_dias", 0, 3650)
    numero("canais", "minutos_min", 0, 600)
    numero("canais", "minutos_max", 0, 1440)
    if cfg["canais"]["minutos_max"] and cfg["canais"]["minutos_min"] > cfg["canais"]["minutos_max"]:
        erros.append("[canais].minutos_min não pode ser maior que minutos_max")
    numero("canais", "max_por_canal", 1, 20)
    numero("canais", "max_na_fila", 1, 50)
    opcao("canais", "resolucao", (480, 720, 1080, 1440, 2160))
    numero("canais", "espaco_minimo_gb", 0, 10000)
    if any(len(str(p).strip()) > 60 for p in cfg["canais"]["ignorar_palavras"]):
        erros.append("[canais].ignorar_palavras tem uma palavra com mais de 60 letras")
    # um nome de pasta só, dentro da pasta de filmes; começando com ponto ela seria pulada na varredura
    pasta_canais = str(cfg["canais"]["pasta"]).strip()
    if (not re.fullmatch(r"[^\\/:*?\"<>|\x00-\x1f]{1,60}", pasta_canais) or pasta_canais.startswith(".")
            or pasta_canais.endswith((" ", "."))):
        erros.append("[canais].pasta deve ser o nome de uma pasta (até 60 letras, sem \\ / : * ? \" < > | e sem "
                     "começar com ponto), que fica dentro da pasta de filmes")
    if not (str(f["whisper_cli"]).strip() and str(f["whisper_vad"]).strip()):
        from .ferramentas import SHA256_WHISPER  # aqui dentro: ferramentas importa este módulo

        if str(f["whisper_versao"]) not in SHA256_WHISPER:
            erros.append(f"[ferramentas].whisper_versao deve ser {', '.join(SHA256_WHISPER)} (a versão com o "
                         "download conferido), ou instale o whisper.cpp à mão e informe whisper_cli e whisper_vad")
    if not re.fullmatch(r"\d{1,3}", str(cfg["youtube"]["categoria"]).strip()):
        erros.append("[youtube].categoria deve ser o número da categoria do YouTube (ex.: 1 = Filmes e animação)")
    if not re.fullmatch(r"v\d{1,3}\.\d", str(cfg["instagram"]["versao_api"]).strip()):
        erros.append("[instagram].versao_api deve ser como v26.0")
    _validar_facebook(cfg, erros)
    porta_yt = cfg["youtube"]["porta_redirect"]
    if porta_yt != 0 and not 1024 <= porta_yt <= 65535:
        erros.append("[youtube].porta_redirect deve ser 0 (automática) ou de 1024 a 65535")
    numero("tiktok", "porta_redirect", 1024, 65535)
    numero("tiktok", "capa_ms", 0, 600000)
    numero("instagram", "capa_ms", 0, 600000)
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
    if re.search(r"[\x00-\x1f*?\"<>|]", str(cfg["manual"]["pasta"])):
        erros.append("[manual].pasta tem caracteres que não valem num caminho do Windows")
    numero("ia", "temperatura", 0, 2)
    numero("ia", "tempo_limite_seg", 10, 1800)
    opcao("criacao", "prioridade", ("filmes", "criacao"))
    numero("criacao", "duracao_alvo_seg", 10, 180)
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
        for chave in ("horarios", "horarios_imagens"):
            for h in cfg[nome].get(chave, []):
                if not isinstance(h, str) or not _HORA.match(h.strip()):
                    erros.append(f"[{nome}].{chave} tem um horário inválido: {h!r} (use HH:MM)")
        for chave in ("dias", "dias_imagens"):
            for d in cfg[nome].get(chave, []):
                if d not in DIAS_SEMANA:
                    erros.append(f"[{nome}].{chave} tem um dia inválido: {d!r} (use {', '.join(DIAS_SEMANA)})")
    _validar_imagens(cfg, erros)
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
            if valor in (None, "") or valor == []:
                continue  # vazio = manter o segredo que já está salvo
            if valor == LIMPAR:
                valor = ""
        if caminho[-1] in ("horarios", "horarios_imagens") and isinstance(valor, list):
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
    _pagina_trocada(cfg, limpo)
    novos = copy.deepcopy(cfg.dados)
    _mesclar_valores(novos, limpo)
    _normalizar(PADRAO, novos)
    novo = Config(novos, cfg.caminho)
    novo.migrado_facebook = cfg.migrado_facebook
    validar(novo)  # lança ErroConfig com a lista de problemas
    return novo, limpo


def _pagina_trocada(cfg: Config, limpo: dict) -> None:
    """A Página do Facebook trocada à mão: o id e o nome confirmados da Página antiga não podem sobrar.

    No navegador e à mão, o id sai do link (ou fica vazio até a próxima conferência). Na API oficial e
    no Upload-Post, a Página só muda pela escolha na lista, que manda pagina, pagina_id e pagina_nome juntos."""
    fb = limpo.get("facebook")
    if not isinstance(fb, dict) or "pagina" not in fb or "pagina_id" in fb:
        return
    if str(fb["pagina"]).strip() == str(cfg["facebook"]["pagina"]).strip():
        return
    envio = fb.get("envio", cfg["facebook"]["envio"])
    if envio in ("oficial", "upload_post"):
        raise ErroConfig("[facebook].pagina: escolha a Página na lista (Redes sociais > Facebook); nesta forma de "
                         "envio ela vem do login")
    fb["pagina_id"] = id_do_link_facebook(fb["pagina"])
    fb["pagina_nome"] = ""


def simular(cfg: Config, alteracoes: dict) -> Config:
    """Config com as alterações aplicadas, sem gravar nada (usado na prévia do painel)."""
    return _aplicar(cfg, alteracoes)[0]


def salvar(cfg: Config, alteracoes: dict) -> Config:
    """Aplica alterações vindas do painel, valida e grava o config.toml mantendo os comentários."""
    import tomlkit

    novo, limpo = _aplicar(cfg, alteracoes)
    if not limpo:
        return novo

    # o arquivo é relido para manter os comentários e o que foi escrito à mão nele
    try:
        texto = cfg.caminho.read_text(encoding="utf-8-sig") if cfg.caminho.exists() else ""
    except (OSError, UnicodeDecodeError) as e:
        raise ErroConfig(f"Não consegui ler o {cfg.caminho.name} para salvar ({e}). Feche o programa que estiver "
                         "com ele aberto e tente de novo.") from e
    from tomlkit.exceptions import ParseError

    try:
        doc = tomlkit.parse(texto)
    except ParseError as e:
        raise ErroConfig(f"O {cfg.caminho.name} tem um erro de sintaxe (foi editado à mão?): {e}. Corrija o arquivo "
                         "ou desfaça a edição; nada foi salvo.") from e
    # ordem fixa: o _gravar_doc cria a tabela que falta, então a seção migrada inteira entra antes da
    # alteração pedida (senão uma primeira gravação de uma chave só deixaria um [facebook] parcial)
    _limpar_legado(doc)
    if cfg.migrado_facebook and "facebook" not in doc:
        _gravar_doc(doc, {"facebook": copy.deepcopy(cfg.dados["facebook"])})
    _gravar_doc(doc, limpo)
    try:
        escrever_atomico(cfg.caminho, tomlkit.dumps(doc))
    except OSError as e:
        raise ErroConfig(f"Não consegui gravar o {cfg.caminho.name} ({e}). Nada foi salvo.") from e
    novo.migrado_facebook = False
    return novo
