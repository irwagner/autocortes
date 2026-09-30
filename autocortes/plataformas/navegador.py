"""Postagem pelo navegador: o AutoCortes preenche a página de envio da rede com a sua sessão.

Você loga uma vez na janela do AutoCortes (perfil separado, em dados/chrome) e a sessão fica
salva ali. Depois, cada post é feito clicando na página da própria rede.

Isto contraria os termos de uso das redes, que só autorizam as APIs oficiais, e pode custar a
conta. Foi ligado a pedido do dono do projeto, para as contas dele. Não há disfarce nenhum:
nada de forjar fingerprint, resolver captcha, API privada ou proxy. Se a rede bloquear, o envio
para e avisa.

Os seletores das páginas são o ponto fraco: eles mudam quando a rede muda o layout, e não há
como testá-los sem uma conta logada. Por isso cada passo tem várias opções, o modo ensaio
preenche tudo sem publicar, e qualquer falha guarda uma imagem da tela em dados/navegador.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from ..config import ROTULOS, Config
from ..navegador import TRAVA, Aba, ErroNavegador, Navegador
from ..textos import Conteudo
from ..util import log
from .base import ErroPublicacao, Plataforma, Resultado
from .manual import textos

# páginas de login e de envio de cada rede
PAGINAS = {
    "youtube": ("https://studio.youtube.com/", "https://studio.youtube.com/"),
    "tiktok": ("https://www.tiktok.com/login", "https://www.tiktok.com/tiktokstudio/upload"),
    "instagram": ("https://www.instagram.com/accounts/login/", "https://www.instagram.com/"),
    "bilibili": ("https://passport.bilibili.com/login", "https://member.bilibili.com/platform/upload/video/frame"),
}
# Sinal de que a sessão está de pé: o que só aparece quando você está logado. Medido nas páginas
# reais em set/2026: deslogado, o YouTube manda para o accounts.google.com, o Instagram mostra o
# campo de senha, e o TikTok e o Bilibili ficam na mesma URL sem nada (nem campo de arquivo).
SESSAO = {
    "youtube": "__ac.existe('ytcp-app') || __ac.existe('#create-icon') ? 1 : 0",
    "tiktok": "__ac.existe('input[type=file]') ? 1 : 0",
    "instagram": "__ac.porTexto('div[role=button], a, span', ['criar', 'create']) ? 1 : 0",
    "bilibili": "__ac.existe('input[type=file]') || __ac.existe('.bcc-upload, .upload-wrp') ? 1 : 0",
}
# endereços de login: se a página cair num deles, a sessão acabou
URL_LOGIN = ("/login", "accounts/login", "accounts.google.com", "passport.bilibili.com", "/signin")
# textos que indicam bloqueio: o roteiro para e não tenta de novo
BLOQUEIO = (
    "verifique que você é humano", "confirme que você não é um robô", "captcha", "verify you are human",
    "suspicious activity", "atividade suspeita", "too many attempts", "tente novamente mais tarde",
    "your account has been", "sua conta foi", "security check", "安全验证",
)


def _conferir_bloqueio(aba: Aba) -> None:
    texto = aba.texto().lower()
    for marca in BLOQUEIO:
        if marca in texto:
            raise ErroNavegador(
                f"a rede pediu verificação ou bloqueou o acesso ('{marca}'). Abra a janela do navegador "
                "em Redes sociais, resolva na mão e tente de novo", "bloqueio",
            )


def _erro_sessao(rede: str) -> ErroNavegador:
    return ErroNavegador(
        f"a sessão do {ROTULOS[rede]} caiu (a página de envio não abriu). Em Redes sociais, clique em "
        "Conectar e entre na conta de novo", "bloqueio",
    )


def esperar_sessao(aba: Aba, rede: str, segundos: float = 30) -> None:
    """Espera a página de envio abrir de verdade; sem sessão, ela nunca abre."""
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        _conferir_bloqueio(aba)
        if any(p in aba.url().lower() for p in URL_LOGIN) or aba.existe("input[type=password]"):
            raise _erro_sessao(rede)
        if aba.js(SESSAO[rede]):
            return
        aba.pausa(1.5)
    raise _erro_sessao(rede)


class ViaNavegador(Plataforma):
    """Uma rede publicada pela própria página dela, no navegador do AutoCortes."""

    via = "navegador"

    def __init__(self, nome: str, cfg: Config):
        self.nome = nome
        self.rotulo = f"{ROTULOS[nome]} (navegador)"
        super().__init__(cfg)
        self.nav = cfg["navegador"]

    # ------------------------------------------------------------ conta
    @property
    def arquivo_token(self) -> Path:
        return self.cfg.pasta_tokens / "navegador.json"

    def conta(self) -> str | None:
        return (self.token().get("contas") or {}).get(self.nome)

    def lembrar_conta(self, descricao: str) -> None:
        dados = self.token()
        dados.setdefault("contas", {})[self.nome] = descricao
        dados.setdefault("visto_em", {})[self.nome] = time.time()
        self.salvar_token(dados)

    def desconectar(self) -> None:
        """Esquece a conta. A sessão em si sai saindo da conta na janela do navegador."""
        dados = self.token()
        (dados.get("contas") or {}).pop(self.nome, None)
        (dados.get("visto_em") or {}).pop(self.nome, None)
        self.salvar_token(dados)

    def pronta(self) -> tuple[bool, str]:
        # barato de propósito: o painel chama isso a cada atualização, e abrir o navegador é caro
        if not (self.cfg.pasta_dados / "chrome").is_dir():
            return False, "falta abrir o navegador e entrar na conta"
        if not self.conta():
            return False, "falta entrar na conta pelo navegador (em Redes sociais, use Conectar)"
        return True, "ok"

    @property
    def pasta_imagens(self) -> Path:
        return self.cfg.pasta_dados / "navegador"

    # ------------------------------------------------------------ login e teste
    def abrir_para_login(self) -> str:
        """Abre a janela do navegador na tela de login da rede (você loga uma vez)."""
        navegador = Navegador(self.cfg)
        navegador.conf = dict(self.nav, visivel=True)  # para logar, a janela precisa aparecer
        navegador.abrir()
        aba = navegador.nova_aba(PAGINAS[self.nome][0])
        aba.fechar()  # deixa a aba aberta para você usar; só solta o controle
        return PAGINAS[self.nome][0]

    def autorizar(self) -> None:
        url = self.abrir_para_login()
        print(
            f"\nAbri a janela do AutoCortes em {url}\n"
            "  1. Entre na conta (e resolva o 2FA, se pedir)\n"
            "  2. A sessão fica salva no perfil do AutoCortes, em dados/chrome\n"
            "  3. Volte aqui e rode: python -m autocortes verificar --online\n"
        )

    def verificar(self) -> str:
        """Abre a página da rede e confere se a sessão está de pé, devolvendo a conta."""
        with TRAVA:
            navegador = Navegador(self.cfg)
            aba = navegador.nova_aba("about:blank")
            try:
                aba.navegar(PAGINAS[self.nome][1])
                esperar_sessao(aba, self.nome)
                conta = self._nome_da_conta(aba) or "conta conectada"
                texto = f"{conta} pelo navegador"
                self.lembrar_conta(texto)
                return texto
            except ErroNavegador as e:
                self._guardar_imagem(aba, "verificar")
                raise ErroPublicacao(f"{self.rotulo}: {e}", e.tipo) from e
            finally:
                navegador.fechar_aba(aba)

    def _nome_da_conta(self, aba: Aba) -> str | None:
        tentativas = {
            "youtube": "(() => { const el = __ac.achar('#channel-title, .channel-title, ytcp-header #entity-name');"
                       " return el ? el.innerText.trim() : 0; })()",
            "tiktok": "(() => { const a = __ac.todos('a[href^=\"/@\"]')[0];"
                      " return a ? a.getAttribute('href').slice(1) : 0; })()",
            # o link do perfil tem a foto dentro; sem o img, pegava "popular" do rodapé deslogado
            "instagram": "(() => { const a = __ac.todos('a[href^=\"/\"]').find((x) =>"
                         " /^\\/[A-Za-z0-9._]+\\/$/.test(x.getAttribute('href') || '') && x.querySelector('img'));"
                         " return a ? '@' + a.getAttribute('href').replace(/\\//g, '') : 0; })()",
            "bilibili": "(() => { const el = __ac.achar('.nickname, .user-name, .username');"
                        " return el ? el.innerText.trim() : 0; })()",
        }
        try:
            valor = aba.js(tentativas[self.nome])
        except ErroNavegador:
            return None
        return str(valor).strip() if valor else None

    def _guardar_imagem(self, aba: Aba, passo: str) -> Path | None:
        destino = self.pasta_imagens / f"{self.nome}-{passo}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        imagem = aba.captura(destino)
        if imagem:
            log.warning("%s: imagem da tela em %s", self.rotulo, imagem)
        return imagem

    # ------------------------------------------------------------ publicação
    def publicar(self, arquivo: Path, conteudo: Conteudo, parar: threading.Event | None = None) -> Resultado:
        ensaio = bool(self.nav["ensaio"])
        roteiro = {"youtube": self._youtube, "tiktok": self._tiktok,
                   "instagram": self._instagram, "bilibili": self._bilibili}[self.nome]
        t = textos(self.cfg, self.nome, conteudo)
        with TRAVA:
            navegador = Navegador(self.cfg, parar)
            aba = navegador.nova_aba("about:blank")
            try:
                aba.navegar(PAGINAS[self.nome][1])
                esperar_sessao(aba, self.nome)
                # quadro de outro site com cara de uploader: eu não alcanço o que está dentro dele
                # (os quadros de anúncio e de estatística das redes são ignorados)
                quadros = [f for f in aba.iframes()
                           if f.startswith("http") and any(m in f.lower() for m in ("upload", "creator", "studio"))]
                if quadros and not aba.existe("input[type=file]"):
                    raise ErroNavegador(
                        "a página de envio está dentro de um quadro (iframe) que eu não alcanço: "
                        f"{quadros[0]}. Use o envio à mão nesta rede até eu ajustar", "bloqueio",
                    )
                resultado = roteiro(aba, arquivo, t, ensaio)
                if ensaio:
                    self._guardar_imagem(aba, "ensaio")
                    raise ErroNavegador(
                        "ensaio: preenchi tudo e NÃO publiquei. Veja a imagem da tela em "
                        f"{self.pasta_imagens}. Desligue o ensaio em Redes sociais para publicar de verdade",
                        "corte",
                    )
                return resultado
            except ErroNavegador as e:
                if not ensaio:
                    self._guardar_imagem(aba, "erro")
                raise ErroPublicacao(f"{self.rotulo}: {e}", e.tipo) from e
            finally:
                navegador.fechar_aba(aba)

    def _fim(self, observacao: str | None = None, url: str | None = None) -> Resultado:
        """Sem API não há id do post: guarda um número nosso e o link, quando a página mostra."""
        return Resultado(f"navegador-{uuid.uuid4().hex[:12]}", url, observacao)

    # ---- TikTok: tiktok.com/tiktokstudio/upload
    def _tiktok(self, aba: Aba, arquivo: Path, t: dict, ensaio: bool) -> Resultado:
        aba.enviar_arquivo(["input[type=file][accept*='video']", "input[type=file]"], arquivo, 60)
        legenda = [
            "div[contenteditable='true'][role='combobox']",
            ".public-DraftEditor-content",
            "div[contenteditable='true']",
        ]
        aba.esperar_seletor(legenda, 120, "o campo da legenda")
        aba.digitar(legenda, t["legenda"])
        # espera o envio terminar: o botão de publicar só libera depois
        aba.esperar(
            "(() => { const b = __ac.porTexto('button', ['post', 'publicar']);"
            " return b && !b.disabled ? 1 : 0; })()", 900, "o TikTok não terminou de receber o vídeo",
        )
        _conferir_bloqueio(aba)
        if ensaio:
            return self._fim()
        aba.clicar_texto("button", ["post", "publicar"], 60)
        aba.esperar(
            "(() => { const t = __ac.texto();"
            " return /manage your posts|gerenciar|foi publicado|posted|enviado com sucesso/.test(t) ? 1 : 0; })()",
            300, "o TikTok não confirmou a publicação",
        )
        return self._fim("publicado pelo navegador; o link aparece no app")

    # ---- YouTube Studio (Polymer, quase tudo em shadow DOM)
    def _youtube(self, aba: Aba, arquivo: Path, t: dict, ensaio: bool) -> Resultado:
        aba.clicar(["ytcp-button#create-icon", "#create-icon", "#upload-button"], 60)
        aba.clicar_texto("tp-yt-paper-item, ytcp-ve, span", ["enviar vídeos", "upload videos", "enviar videos"], 30)
        aba.enviar_arquivo("input[type=file]", arquivo, 60)
        aba.digitar(["#title-textarea #textbox", "ytcp-social-suggestions-textbox#title-textarea #textbox"],
                    t["titulo"], 120)
        aba.digitar(["#description-textarea #textbox", "ytcp-social-suggestions-textbox#description-textarea #textbox"],
                    t["descricao"])
        # público-alvo: não é conteúdo para crianças
        aba.clicar(["tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']",
                    "#audience tp-yt-paper-radio-button:nth-of-type(2)"], 60)
        try:  # tags ficam em "Mostrar mais"; se não abrir, segue sem elas
            aba.clicar_texto("ytcp-button, button", ["mostrar mais", "show more"], 10)
            for tag in t["tags"][:15]:
                aba.digitar(["#tags-container input", "input[aria-label*='ags']"], tag, 10)
                aba.tecla("Enter")
        except ErroNavegador:
            log.info("YouTube: não achei o campo de tags; o vídeo vai sem elas")
        for _ in range(3):  # detalhes > elementos > verificações > visibilidade
            aba.clicar(["#next-button", "ytcp-button#next-button"], 60)
        privacidade = {"public": "PUBLIC", "unlisted": "UNLISTED", "private": "PRIVATE"}[
            str(self.cfg["youtube"]["privacidade"])]
        aba.clicar([f"tp-yt-paper-radio-button[name='{privacidade}']"], 60)
        aba.esperar(  # o envio precisa terminar antes de salvar
            "(() => { const t = __ac.texto();"
            " return /verifica(ç|c)ões conclu|checks complete|processamento conclu|processing done|"
            "envio conclu|upload complete/i.test(t) ? 1 : 0; })()", 1800, "o YouTube não terminou de receber o vídeo",
        )
        _conferir_bloqueio(aba)
        if ensaio:
            return self._fim()
        aba.clicar(["#done-button", "ytcp-button#done-button"], 60)
        link = None
        try:
            link = aba.esperar(
                "(() => { const a = __ac.todos('a[href*=\"youtu\"]').find((x) => /shorts|youtu\\.be/.test(x.href));"
                " return a ? a.href : 0; })()", 120, "o link do vídeo",
            )
        except ErroNavegador:
            log.info("YouTube: publicado, mas não achei o link na tela")
        return self._fim(None, str(link) if link else None)

    # ---- Instagram (instagram.com, criar Reel)
    def _instagram(self, aba: Aba, arquivo: Path, t: dict, ensaio: bool) -> Resultado:
        aba.clicar_texto("div[role='button'], a, span", ["criar", "create"], 60)
        aba.enviar_arquivo(["input[type=file][accept*='video']", "input[type=file]"], arquivo, 60)
        # recortar > editar > legenda (o Instagram muda a quantidade de etapas)
        for _ in range(3):
            try:
                aba.clicar_texto("div[role='button'], button", ["avançar", "next"], 20)
            except ErroNavegador:
                break
        legenda = ["div[contenteditable='true'][role='textbox']", "textarea[aria-label*='legenda']",
                   "div[contenteditable='true']"]
        aba.esperar_seletor(legenda, 60, "o campo da legenda")
        aba.digitar(legenda, t["legenda"])
        _conferir_bloqueio(aba)
        if ensaio:
            return self._fim()
        aba.clicar_texto("div[role='button'], button", ["compartilhar", "share"], 60)
        aba.esperar(
            "(() => { const t = __ac.texto();"
            " return /foi compartilhad|compartilhado|has been shared|your reel/.test(t) ? 1 : 0; })()",
            900, "o Instagram não confirmou a publicação",
        )
        return self._fim("publicado pelo navegador; o link aparece no perfil")

    # ---- Bilibili (member.bilibili.com)
    def _bilibili(self, aba: Aba, arquivo: Path, t: dict, ensaio: bool) -> Resultado:
        aba.enviar_arquivo("input[type=file]", arquivo, 60)
        aba.digitar(["input[placeholder*='标题']", ".video-title input", "input[maxlength='80']"], t["titulo"], 120)
        observacoes = []
        try:  # 转载 (repost) e a fonte: o corte é de um filme de terceiros
            aba.clicar_texto("div, span, label", ["转载"], 20)
            aba.digitar(["input[placeholder*='转载']", "input[placeholder*='来源']"], t["fonte"], 20)
        except ErroNavegador:
            observacoes.append("confira no site se o tipo ficou 转载 com a fonte")
        try:
            aba.digitar([".ql-editor", "div[data-mode='rich']", "textarea[placeholder*='简介']"], t["descricao"], 20)
        except ErroNavegador:
            observacoes.append("a descrição pode não ter entrado")
        try:
            for tag in t["tags"][:10]:
                aba.digitar(["input[placeholder*='标签']", "input[placeholder*='tag']"], tag, 15)
                aba.tecla("Enter")
        except ErroNavegador:
            observacoes.append("as tags podem não ter entrado")
        aba.esperar(
            "(() => { const t = __ac.texto(); return /上传完成|上传成功|100%/.test(t) ? 1 : 0; })()",
            1800, "o Bilibili não terminou de receber o vídeo",
        )
        _conferir_bloqueio(aba)
        if ensaio:
            return self._fim()
        aba.clicar_texto("span, button, div", ["立即投稿", "投稿"], 60)
        aba.esperar(
            "(() => { const t = __ac.texto(); return /投稿成功|稿件投递成功|投递成功/.test(t) ? 1 : 0; })()",
            300, "o Bilibili não confirmou o envio",
        )
        observacoes.append("o vídeo passa pela revisão do Bilibili antes de aparecer")
        return self._fim("; ".join(observacoes))
