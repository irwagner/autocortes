"""Com que perfil a janela do navegador está no Facebook, e a troca para a Página certa.

É a única porta que decide "estou na Página certa" no envio pelo navegador. Dois sinais, que
precisam concordar (Sinal A e Sinal B do desenho, 30/10/2026):
- o cookie: c_user é a pessoa e i_user é a Página em que ela está agindo (o DevTools lê os dois
  mesmo com HttpOnly). Sem i_user, a sessão está no perfil pessoal;
- o destino de /me: o Facebook manda para o perfil em uso (profile.php?id=N ou o link da Página).

Nenhum dos dois foi medido com conta logada aqui: na dúvida, não posta. O c_user nunca entra num
texto (motivo, registro, navegador.json): tudo o que é mostrado passa por descrever.

Este módulo só conhece a aba (navegador.py); quem conhece a rede e o roteiro é plataformas/navegador.py.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urlsplit

from ..navegador import Aba, ErroNavegador, css

log = logging.getLogger("autocortes")

BASE = "https://www.facebook.com"

# Textos da troca (pt-BR, inglês e espanhol). Medidos por terceiros, não com conta logada aqui:
# ajuste aqui se o Facebook mudar, sem mexer na lógica.
TEXTOS_TROCA_DIRETA = ("Mudar agora", "Alternar agora", "Mudar para a Página", "Switch now", "Switch into",
                       "Cambiar ahora")
TEXTOS_MENU_CONTA = ('[aria-label="Seu perfil"]', '[aria-label="Your profile"]', '[aria-label="Conta"]',
                     '[aria-label="Account"]')
TEXTOS_TODOS_PERFIS = ("Ver todos os perfis", "See all profiles", "Ver todos los perfiles")
# quanto a troca espera o cookie mudar depois do clique
ESPERA_TROCA = 30
# quanto cada botão da troca pode demorar a aparecer (sem ele, o próximo caminho)
ESPERA_BOTAO = 8
# quanto a lista de perfis pode demorar a mostrar a linha da Página
ESPERA_LISTA = 10

_NUMERO = re.compile(r"\d{1,30}")
_LINK = re.compile(r"^(?:https?://)?(?:[a-z0-9-]+\.)*(?:facebook|fb)\.com(?:[/?#]|$)", re.I)
_ID_NA_URL = (re.compile(r"[?&]id=(\d{1,30})(?:&|#|$)"), re.compile(r"/people/[^/?#]+/(\d{1,30})(?:[/?#]|$)"))


# ---------------------------------------------------------------- partes puras

def normalizar(texto) -> str:
    """Nome comparável: NFC, sem maiúsculas, espaços juntados, sem o "(3) " e o " | Facebook" do título."""
    t = " ".join(unicodedata.normalize("NFC", str(texto or "")).split())
    t = re.sub(r"^\(\d+\+?\)\s*", "", t)
    t = re.sub(r"\s*\|\s*Facebook$", "", t, flags=re.I)
    return t.casefold()


def _titulo_limpo(texto) -> str:
    """O título do /me para mostrar (sem o contador de avisos e o " | Facebook")."""
    t = " ".join(unicodedata.normalize("NFC", str(texto or "")).split())
    t = re.sub(r"^\(\d+\+?\)\s*", "", t)
    return re.sub(r"\s*\|\s*Facebook$", "", t, flags=re.I).strip()


def _id_da_url(url: str) -> str | None:
    for expressao in _ID_NA_URL:
        m = expressao.search(url or "")
        if m:
            return m.group(1)
    return None


def _caminho(url: str) -> str | None:
    texto = str(url or "").strip()
    if not re.match(r"^https?://", texto, re.I):
        texto = "https://" + texto
    try:
        caminho = urlsplit(texto).path
    except ValueError:
        return None
    caminho = caminho.rstrip("/").lower()
    return caminho or None


def ler_entrada(texto) -> dict:
    """O que você informou em [facebook].pagina: {"id", "caminho", "nome"} (só um deles vem preenchido).

    Dígitos, profile.php?id=N e /people/<nome>/N dão o id; outro link do facebook.com dá o caminho
    (minúsculo, sem barra final nem consulta); texto comum é o nome, já normalizado.
    """
    t = str(texto or "").strip()
    vazio = {"id": None, "caminho": None, "nome": None}
    if not t:
        return vazio
    if _NUMERO.fullmatch(t):
        return dict(vazio, id=t)
    if _LINK.match(t):
        numero = _id_da_url(t)
        if numero:
            return dict(vazio, id=numero)
        caminho = _caminho(t)
        return dict(vazio, caminho=caminho) if caminho and caminho != "/profile.php" else vazio
    return dict(vazio, nome=normalizar(t) or None)


def _limpo(valor) -> str | None:
    valor = str(valor or "").strip()
    return valor if _NUMERO.fullmatch(valor) else None


@dataclass
class Identidade:
    """Com que perfil a janela está agindo. Nunca guarda o c_user nem a URL do /me."""

    agindo_como: str  # pagina | pessoal | outra | deslogado
    id: str | None = None  # o i_user (None no perfil pessoal)
    nome: str | None = None  # o título do /me, só quando o id da URL é o i_user
    confere: bool = False
    motivo: str = ""
    aprendido: dict | None = None  # {"pagina_id", "pagina_nome"}, só com a Página informada por link
    sugestao: dict | None = None  # {"pagina_id", "pagina_nome"}, só com a Página informada pelo nome

    def publico(self) -> dict:
        return asdict(self)


def _usuarios(ck: dict) -> tuple[str | None, str | None]:
    """c_user e i_user válidos; i_user igual ao c_user é o perfil pessoal."""
    c, i = _limpo(ck.get("c_user")), _limpo(ck.get("i_user"))
    return c, (None if i == c else i)


def _nome_valido(ck: dict, me: dict | None) -> str | None:
    """O título do /me só vale (e só aparece) quando a URL dele traz o id do i_user."""
    c, i = _usuarios(ck)
    if not (me and c and i and me.get("id") == i):
        return None
    return _titulo_limpo(me.get("titulo")) or None


def _pagina(numero: str, nome: str | None) -> str:
    return f"a Página {nome} (id {numero})" if nome else f"a Página id {numero}"


def descrever(ck: dict, me: dict | None = None, caminho_da_pagina: str | None = None) -> str:
    """O que os sinais viram, em texto. Dona da regra de privacidade: o c_user nunca sai daqui.

    Um id igual ao c_user (no cookie ou na URL do /me) vira "o seu perfil pessoal"; um id de Página,
    "a Página id N" (com o nome só quando o título vale); URL sem id reconhecível, "uma página que
    não reconheço". Sem c_user, nenhum id do /me aparece (ele poderia ser o pessoal).
    """
    c, i = _usuarios(ck)
    nome = _nome_valido(ck, me)
    if not c:
        pelo_cookie = "nenhuma sessão aberta"
    elif not i:
        pelo_cookie = "o seu perfil pessoal"
    else:
        pelo_cookie = _pagina(i, nome)
    if me is None:
        return pelo_cookie
    numero = me.get("id")
    if numero and c and numero == c:
        pelo_me = "o seu perfil pessoal"
    elif numero and c and numero == i:
        pelo_me = _pagina(numero, nome)
    elif numero and c:
        pelo_me = _pagina(numero, None)
    elif caminho_da_pagina and me.get("caminho") == caminho_da_pagina:
        pelo_me = "a Página informada"
    else:
        pelo_me = "uma página que não reconheço"
    if pelo_me == pelo_cookie:
        return pelo_cookie
    return f"{pelo_cookie} pelo cookie, mas {pelo_me} pelo /me"


def alvo(cfg_fb: dict) -> str:
    """Como a Página configurada é chamada nas mensagens."""
    nome = str(cfg_fb.get("pagina_nome") or "").strip()
    numero = str(cfg_fb.get("pagina_id") or "").strip()
    if nome and numero:
        return f"{nome} (id {numero})"
    if nome or numero:
        return nome or f"id {numero}"
    pagina = str(cfg_fb.get("pagina") or "").strip()
    return pagina if pagina and not _LINK.match(pagina) else "informada"


def decidir(ck: dict, me: dict | None, cfg_fb: dict) -> Identidade:
    """Junta os dois sinais e a Página configurada. Pura: os testes passam os sinais prontos."""
    c, i = _usuarios(ck)
    me = me or {}
    pagina_id = str(cfg_fb.get("pagina_id") or "").strip()
    pagina_nome = str(cfg_fb.get("pagina_nome") or "").strip()
    entrada = ler_entrada(cfg_fb.get("pagina"))
    visto = descrever(ck, me, entrada["caminho"])
    nome = _nome_valido(ck, me)
    if not c:
        return Identidade("deslogado", motivo="a sessão do Facebook não está aberta nesta janela (entre na conta "
                                               "em Redes sociais > Facebook)")
    if not i:
        agindo = "pessoal"
    elif i == (pagina_id or entrada["id"]):
        agindo = "pagina"
    else:
        agindo = "outra"
    ident = Identidade(agindo, i, nome)
    de_quem = f"a janela está agindo como {visto}"
    if pagina_id:
        sinal_a = i == pagina_id
        sinal_b = (me.get("id") == pagina_id
                   or bool(entrada["caminho"] and me.get("caminho") == entrada["caminho"])
                   or bool(pagina_nome and normalizar(me.get("titulo")) == normalizar(pagina_nome)))
        if sinal_a and sinal_b:
            ident.confere = True
            ident.motivo = f"agindo como {_pagina(i, nome or pagina_nome or None)}"
        elif sinal_a:
            ident.motivo = f"os sinais não concordam: {visto}"
        else:
            ident.motivo = de_quem
        return ident
    if entrada["id"] or entrada["caminho"]:
        # o link aponta uma Página só: o cookie e o /me batendo com ele confirmam e ensinam o id
        bate = bool(i) and (
            (entrada["id"] and i == entrada["id"] and me.get("id") == entrada["id"])
            or (entrada["caminho"] and me.get("caminho") == entrada["caminho"]))
        if bate:
            ident.agindo_como = "pagina"
            ident.confere = True
            ident.aprendido = {"pagina_id": i, "pagina_nome": nome or ""}
            ident.motivo = f"agindo como {_pagina(i, nome)}"
        elif i and entrada["id"] == i:
            ident.motivo = f"os sinais não concordam: {visto}"
        else:
            ident.motivo = de_quem
        return ident
    if entrada["nome"]:
        # só o nome: duas Páginas podem ter o mesmo título, então nunca aprende sozinho
        titulo = _titulo_limpo(me.get("titulo"))
        if i and titulo and normalizar(titulo) == entrada["nome"]:
            ident.agindo_como = "pagina"
            ident.sugestao = {"pagina_id": i, "pagina_nome": titulo}
            ident.motivo = (f"agindo como a Página {titulo} (id {i}); confirme em Redes sociais > Facebook "
                            "se é esta")
        else:
            ident.motivo = de_quem
        return ident
    ident.motivo = "falta informar a Página em Redes sociais > Facebook (o link dela ou o nome exato)"
    return ident


def mensagem(cfg_fb: dict, ident: Identidade) -> str:
    """O texto do "não confirmei a Página" (envio, gravação e Testar sessão)."""
    texto = f"não consegui confirmar que estou na Página {alvo(cfg_fb)}: {ident.motivo}"
    if ident.sugestao:
        return texto
    return (texto + ". Troque para a Página na janela do navegador (ou informe o link dela) e use Testar "
            "sessão em Redes sociais > Facebook")


class ErroPagina(ErroNavegador):
    """A janela não está (ou deixou de estar) na Página configurada: nada é postado até você testar a conta."""

    def __init__(self, mensagem_: str, identidade: Identidade):
        super().__init__(mensagem_, "bloqueio", persistente=True)
        self.identidade = identidade


# ---------------------------------------------------------------- leitura na aba

def cookies(aba: Aba) -> dict:
    """c_user e i_user da sessão (lidos pelo DevTools, mesmo HttpOnly); valor que não é número vira None."""
    r = aba.comando("Network.getCookies", urls=[BASE + "/"])
    saida: dict = {"c_user": None, "i_user": None}
    for cookie in r.get("cookies") or []:
        nome = cookie.get("name")
        if nome in saida and saida[nome] is None:
            saida[nome] = _limpo(cookie.get("value"))
    return saida


def logado(aba: Aba) -> bool:
    return bool(cookies(aba).get("c_user")) and not aba.existe("input[type=password]")


def destino_me(aba: Aba) -> dict:
    """Para onde o /me leva: {"url", "id", "caminho", "titulo"} (a URL fica só aqui, nunca num texto)."""
    aba.navegar(BASE + "/me")
    url = aba.url()
    titulo = str(aba.avaliar("document.title") or "")
    return {"url": url, "id": _id_da_url(url), "caminho": _caminho(url), "titulo": titulo}


def _ler(aba: Aba, cfg) -> tuple[Identidade, dict]:
    ck = cookies(aba)
    me = destino_me(aba)
    return decidir(ck, me, cfg["facebook"]), ck


def _conferir(aba: Aba, cfg, trocar: bool, conferir_bloqueio=None) -> tuple[Identidade, dict]:
    ident, ck = _ler(aba, cfg)
    if not ident.confere and trocar and ident.sugestao is None and ident.agindo_como != "deslogado":
        log.info("Facebook: %s; vou tentar trocar para a Página", ident.motivo)
        globals()["trocar"](aba, cfg, conferir_bloqueio)
        ident, ck = _ler(aba, cfg)
    return ident, ck


def conferir(aba: Aba, cfg, trocar: bool, conferir_bloqueio=None) -> Identidade:
    """Os dois sinais (e, se não conferem e trocar=True, uma troca e outra leitura). Não levanta erro."""
    return _conferir(aba, cfg, trocar, conferir_bloqueio)[0]


def exigir(aba: Aba, cfg, trocar: bool, conferir_bloqueio=None) -> tuple[Identidade, str]:
    """Como conferir, mas sem a Página confirmada levanta ErroPagina. Devolve o c_user à parte."""
    ident, ck = _conferir(aba, cfg, trocar, conferir_bloqueio)
    if not ident.confere:
        raise ErroPagina(mensagem(cfg["facebook"], ident), ident)
    return ident, ck["c_user"]


def antes_de_publicar(aba: Aba, cfg, c_user_inicial: str) -> None:
    """Logo antes do clique de publicar: só o cookie (o /me tiraria a aba do compositor).

    O mesmo c_user do começo do envio, o i_user igual ao pagina_id e, quando a URL traz asset_id,
    ele também igual. Senão, ErroPagina, e nada é clicado.
    """
    fb = cfg["facebook"]
    pagina_id = str(fb.get("pagina_id") or "").strip()
    ck = cookies(aba)
    c, i = _usuarios(ck)
    agora = descrever(ck)
    certo = bool(pagina_id) and c == c_user_inicial and i == pagina_id
    if certo:
        try:
            ativo = (parse_qs(urlsplit(aba.url()).query).get("asset_id") or [""])[0]
        except ValueError:
            ativo = ""
        if ativo and ativo != pagina_id:
            certo = False
            agora = ("a página de publicar de outra Página"
                     + (f" (id {ativo})" if _limpo(ativo) and ativo != c else ""))
    if not certo:
        agindo = "deslogado" if not c else ("pessoal" if not i else ("pagina" if i == pagina_id else "outra"))
        ident = Identidade(agindo, i if c else None, motivo=f"a Página mudou no meio do envio (agora: {agora})")
        raise ErroPagina(f"a Página mudou no meio do envio (agora: {agora}); não publiquei", ident)


# ---------------------------------------------------------------- troca

# A linha da Página na lista de perfis (R3). Candidatos: elementos visíveis com o texto igual ao
# nome (entrando no shadow DOM) e dentro de um role=dialog|menu; de cada aninhamento, só o mais
# interno. Com o id da Página, antes um link para ela no mesmo tipo de contêiner. Dois candidatos
# pelo nome: não clica (duas Páginas com o mesmo nome).
_ESCOLHER = r"""
((nome, id) => {
  const norm = (t) => String(t || "").normalize("NFC").toLocaleLowerCase().replace(/\s+/g, " ").trim();
  const sobe = (el) => {
    const p = el.parentNode;
    if (!p) return null;
    if (p.nodeType === 11 && p.host) return p.host;
    return p.nodeType === 1 ? p : null;
  };
  const conteiner = (el) => {
    for (let x = sobe(el); x; x = sobe(x)) {
      const papel = x.getAttribute ? x.getAttribute("role") : "";
      if (papel === "dialog" || papel === "menu") return x;
    }
    return null;
  };
  const dentroDe = (a, b) => { for (let x = sobe(a); x; x = sobe(x)) { if (x === b) return true; } return false; };
  const internos = (lista) => lista.filter((el) => !lista.some((o) => o !== el && dentroDe(o, el)));
  const PAPEIS = ["button", "link", "menuitem", "menuitemradio", "option", "radio", "tab"];
  const clicavel = (el) => {
    const tag = (el.tagName || "").toLowerCase();
    if (["a", "button", "label", "option"].includes(tag)) return true;
    if (PAPEIS.includes(el.getAttribute("role") || "")) return true;
    if (el.hasAttribute("tabindex") || el.hasAttribute("onclick")) return true;
    try { return getComputedStyle(el).cursor === "pointer"; } catch (e) { return false; }
  };
  const clicar = (el, por) => {
    const caixa = conteiner(el);
    let alvo = el;
    for (let x = el; x && x !== caixa; x = sobe(x)) { if (clicavel(x)) { alvo = x; break; } }
    alvo.scrollIntoView({ block: "center" });
    alvo.click();
    return { estado: "clicado", por };
  };
  if (id) {
    const re = new RegExp("(?:[?&]id=" + id + "(?:&|#|$))|(?:/" + id + "(?:[/?#]|$))");
    const links = internos(__ac.todos("[href]").filter((el) => re.test(el.getAttribute("href") || "")
      && __ac.visivel(el) && conteiner(el)));
    // vários links com o mesmo id são a mesma Página (a foto e o nome na mesma linha)
    if (links.length) return clicar(links[0], "id");
  }
  const alvo = norm(nome);
  if (!alvo) return { estado: "nenhum" };
  const junto = alvo.replace(/ /g, "");
  const candidatos = internos(__ac.todos("*").filter((el) => {
    const bruto = norm([el.textContent, el.getAttribute("aria-label")].join(" ")).replace(/ /g, "");
    if (!bruto.includes(junto) || !__ac.visivel(el) || !conteiner(el)) return false;
    return [el.innerText, el.getAttribute("aria-label"), __ac.textoDe(el)].some((t) => norm(t) === alvo);
  }));
  if (!candidatos.length) return { estado: "nenhum" };
  if (candidatos.length > 1) return { estado: "repetido", n: candidatos.length };
  return clicar(candidatos[0], "nome");
})
"""


def escolher_perfil(aba: Aba, nome: str, pagina_id: str | None) -> dict:
    """Procura (e clica) a linha da Página na lista de perfis aberta: {"estado": nenhum|repetido|clicado}."""
    numero = _limpo(pagina_id) or ""
    r = aba.js(f"{_ESCOLHER}({json.dumps(nome or '')}, {json.dumps(numero)})")
    return r if isinstance(r, dict) else {"estado": "nenhum"}


def _bloqueio(texto: str) -> ErroNavegador:
    return ErroNavegador(texto, "bloqueio", persistente=True)


def _vigiar(aba: Aba, conferir_bloqueio) -> None:
    """Checkpoint, senha ou aviso de bloqueio no meio da troca: é com você."""
    if conferir_bloqueio is not None:
        conferir_bloqueio(aba)
    if "/checkpoint" in aba.url().lower():
        raise _bloqueio("o Facebook pediu uma verificação: resolva na janela do navegador e teste a conta")
    if aba.existe("input[type=password]"):
        raise _bloqueio("o Facebook pediu a senha durante a troca de perfil: entre na janela do navegador e "
                        "teste a conta")


def _clicou(aba: Aba, alvos: list[dict], o_que: str, conferir_bloqueio) -> bool:
    """Clica se o botão aparecer em poucos segundos; sem ele, o chamador segue para o próximo caminho."""
    _vigiar(aba, conferir_bloqueio)
    try:
        aba.clicar_alvos(alvos, ESPERA_BOTAO, o_que=o_que)
    except ErroNavegador as e:
        if e.conexao or e.persistente:
            raise
        return False
    aba.pausa()
    return True


def _esperar_cookie(aba: Aba, pagina_id: str | None, antes: str | None, conferir_bloqueio) -> bool:
    """Depois do clique, só o cookie novo conta (um clique "aceito" não prova nada)."""
    limite = time.monotonic() + ESPERA_TROCA
    while True:
        _vigiar(aba, conferir_bloqueio)
        i = _usuarios(cookies(aba))[1]
        if (i == pagina_id) if pagina_id else (i is not None and i != antes):
            return True
        if time.monotonic() >= limite:
            return False
        aba.pausa_fixa(1)


def trocar(aba: Aba, cfg, conferir_bloqueio=None) -> None:
    """Uma rodada de troca para a Página: o botão da própria Página e, sem ele, a lista de perfis.

    Checkpoint, senha ou bloqueio viram ErroNavegador persistente. O resto (botão que não aparece,
    cookie que não muda) só volta: a conferência seguinte explica o que viu.
    """
    fb = cfg["facebook"]
    entrada = ler_entrada(fb.get("pagina"))
    pagina_id = _limpo(fb.get("pagina_id")) or entrada["id"]
    nome = str(fb.get("pagina_nome") or "").strip() or (str(fb.get("pagina") or "").strip() if entrada["nome"] else "")
    antes = _usuarios(cookies(aba))[1]
    # 1. o botão na própria Página ("Mudar agora")
    if pagina_id or entrada["caminho"]:
        link = f"{BASE}/profile.php?id={pagina_id}" if pagina_id else BASE + entrada["caminho"]
        aba.navegar(link)
        if _clicou(aba, [{"v": list(TEXTOS_TROCA_DIRETA), "t": True}], "o botão de mudar para a Página",
                   conferir_bloqueio):
            if not _esperar_cookie(aba, pagina_id, antes, conferir_bloqueio):
                log.info("Facebook: cliquei em mudar para a Página, mas o perfil não mudou")
            return
    # 2. o menu da conta, "Ver todos os perfis" e a linha da Página
    if not nome and not pagina_id:
        log.info("Facebook: sem o nome nem o id da Página, não sei qual linha escolher na lista de perfis")
        return
    aba.navegar(BASE + "/")
    if not _clicou(aba, css(TEXTOS_MENU_CONTA), "o menu da conta", conferir_bloqueio):
        log.info("Facebook: não achei o menu da conta para trocar de perfil")
        return
    _clicou(aba, [{"v": list(TEXTOS_TODOS_PERFIS), "t": True}], "Ver todos os perfis", conferir_bloqueio)
    limite = time.monotonic() + ESPERA_LISTA
    while True:
        _vigiar(aba, conferir_bloqueio)
        r = escolher_perfil(aba, nome, pagina_id)
        if r.get("estado") == "repetido":
            raise _bloqueio(f"há {r.get('n') or 'mais de uma'} Páginas com o nome {nome} na lista de perfis: "
                            "informe o link da Página em Redes sociais > Facebook")
        if r.get("estado") == "clicado":
            break
        if time.monotonic() >= limite:
            log.info("Facebook: não achei a Página na lista de perfis")
            return
        aba.pausa_fixa(0.5)
    aba.pausa()
    if not _esperar_cookie(aba, pagina_id, antes, conferir_bloqueio):
        log.info("Facebook: escolhi a Página na lista de perfis, mas o perfil não mudou")
