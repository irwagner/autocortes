"""Controle do Chrome pelo protocolo do DevTools (CDP), para postar pelo navegador.

O AutoCortes abre o Chrome com um perfil separado, guardado em dados/chrome. Você loga
uma vez em cada rede nessa janela e a sessão fica ali entre reinícios. Depois, o envio é
feito clicando na própria página de postagem da rede, como se fosse você.

Só escuta em 127.0.0.1. Nada de forjar fingerprint, resolver captcha, API privada ou proxy:
se a rede bloquear, o envio para e avisa.

O CDP fala WebSocket, que a biblioteca padrão do Python não tem: o cliente mínimo (RFC 6455)
está em `websocket.py`, para não precisar de dependência nova.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from .config import Config
from .util import ler_json, log, salvar_json
from .websocket import ErroWebsocket, Websocket

# uma aba por vez: as redes dividem a mesma janela do navegador
TRAVA = threading.Lock()
# anotação de qual janela do Chrome é deste perfil (dentro da pasta do perfil do Chrome)
ARQUIVO_JANELA = ".autocortes-janela.json"
PROGRAMAS = (
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
)
# eventos que dizem que a aba acabou: esperar a resposta dela não adianta mais
_EVENTOS_FIM = ("Inspector.detached", "Inspector.targetCrashed")
# o `accept` de um campo que aceita JPEG (a mesma expressão do JS do gravador)
_ACEITA_IMAGEM = r"/(^|,)\s*(image\/\*|image\/jpeg|\.jpe?g)\s*(,|$)/i"


class ErroNavegador(Exception):
    """Falha ao controlar o navegador (com o tipo que o publicador entende).

    conexao=True quer dizer que a conversa com o navegador caiu (janela fechada, aba travada,
    sem resposta): não adianta esperar a página mudar, o passo falha na hora.
    persistente=True é o bloqueio que não passa sozinho (a rede pediu verificação): a rede só
    volta quando você resolve na janela e testa a conta de novo.
    parado=True (só com o tipo "formato") quer dizer que as imagens da rede param até você gravar
    o roteiro de imagem de novo: o campo não aceita imagem ou o roteiro de imagem sumiu.
    """

    def __init__(self, mensagem: str, tipo: str = "temporario", conexao: bool = False, persistente: bool = False,
                 parado: bool = False):
        super().__init__(mensagem)
        self.tipo = tipo
        self.conexao = conexao
        self.persistente = persistente
        self.parado = parado


class _Websocket(Websocket):
    """O cliente de `websocket.py` com os erros contados como falha do navegador."""

    def __init__(self, url: str, timeout: float = 60):
        try:
            super().__init__(url, timeout=timeout, rotulo="o navegador")
        except ErroWebsocket as e:
            raise ErroNavegador(str(e), conexao=True) from e

    def enviar(self, texto: str) -> None:
        try:
            super().enviar(texto)
        except ErroWebsocket as e:
            raise ErroNavegador(str(e), conexao=True) from e

    def receber(self) -> str:
        try:
            return super().receber()
        except ErroWebsocket as e:
            raise ErroNavegador(str(e), conexao=True) from e


# ---------------------------------------------------------------- aba

# Ajudante injetado na página. Faz busca profunda, entrando no shadow DOM: o YouTube Studio
# monta quase tudo assim (Polymer), e um querySelector comum não acha nada lá dentro.
AJUDANTE = r"""
window.__ac = (() => {
  const visivel = (el) => {
    if (!el) return false;
    if (el.tagName === "INPUT" && el.type === "file") return true;  // campo de arquivo costuma ficar escondido
    let e;
    try { e = getComputedStyle(el); } catch (erro) { return false; }
    if (e.visibility === "hidden" || e.display === "none" || Number(e.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width + r.height > 0;
  };
  const raizes = () => {
    const achadas = [document];
    const fila = [document];
    while (fila.length) {
      const raiz = fila.shift();
      for (const el of raiz.querySelectorAll("*")) {
        if (el.shadowRoot) { achadas.push(el.shadowRoot); fila.push(el.shadowRoot); }
      }
    }
    return achadas;
  };
  const todos = (sel) => {
    const saida = [];
    for (const raiz of raizes()) {
      try { for (const el of raiz.querySelectorAll(sel)) saida.push(el); } catch (e) { /* seletor inválido */ }
    }
    return saida;
  };
  const achar = (sel) => todos(sel).find(visivel) || todos(sel)[0] || null;
  const limpar = (t) => String(t || "").replace(/\s+/g, " ").trim().toLowerCase();
  const atributo = (el, nome) => (el && el.getAttribute ? el.getAttribute(nome) || "" : "");
  // junta tudo que identifica o botão: o texto dentro dele, o rótulo de acessibilidade e a dica
  const textoDe = (el) => limpar([el.innerText, atributo(el, "aria-label"), atributo(el, "title"),
                                  el.value].filter(Boolean).join(" "));
  // botão e afins: é até ele que o "desabilitado" de quem está em volta vale
  const TAGS_BOTAO = ["button", "a", "summary", "label", "option", "input", "select", "textarea"];
  const PAPEIS_BOTAO = ["button", "link", "menuitem", "menuitemradio", "menuitemcheckbox", "tab", "option",
                        "checkbox", "radio", "switch"];
  const ehBotao = (el) => TAGS_BOTAO.includes((el.tagName || "").toLowerCase())
    || PAPEIS_BOTAO.includes(atributo(el, "role")) || (!!el.hasAttribute && el.hasAttribute("tabindex"));
  const marcado = (el) => {
    try { if (el.matches && el.matches(":disabled")) return true; } catch (e) { /* sem suporte */ }
    if (el.disabled === true || atributo(el, "aria-disabled") === "true") return true;
    if (el.hasAttribute && el.hasAttribute("disabled") && atributo(el, "disabled") !== "false") return true;
    return !!el.hasAttribute && el.hasAttribute("inert");
  };
  const classeDesligada = (el) => /disabled/i.test(atributo(el, "class"));
  const semPonteiro = (el) => { try { return getComputedStyle(el).pointerEvents === "none"; } catch (e) { return false; } };
  const pai = (el) => el.parentElement || (el.getRootNode && el.getRootNode() && el.getRootNode().host) || null;
  // Desabilitado de verdade. O texto que eu acho costuma ser um rótulo dentro do botão (o "Post"
  // do TikTok é um div dentro dele), então subo até o botão, atravessando o shadow DOM, no máximo
  // 6 níveis. Classe com "disabled" vale no próprio elemento e no botão; pointer-events só no botão.
  const desligado = (el) => {
    let atual = el;
    for (let nivel = 0; atual && atual.nodeType === 1 && nivel <= 6; nivel++) {
      const botao = ehBotao(atual);
      if (marcado(atual)) return true;
      if ((botao || nivel === 0) && classeDesligada(atual)) return true;
      if (botao) return semPonteiro(atual);
      atual = pai(atual);
    }
    return false;
  };
  const usavel = (el) => !!el && visivel(el) && !desligado(el);
  const clicavel = (el) => {
    const tag = (el.tagName || "").toLowerCase();
    if (["button", "a", "summary", "label", "option"].includes(tag)) return true;
    if (["button", "link", "menuitem", "tab", "option", "checkbox", "radio"].includes(atributo(el, "role"))) return true;
    if (el.hasAttribute && (el.hasAttribute("tabindex") || el.hasAttribute("onclick"))) return true;
    try { return getComputedStyle(el).cursor === "pointer"; } catch (e) { return false; }
  };
  const CJK = /[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uac00-\ud7af]/;
  const escapar = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const expressoes = new Map();
  // palavra inteira (em chinês e japonês não há espaço entre as palavras: lá vale o trecho)
  const casa = (t, alvo) => {
    if (CJK.test(alvo)) return t.includes(alvo);
    let re = expressoes.get(alvo);
    if (!re) {
      re = new RegExp("(^|[^\\p{L}\\p{N}])" + escapar(alvo) + "($|[^\\p{L}\\p{N}])", "u");
      expressoes.set(alvo, re);
    }
    return re.test(t);
  };
  // Procura em qualquer elemento, não só em button/span/a: o botão de publicar do TikTok é uma div.
  // Nota melhor para quem casa exato, é clicável e tem o texto nele mesmo (e não num filho). Casar
  // em parte só vale com a palavra inteira e até 20 letras a mais: "Publicar" acha "Publicar
  // agora", mas não "Publicações" nem um parágrafo que cita a palavra.
  const porTexto = (sel, textos, incluirDesligado) => {
    const alvos = (Array.isArray(textos) ? textos : [textos]).map(limpar).filter(Boolean);
    if (!alvos.length) return null;
    // primeiro um filtro barato pelo textContent, para não medir estilo de milhares de elementos
    const candidatos = todos(sel || "*").filter((el) => {
      const bruto = limpar([el.textContent, atributo(el, "aria-label"), atributo(el, "title")].join(" "));
      return bruto && alvos.some((a) => bruto.includes(a));
    });
    let melhor = null;
    let melhorNota = -1;
    for (const el of candidatos) {
      if (!visivel(el)) continue;
      if (!incluirDesligado && desligado(el)) continue;
      const t = textoDe(el);
      if (!t) continue;
      let nota = -1;
      for (const alvo of alvos) {
        const sobra = t.length - alvo.length;
        if (t === alvo) nota = Math.max(nota, 100);
        else if (sobra <= 20 && casa(t, alvo)) nota = Math.max(nota, 60 - sobra);
      }
      if (nota < 0) continue;
      if (clicavel(el)) nota += 25;
      if (!el.children || el.children.length === 0) nota += 10;
      if (nota > melhorNota) { melhorNota = nota; melhor = el; }
    }
    return melhor;
  };
  // Alternativas de um passo: [{v: seletor ou texto, t: é texto?, fraco: seletor genérico?, em: onde
  // procurar o texto}]. A primeira que existir na tela vence. Se ela estiver desabilitada, a resposta
  // é esperar, e nunca pular para a próxima (que pode ser um botão qualquer). Seletor genérico só
  // vale quando há um elemento visível só com ele.
  const resolver = (alvos) => {
    for (let i = 0; i < alvos.length; i++) {
      const a = alvos[i];
      let el = null;
      if (a.t) el = porTexto(a.em || "*", a.v, true);
      else {
        const vistos = todos(a.v).filter(visivel);
        if (a.fraco) el = vistos.length === 1 ? vistos[0] : null;
        else el = vistos.find((x) => !desligado(x)) || vistos[0] || null;
      }
      if (el) return { el, i };
    }
    return null;
  };
  const situacao = (alvos) => {
    const r = resolver(alvos);
    return r ? { estado: desligado(r.el) ? "desligado" : "pronto", i: r.i } : { estado: "nada" };
  };
  const clicar = (el) => {
    // Ícones SVG têm nome de acessibilidade, mas não têm HTMLElement.click().
    // Aciona o elemento HTML que os contém, propagando o clique até o controle.
    while (el && typeof el.click !== "function") el = pai(el);
    if (!el) return false;
    el.scrollIntoView({ block: "center" });
    el.click();
    return true;
  };
  const clicarAlt = (alvos) => {
    const r = resolver(alvos);
    if (!r) return { estado: "nada" };
    if (desligado(r.el)) return { estado: "desligado", i: r.i };
    if (!clicar(r.el)) return { estado: "nada" };
    return { estado: "clicado", i: r.i };
  };
  const focoProfundo = () => {
    let a = document.activeElement;
    while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
    return a;
  };
  const dentroDe = (a, el) => { for (let x = a; x; x = pai(x)) { if (x === el) return true; } return false; };
  const focado = (el) => { const a = focoProfundo(); return !!a && a !== document.body && dentroDe(a, el); };
  const EDITAVEL = '[contenteditable="true"], [contenteditable=""], textarea, input:not([type=hidden]):not([type=file])';
  // Foca o campo e só então apaga o que havia nele: sem foco, o texto iria para outro lugar (e o
  // "selecionar tudo" apagaria o campo em que o foco ficou).
  const focarAlt = (alvos) => {
    const r = resolver(alvos);
    if (!r) return { estado: "nada" };
    if (desligado(r.el)) return { estado: "desligado", i: r.i };
    let el = r.el;
    el.scrollIntoView({ block: "center" });
    el.focus();
    if (!focado(el)) {
      const dentro = el.querySelector ? el.querySelector(EDITAVEL) : null;
      if (dentro) { dentro.focus(); if (focado(dentro)) el = dentro; }
    }
    if (!focado(el)) return { estado: "sem_foco", i: r.i };
    if (el.isContentEditable) {
      document.execCommand("selectAll", false, null);
      document.execCommand("delete", false, null);
    } else if (el.select) el.select();
    window.__acCampo = el;
    return { estado: "focado", i: r.i };
  };
  const avisarCampo = () => {
    const el = window.__acCampo;
    if (!el) return null;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return String((el.isContentEditable ? el.innerText : el.value) || "");
  };
  // texto das janelas por cima da página, sem o que está dentro dos campos (que é o que eu escrevi)
  const textoDialogos = () => {
    const partes = [];
    for (const d of todos('[role="dialog"], [role="alertdialog"], dialog[open], [aria-modal="true"]')) {
      if (!visivel(d)) continue;
      let t = d.innerText || "";
      for (const c of d.querySelectorAll(EDITAVEL)) {
        const v = c.isContentEditable ? c.innerText : c.value;
        if (v) t = t.split(v).join(" ");
      }
      partes.push(t);
    }
    return limpar(partes.join(" "));
  };
  return {
    achar, todos, porTexto, visivel, desligado, usavel, textoDe, situacao, clicarAlt, focarAlt, avisarCampo,
    textoDialogos,
    // existe = há um visível (o campo de arquivo conta sempre); presente = há no DOM, mesmo escondido
    existe: (sel) => todos(sel).some(visivel),
    presente: (sel) => todos(sel).length > 0,
    // ligado = achou um visível e habilitado (o botão de publicar só libera quando o envio termina)
    ligado: (sel) => todos(sel).some(usavel),
    primeiroUsavel: (sel) => todos(sel).find(usavel) || null,
    // quadros de outro site (iframe) não entram na busca: os roteiros avisam quando aparecem
    iframes: () => [...document.querySelectorAll("iframe")].map((f) => f.getAttribute("src") || "(sem src)"),
    // O botão "selecionar vídeo" do site manda o navegador abrir a janela do Windows, que eu não
    // consigo fechar e travaria o envio. Aqui o clique no campo de arquivo não abre nada: quem
    // entrega o arquivo é o DevTools (DOM.setFileInputFiles).
    travarArquivo: () => {
      if (window.__acTravado) return true;
      const original = HTMLInputElement.prototype.click;
      HTMLInputElement.prototype.click = function () {
        if (this.type === "file") return undefined;
        return original.apply(this, arguments);
      };
      if (window.showOpenFilePicker) window.showOpenFilePicker = () => Promise.reject(new Error("cancelado"));
      window.__acTravado = true;
      return true;
    },
    // junta o texto do shadow DOM: o innerText nem sempre alcança o que está dentro dele
    texto: () => {
      let t = document.body ? document.body.innerText : "";
      for (const raiz of raizes()) { if (raiz !== document) t += " " + (raiz.textContent || ""); }
      return limpar(t);
    },
    valor: (sel) => { const el = achar(sel); return el ? (el.isContentEditable ? el.innerText : el.value) : null; },
  };
})();
1
"""


def css(seletores) -> list[dict]:
    """Alternativas por seletor CSS, no formato do ajudante (os roteiros embutidos usam assim)."""
    lista = [seletores] if isinstance(seletores, str) else list(seletores)
    return [{"v": str(s), "t": False, "fraco": False} for s in lista]


def descrever_alvos(alvos: list[dict]) -> str:
    partes = []
    for alvo in alvos:
        valor = alvo["v"]
        if alvo.get("t"):
            partes.append(" ou ".join(f'"{v}"' for v in (valor if isinstance(valor, list) else [valor])))
        else:
            partes.append(str(valor))
    return " ou ".join(partes)


def _comeco(texto: str) -> str:
    return "".join(str(texto).split()).lower()[:12]


class Aba:
    """Uma aba do navegador: comandos do CDP e os passos que os roteiros usam."""

    def __init__(self, ws: _Websocket, parar: threading.Event | None = None, ritmo: tuple[float, float] = (0.4, 1.2),
                 prazo: float = 60):
        self._ws = ws
        self._id = 0
        self._eventos: list[dict] = []
        self.parar = parar
        self.ritmo = ritmo
        self.prazo = max(10.0, float(prazo))  # quanto um comando do CDP pode demorar a responder
        self.comando("Page.enable")
        self.comando("Runtime.enable")
        self.comando("DOM.enable")
        try:
            self.comando("Inspector.enable")  # para saber na hora se a aba fechou ou travou
        except ErroNavegador as e:
            if e.conexao:
                raise

    # ---- CDP
    def comando(self, metodo: str, _prazo: float | None = None, **params) -> dict:
        self._id += 1
        meu = self._id
        self._ws.enviar(json.dumps({"id": meu, "method": metodo, "params": params}))
        prazo = float(_prazo or self.prazo)
        limite = time.monotonic() + prazo
        while True:
            if time.monotonic() > limite:
                raise ErroNavegador(f"o navegador não respondeu a {metodo} em {int(prazo)} s", conexao=True)
            bruto = self._ws.receber()
            try:
                msg = json.loads(bruto)
            except ValueError:
                continue
            if msg.get("id") != meu:
                evento = msg.get("method")
                if evento in _EVENTOS_FIM:
                    raise ErroNavegador("a aba do navegador fechou ou travou no meio do envio", conexao=True)
                if evento:
                    self._eventos.append(msg)
                    del self._eventos[:-200]
                continue
            if "error" in msg:
                erro = msg["error"] or {}
                raise ErroNavegador(f"{metodo}: {erro.get('message') or erro}")
            return msg.get("result") or {}

    def avaliar(self, expressao: str, promessa: bool = False, objeto: bool = False):
        r = self.comando("Runtime.evaluate", expression=expressao, returnByValue=not objeto,
                         awaitPromise=promessa, userGesture=True)
        if r.get("exceptionDetails"):
            detalhe = r["exceptionDetails"]
            excecao = detalhe.get("exception") or {}
            raise ErroNavegador(f"erro na página: {excecao.get('description') or detalhe.get('text')}")
        resultado = r.get("result") or {}
        return resultado if objeto else resultado.get("value")

    def _garantir_ajudante(self) -> None:
        """Injeta o ajudante de busca profunda (perdido a cada navegação)."""
        if not self.avaliar("typeof window.__ac !== 'undefined' ? 1 : 0"):
            self.avaliar(AJUDANTE)

    def js(self, corpo: str, promessa: bool = False):
        """Roda JS com o ajudante disponível (use __ac.achar, __ac.porTexto...)."""
        self._garantir_ajudante()
        return self.avaliar(corpo, promessa)

    # ---- ritmo e espera
    def _checar_parar(self) -> None:
        if self.parar is not None and self.parar.is_set():
            raise ErroNavegador("interrompido pelo usuário")

    def pausa(self, fator: float = 1.0) -> None:
        """Espera um tempo aleatório entre os passos (ritmo humano, não é disfarce)."""
        import random

        self.pausa_fixa(random.uniform(*self.ritmo) * fator)

    def pausa_fixa(self, segundos: float) -> None:
        """Espera o tempo pedido, acordando na hora se o motor pedir para parar."""
        if self.parar is not None:
            if self.parar.wait(segundos):
                raise ErroNavegador("interrompido pelo usuário")
        else:
            time.sleep(segundos)

    def esperar(self, expressao: str, segundos: float = 30, o_que: str = ""):
        """Espera a expressão JS virar verdadeira e devolve o valor dela."""
        limite = time.monotonic() + segundos
        ultimo = None
        while time.monotonic() < limite:
            self._checar_parar()
            try:
                ultimo = self.js(expressao)
            except ErroNavegador as e:
                if e.conexao:
                    raise  # a janela caiu: esperar mais não muda nada
                ultimo = None  # a página pode estar trocando
            if ultimo:
                return ultimo
            time.sleep(0.4)
        raise ErroNavegador(f"a página não chegou ao esperado{f': {o_que}' if o_que else ''}")

    def _sondar(self, expressao: str):
        """Uma consulta à página que pode falhar enquanto ela troca (só a conexão caída sobe)."""
        try:
            return self.js(expressao)
        except ErroNavegador as e:
            if e.conexao:
                raise
            return None

    # ---- ações
    def navegar(self, url: str, segundos: float = 45) -> None:
        self.comando("Page.navigate", url=url)
        self.esperar("document.readyState === 'complete' || document.readyState === 'interactive'",
                     segundos, "a página não abriu")
        self._garantir_ajudante()
        self.pausa()

    def url(self) -> str:
        return str(self.avaliar("location.href") or "")

    def texto(self) -> str:
        return str(self.js("__ac.texto()") or "")

    def texto_dialogos(self) -> str:
        """Texto das janelas por cima da página (avisos e bloqueios), sem o que eu escrevi nos campos."""
        return str(self.js("__ac.textoDialogos()") or "")

    def iframes(self) -> list[str]:
        """Quadros de outro endereço na página: a busca não entra neles, então os roteiros avisam."""
        return list(self.js("__ac.iframes()") or [])

    def existe(self, seletor: str) -> bool:
        """Há um elemento visível com este seletor (o campo de arquivo conta mesmo escondido)."""
        return bool(self.js(f"__ac.existe({json.dumps(seletor)})"))

    def presente(self, seletor: str) -> bool:
        """Há um elemento com este seletor, mesmo escondido."""
        return bool(self.js(f"__ac.presente({json.dumps(seletor)})"))

    def primeiro(self, seletores, segundos: float = 30, o_que: str = "") -> str:
        """O primeiro seletor da lista que aparecer na página (os sites mudam de layout)."""
        lista = [seletores] if isinstance(seletores, str) else list(seletores)
        achado = self.esperar(
            "(() => { for (const s of " + json.dumps(lista) + ") { if (__ac.existe(s)) return s; } return 0; })()",
            segundos, o_que or " ou ".join(lista),
        )
        return str(achado)

    def esperar_seletor(self, seletor, segundos: float = 30, o_que: str = "") -> str:
        return self.primeiro(seletor, segundos, o_que)

    def _esperar_alvos(self, funcao: str, alvos: list[dict], segundos: float, o_que: str,
                       antes=None) -> dict:
        """Repete __ac.<funcao>(alvos) até dar certo, esperando o alvo aparecer e ficar habilitado."""
        descricao = o_que or descrever_alvos(alvos)
        lista = json.dumps(alvos)
        limite = time.monotonic() + segundos
        visto: str | None = None
        while True:
            self._checar_parar()
            estado = (self._sondar(f"__ac.situacao({lista})") or {}).get("estado")
            if estado == "pronto":
                if antes is not None:
                    antes()  # o que precisa acontecer logo antes da ação (anotar que o post vai sair)
                r = self._sondar(f"__ac.{funcao}({lista})") or {}
                if r.get("estado") in ("clicado", "focado"):
                    return r
                estado = r.get("estado") or estado
            if estado in ("desligado", "sem_foco"):
                visto = estado
            if time.monotonic() >= limite:
                break
            time.sleep(0.4)
        if visto == "desligado":
            raise ErroNavegador(f"{descricao} continuou desabilitado por {int(segundos)} s")
        if visto == "sem_foco":
            raise ErroNavegador(f"achei {descricao}, mas o campo não recebeu o foco (o texto iria para outro lugar)")
        raise ErroNavegador(f"não achei {descricao} na tela")

    def clicar_alvos(self, alvos: list[dict], segundos: float = 30, antes=None, o_que: str = "") -> int:
        """Clica no primeiro alvo que existir, esperando ele habilitar. Devolve qual alternativa foi."""
        r = self._esperar_alvos("clicarAlt", alvos, segundos, o_que, antes)
        self.pausa()
        return int(r.get("i") or 0)

    def clicar(self, seletor, segundos: float = 30) -> None:
        self.clicar_alvos(css(seletor), segundos)

    def clicar_texto(self, seletor: str, textos, segundos: float = 30) -> None:
        """Clica pelo texto do botão (mais estável que a classe, que muda a cada versão do site).

        Fica com o elemento que melhor casa com o texto, habilitado ou não: se ele estiver
        desabilitado, espera liberar em vez de clicar em outro com o mesmo texto.
        """
        lista = [textos] if isinstance(textos, str) else list(textos)
        self.clicar_alvos([{"v": lista, "t": True, "em": seletor}], segundos, o_que=f"botão {' ou '.join(lista)}")

    def valor(self, seletor: str):
        return self.js(f"__ac.valor({json.dumps(seletor)})")

    def tecla(self, nome: str = "Enter") -> None:
        """Tecla de verdade no campo com foco (campos de tag confirmam com Enter)."""
        teclas = {"Enter": (13, "Enter", "\r"), "Tab": (9, "Tab", None), "Escape": (27, "Escape", None)}
        codigo, chave, texto = teclas.get(nome, (13, "Enter", "\r"))
        comum = {"key": chave, "code": chave, "windowsVirtualKeyCode": codigo, "nativeVirtualKeyCode": codigo}
        self.comando("Input.dispatchKeyEvent", type="rawKeyDown", **comum)
        if texto:
            self.comando("Input.dispatchKeyEvent", type="char", text=texto, **comum)
        self.comando("Input.dispatchKeyEvent", type="keyUp", **comum)
        self.pausa(0.5)

    def digitar_alvos(self, alvos: list[dict], texto: str, segundos: float = 30, o_que: str = "") -> None:
        """Escreve num campo ou num div editável, disparando os eventos que o site espera.

        Só escreve depois de confirmar que o foco está no campo: o Input.insertText escreve onde
        o foco estiver, e sem essa conferência o texto iria parar em outro campo.
        """
        self._esperar_alvos("focarAlt", alvos, segundos, o_que)
        if texto:
            self.comando("Input.insertText", text=texto)
        escrito = self._sondar("__ac.avisarCampo()")
        if texto and isinstance(escrito, str) and _comeco(texto) not in "".join(escrito.split()).lower():
            # o site pode transformar o texto (hashtag vira etiqueta): só aviso, não paro o envio
            log.warning("Navegador: o campo %s não mostra o começo do que escrevi", o_que or descrever_alvos(alvos))
        self.pausa()

    def digitar(self, seletor, texto: str, segundos: float = 30) -> None:
        self.digitar_alvos(css(seletor), texto, segundos)

    def enviar_arquivo(self, seletor, arquivo: Path, segundos: float = 30) -> None:
        """Entrega o vídeo ao <input type=file> da página (mesmo escondido ou no shadow DOM)."""
        self.enviar_arquivos(seletor, [arquivo], segundos)

    def _soltar(self, id_objeto: str) -> None:
        try:
            self.comando("Runtime.releaseObject", objectId=id_objeto)
        except ErroNavegador as e:
            if e.conexao:
                raise

    def _campo_de_imagem(self, seletores: list[str], alvo: str, midia: str, quantos: int) -> str:
        """O objeto do campo que recebe estes arquivos, conferindo `multiple` e `accept`.

        Com imagem, fica com o primeiro campo (entre os que casam com o seletor) que aceita imagem:
        a página pode ter um campo de vídeo antes do de foto.
        """
        lista = [alvo] + [s for s in seletores if s != alvo]
        objeto = self.avaliar(
            "(() => { const v = []; for (const s of " + json.dumps(lista) + ") { for (const el of __ac.todos(s)) "
            "{ if (!v.includes(el)) v.push(el); } } return v; })()", objeto=True)
        id_lista = objeto.get("objectId") if isinstance(objeto, dict) else None
        if not id_lista:
            raise ErroNavegador(f"o campo de arquivo não virou objeto ({alvo}): {objeto}")
        try:
            r = self.comando(
                "Runtime.callFunctionOn", objectId=id_lista, returnByValue=True,
                functionDeclaration="function () { return this.map((el) => ({ multiplo: !!el.multiple, "
                                    f"aceita: !el.accept || {_ACEITA_IMAGEM}.test(el.accept) }})); }}")
            campos = (r.get("result") or {}).get("value") or []
            if not campos:
                raise ErroNavegador(f"o campo de arquivo sumiu da página ({alvo})")
            indice = 0
            if midia == "imagem":
                indice = next((i for i, c in enumerate(campos) if c.get("aceita")), -1)
                if indice < 0:
                    raise ErroNavegador("o campo de arquivo desta página não aceita imagem", "formato", parado=True)
            if quantos > 1 and not campos[indice].get("multiplo"):
                raise ErroNavegador("o campo de arquivo aceita uma imagem por vez: o carrossel não sai por este "
                                    "roteiro", "formato")
            r = self.comando("Runtime.callFunctionOn", objectId=id_lista,
                             functionDeclaration="function (i) { return this[i]; }", arguments=[{"value": indice}])
            id_objeto = (r.get("result") or {}).get("objectId")
            if not id_objeto:
                raise ErroNavegador(f"o campo de arquivo não virou objeto ({alvo})")
            return id_objeto
        finally:
            self._soltar(id_lista)

    def enviar_arquivos(self, seletor, arquivos, segundos: float = 30, midia: str = "video") -> None:
        """Entrega os arquivos, na ordem, ao <input type=file> da página (mesmo escondido ou no shadow DOM).

        O vídeo (um arquivo) segue o caminho de sempre, sem conferir o campo. Com imagem ou com mais
        de um arquivo, confere antes se o campo aceita imagem e mais de um arquivo (tipo "formato").
        """
        arquivos = [Path(a) for a in arquivos]
        for arquivo in arquivos:
            if not arquivo.is_file():
                # o Chrome aceita caminho inexistente sem reclamar, e o envio falharia lá na frente
                o_que = "a imagem" if midia == "imagem" else "o vídeo"
                raise ErroNavegador(f"{o_que} não está mais em {arquivo}", "corte")
        seletores = [seletor] if isinstance(seletor, str) else list(seletor)
        alvo = self.primeiro(seletores, segundos, "campo de arquivo")
        # pelo objeto, e não por DOM.querySelector: este acha também o que está dentro do shadow DOM
        self._garantir_ajudante()
        if midia == "imagem" or len(arquivos) > 1:
            id_objeto = self._campo_de_imagem(seletores, alvo, midia, len(arquivos))
        else:
            objeto = self.avaliar(f"__ac.achar({json.dumps(alvo)})", objeto=True)
            id_objeto = objeto.get("objectId") if isinstance(objeto, dict) else None
            if not id_objeto:
                raise ErroNavegador(f"o campo de arquivo não virou objeto ({alvo}): {objeto}")
        try:
            self.comando("DOM.getDocument", depth=0)  # sem isto o DOM não sabe mapear o objeto
            no = self.comando("DOM.requestNode", objectId=id_objeto).get("nodeId")
            if not no:
                raise ErroNavegador(f"não consegui apontar o campo de arquivo ({alvo})")
            self.comando("DOM.setFileInputFiles", nodeId=no, files=[str(a.resolve()) for a in arquivos])
        finally:
            self._soltar(id_objeto)
        self.pausa(2.0)

    def captura(self, destino: Path) -> Path | None:
        """Imagem da aba, para você ver onde o envio parou."""
        try:
            dados = (self.comando("Page.captureScreenshot", format="png") or {}).get("data")
            if not dados:
                return None
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(base64.b64decode(dados))
            return destino
        except (ErroNavegador, ValueError, OSError):
            return None

    def fechar(self) -> None:
        self._ws.fechar()


# ---------------------------------------------------------------- navegador

def caminho_programa(cfg: Config) -> Path:
    escolhido = str(cfg["navegador"]["programa"]).strip()
    if escolhido:
        caminho = Path(escolhido)
        if not caminho.exists():
            raise ErroNavegador(f"não encontrei o navegador em {caminho}", "bloqueio")
        return caminho
    for caminho in PROGRAMAS:
        if str(caminho) and caminho.exists():
            return caminho
    achado = shutil.which("chrome") or shutil.which("msedge")
    if achado:
        return Path(achado)
    raise ErroNavegador(
        "não encontrei o Chrome nem o Edge. Informe o caminho em Redes sociais > Postagem pelo navegador",
        "bloqueio",
    )


class Navegador:
    """Abre (ou reaproveita) a janela do Chrome com o perfil do AutoCortes."""

    def __init__(self, cfg: Config, parar: threading.Event | None = None):
        self.cfg = cfg
        self.parar = parar
        self.conf = cfg["navegador"]
        self.porta = int(self.conf["porta"])
        self.perfil = cfg.pasta_dados / "chrome"
        self.base = f"http://127.0.0.1:{self.porta}"

    # ---- processo
    def responde(self) -> bool:
        try:
            r = requests.get(f"{self.base}/json/version", timeout=3)
            return r.status_code == 200 and "Browser" in r.json()
        except (requests.RequestException, ValueError):
            return False

    def _alvo_atual(self) -> str:
        """Identificador da janela que está respondendo nesta porta (único por processo do Chrome)."""
        try:
            url = str(requests.get(f"{self.base}/json/version", timeout=3).json().get("webSocketDebuggerUrl") or "")
        except (requests.RequestException, ValueError, AttributeError):
            return ""
        return urlparse(url).path if url else ""

    def _anotar_janela(self) -> None:
        alvo = self._alvo_atual()
        if alvo:
            salvar_json(self.perfil / ARQUIVO_JANELA, {"porta": self.porta, "alvo": alvo, "quando": time.time()})

    def _porta_so_nossa(self) -> bool:
        """Nenhum outro perfil desta instalação usa esta porta do navegador."""
        try:
            from . import perfis  # aqui dentro para não amarrar os módulos na importação

            nossa = self.cfg.pasta_dados.resolve()
            return not any(
                p.porta_navegador == self.porta and p.pasta_dados.resolve() != nossa for p in perfis.listar()
            )
        except Exception:  # noqa: BLE001 (sem saber quem mais usa a porta, não adoto a janela)
            log.warning("Navegador: não consegui conferir os outros perfis; não uso a janela da porta %s", self.porta)
            return False

    def _perfil_em_uso(self) -> bool:
        """O Chrome segura o arquivo lockfile da pasta do perfil enquanto está aberto com ela."""
        trava = self.perfil / "lockfile"
        if not trava.is_file():
            return False
        try:
            with open(trava, "ab"):
                return False
        except PermissionError:
            return True
        except OSError:
            return False

    def nosso(self) -> bool:
        """Quem responde nesta porta é a janela deste perfil?

        Com vários perfis, dois configs com a mesma porta fariam o segundo postar com a conta do
        primeiro, sem erro nenhum. O Chrome de hoje não grava mais o DevToolsActivePort, e a linha
        de comando pelo CDP só vem com --enable-automation (que entrega a automação para as redes).
        Então eu anoto, na pasta do perfil, qual janela eu abri: o alvo é único por processo.

        Sem a anotação (ou com uma antiga), só adoto a janela se o Chrome estiver mesmo aberto com
        o nosso perfil (o lockfile dele está preso) e nenhum outro perfil usar esta porta. Um Chrome
        qualquer com depuração na 9222 não passa.
        """
        alvo = self._alvo_atual()
        if not alvo:
            return False
        marca = ler_json(self.perfil / ARQUIVO_JANELA, {}) or {}
        if marca.get("porta") == self.porta and marca.get("alvo") == alvo:
            return True
        if self._perfil_em_uso() and self._porta_so_nossa():
            self._anotar_janela()
            return True
        return False

    def abrir(self, esperar_seg: float = 40) -> None:
        """Sobe a janela se ela não estiver aberta (a sessão fica no perfil, em dados/chrome)."""
        if self.responde():
            if self.nosso():
                return
            raise ErroNavegador(
                f"a porta {self.porta} já está ocupada por outro navegador (outro perfil do AutoCortes, ou o "
                "Chrome aberto com depuração). Não uso essa janela para não postar na conta errada: troque a "
                "porta em Redes sociais > Postagem pelo navegador.",
                "bloqueio",
            )
        programa = caminho_programa(self.cfg)
        self.perfil.mkdir(parents=True, exist_ok=True)
        argumentos = [
            str(programa),
            f"--remote-debugging-port={self.porta}",
            "--remote-debugging-address=127.0.0.1",  # nunca 0.0.0.0
            f"--user-data-dir={self.perfil}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--restore-last-session=false",
            "about:blank",
        ]
        if not self.conf["visivel"]:
            argumentos.insert(1, "--headless=new")
        log.info("Abrindo o navegador do AutoCortes (%s) com o perfil %s", programa.name, self.perfil)
        try:
            subprocess.Popen(
                argumentos, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except OSError as e:
            raise ErroNavegador(f"não consegui abrir o navegador ({e})", "bloqueio") from e
        limite = time.monotonic() + esperar_seg
        while time.monotonic() < limite:
            if self.parar is not None and self.parar.is_set():
                raise ErroNavegador("interrompido pelo usuário")
            if self.responde():
                self._anotar_janela()  # esta janela é deste perfil, para não confundir com a de outro
                return
            time.sleep(0.5)
        raise ErroNavegador(
            f"o navegador não respondeu na porta {self.porta}. Se você já usa essa porta, troque em "
            "Redes sociais > Postagem pelo navegador", "bloqueio",
        )

    def _ws_navegador(self) -> str:
        try:
            return str(requests.get(f"{self.base}/json/version", timeout=5).json()["webSocketDebuggerUrl"])
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            raise ErroNavegador(f"não consegui falar com o navegador ({e})", conexao=True) from e

    def nova_aba(self, url: str = "about:blank") -> Aba:
        """Abre uma aba e devolve o controle dela."""
        self.abrir()
        principal = _Websocket(self._ws_navegador(), timeout=30)
        try:
            principal.enviar(json.dumps({"id": 1, "method": "Target.createTarget", "params": {"url": url}}))
            alvo = None
            for _ in range(20):
                try:
                    msg = json.loads(principal.receber())
                except ValueError:
                    continue
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise ErroNavegador(f"não consegui abrir a aba ({(msg['error'] or {}).get('message')})")
                    alvo = (msg.get("result") or {}).get("targetId")
                    break
            if not alvo:
                raise ErroNavegador("o navegador não devolveu a aba")
        finally:
            principal.fechar()
        ritmo = (float(self.conf["pausa_min_seg"]), max(float(self.conf["pausa_min_seg"]),
                                                        float(self.conf["pausa_max_seg"])))
        limite = float(self.conf["tempo_limite_seg"])
        ws = _Websocket(f"ws://127.0.0.1:{self.porta}/devtools/page/{alvo}", timeout=limite)
        try:
            aba = Aba(ws, self.parar, ritmo, prazo=limite)
        except ErroNavegador:
            ws.fechar()
            raise
        aba._alvo = alvo
        return aba

    def fechar_aba(self, aba: Aba) -> None:
        alvo = getattr(aba, "_alvo", None)
        aba.fechar()
        if alvo:
            try:
                requests.get(f"{self.base}/json/close/{alvo}", timeout=5)
            except requests.RequestException:
                pass

    def encerrar(self) -> None:
        """Fecha a janela do navegador (a sessão continua salva no perfil)."""
        try:
            ws = _Websocket(self._ws_navegador(), timeout=5)
        except ErroNavegador:
            return
        try:
            ws.enviar(json.dumps({"id": 1, "method": "Browser.close", "params": {}}))
            ws.receber()
        except ErroNavegador:
            pass
        finally:
            ws.fechar()


# ---------------------------------------------------------------- janela à parte

def porta_livre(evitar=()) -> int:
    """Uma porta livre em 127.0.0.1, fora das de `evitar` (as do painel e do navegador dos perfis)."""
    proibidas = {int(p) for p in evitar if p}
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            porta = int(s.getsockname()[1])
        if porta not in proibidas:
            return porta
    raise ErroNavegador("não achei uma porta livre", "bloqueio")


class NavegadorTemporario(Navegador):
    """Um Chrome à parte, com perfil e porta só dele: nunca a janela nem o perfil das postagens
    (dados/chrome). A porta é escolhida na hora e o perfil é novo, então não há a conferência de
    `nosso()`: quem responde na porta é o processo que eu abri."""

    def __init__(self, cfg: Config, perfil: Path, parar: threading.Event | None = None, extras=(), evitar=()):
        super().__init__(cfg, parar)
        self.perfil = Path(perfil)
        if self.perfil.resolve() == (cfg.pasta_dados / "chrome").resolve():
            raise ErroNavegador("a janela à parte nunca usa o perfil das postagens", "bloqueio")
        self.conf = dict(cfg["navegador"], visivel=True)  # você precisa ver a janela
        self.extras = tuple(extras)  # os testes põem --headless=new aqui
        self.porta = porta_livre({int(cfg["navegador"]["porta"]), int(cfg["painel"]["porta"]), *evitar})
        self.base = f"http://127.0.0.1:{self.porta}"
        self.processo: subprocess.Popen | None = None

    @property
    def vivo(self) -> bool:
        return self.processo is not None and self.processo.poll() is None

    def abrir(self, url: str = "about:blank", esperar_seg: float = 40) -> None:
        """Abre a janela no endereço pedido (não faz nada se ela já estiver aberta e respondendo)."""
        if self.vivo and self.responde():
            return
        programa = caminho_programa(self.cfg)
        self.perfil.mkdir(parents=True, exist_ok=True)
        argumentos = [
            str(programa),
            f"--remote-debugging-port={self.porta}",
            "--remote-debugging-address=127.0.0.1",  # nunca 0.0.0.0
            f"--user-data-dir={self.perfil}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--restore-last-session=false",
            *self.extras,
            url,
        ]
        log.info("Abrindo uma janela à parte do navegador (%s), com o perfil temporário %s", programa.name,
                 self.perfil)
        try:
            self.processo = subprocess.Popen(
                argumentos, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except OSError as e:
            raise ErroNavegador(f"não consegui abrir o navegador ({e})", "bloqueio") from e
        limite = time.monotonic() + esperar_seg
        while time.monotonic() < limite:
            if self.parar is not None and self.parar.is_set():
                raise ErroNavegador("interrompido pelo usuário")
            if self.processo.poll() is not None:
                raise ErroNavegador("o navegador fechou logo depois de abrir", "bloqueio")
            if self.responde():
                return
            time.sleep(0.5)
        raise ErroNavegador(f"o navegador não respondeu na porta {self.porta}", "bloqueio")

    def comando(self, metodo: str, _prazo: float = 15, **params) -> dict:
        """Um comando do CDP no alvo do navegador (não de uma aba), como o Storage.getCookies."""
        ws = _Websocket(self._ws_navegador(), timeout=_prazo)
        try:
            ws.enviar(json.dumps({"id": 1, "method": metodo, "params": params}))
            limite = time.monotonic() + _prazo
            while time.monotonic() < limite:
                try:
                    msg = json.loads(ws.receber())
                except ValueError:
                    continue
                if msg.get("id") != 1:
                    continue  # evento
                if "error" in msg:
                    erro = msg["error"] or {}
                    raise ErroNavegador(f"{metodo}: {erro.get('message') or erro}")
                return msg.get("result") or {}
            raise ErroNavegador(f"o navegador não respondeu a {metodo} em {int(_prazo)} s", conexao=True)
        finally:
            ws.fechar()

    def cookies(self) -> list[dict]:
        """Todos os cookies do perfil desta janela."""
        return self.comando("Storage.getCookies").get("cookies") or []

    def paginas(self) -> list[dict]:
        try:
            lista = requests.get(f"{self.base}/json/list", timeout=5).json()
        except (requests.RequestException, ValueError) as e:
            raise ErroNavegador(f"não consegui falar com o navegador ({e})", conexao=True) from e
        return [p for p in lista if isinstance(p, dict) and p.get("type") == "page" and p.get("id")]

    def so_uma_aba(self, url: str, segundos: float = 30) -> None:
        """Deixa uma aba só, no endereço pedido (fecha as outras)."""
        paginas = self.paginas()
        if not paginas:
            aba = self.nova_aba()
            try:
                aba.navegar(url, segundos)
            finally:
                aba.fechar()
            return
        for outra in paginas[1:]:
            try:
                requests.get(f"{self.base}/json/close/{outra['id']}", timeout=5)
            except requests.RequestException:
                pass
        ws = _Websocket(f"ws://127.0.0.1:{self.porta}/devtools/page/{paginas[0]['id']}", timeout=segundos)
        try:
            Aba(ws, self.parar, prazo=segundos).navegar(url, segundos)
        finally:
            ws.fechar()

    def encerrar(self) -> None:
        """Fecha a janela (Browser.close) e espera o processo acabar; se ele não acabar, mata a árvore."""
        if self.processo is None:
            return
        super().encerrar()
        try:
            self.processo.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.processo.pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            self.processo.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("Navegador: a janela à parte (porta %s) não fechou", self.porta)
