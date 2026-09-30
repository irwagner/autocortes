"""Aprender a postar: grava você publicando um vídeo à mão e guarda o roteiro para repetir depois.

O AutoCortes abre a página de envio da rede na janela dele (já logada), escuta os seus cliques e
o que você digita, e monta um roteiro com várias formas de achar cada elemento. Depois, na hora
de postar sozinho, ele repete esses passos com o texto do corte.

Para saber qual campo é qual, o painel te dá textos-marca (@@TITULO@@, @@DESCRICAO@@...): quando
você cola um deles num campo, o gravador anota o papel daquele campo. Nada de adivinhação.

Nunca grava campo de senha nem nada digitado em página de login.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import Config
from .util import log, salvar_json

VERSAO = 1
# textos-marca que você cola nos campos para o gravador saber o papel de cada um
MARCAS = {
    "titulo": "@@TITULO@@",
    "descricao": "@@DESCRICAO@@",
    "legenda": "@@LEGENDA@@",
    "tags": "@@TAGS@@",
    "fonte": "@@FONTE@@",
}
# o que cada rede precisa que você marque (o resto do que você digitar é repetido igual)
PAPEIS_DA_REDE = {
    "youtube": ("titulo", "descricao", "tags"),
    "tiktok": ("legenda",),
    "instagram": ("legenda",),
    "bilibili": ("titulo", "descricao", "tags", "fonte"),
}

# Gravador injetado na página. Escuta na fase de captura e usa composedPath, então enxerga
# também o que está dentro do shadow DOM (YouTube Studio).
SCRIPT = r"""
window.__acGrav = (() => {
  const passos = [];
  const limpar = (t) => String(t || "").replace(/\s+/g, " ").trim();
  const ehLogin = () => /\/login|accounts\/login|accounts\.google\.com|passport\.bilibili|\/signin/.test(location.href);
  // id ou atributo com cara de gerado (react-aria-1234, :r3:, ember42) não serve de âncora
  const estavel = (v) => {
    const s = limpar(v);
    if (!s || s.length > 60) return false;
    if (/^[:_]/.test(s)) return false;
    if (/\d{4,}/.test(s)) return false;
    if (/^(react|ember|radix|mui|headlessui|aria)[-_:]/i.test(s)) return false;
    return true;
  };
  const csse = (v) => (window.CSS && CSS.escape ? CSS.escape(v) : String(v).replace(/["\\]/g, "\\$&"));
  const seletores = (el) => {
    const saida = [];
    const tag = (el.tagName || "").toLowerCase();
    if (el.id && estavel(el.id)) saida.push("#" + csse(el.id));
    for (const attr of ["data-e2e", "data-testid", "data-test-id", "data-tt", "name", "aria-label", "placeholder"]) {
      const v = el.getAttribute && el.getAttribute(attr);
      if (v && estavel(v)) saida.push(`${tag}[${attr}="${csse(v)}"]`);
    }
    if (tag === "input" && el.type) saida.push(`input[type="${csse(el.type)}"]`);
    if (el.getAttribute && el.getAttribute("role")) saida.push(`${tag}[role="${csse(el.getAttribute("role"))}"]`);
    if (el.isContentEditable) saida.push(`${tag}[contenteditable="true"]`);
    // caminho curto até um ancestral com âncora (último recurso)
    let atual = el, caminho = [], niveis = 0;
    while (atual && niveis < 4) {
      const t = (atual.tagName || "").toLowerCase();
      if (!t) break;
      if (atual.id && estavel(atual.id)) { caminho.unshift("#" + csse(atual.id)); break; }
      const pai = atual.parentElement;
      if (!pai) { caminho.unshift(t); break; }
      const iguais = [...pai.children].filter((x) => x.tagName === atual.tagName);
      caminho.unshift(iguais.length > 1 ? `${t}:nth-of-type(${iguais.indexOf(atual) + 1})` : t);
      atual = pai;
      niveis++;
    }
    if (caminho.length) saida.push(caminho.join(" > "));
    return [...new Set(saida)];
  };
  const anotar = (passo) => {
    if (ehLogin()) return;  // nada do que você faz numa tela de login é gravado
    passo.em = Date.now();
    passo.url = location.href;
    passos.push(passo);
    if (passos.length > 400) passos.shift();
  };
  const alvo = (ev) => (ev.composedPath && ev.composedPath()[0]) || ev.target;
  document.addEventListener("click", (ev) => {
    const el = alvo(ev);
    if (!el || !el.tagName) return;
    if (el.tagName === "INPUT" && (el.type === "file" || el.type === "password")) return;
    anotar({ tipo: "clicar", seletores: seletores(el), texto: limpar(el.innerText || el.textContent).slice(0, 60),
             rotulo: limpar(el.getAttribute && el.getAttribute("aria-label")).slice(0, 60) });
  }, true);
  document.addEventListener("change", (ev) => {
    const el = alvo(ev);
    if (!el || el.tagName !== "INPUT") return;
    if (el.type === "file") anotar({ tipo: "arquivo", seletores: seletores(el) });
  }, true);
  const digitou = (ev) => {
    const el = alvo(ev);
    if (!el || !el.tagName) return;
    // senha nunca; arquivo tem passo próprio (o value dele é "C:\fakepath\...", que não se digita)
    if (el.tagName === "INPUT" && (el.type === "password" || el.type === "file")) return;
    const valor = el.isContentEditable ? limpar(el.innerText) : (el.value !== undefined ? limpar(el.value) : "");
    if (!valor) return;
    const anterior = passos[passos.length - 1];
    if (anterior && anterior.tipo === "digitar" && anterior.chave === (el.id || "") + seletores(el)[0]) {
      anterior.valor = valor;  // junta o que você digitou aos poucos num só passo
      return;
    }
    anotar({ tipo: "digitar", seletores: seletores(el), valor, chave: (el.id || "") + seletores(el)[0] });
  };
  document.addEventListener("input", digitou, true);
  document.addEventListener("change", digitou, true);
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" && ev.key !== "Tab") return;
    const el = alvo(ev);
    if (el && el.tagName === "INPUT" && el.type === "password") return;
    anotar({ tipo: "tecla", tecla: ev.key });
  }, true);
  return {
    passos,
    tirar: () => passos.splice(0, passos.length),
    texto: () => limpar(document.body ? document.body.innerText : "").slice(0, 3000),
  };
})();
1
"""


def arquivo_roteiro(cfg: Config, rede: str) -> Path:
    return cfg.pasta_dados / "roteiros" / f"{rede}.json"


def papeis(rede: str) -> dict:
    """Os textos-marca que você precisa colar nesta rede."""
    return {papel: MARCAS[papel] for papel in PAPEIS_DA_REDE.get(rede, ())}


def _papel_do_valor(valor: str) -> str | None:
    for papel, marca in MARCAS.items():
        if marca in valor:
            return papel
    return None


def limpar_passos(rede: str, passos: list[dict]) -> list[dict]:
    """Tira o que não serve e marca o papel dos campos pelos textos-marca."""
    saida: list[dict] = []
    for passo in passos:
        tipo = passo.get("tipo")
        seletores = [s for s in (passo.get("seletores") or []) if s]
        if tipo in ("clicar", "arquivo", "digitar") and not seletores:
            continue
        limpo: dict = {"tipo": tipo, "seletores": seletores}
        if tipo == "clicar":
            texto = str(passo.get("texto") or "")
            rotulo = str(passo.get("rotulo") or "")
            # clique com texto curto vira também busca por texto, que aguenta troca de classe
            if 0 < len(texto) <= 40:
                limpo["texto"] = texto
            if rotulo:
                limpo["rotulo"] = rotulo
        elif tipo == "digitar":
            valor = str(passo.get("valor") or "")
            if "fakepath" in valor or any("type=\"file\"" in s for s in seletores):
                continue  # resquício do campo de arquivo, não é texto que você digitou
            papel = _papel_do_valor(valor)
            if papel:
                limpo["papel"] = papel
            else:
                limpo["valor"] = valor  # texto seu, repetido igual
        elif tipo == "tecla":
            limpo["tecla"] = passo.get("tecla") or "Enter"
        elif tipo != "arquivo":
            continue
        if passo.get("url"):
            limpo["url"] = passo["url"]
        saida.append(limpo)
    return _juntar(saida)


def _mesmo_campo(a: dict, b: dict) -> bool:
    return (a["seletores"] == b["seletores"] and a.get("papel") == b.get("papel")
            and a.get("valor") == b.get("valor"))


def _juntar(passos: list[dict]) -> list[dict]:
    """Junta digitações repetidas no mesmo campo e tira cliques duplicados seguidos."""
    saida: list[dict] = []
    for passo in passos:
        anterior = saida[-1] if saida else None
        if anterior and passo["tipo"] == "digitar" and anterior["tipo"] == "digitar" \
                and anterior["seletores"] == passo["seletores"]:
            saida[-1] = passo
            continue
        if passo["tipo"] == "digitar":
            # o Enter num campo de tag dispara outro evento no mesmo campo: não repetir
            anteriores = [p for p in saida if p["tipo"] != "tecla"]
            if anteriores and anteriores[-1]["tipo"] == "digitar" and _mesmo_campo(anteriores[-1], passo):
                continue
        if anterior and passo["tipo"] == "clicar" and anterior["tipo"] == "clicar" \
                and anterior["seletores"] == passo["seletores"] and anterior.get("texto") == passo.get("texto"):
            continue
        saida.append(passo)
    return saida


def salvar(cfg: Config, rede: str, passos: list[dict], url_inicial: str, confirmacao: str = "") -> dict:
    """Grava o roteiro aprendido e devolve o resumo dele."""
    limpos = limpar_passos(rede, passos)
    faltando = [p for p in PAPEIS_DA_REDE.get(rede, ()) if not any(x.get("papel") == p for x in limpos)]
    roteiro = {
        "versao": VERSAO,
        "rede": rede,
        "gravado_em": time.time(),
        "url_inicial": url_inicial,
        "passos": limpos,
        "confirmacao": confirmacao[:300],
    }
    destino = arquivo_roteiro(cfg, rede)
    salvar_json(destino, roteiro)
    log.info("%s: roteiro aprendido com %d passos salvo em %s", rede, len(limpos), destino)
    return {**resumo(roteiro), "faltando": faltando, "arquivo": str(destino)}


def carregar(cfg: Config, rede: str) -> dict | None:
    arquivo = arquivo_roteiro(cfg, rede)
    if not arquivo.is_file():
        return None
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        log.warning("%s: roteiro aprendido ilegível (%s)", rede, e)
        return None
    if not isinstance(dados, dict) or dados.get("versao") != VERSAO or not dados.get("passos"):
        return None
    return dados


def resumo(roteiro: dict | None) -> dict:
    """Resumo para o painel: quantos passos, quais papéis e quando foi gravado."""
    if not roteiro:
        return {"tem": False}
    passos = roteiro.get("passos") or []
    contagem: dict[str, int] = {}
    for passo in passos:
        contagem[passo["tipo"]] = contagem.get(passo["tipo"], 0) + 1
    return {
        "tem": True,
        "gravado_em": roteiro.get("gravado_em"),
        "passos": len(passos),
        "contagem": contagem,
        "papeis": sorted({p["papel"] for p in passos if p.get("papel")}),
        "tem_arquivo": any(p["tipo"] == "arquivo" for p in passos),
        "confirmacao": roteiro.get("confirmacao") or "",
    }
