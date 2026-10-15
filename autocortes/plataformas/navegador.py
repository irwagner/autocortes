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

import itertools
import json
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .. import gravador, roteiro
from ..config import ROTULOS, Config
from ..navegador import TRAVA, Aba, ErroNavegador, Navegador, css
from ..textos import Conteudo
from ..util import log
from .base import Entrega, ErroPublicacao, Plataforma, Resultado
from .manual import textos

# páginas de login e de envio de cada rede
PAGINAS = {
    "youtube": ("https://studio.youtube.com/", "https://studio.youtube.com/"),
    "tiktok": ("https://www.tiktok.com/login", "https://www.tiktok.com/tiktokstudio/upload"),
    "instagram": ("https://www.instagram.com/accounts/login/", "https://www.instagram.com/"),
    "bilibili": ("https://passport.bilibili.com/login", "https://member.bilibili.com/platform/upload/video/frame"),
}


def _quadro_de_envio(url: str) -> bool:
    """Endereço de um uploader; parâmetros de cookies podem só citar a página do Studio."""
    try:
        endereco = urlsplit(url)
    except ValueError:
        return False
    destino = (endereco.netloc + endereco.path).lower()
    return endereco.scheme in ("http", "https") and any(m in destino for m in ("upload", "creator", "studio"))


# Sinal de que a sessão está de pé: o que só aparece quando você está logado. Medido nas páginas
# reais em set/2026: deslogado, o YouTube manda para o accounts.google.com, o Instagram mostra o
# campo de senha, e o TikTok e o Bilibili ficam na mesma URL sem nada (nem campo de arquivo).
# No menu compacto do Instagram, "Criar" fica escondido e o nome visível vem do SVG.
# Clicamos no link/botão que contém o ícone: SVG não tem o método click() de um elemento HTML.
NOMES_CRIAR_INSTAGRAM = ("Novo post", "New post", "Nova publicação", "New publication", "Criar", "Create")
CRIAR_INSTAGRAM = css([
    f':is(a, button, [role="button"], [role="link"]):has(svg[aria-label={json.dumps(nome)} i])'
    for nome in NOMES_CRIAR_INSTAGRAM
]) + [{"v": list(NOMES_CRIAR_INSTAGRAM), "t": True, "em": 'button, a, [role="button"], [role="link"]'}]
# No layout atual, Novo post abre um menu com Postar, Vídeo ao vivo e Anúncio.
POSTAR_INSTAGRAM = css([
    f'a[href="#"]:has(svg[aria-label={json.dumps(nome)} i])' for nome in ("Postar", "Post")
])

SESSAO = {
    "youtube": "__ac.presente('ytcp-app') || __ac.existe('#create-icon') ? 1 : 0",
    "tiktok": "__ac.existe('input[type=file]') ? 1 : 0",
    "instagram": f"__ac.situacao({json.dumps(CRIAR_INSTAGRAM)}).estado === 'pronto' ? 1 : 0",
    "bilibili": "__ac.existe('input[type=file]') || __ac.existe('.bcc-upload, .upload-wrp') ? 1 : 0",
}
# endereços de login: se a página cair num deles, a sessão acabou
URL_LOGIN = ("/login", "accounts/login", "accounts.google.com", "passport.bilibili.com", "/signin")
# Gravações em andamento (rede -> sessões): o motor não posta pelo navegador enquanto você grava.
# Conta por sessão: o fim de uma gravação antiga não pode apagar a marca da nova ("Gravar de novo").
GRAVANDO: dict[str, set[int]] = {}
_TRAVA_GRAVANDO = threading.Lock()
_SESSOES = itertools.count(1)
# textos que indicam bloqueio: o roteiro para e não tenta de novo
BLOQUEIO = (
    "verifique que você é humano", "confirme que você não é um robô", "captcha", "verify you are human",
    "suspicious activity", "atividade suspeita", "too many attempts", "tente novamente mais tarde",
    "your account has been", "sua conta foi", "security check", "安全验证",
)
# imagens de tela guardadas por rede (as mais antigas saem)
MAX_IMAGENS = 30
# link de um post publicado
_LINK_POST = r"youtu\.be/|/shorts/|/video/|/reel/|/p/|bilibili\.com/video/"
# espera do passo que publica e da confirmação: o botão só libera quando o envio do vídeo termina
ESPERA_PUBLICAR = 900


def gravando() -> list[str]:
    """As redes com gravação de roteiro aberta agora."""
    with _TRAVA_GRAVANDO:
        return sorted(rede for rede, sessoes in GRAVANDO.items() if sessoes)


def _marcar_gravacao(rede: str, sessao: int, aberta: bool) -> None:
    with _TRAVA_GRAVANDO:
        sessoes = GRAVANDO.setdefault(rede, set())
        if aberta:
            sessoes.add(sessao)
        else:
            sessoes.discard(sessao)


def _marca_de_bloqueio(texto: str) -> str | None:
    texto = texto.lower()
    return next((marca for marca in BLOQUEIO if marca in texto), None)


def _conferir_bloqueio(aba: Aba, so_dialogos: bool = False) -> None:
    """Para se a rede pediu verificação ou bloqueou.

    Depois que eu escrevo os textos do corte, só olho as janelas por cima da página: a legenda
    pode ter uma fala como "atividade suspeita", e a página inteira mostra o que eu escrevi.
    """
    marca = _marca_de_bloqueio(aba.texto_dialogos() if so_dialogos else aba.texto())
    if marca:
        raise ErroNavegador(
            f"a rede pediu verificação ou bloqueou o acesso ('{marca}'). Abra a janela do navegador "
            "em Redes sociais, resolva na mão e teste a conta de novo", "bloqueio", persistente=True,
        )


def _erro_sessao(rede: str) -> ErroNavegador:
    return ErroNavegador(
        f"não encontrei a sua sessão do {ROTULOS[rede]} na janela do AutoCortes: a página de envio não abriu. "
        "Clique em Conectar, entre na conta nessa janela (e não numa aba do seu Chrome normal) e tente de novo",
        "bloqueio",
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


def _chave_do_link(link: str) -> str:
    """O id do post no link (youtu.be/ID e youtube.com/shorts/ID são o mesmo vídeo)."""
    partes = [p for p in re.split(r"[/?#&=]", link) if p]
    return partes[-1] if partes else link


class ViaNavegador(Plataforma):
    """Uma rede publicada pela própria página dela, no navegador do AutoCortes."""

    via = "navegador"

    def __init__(self, nome: str, cfg: Config):
        self.nome = nome
        self.rotulo = f"{ROTULOS[nome]} (navegador)"
        super().__init__(cfg)
        self.nav = cfg["navegador"]
        self._id_envio = ""

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
        if not TRAVA.acquire(timeout=5):
            raise ErroPublicacao(f"{self.rotulo}: o navegador está postando agora. Teste de novo quando o "
                                 "envio terminar", "temporario")
        try:
            navegador = Navegador(self.cfg)
            aba = None
            try:
                aba = navegador.nova_aba("about:blank")
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
                if aba is not None:
                    navegador.fechar_aba(aba)
        finally:
            TRAVA.release()

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

    def _guardar_imagem(self, aba: Aba | None, passo: str) -> Path | None:
        if aba is None:
            return None
        destino = self.pasta_imagens / f"{self.nome}-{passo}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        imagem = aba.captura(destino)
        if imagem:
            log.warning("%s: imagem da tela em %s", self.rotulo, imagem)
            try:  # só as últimas de cada rede: um dia de erros não enche o disco
                antigas = sorted(self.pasta_imagens.glob(f"{self.nome}-*.png"),
                                 key=lambda p: p.stat().st_mtime, reverse=True)
                for velha in antigas[MAX_IMAGENS:]:
                    velha.unlink(missing_ok=True)
            except OSError:
                pass
        return imagem

    # ------------------------------------------------------------ publicação
    def publicar(self, arquivo: Path, conteudo: Conteudo, parar: threading.Event | None = None,
                 entrega: Entrega | None = None) -> Resultado:
        entrega = entrega if entrega is not None else Entrega()
        ensaio = bool(self.nav["ensaio"])
        # o seu roteiro (dados/roteiros) ou, sem ele, o padrão que vem com a instalação
        aprendido, origem = gravador.carregar_com_origem(self.cfg, self.nome)
        if aprendido:
            def passo_a_passo(aba, arq, t, ens, ent):
                return self._repetir(aba, aprendido, arq, t, ens, ent, origem or "perfil")
        else:
            passo_a_passo = {"youtube": self._youtube, "tiktok": self._tiktok,
                             "instagram": self._instagram, "bilibili": self._bilibili}[self.nome]
        t = textos(self.cfg, self.nome, conteudo)
        # outro envio pelo navegador (de outra rede) pode estar no meio: espera a vez dele
        if not TRAVA.acquire(timeout=600):
            raise ErroPublicacao(f"{self.rotulo}: o navegador ficou ocupado com outro envio por mais de 10 min; "
                                 "tento de novo depois", "temporario")
        try:
            # conferido com a trava na mão: a gravação marca a rede segurando a mesma trava
            redes = gravando()
            if redes:
                raise ErroPublicacao(
                    f"{self.rotulo}: você está gravando o roteiro de {', '.join(ROTULOS[r] for r in redes)} agora. "
                    "Termine a gravação no painel e eu posto em seguida", "temporario",
                )
            self._id_envio = f"navegador-{uuid.uuid4().hex[:12]}"
            navegador = Navegador(self.cfg, parar)
            aba = None
            try:
                aba = navegador.nova_aba("about:blank")
                aba.navegar(PAGINAS[self.nome][1])
                esperar_sessao(aba, self.nome)
                # quadro de outro site com cara de uploader: eu não alcanço o que está dentro dele
                # (os quadros de anúncio e de estatística das redes são ignorados)
                quadros = [f for f in aba.iframes() if _quadro_de_envio(f)]
                if quadros and not aba.existe("input[type=file]"):
                    raise ErroNavegador(
                        "a página de envio está dentro de um quadro (iframe) que eu não alcanço: "
                        f"{quadros[0]}. Use o envio à mão nesta rede até eu ajustar", "bloqueio",
                    )
                resultado = passo_a_passo(aba, arquivo, t, ensaio, entrega)
                if ensaio:
                    imagem = self._guardar_imagem(aba, "ensaio")
                    raise ErroPublicacao(
                        f"{self.rotulo}: ensaio: preenchi tudo e NÃO publiquei. Veja a imagem da tela em "
                        f"{imagem or self.pasta_imagens}. Desligue o ensaio em Redes sociais para publicar de verdade",
                        "ensaio",
                    )
                return resultado
            except ErroNavegador as e:
                tipo, persistente = e.tipo, e.persistente
                if aba is not None and not e.conexao:
                    self._guardar_imagem(aba, "erro")
                    if tipo != "bloqueio":
                        # um aviso de verificação por cima da página explica a falha melhor que o seletor
                        try:
                            marca = _marca_de_bloqueio(aba.texto_dialogos())
                        except ErroNavegador:
                            marca = None
                        if marca:
                            tipo, persistente = "bloqueio", True
                            e = ErroNavegador(f"{e} (a rede mostrou '{marca}': resolva na janela do navegador "
                                              "e teste a conta de novo)", "bloqueio")
                raise ErroPublicacao(f"{self.rotulo}: {e}", tipo, persistente=persistente) from e
            finally:
                if aba is not None:
                    navegador.fechar_aba(aba)
        finally:
            TRAVA.release()

    def _aceitar(self, entrega: Entrega) -> None:
        """Logo antes do clique que publica: a partir daqui o post pode ter saído."""
        if not entrega.aceita:
            entrega.aceitou(self._id_envio)

    def _fim(self, observacao: str | None = None, url: str | None = None, confirmado: bool = True) -> Resultado:
        """Sem API não há id do post: guarda um número nosso e o link, quando a página mostra."""
        return Resultado(self._id_envio or f"navegador-{uuid.uuid4().hex[:12]}", url, observacao, confirmado)

    # ---- roteiro que você gravou (e pode editar no painel)
    def _repetir(self, aba: Aba, texto_roteiro: str, arquivo: Path, t: dict, ensaio: bool,
                 entrega: Entrega, origem: str = "perfil") -> Resultado:
        padrao = origem == "padrao"
        nome_roteiro = "o roteiro padrão do AutoCortes" if padrao else "o roteiro"
        # o padrão não é seu para ajustar: o caminho é gravar o seu (ou esperar a atualização)
        conserto = ("Grave o seu em Redes sociais > Aprender a postar" if padrao
                    else "Ajuste ou grave de novo em Redes sociais > Ver o roteiro")
        try:
            instrucoes = roteiro.validar(texto_roteiro, self.nome)
        except roteiro.ErroRoteiro as e:
            raise ErroNavegador(
                f"{nome_roteiro} tem linha com problema: {'; '.join(e.erros[:3])}. "
                + ("Grave o seu em Redes sociais > Aprender a postar" if padrao
                   else "Corrija em Redes sociais > Ver o roteiro"), "bloqueio",
            ) from e
        aba.js("__ac.travarArquivo()")  # o clique em "selecionar vídeo" não pode abrir a janela do Windows
        depois_do_video = publicou = False
        links_antes: set[str] = set()
        confirmado: bool | None = None  # None = o roteiro não tem linha conferir
        sem_confirmacao: list[str] = []
        for ins in instrucoes:
            # O clique que publica: a linha "publicar" ou, de rede de segurança, qualquer clique com
            # cara de publicar depois do vídeo (a gravação pode ter dado o nome errado ao passo).
            ponto = (depois_do_video and not publicou and ins.comando in ("publicar", "clicar")
                     and (ins.comando == "publicar" or roteiro.parece_publicar(ins.alvos)))
            if ponto and ensaio:
                log.info("%s: ensaio, paro antes da linha %d (\u201c%s\u201d)", self.rotulo, ins.linha, ins.descricao)
                return self._fim()
            try:
                if ins.comando == "video":
                    links_antes = self._links(aba)
                if ponto:
                    _conferir_bloqueio(aba, so_dialogos=True)
                    self._clicar(aba, ins, ESPERA_PUBLICAR, antes=lambda: self._aceitar(entrega))
                    publicou = True
                elif ins.comando == "conferir" and publicou:
                    try:
                        self._esperar_alvo(aba, ins, ESPERA_PUBLICAR)
                        confirmado = True if confirmado is None else confirmado
                    except ErroNavegador as e:
                        if e.conexao or (aba.parar is not None and aba.parar.is_set()):
                            raise
                        log.warning("%s: %s (o vídeo pode ter saído; fica para você conferir)", self.rotulo, e)
                        confirmado = False
                        sem_confirmacao.append(f"não vi {' ou '.join(str(a) for a in ins.alvos)} na tela")
                else:
                    self._rodar(aba, ins, arquivo, t)
            except ErroNavegador as e:
                parou = aba.parar is not None and aba.parar.is_set()
                if ins.opcional and not e.conexao and not parou:
                    log.info("%s: passo opcional da linha %d não deu, seguindo (%s)", self.rotulo, ins.linha, e)
                    continue
                if e.conexao or e.persistente or parou:
                    # a janela caiu, a rede pediu verificação ou o motor parou: o roteiro não tem culpa,
                    # e "ajuste o roteiro" mandaria você mexer no lugar errado
                    raise
                raise ErroNavegador(
                    f"a linha {ins.linha} d{nome_roteiro} não funcionou (\u201c{ins.descricao}\u201d): {e}. "
                    f"{conserto}", e.tipo, e.conexao, e.persistente,
                ) from e
            if ins.comando == "video":
                depois_do_video = True
        if not publicou:
            raise ErroNavegador("o roteiro terminou sem passar pelo clique de publicar", "bloqueio")
        link = self._link_novo(aba, links_antes)
        if confirmado is None:
            return self._fim(f"publicado pel{nome_roteiro}; ele não tem linha conferir, então não vi a "
                             "confirmação na tela", link)
        if not confirmado:
            return self._fim(f"{nome_roteiro} chegou ao fim, mas " + "; ".join(sem_confirmacao)
                             + ". Confira na rede se o vídeo saiu", link, confirmado=False)
        return self._fim("publicado pelo roteiro padrão do AutoCortes" if padrao
                         else "publicado pelo roteiro que você gravou", link)

    def _rodar(self, aba: Aba, ins: roteiro.Instrucao, arquivo: Path, t: dict) -> None:
        segundos = ESPERA_PUBLICAR if ins.comando in ("publicar", "conferir") else 90
        if ins.comando == "abrir":
            aba.navegar(ins.url, 60)
            aba.js("__ac.travarArquivo()")  # a página nova não tem a trava do seletor de arquivo
        elif ins.comando == "video":
            aba.enviar_arquivo(self._seletores(ins), arquivo, segundos)
        elif ins.comando in ("clicar", "publicar"):
            self._clicar(aba, ins, segundos)
        elif ins.comando == "escrever":
            self._escrever(aba, ins, t, segundos)
        elif ins.comando == "tags":
            for tag in (t.get("tags") or [])[:15]:  # cada tag entra e é confirmada com Enter
                aba.digitar_alvos(self._alvos(ins), str(tag), segundos, ins.descricao)
                aba.tecla("Enter")
        elif ins.comando == "tecla":
            aba.tecla(ins.tecla)
        elif ins.comando == "esperar":
            if ins.segundos is not None:
                aba.pausa_fixa(ins.segundos)
            else:
                self._esperar_alvo(aba, ins, 300)
        elif ins.comando == "conferir":  # antes de publicar, é uma espera como as outras
            self._esperar_alvo(aba, ins, segundos)
        elif ins.comando == "rolar":
            aba.js(f"(() => {{ window.scrollBy(0, {int(ins.segundos or 600)}); return 1; }})()")
            aba.pausa()

    @staticmethod
    def _seletores(ins: roteiro.Instrucao) -> list[str]:
        return [a.valor for a in ins.alvos if not a.por_texto] or ["input[type=file]"]

    @staticmethod
    def _alvos(ins: roteiro.Instrucao) -> list[dict]:
        """As alternativas da linha no formato do ajudante, com os seletores genéricos marcados."""
        return [{"v": a.valor, "t": True} if a.por_texto else {"v": a.valor, "t": False, "fraco": roteiro.fraco(a.valor)}
                for a in ins.alvos]

    def _clicar(self, aba: Aba, ins: roteiro.Instrucao, segundos: float, antes=None) -> None:
        aba.js("__ac.travarArquivo()")  # a página pode ter navegado e perdido a trava
        alvos = []
        for alvo in ins.alvos:
            # A gravação feita com o menu aberto anota "Criar". No menu compacto,
            # o mesmo controle só mostra o ícone Novo post.
            if self.nome == "instagram" and alvo.por_texto and alvo.valor.strip().casefold() in ("criar", "create"):
                alvos.extend(CRIAR_INSTAGRAM)
            else:
                alvos.extend(self._alvos(roteiro.Instrucao(comando=ins.comando, alvos=[alvo])))
        # a primeira alternativa que existir vence; desabilitada, espera liberar (o botão de publicar
        # só libera quando o envio termina) em vez de cair num seletor genérico qualquer
        aba.clicar_alvos(alvos, segundos, antes=antes, o_que=" ou ".join(str(a) for a in ins.alvos))

    def _escrever(self, aba: Aba, ins: roteiro.Instrucao, t: dict, segundos: float) -> None:
        valor = ins.texto if ins.papel is None else t.get(ins.papel)
        if isinstance(valor, list):
            valor = ", ".join(str(x) for x in valor)
        aba.digitar_alvos(self._alvos(ins), str(valor or ""), segundos, " ou ".join(str(a) for a in ins.alvos))

    def _esperar_alvo(self, aba: Aba, ins: roteiro.Instrucao, segundos: float) -> None:
        """Espera um texto aparecer na tela, ou um elemento existir e estar habilitado."""
        condicoes = [f"__ac.texto().includes({json.dumps(' '.join(a.valor.lower().split()))})"
                     for a in ins.alvos if a.por_texto]
        seletores = [a for a in self._alvos(ins) if not a["t"]]
        if seletores:
            condicoes.append(f"__ac.situacao({json.dumps(seletores)}).estado === 'pronto'")
        aba.esperar("(() => (" + " || ".join(condicoes) + ") ? 1 : 0)()", segundos,
                    " ou ".join(str(a) for a in ins.alvos))

    def _links(self, aba: Aba) -> set[str]:
        """Links de post que estão na página agora."""
        try:
            lista = aba.js("(() => __ac.todos('a[href]').map((x) => x.href).filter((h) => /"
                           + _LINK_POST.replace("/", "\\/") + "/.test(h)))()")
        except ErroNavegador:
            return set()
        return {str(h) for h in (lista or []) if str(h).startswith("https://")}

    def _link_novo(self, aba: Aba, antes: set[str]) -> str | None:
        """O link do post que acabou de sair: só conta link que não estava na página antes do vídeo.

        Depois de publicar, a rede pode abrir a lista dos seus posts: com mais de um post novo
        na tela, não dá para saber qual é este, e fica sem link.
        """
        novos = sorted(self._links(aba) - antes)
        if novos and len({_chave_do_link(n) for n in novos}) == 1:
            return novos[0]
        return None

    # ---- TikTok: tiktok.com/tiktokstudio/upload
    def _tiktok(self, aba: Aba, arquivo: Path, t: dict, ensaio: bool, entrega: Entrega) -> Resultado:
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
            "(() => { const b = __ac.porTexto('button', ['post', 'publicar'], true);"
            " return b && !__ac.desligado(b) ? 1 : 0; })()", ESPERA_PUBLICAR,
            "o TikTok não terminou de receber o vídeo",
        )
        _conferir_bloqueio(aba, so_dialogos=True)
        if ensaio:
            return self._fim()
        aba.clicar_alvos([{"v": ["post", "publicar"], "t": True, "em": "button"}], 60,
                         antes=lambda: self._aceitar(entrega), o_que="botão Post")
        if not self._confirmar(aba, "/manage your posts|gerenciar|foi publicado|posted|enviado com sucesso/", 300):
            return self._fim("cliquei em publicar, mas o TikTok não mostrou a confirmação; confira no app",
                             confirmado=False)
        return self._fim("publicado pelo navegador; o link aparece no app")

    def _confirmar(self, aba: Aba, expressao: str, segundos: float) -> bool:
        """Espera a confirmação depois de publicar. Sem ela, o post fica para você conferir."""
        try:
            aba.esperar(f"(() => {expressao}.test(__ac.texto()) ? 1 : 0)()", segundos, "a confirmação")
            return True
        except ErroNavegador as e:
            if e.conexao or (aba.parar is not None and aba.parar.is_set()):
                raise
            log.warning("%s: %s", self.rotulo, e)
            return False

    # ---- YouTube Studio (Polymer, quase tudo em shadow DOM)
    def _youtube(self, aba: Aba, arquivo: Path, t: dict, ensaio: bool, entrega: Entrega) -> Resultado:
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
        except ErroNavegador as e:
            if e.conexao:
                raise
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
        _conferir_bloqueio(aba, so_dialogos=True)
        if ensaio:
            return self._fim()
        aba.clicar_alvos(css(["#done-button", "ytcp-button#done-button"]), 60, antes=lambda: self._aceitar(entrega))
        link = None
        try:
            link = aba.esperar(
                "(() => { const a = __ac.todos('a[href*=\"youtu\"]').find((x) => /shorts|youtu\\.be/.test(x.href));"
                " return a ? a.href : 0; })()", 120, "o link do vídeo",
            )
        except ErroNavegador as e:
            if e.conexao or (aba.parar is not None and aba.parar.is_set()):
                raise
            log.info("YouTube: cliquei em publicar, mas não achei o link na tela")
        if not link:
            return self._fim("cliquei em publicar, mas não vi o link do vídeo; confira no YouTube Studio",
                             confirmado=False)
        return self._fim(None, str(link))

    # ---- Instagram (instagram.com, criar Reel)
    @staticmethod
    def _abrir_envio_instagram(aba: Aba, segundos: float = 60) -> None:
        aba.clicar_alvos(CRIAR_INSTAGRAM, segundos, o_que="botão Novo post / Criar")
        aba.esperar(
            f"__ac.presente('input[type=file]') || __ac.situacao({json.dumps(POSTAR_INSTAGRAM)}).estado === 'pronto'",
            segundos, "o envio do Instagram ou o menu Novo post",
        )
        if not aba.js("__ac.presente('input[type=file]')"):
            aba.clicar_alvos(POSTAR_INSTAGRAM, segundos, o_que="opção Postar do menu Novo post")
            aba.esperar_seletor("input[type=file]", segundos, "o campo de vídeo do Instagram")

    def _instagram(self, aba: Aba, arquivo: Path, t: dict, ensaio: bool, entrega: Entrega) -> Resultado:
        self._abrir_envio_instagram(aba)
        aba.enviar_arquivo(["input[type=file][accept*='video']", "input[type=file]"], arquivo, 60)
        # recortar > editar > legenda (o Instagram muda a quantidade de etapas)
        for _ in range(3):
            try:
                aba.clicar_texto("div[role='button'], button", ["avançar", "next"], 20)
            except ErroNavegador as e:
                if e.conexao:
                    raise
                break
        legenda = ["div[contenteditable='true'][role='textbox']", "textarea[aria-label*='legenda']",
                   "div[contenteditable='true']"]
        aba.esperar_seletor(legenda, 60, "o campo da legenda")
        aba.digitar(legenda, t["legenda"])
        _conferir_bloqueio(aba, so_dialogos=True)
        if ensaio:
            return self._fim()
        aba.clicar_alvos([{"v": ["compartilhar", "share"], "t": True, "em": "div[role='button'], button"}], 60,
                         antes=lambda: self._aceitar(entrega), o_que="botão Compartilhar")
        if not self._confirmar(aba, "/foi compartilhad|compartilhado|has been shared|your reel/", ESPERA_PUBLICAR):
            return self._fim("cliquei em compartilhar, mas o Instagram não mostrou a confirmação; confira no perfil",
                             confirmado=False)
        return self._fim("publicado pelo navegador; o link aparece no perfil")

    # ---- Bilibili (member.bilibili.com)
    def _bilibili(self, aba: Aba, arquivo: Path, t: dict, ensaio: bool, entrega: Entrega) -> Resultado:
        aba.enviar_arquivo("input[type=file]", arquivo, 60)
        aba.digitar(["input[placeholder*='标题']", ".video-title input", "input[maxlength='80']"], t["titulo"], 120)
        observacoes = []
        try:  # 转载 (repost) e a fonte: o corte é de um filme de terceiros
            aba.clicar_texto("div, span, label", ["转载"], 20)
            aba.digitar(["input[placeholder*='转载']", "input[placeholder*='来源']"], t["fonte"], 20)
        except ErroNavegador as e:
            if e.conexao:
                raise
            observacoes.append("confira no site se o tipo ficou 转载 com a fonte")
        try:
            aba.digitar([".ql-editor", "div[data-mode='rich']", "textarea[placeholder*='简介']"], t["descricao"], 20)
        except ErroNavegador as e:
            if e.conexao:
                raise
            observacoes.append("a descrição pode não ter entrado")
        try:
            for tag in t["tags"][:10]:
                aba.digitar(["input[placeholder*='标签']", "input[placeholder*='tag']"], tag, 15)
                aba.tecla("Enter")
        except ErroNavegador as e:
            if e.conexao:
                raise
            observacoes.append("as tags podem não ter entrado")
        aba.esperar(
            "(() => { const t = __ac.texto(); return /上传完成|上传成功|100%/.test(t) ? 1 : 0; })()",
            1800, "o Bilibili não terminou de receber o vídeo",
        )
        _conferir_bloqueio(aba, so_dialogos=True)
        if ensaio:
            return self._fim()
        aba.clicar_alvos([{"v": ["立即投稿", "投稿"], "t": True, "em": "span, button, div"}], 60,
                         antes=lambda: self._aceitar(entrega), o_que="botão 立即投稿")
        if not self._confirmar(aba, "/投稿成功|稿件投递成功|投递成功/", 300):
            observacoes.append("cliquei em 投稿, mas o Bilibili não mostrou a confirmação; confira no site")
            return self._fim("; ".join(observacoes), confirmado=False)
        observacoes.append("o vídeo passa pela revisão do Bilibili antes de aparecer")
        return self._fim("; ".join(observacoes))


# ---------------------------------------------------------------- aprender a postar

class Gravacao:
    """Uma gravação em andamento: a janela fica aberta enquanto você posta um vídeo à mão."""

    LIMITE_SEG = 45 * 60  # esquecida aberta, ela se encerra sozinha (e fecha a aba)

    def __init__(self, cfg: Config, rede: str):
        self.cfg = cfg
        self.rede = rede
        self.passos: list[dict] = []
        self.erro: str | None = None
        self.url = ""
        self.texto = ""
        self.texto_inicial = ""  # a página antes de você postar: o que já estava lá não serve de confirmação
        self.inicio = time.time()
        self.sessao = next(_SESSOES)
        self._parar = threading.Event()
        self._navegador = Navegador(cfg)
        self._navegador.conf = dict(cfg["navegador"], visivel=True)  # você precisa ver a janela
        self._aba: Aba | None = None
        self._trava = threading.Lock()
        self.thread: threading.Thread | None = None

    def iniciar(self) -> None:
        # a marca entra segurando a trava do navegador: um envio no meio termina antes, e o próximo
        # já encontra a gravação e espera
        if not TRAVA.acquire(timeout=5):
            raise ErroNavegador("o AutoCortes está postando pelo navegador agora. Espere o envio terminar e "
                                "comece a gravação de novo", "temporario")
        try:
            _marcar_gravacao(self.rede, self.sessao, True)
        finally:
            TRAVA.release()
        try:
            self._navegador.abrir()
            aba = self._navegador.nova_aba("about:blank")
            try:
                aba.navegar(PAGINAS[self.rede][1], 60)
                esperar_sessao(aba, self.rede)
                aba.avaliar(gravador.SCRIPT)
                self.texto_inicial = str(aba.avaliar("window.__acGrav.texto()") or "")
            except ErroNavegador:
                self._navegador.fechar_aba(aba)
                raise
        except BaseException:
            _marcar_gravacao(self.rede, self.sessao, False)
            raise
        self._aba = aba
        self.url = aba.url()
        self.thread = threading.Thread(target=self._acompanhar, name=f"gravar-{self.rede}", daemon=True)
        self.thread.start()
        log.info("%s: gravando o roteiro. Poste um vídeo à mão na janela que abriu.", ROTULOS[self.rede])

    def _colher(self) -> None:
        """Traz o que o gravador anotou na página (precisa da trava)."""
        if self._aba is None:
            return
        if not self._aba.avaliar("typeof window.__acGrav !== 'undefined' ? 1 : 0"):
            self._aba.avaliar(gravador.SCRIPT)  # a página navegou e levou o gravador
        novos = self._aba.avaliar("window.__acGrav.tirar()") or []
        self.passos.extend(n for n in novos if isinstance(n, dict))
        self.url = self._aba.url()
        self.texto = str(self._aba.avaliar("window.__acGrav.texto()") or "")

    def _fechar_aba(self) -> None:
        with self._trava:
            aba, self._aba = self._aba, None
        if aba is not None:
            self._navegador.fechar_aba(aba)

    def _acompanhar(self) -> None:
        """Recolhe os passos de tempo em tempo e reinjeta o gravador quando a página troca."""
        try:
            while not self._parar.wait(1.5):
                if time.time() - self.inicio > self.LIMITE_SEG:
                    self.erro = "a gravação passou de 45 min e foi encerrada (o que foi anotado até aqui dá para salvar)"
                    self._fechar_aba()
                    break
                with self._trava:
                    if self._aba is None:
                        break
                    try:
                        self._colher()
                    except ErroNavegador as e:
                        self.erro = f"perdi a janela da gravação ({e})"
                        break
        finally:
            _marcar_gravacao(self.rede, self.sessao, False)

    def situacao(self) -> dict:
        lista = gravador.lista_passos(self.rede, self.passos)
        return {
            "rede": self.rede,
            "passos": len(lista),
            "lista": lista,
            "brutos": len(self.passos),
            "url": self.url,
            "erro": self.erro,
            "parada": self._parar.is_set() or self._aba is None,
            "segundos": int(time.time() - self.inicio),
            "marcas": gravador.papeis(self.rede),
        }

    def desfazer(self) -> dict:
        """Apaga o último passo anotado (você errou um clique e quer refazer na hora)."""
        with self._trava:
            try:
                self._colher()
            except ErroNavegador:
                pass
            alvo = len(gravador.limpar_passos(self.rede, self.passos))
            while self.passos and alvo and len(gravador.limpar_passos(self.rede, self.passos)) >= alvo:
                self.passos.pop()
        return self.situacao()

    def recomecar(self) -> dict:
        """Zera o que foi anotado, sem fechar a janela: você recomeça do zero."""
        with self._trava:
            try:
                self._colher()
            except ErroNavegador:
                pass
            self.passos.clear()
            self.inicio = time.time()
        return self.situacao()

    def concluir(self) -> dict:
        """Salva o que foi gravado e fecha a aba."""
        self._parar.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        diagnostico: dict = {}
        with self._trava:
            aba = self._aba
            if aba is not None:
                try:  # última colheita, com o que ficou na tela ao terminar
                    self._colher()
                    diagnostico = aba.avaliar("window.__acGrav.diagnostico()") or {}
                except ErroNavegador:
                    pass
                self._aba = None
        if aba is not None:
            self._navegador.fechar_aba(aba)
        _marcar_gravacao(self.rede, self.sessao, False)
        if not self.passos:
            raise ErroNavegador(
                "não gravei nenhum passo. Poste um vídeo à mão na janela que abriu antes de concluir", "corte")
        resultado = gravador.salvar(self.cfg, self.rede, self.passos, PAGINAS[self.rede][1], self.texto,
                                    self.texto_inicial)
        if not resultado.get("ok") and diagnostico:
            # sem digitação anotada, o diagnóstico mostra se o campo estava num quadro (iframe)
            log.warning("%s: diagnóstico da página ao terminar: %s", ROTULOS[self.rede], diagnostico)
            resultado["diagnostico"] = diagnostico
        return resultado

    def cancelar(self) -> None:
        self._parar.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self._fechar_aba()
        _marcar_gravacao(self.rede, self.sessao, False)
