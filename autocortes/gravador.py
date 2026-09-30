"""Aprender a postar: grava você publicando um vídeo à mão e escreve o roteiro em texto.

O AutoCortes abre a página de envio da rede na janela dele (já logada), escuta os seus cliques e
o que você digita, e escreve um roteiro na linguagem de `roteiro.py`, que você pode ler e editar
no painel.

Para saber qual campo é qual, o painel te dá textos-marca (@@TITULO@@, @@DESCRICAO@@...): quando
você cola um deles num campo, o gravador anota o papel daquele campo. Numa rede com um campo de
texto só, nem precisa: ele descobre por eliminação.

Nunca grava campo de senha nem nada digitado em página de login.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import Config
from .roteiro import MARCAS, NOMES, PAPEIS_DA_REDE, gerar
from .util import log, sem_acentos

__all__ = ["SCRIPT", "analisar", "arquivo_roteiro", "carregar", "lista_passos", "papeis", "salvar"]

_MARCA = re.compile(r"@@\s*([^@\s]{2,20})\s*@@")

# Gravador injetado na página. Escuta na fase de captura e usa composedPath, então enxerga
# também o que está dentro do shadow DOM (YouTube Studio). Além dos eventos, varre os campos de
# texto de tempo em tempo: o editor de legenda do TikTok não dispara evento nenhum que dê para
# escutar de fora, e a varredura é o que garante que o texto seja anotado.
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
  // composedPath entra no shadow DOM; se cair num nó de texto, sobe até o elemento
  const alvo = (ev) => {
    let el = (ev.composedPath && ev.composedPath()[0]) || ev.target;
    while (el && !el.tagName && el.parentElement) el = el.parentElement;
    return el;
  };
  document.addEventListener("click", (ev) => {
    const el = alvo(ev);
    if (!el || !el.tagName) return;
    if (el.tagName === "INPUT" && (el.type === "file" || el.type === "password")) return;
    const attr = (n) => limpar(el.getAttribute && el.getAttribute(n)).slice(0, 60);
    anotar({ tipo: "clicar", seletores: seletores(el), texto: limpar(el.innerText || el.textContent).slice(0, 60),
             rotulo: attr("aria-label"), dica: attr("title"), tag: (el.tagName || "").toLowerCase() });
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

  // --- varredura dos campos de texto (não depende de evento nenhum)
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
  const TIPOS_TEXTO = ["", "text", "search", "url", "email", "tel", "number"];
  const campos = () => {
    const saida = [];
    for (const raiz of raizes()) {
      for (const el of raiz.querySelectorAll('input, textarea, [contenteditable="true"], [contenteditable=""]')) {
        if (el.tagName === "INPUT" && !TIPOS_TEXTO.includes(el.type || "")) continue;
        saida.push(el);
      }
    }
    return saida;
  };
  const conteudo = (el) => (el.isContentEditable ? limpar(el.innerText) : limpar(el.value));
  const vistos = new Map();
  const varrer = () => {
    if (ehLogin()) return;
    for (const el of campos()) {
      let valor;
      try { valor = conteudo(el); } catch (e) { continue; }
      if (vistos.get(el) === valor) continue;
      vistos.set(el, valor);
      if (!valor) continue;
      const chave = (el.id || "") + "|" + (seletores(el)[0] || "");
      const existente = passos.find((p) => p.tipo === "digitar" && p.chave === chave);
      if (existente) { existente.valor = valor; existente.em = Date.now(); continue; }
      anotar({ tipo: "digitar", seletores: seletores(el), valor, chave });
    }
  };
  for (const el of campos()) { try { vistos.set(el, conteudo(el)); } catch (e) { /* ignora */ } }
  const relogio = setInterval(varrer, 900);
  return {
    passos,
    varrer,
    tirar: () => { varrer(); return passos.splice(0, passos.length); },
    parar: () => clearInterval(relogio),
    texto: () => limpar(document.body ? document.body.innerText : "").slice(0, 3000),
    // se a digitação não for anotada, isto diz onde ela estava escondida
    diagnostico: () => ({
      campos: campos().length,
      com_texto: campos().filter((el) => { try { return !!conteudo(el); } catch (e) { return false; } })
        .map((el) => ({ seletor: seletores(el)[0] || "?", tamanho: conteudo(el).length })).slice(0, 8),
      quadros: [...document.querySelectorAll("iframe")].map((f) => (f.getAttribute("src") || "(sem src)").slice(0, 120)),
      url: location.href,
    }),
  };
})();
1
"""


def arquivo_roteiro(cfg: Config, rede: str) -> Path:
    return cfg.pasta_dados / "roteiros" / f"{rede}.txt"


def papeis(rede: str) -> dict:
    """Os textos-marca que você precisa colar nesta rede."""
    return {papel: MARCAS[papel] for papel in PAPEIS_DA_REDE.get(rede, ())}


def _papel_do_valor(valor: str) -> tuple[str | None, list[str]]:
    """O papel do campo pela marca colada, e as marcas que eu não reconheci."""
    estranhas: list[str] = []
    for achado in _MARCA.finditer(valor):
        palavra = sem_acentos(achado.group(1)).lower().strip("_-. ")
        for papel in MARCAS:
            base = sem_acentos(papel).lower()
            if palavra.startswith(base[:5]) or base.startswith(palavra[:5]):
                return papel, []
        estranhas.append(achado.group(0))
    return None, estranhas


def limpar_passos(rede: str, passos: list[dict]) -> list[dict]:
    """Tira o que não serve e marca o papel dos campos pelos textos-marca."""
    saida: list[dict] = []
    # a varredura pode anotar um campo depois de um clique: a hora manda na ordem
    passos = sorted(passos, key=lambda p: p.get("em") or 0)
    for passo in passos:
        tipo = passo.get("tipo")
        seletores = [s for s in (passo.get("seletores") or []) if s]
        if tipo in ("clicar", "arquivo", "digitar") and not seletores:
            continue
        limpo: dict = {"tipo": tipo, "seletores": seletores}
        if tipo == "clicar":
            texto = str(passo.get("texto") or "")
            # clique com texto curto vira também busca por texto, que aguenta troca de classe
            if 0 < len(texto) <= 40:
                limpo["texto"] = texto
            for extra in ("rotulo", "dica", "tag"):
                if passo.get(extra):
                    limpo[extra] = str(passo[extra])[:60]
        elif tipo == "digitar":
            valor = str(passo.get("valor") or "")
            if "fakepath" in valor or any("type=\"file\"" in s for s in seletores):
                continue  # resquício do campo de arquivo, não é texto que você digitou
            papel, estranhas = _papel_do_valor(valor)
            if papel:
                limpo["papel"] = papel
            elif estranhas:
                # marca que eu não conheço: nunca digitar isso num post de verdade
                limpo["marca_estranha"] = estranhas[0]
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
    """Um passo por campo (com o texto final) e sem cliques repetidos seguidos."""
    saida: list[dict] = []
    por_campo: dict[tuple, dict] = {}
    for passo in passos:
        anterior = saida[-1] if saida else None
        if passo["tipo"] == "digitar":
            chave = tuple(passo["seletores"])
            ja = por_campo.get(chave)
            if ja is not None:
                # o mesmo campo digitado de novo (ou revisto pela varredura): fica o texto final,
                # no lugar onde ele apareceu primeiro
                ja["valor"] = passo.get("valor", ja.get("valor"))
                if passo.get("marca_estranha"):
                    ja["marca_estranha"] = passo["marca_estranha"]
                elif "valor" in passo:
                    ja.pop("marca_estranha", None)
                if passo.get("papel"):
                    ja["papel"] = passo["papel"]
                    ja.pop("valor", None)
                    ja.pop("marca_estranha", None)
                continue
            por_campo[chave] = passo
        if anterior and passo["tipo"] == "clicar" and anterior["tipo"] == "clicar" \
                and anterior["seletores"] == passo["seletores"] and anterior.get("texto") == passo.get("texto"):
            continue
        saida.append(passo)
    return saida


def analisar(rede: str, passos: list[dict]) -> dict:
    """Limpa os passos e diz se o roteiro serve: campos identificados, o que falta e o que sobrou."""
    limpos = limpar_passos(rede, passos)
    necessarios = list(PAPEIS_DA_REDE.get(rede, ()))
    achados = {p["papel"] for p in limpos if p.get("papel")}
    faltando = [p for p in necessarios if p not in achados]
    inferido = None
    sem_dono = [p for p in limpos if p["tipo"] == "digitar" and not p.get("papel")]
    if len(faltando) == 1 and len(sem_dono) == 1:
        # um papel faltando e um único campo de texto sem dono: não tem como errar
        sem_dono[0]["papel"] = faltando[0]
        sem_dono[0].pop("valor", None)
        sem_dono[0].pop("marca_estranha", None)
        inferido, faltando = faltando[0], []
    estranhas = [p["marca_estranha"] for p in limpos if p.get("marca_estranha")]
    cliques = [i for i, p in enumerate(limpos) if p["tipo"] == "clicar"]
    return {
        "passos_limpos": limpos,
        "faltando": faltando,
        "estranhas": estranhas,
        "inferido": inferido,
        "tem_arquivo": any(p["tipo"] == "arquivo" for p in limpos),
        "publicar_em": cliques[-1] if cliques else None,
    }


def fragil(passo: dict) -> bool:
    """Passo sem âncora estável: só sobrou o caminho na página, que quebra fácil."""
    seletores = passo.get("seletores") or []
    if not seletores:
        return True
    if passo["tipo"] == "clicar" and (passo.get("texto") or passo.get("rotulo") or passo.get("dica")):
        return False  # o texto do botão serve de âncora
    return all(" > " in s or s in ("input[type=\"text\"]", "div", "span", "button") for s in seletores)


def descrever(passo: dict) -> str:
    """O passo em uma linha, para o log da gravação."""
    tipo = passo["tipo"]
    if tipo == "arquivo":
        return "escolheu o vídeo"
    if tipo == "tecla":
        return f"teclou {passo.get('tecla') or 'Enter'}"
    if tipo == "clicar":
        alvo = passo.get("texto") or passo.get("rotulo") or passo.get("dica")
        return f"clicou em \u201c{alvo}\u201d" if alvo else f"clicou em {(passo.get('seletores') or ['?'])[0]}"
    onde = (passo.get("seletores") or ["?"])[0]
    if passo.get("papel"):
        return f"escreveu a {NOMES.get(passo['papel'], passo['papel'])} em {onde}"
    if passo.get("marca_estranha"):
        return f"colou {passo['marca_estranha']} em {onde} (marca que eu não conheço)"
    valor = str(passo.get("valor") or "")
    return f"escreveu \u201c{valor[:40]}{'…' if len(valor) > 40 else ''}\u201d em {onde}"


def lista_passos(rede: str, passos: list[dict]) -> list[dict]:
    """Os passos já limpos, descritos para o log ao vivo da gravação."""
    saida = []
    a = analisar(rede, passos)
    for i, passo in enumerate(a["passos_limpos"], 1):
        saida.append({
            "n": i, "tipo": passo["tipo"], "texto": descrever(passo),
            "fragil": fragil(passo), "papel": passo.get("papel"),
            "estranha": passo.get("marca_estranha"),
        })
    return saida


def salvar(cfg: Config, rede: str, passos: list[dict], url_inicial: str, confirmacao: str = "") -> dict:
    """Escreve o roteiro em texto, se ele servir. Devolve ok=False sem gravar quando não serve."""
    a = analisar(rede, passos)
    limpos = a.pop("passos_limpos")
    if a["faltando"] or a["estranhas"] or not a["tem_arquivo"]:
        # roteiro pela metade postaria texto errado: não gravo nem apago o que já havia
        log.warning("%s: gravação descartada (falta %s, marcas estranhas %s, vídeo %s)",
                    rede, a["faltando"] or "nada", a["estranhas"] or "nenhuma", a["tem_arquivo"])
        return {"ok": False, "passos": len(limpos), **a}
    texto = gerar(rede, limpos, url_inicial, confirmacao, a["publicar_em"])
    destino = arquivo_roteiro(cfg, rede)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(texto, encoding="utf-8")
    log.info("%s: roteiro de %d passos escrito em %s", rede, len(limpos), destino)
    return {"ok": True, "passos": len(limpos), "papeis": sorted({p["papel"] for p in limpos if p.get("papel")}),
            **a, "arquivo": str(destino), "texto": texto}


def carregar(cfg: Config, rede: str) -> str | None:
    """O texto do roteiro salvo ("" não conta)."""
    arquivo = arquivo_roteiro(cfg, rede)
    if not arquivo.is_file():
        return None
    try:
        texto = arquivo.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("%s: não consegui ler o roteiro (%s)", rede, e)
        return None
    return texto if texto.strip() else None
