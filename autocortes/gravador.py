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

from . import roteiro
from .config import RAIZ, Config
from .roteiro import (MARCAS, NOMES, PAPEIS_DA_REDE, PAPEIS_OPCIONAIS, Alvo, fraco, gerar, midia_do_roteiro,
                      parece_publicar)
from .util import escrever_atomico, log, sem_acentos

__all__ = ["NOMES_EXEMPLO", "PASTA_PADRAO", "SCRIPT", "analisar", "arquivo_padrao", "arquivo_roteiro", "carregar",
           "carregar_com_origem", "gravar_roteiro", "lista_passos", "mascarar_exemplos", "papeis", "salvar"]

# os arquivos que você escolhe na gravação de imagem: nome fixo e curto, para eu reconhecer (e tirar
# do roteiro) onde a página mostrar o nome deles
NOMES_EXEMPLO = tuple(f"exemplo-{n}.jpg" for n in range(1, 6))
SEM_FOTOS = ("O campo de arquivo desta página não aceita imagem. No TikTok, isso quer dizer que esta conta ainda "
             "não posta fotos pelo computador: as imagens do TikTok ficam desligadas.")
ARRASTOU = ("Você arrastou o arquivo para a página, e isso eu não consigo repetir. Grave de novo escolhendo o "
            "arquivo pelo botão da própria página.")

# Roteiros que vêm com a instalação (ficam no repositório). Valem enquanto o perfil não tem o
# seu em dados/roteiros: assim quem instala posta sem gravar primeiro, e quando a página de uma
# rede muda, a correção chega a todos pela atualização. O do perfil continua sendo a fonte única
# dele: editar ou gravar sempre escreve em dados/roteiros, nunca aqui.
PASTA_PADRAO = RAIZ / "roteiros"

_MARCA = re.compile(r"@@\s*([^@\s]{2,20})\s*@@")
# o nome dos arquivos de exemplo da gravação de imagem ("exemplo-1.jpg", "Exemplo 1", cortado no meio)
_NOME_EXEMPLO = re.compile(r"exemplo[\s_\-]*\d")
# o nome inteiro na tela, com a extensão ("exemplo-1.jpg", "Exemplo 2")
_EXEMPLO_NA_TELA = re.compile(r"\S*exemplo[\s_\-]*\d\S*", re.I)


def _tem_nome_de_exemplo(valor) -> bool:
    """O texto cita um arquivo de exemplo da gravação de imagem (não pode ir para o roteiro)."""
    return bool(_NOME_EXEMPLO.search(sem_acentos(str(valor or "")).lower()))


def mascarar_exemplos(obj):
    """O mesmo objeto (dicionários e listas, recursivo) com o nome dos arquivos de exemplo escondido."""
    if isinstance(obj, str):
        return "(nome do arquivo)" if _tem_nome_de_exemplo(obj) else obj
    if isinstance(obj, dict):
        return {k: mascarar_exemplos(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mascarar_exemplos(v) for v in obj]
    return obj

# Gravador injetado na página. Escuta na fase de captura e usa composedPath, então enxerga
# também o que está dentro do shadow DOM (YouTube Studio). Além dos eventos, varre os campos de
# texto de tempo em tempo: o editor de legenda do TikTok não dispara evento nenhum que dê para
# escutar de fora, e a varredura é o que garante que o texto seja anotado.
SCRIPT = r"""
window.__acGrav = (() => {
  const passos = [];
  const limpar = (t) => String(t || "").replace(/\s+/g, " ").trim();
  const ehLogin = () => /\/login|accounts\/login|accounts\.google\.com|passport\.bilibili|\/signin/.test(location.href);
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
  // Senha (mesmo com "mostrar senha", que troca o type para text), código de verificação e cartão.
  // Com um campo assim na tela (um login numa janela por cima da página, por exemplo), nada é gravado.
  const SENSIVEL = 'input[type="password"], input[autocomplete~="current-password"], ' +
    'input[autocomplete~="new-password"], input[autocomplete~="one-time-code"], input[autocomplete^="cc-"], ' +
    'input[name*="pass" i], input[id*="pass" i], input[name*="senha" i], input[id*="senha" i]';
  const aparece = (el) => {
    try {
      const r = el.getBoundingClientRect();
      return r.width + r.height > 0 && getComputedStyle(el).visibility !== "hidden";
    } catch (e) { return false; }
  };
  let sensivelEm = 0, sensivelAgora = false;
  const sensivel = () => {
    if (ehLogin()) return true;
    if (Date.now() - sensivelEm < 400) return sensivelAgora;
    sensivelEm = Date.now();
    sensivelAgora = false;
    for (const raiz of raizes()) {
      for (const el of raiz.querySelectorAll(SENSIVEL)) { if (aparece(el)) { sensivelAgora = true; return true; } }
    }
    return false;
  };
  // e-mail, telefone, usuário e afins: dado pessoal, nunca vira passo do roteiro
  const campoSensivel = (el) => {
    if (!el || el.tagName !== "INPUT") return false;
    try { if (el.matches(SENSIVEL)) return true; } catch (e) { /* seletor sem suporte */ }
    const auto = String(el.getAttribute("autocomplete") || "").toLowerCase();
    const nome = String(el.getAttribute("name") || "") + " " + String(el.id || "");
    return ["email", "tel"].includes(el.type) || /username|email|password|one-time-code|cc-|tel/.test(auto)
      || /user|login|e-?mail|senha|pass|phone|fone|otp/i.test(nome);
  };
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
    const raizesDaPagina = raizes();
    const quantidade = (s) => raizesDaPagina.reduce((total, raiz) => total + raiz.querySelectorAll(s).length, 0);
    const tag = (el.tagName || "").toLowerCase();
    if (el.id && estavel(el.id)) saida.push("#" + csse(el.id));
    for (const attr of ["data-e2e", "data-testid", "data-test-id", "data-tt", "name", "aria-label", "placeholder"]) {
      const v = el.getAttribute && el.getAttribute(attr);
      if (v && estavel(v)) saida.push(`${tag}[${attr}="${csse(v)}"]`);
    }
    if (tag === "input" && el.type) saida.push(`input[type="${csse(el.type)}"]`);
    if (el.getAttribute && el.getAttribute("role")) saida.push(`${tag}[role="${csse(el.getAttribute("role"))}"]`);
    if (el.isContentEditable) saida.push(`${tag}[contenteditable="true"]`);
    // Menus compactos escondem o texto "Criar"; o nome Novo post fica no SVG.
    // Grava o controle que contém esse ícone, sem depender do caminho dos divs.
    if (el.matches('a, button, [role="button"], [role="link"]')) {
      for (const icone of el.querySelectorAll('svg[aria-label]')) {
        const nome = icone.getAttribute("aria-label");
        if (estavel(nome) && aparece(icone)) {
          const seletor = `${tag}:has(svg[aria-label="${csse(nome)}"])`;
          if (quantidade(seletor) === 1) saida.push(seletor);
        }
      }
    }
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
    // O Studio repete #textbox (título/descrição) e #text-input (tags e outros campos).
    // A âncora do campo pai vem primeiro: o id interno sozinho apontaria outra caixa.
    if (caminho.length) {
      let pai = el.parentElement;
      for (let n = 0; pai && n < 12; n++, pai = pai.parentElement) {
        if (pai.id && estavel(pai.id)) {
          const escopo = "#" + csse(pai.id) + " " + (saida[0] || tag);
          // #input, #container e #outer também se repetem no Studio. Sobe até o campo único.
          if (quantidade(escopo) === 1) { saida.unshift(escopo); break; }
        }
      }
      saida.push(caminho.join(" > "));
    }
    const unicos = [...new Set(saida)];
    const precisos = unicos.filter((s) => quantidade(s) === 1);
    if (precisos.length) return precisos;
    const id = el.id && estavel(el.id) ? "#" + csse(el.id) : "";
    if (id && quantidade(id) > 1) {
      return unicos.filter((s) => s !== id);  // id repetido também não serve como alternativa
    }
    return unicos;
  };
  const anotar = (passo) => {
    if (sensivel()) return;  // nada do que você faz numa tela de login (ou com senha na tela) é gravado
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
  const campoDe = (el) => {
    // O evento pode vir de um span dentro do editor; o campo é quem tem contenteditable.
    while (el && el.isContentEditable && el.parentElement && el.parentElement.isContentEditable) {
      el = el.parentElement;
    }
    return el && (el.isContentEditable || ["INPUT", "TEXTAREA"].includes(el.tagName)) ? el : null;
  };
  const clicavelDe = (el) => {
    // O Studio põe ícones e camadas de animação dentro do botão. É o botão que
    // tem nome, estado e âncora estável; o div tocado pode desaparecer no próximo clique.
    const bruto = el;
    while (el && el.tagName) {
      if (el.matches('button, a[href], input, textarea, select, ytcp-button, ytcp-icon-button, ' +
                     '[role="button"], [role="link"], [role="radio"], [role="checkbox"], ' +
                     '[role="menuitem"], [role="tab"]')) return el;
      el = el.parentElement || (el.getRootNode && el.getRootNode().host);
    }
    return bruto;
  };
  const conteudo = (el) => limpar(el.isContentEditable ? (el.innerText || el.textContent) : el.value);
  const temMarca = (valor) => /@@\s*[^@\s]{2,20}\s*@@/.test(valor);
  const vistos = new Map();
  const chaves = new WeakMap();
  let proximaChave = 0;
  const registrarTexto = (el, colado) => {
    el = campoDe(el);
    if (!el || campoSensivel(el) || sensivel()) return;
    if (el.tagName === "INPUT" && (el.type === "password" || el.type === "file")) return;
    const valor = colado === undefined ? conteudo(el) : limpar(colado);
    if (colado === undefined && vistos.get(el) === valor) return;
    if (colado === undefined) vistos.set(el, valor);
    if (!valor) return;
    if (!chaves.has(el)) chaves.set(el, String(++proximaChave));
    const chave = chaves.get(el);
    const anterior = passos.find((p) => p.tipo === "digitar" && p.chave === chave);
    if (anterior) {
      // A marca identifica o campo mesmo que você depois ponha um texto real ou confirme a tag.
      // Preserva também a hora inicial: rever um campo não o leva para depois de "Publicar".
      if (!temMarca(anterior.valor) || temMarca(valor)) anterior.valor = valor;
      return;
    }
    anotar({ tipo: "digitar", seletores: seletores(el), valor, chave });
  };
  document.addEventListener("click", (ev) => {
    const bruto = alvo(ev);
    const el = campoDe(bruto) || clicavelDe(bruto);
    if (!el || !el.tagName) return;
    if (el.tagName === "INPUT" && (el.type === "file" || el.type === "password")) return;
    if (campoSensivel(el)) return;
    varrer();  // captura antes de um botão fechar ou trocar a tela dos campos
    const attr = (n) => limpar(el.getAttribute && el.getAttribute(n)).slice(0, 60);
    // clique num campo de texto: o que está escrito nele é seu, não o nome do botão
    const editavel = el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
    anotar({ tipo: "clicar", seletores: seletores(el),
             texto: editavel ? "" : limpar(el.innerText || el.textContent).slice(0, 60),
             rotulo: attr("aria-label"), dica: attr("title"), tag: (el.tagName || "").toLowerCase() });
  }, true);
  // Do campo de arquivo só vão quantos arquivos, se ele aceita vários e se aceita foto: nunca o
  // nome dos arquivos (nem o value ou o accept).
  document.addEventListener("change", (ev) => {
    const el = alvo(ev);
    if (!el || el.tagName !== "INPUT") return;
    if (el.type === "file") anotar({ tipo: "arquivo", seletores: seletores(el),
      quantos: el.files ? el.files.length : 0, multiplo: !!el.multiple,
      aceita_imagem: !el.accept || /(^|,)\s*(image\/\*|image\/jpeg|\.jpe?g)\s*(,|$)/i.test(el.accept) });
  }, true);
  // arquivo arrastado para a página: a escolha não passa pelo campo, e o roteiro não teria como repetir
  document.addEventListener("drop", (ev) => {
    const quantos = ev.dataTransfer && ev.dataTransfer.files ? ev.dataTransfer.files.length : 0;
    if (quantos) anotar({ tipo: "soltou", quantos });
  }, true);
  const digitou = (ev) => {
    registrarTexto(alvo(ev));
  };
  document.addEventListener("input", digitou, true);
  document.addEventListener("change", digitou, true);
  document.addEventListener("focusout", digitou, true);
  document.addEventListener("paste", (ev) => {
    const texto = ev.clipboardData && ev.clipboardData.getData("text/plain");
    // Alguns editores transformam a colagem em chips sem emitir input para fora do componente.
    if (texto && temMarca(texto)) registrarTexto(alvo(ev), texto);
  }, true);
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" && ev.key !== "Tab") return;
    const el = alvo(ev);
    if (campoSensivel(el)) return;
    registrarTexto(el);  // Enter pode transformar a tag em chip e esvaziar o input imediatamente
    anotar({ tipo: "tecla", tecla: ev.key });
  }, true);

  // --- varredura dos campos de texto (não depende de evento nenhum)
  const TIPOS_TEXTO = ["", "text", "search", "url", "number"];
  const campos = () => {
    const saida = [];
    for (const raiz of raizes()) {
      for (const el of raiz.querySelectorAll('input, textarea, [contenteditable]')) {
        if (el.tagName === "INPUT" && (!TIPOS_TEXTO.includes(el.type || "") || campoSensivel(el))) continue;
        const campo = campoDe(el);
        if (campo && aparece(campo) && !saida.includes(campo)) saida.push(campo);
      }
    }
    return saida;
  };
  const varrer = () => {
    if (sensivel()) return;
    for (const el of campos()) {
      try { registrarTexto(el); } catch (e) { /* campo removido enquanto eu varria */ }
    }
  };
  for (const el of campos()) {
    try {
      const valor = conteudo(el);
      if (temMarca(valor)) registrarTexto(el);  // a página pode ter trocado depois de você colar
      else vistos.set(el, valor);
    } catch (e) { /* ignora */ }
  }
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
    """Os textos-marca que você pode colar neste roteiro (os da rede e os opcionais, como o título
    do post de fotos do TikTok)."""
    return {papel: MARCAS[papel] for papel in (*PAPEIS_DA_REDE.get(rede, ()), *PAPEIS_OPCIONAIS.get(rede, ()))}


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


def _inteiro(valor, minimo: int, maximo: int) -> int | None:
    if isinstance(valor, bool) or not isinstance(valor, int) or not minimo <= valor <= maximo:
        return None
    return valor


def limpar_passos(rede: str, passos: list[dict], exemplos: bool = False) -> list[dict]:
    """Tira o que não serve e marca o papel dos campos pelos textos-marca.

    exemplos=True (a gravação de imagem): o nome dos arquivos de exemplo não entra no roteiro. O
    clique que o cita sai inteiro, o seletor que o cita sai da lista, e o campo que a página
    preencheu com ele fica marcado como preenchido_pela_pagina, sem o texto.
    """
    saida: list[dict] = []
    # a varredura pode anotar um campo depois de um clique: a hora manda na ordem
    passos = sorted(passos, key=lambda p: p.get("em") or 0)
    for passo in passos:
        tipo = passo.get("tipo")
        seletores = [s for s in (passo.get("seletores") or []) if s]
        if exemplos:
            seletores = [s for s in seletores if not _tem_nome_de_exemplo(s)]
        if tipo in ("clicar", "arquivo", "digitar") and not seletores:
            continue
        limpo: dict = {"tipo": tipo, "seletores": seletores}
        if tipo == "clicar":
            if exemplos and any(_tem_nome_de_exemplo(passo.get(c)) for c in ("texto", "rotulo", "dica")):
                continue  # o clique no nome ou na miniatura do arquivo: na repetição ele nem existiria
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
            # o papel vem antes do filtro: "exemplo-1 @@LEGENDA@@" (a marca colada no que a página
            # preencheu) é o campo da legenda
            papel, estranhas = _papel_do_valor(valor)
            if papel:
                limpo["papel"] = papel
            elif exemplos and _tem_nome_de_exemplo(valor):
                limpo["preenchido_pela_pagina"] = True
            elif estranhas:
                # Sem papel reconhecido, o texto fica fixo no roteiro, com aviso no painel.
                limpo["marca_estranha"] = estranhas[0]
                limpo["valor"] = valor
            else:
                limpo["valor"] = valor  # texto seu, repetido igual
        elif tipo == "tecla":
            limpo["tecla"] = passo.get("tecla") or "Enter"
        elif tipo == "arquivo":
            # cada chave conferida sozinha: um valor estranho tira a chave, nunca o passo
            limpo["midia"] = midia_do_roteiro(rede)
            quantos = _inteiro(passo.get("quantos"), 0, 99)
            if quantos is not None:
                limpo["quantos"] = quantos
            for chave in ("multiplo", "aceita_imagem"):
                if isinstance(passo.get(chave), bool):
                    limpo[chave] = passo[chave]
        elif tipo == "soltou":
            quantos = _inteiro(passo.get("quantos"), 0, 99)
            if quantos is not None:
                limpo["quantos"] = quantos
        else:
            continue
        if passo.get("url"):
            limpo["url"] = passo["url"]
        saida.append(limpo)
    return _juntar(saida)


def _juntar(passos: list[dict]) -> list[dict]:
    """Um passo por campo (com o texto final), preservando cada clique."""
    saida: list[dict] = []
    por_campo: dict[tuple, dict] = {}
    for passo in passos:
        if passo["tipo"] == "digitar":
            chave = tuple(passo["seletores"])
            ja = por_campo.get(chave)
            if ja is not None:
                if passo.get("preenchido_pela_pagina"):
                    # a página voltou a pôr o nome do arquivo: o seu texto (ou a marca) fica
                    if "valor" not in ja and not ja.get("papel"):
                        ja["preenchido_pela_pagina"] = True
                    continue
                if passo.get("papel") or "valor" in passo:
                    ja.pop("preenchido_pela_pagina", None)  # você trocou o que a página preencheu
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
        # O mesmo botão pode avançar várias telas. Os três "Próximo" do Studio
        # são três ações, mesmo com o mesmo seletor e texto.
        saida.append(passo)
    return saida


def analisar(rede: str, passos: list[dict], exemplos: bool = False) -> dict:
    """Limpa os passos e diz se o roteiro serve: campos identificados, o que falta e o que sobrou."""
    limpos = limpar_passos(rede, passos, exemplos)
    necessarios = list(PAPEIS_DA_REDE.get(rede, ()))
    achados = {p["papel"] for p in limpos if p.get("papel")}
    faltando = [p for p in necessarios if p not in achados]
    inferido = None
    # o campo que a página preencheu sozinha não é candidato: no TikTok, o título ficaria com a legenda
    sem_dono = [p for p in limpos if p["tipo"] == "digitar" and not p.get("papel")
                and not p.get("preenchido_pela_pagina")]
    if len(faltando) == 1 and len(sem_dono) == 1:
        # um papel faltando e um único campo de texto sem dono: não tem como errar
        sem_dono[0]["papel"] = faltando[0]
        sem_dono[0].pop("valor", None)
        sem_dono[0].pop("marca_estranha", None)
        inferido, faltando = faltando[0], []
    estranhas = [p["marca_estranha"] for p in limpos if p.get("marca_estranha")]
    publicar_em, incerto = _clique_de_publicar(limpos)
    arquivo = next((p for p in limpos if p["tipo"] == "arquivo"), None)
    return {
        "passos_limpos": limpos,
        "faltando": faltando,
        "estranhas": estranhas,
        "inferido": inferido,
        "tem_arquivo": arquivo is not None,
        "publicar_em": publicar_em,
        "publicar_incerto": incerto,
        # o campo de arquivo do roteiro de imagem aceita uma imagem por vez: o carrossel não sai por ele
        "so_uma": bool(midia_do_roteiro(rede) == "imagem" and arquivo is not None
                       and arquivo.get("multiplo") is False),
    }


def _parece_publicar(passo: dict) -> bool:
    alvos = [Alvo(str(s)) for s in passo.get("seletores") or []]
    alvos += [Alvo(str(passo[c]), True) for c in ("texto", "rotulo", "dica") if passo.get(c)]
    return parece_publicar(alvos)


def _clique_de_publicar(passos: list[dict]) -> tuple[int | None, bool]:
    """Qual clique publica: o primeiro com cara de publicar depois do vídeo.

    Não é o último clique: depois de "Publicar" ainda vêm a confirmação, o "Ver post" ou o
    fechar do aviso, e o ensaio executaria o clique que publica. Sem nenhum com cara de publicar,
    fica o último depois do vídeo, marcado como incerto (o roteiro sai com um aviso).
    """
    cliques = [i for i, p in enumerate(passos) if p["tipo"] == "clicar"]
    arquivo = next((i for i, p in enumerate(passos) if p["tipo"] == "arquivo"), None)
    depois = [i for i in cliques if arquivo is not None and i > arquivo]
    for i in depois:
        if _parece_publicar(passos[i]):
            return i, False
    if depois:
        return depois[-1], True
    return (cliques[-1], True) if cliques else (None, False)


def fragil(passo: dict) -> bool:
    """Passo sem âncora estável: só sobraram seletores genéricos, que casam com qualquer coisa."""
    if passo["tipo"] == "soltou":
        return False  # não vira linha do roteiro (o salvar avisa)
    seletores = passo.get("seletores") or []
    if not seletores:
        return True
    if passo["tipo"] == "arquivo":
        return False  # o campo de arquivo costuma ser o único da página
    if passo["tipo"] == "clicar" and (passo.get("texto") or passo.get("rotulo") or passo.get("dica")):
        return False  # o texto do botão serve de âncora
    return all(fraco(s) for s in seletores)


def descrever(passo: dict) -> str:
    """O passo em uma linha, para o log da gravação."""
    tipo = passo["tipo"]
    if tipo == "arquivo":
        if passo.get("midia") != "imagem":
            return "escolheu o vídeo"
        quantos = passo.get("quantos") or 0
        return "escolheu 1 imagem" if quantos == 1 else (f"escolheu {quantos} imagens" if quantos
                                                          else "escolheu as imagens")
    if tipo == "soltou":
        return "arrastou o arquivo para a página (isso eu não repito: use o botão de escolher arquivo)"
    if tipo == "tecla":
        return f"teclou {passo.get('tecla') or 'Enter'}"
    if tipo == "clicar":
        alvo = passo.get("texto") or passo.get("rotulo") or passo.get("dica")
        return f"clicou em \u201c{alvo}\u201d" if alvo else f"clicou em {(passo.get('seletores') or ['?'])[0]}"
    onde = (passo.get("seletores") or ["?"])[0]
    if passo.get("preenchido_pela_pagina") and not passo.get("papel"):
        return f"a página preencheu um campo com o nome do arquivo ({onde})"
    if passo.get("papel"):
        return f"escreveu a {NOMES.get(passo['papel'], passo['papel'])} em {onde}"
    if passo.get("marca_estranha"):
        return f"colou {passo['marca_estranha']} em {onde} (marca que eu não conheço)"
    valor = str(passo.get("valor") or "")
    return f"escreveu \u201c{valor[:40]}{'…' if len(valor) > 40 else ''}\u201d em {onde}"


def lista_passos(rede: str, passos: list[dict], exemplos: bool = False) -> list[dict]:
    """Os passos já limpos, descritos para o log ao vivo da gravação."""
    saida = []
    a = analisar(rede, passos, exemplos)
    for i, passo in enumerate(a["passos_limpos"], 1):
        saida.append({
            "n": i, "tipo": passo["tipo"], "texto": descrever(passo),
            "fragil": fragil(passo), "papel": passo.get("papel"),
            "estranha": passo.get("marca_estranha"),
            # o clique que eu vou tratar como "publicar" (o ensaio para nele)
            "publica": i - 1 == a["publicar_em"], "publica_incerto": i - 1 == a["publicar_em"] and a["publicar_incerto"],
        })
    return saida


def gravar_roteiro(cfg: Config, rede: str, texto: str) -> Path:
    """Grava o roteiro de uma vez só (temporário + troca): nunca fica um arquivo pela metade."""
    destino = arquivo_roteiro(cfg, rede)
    escrever_atomico(destino, texto if texto.endswith("\n") else texto + "\n")
    return destino


def salvar(cfg: Config, rede: str, passos: list[dict], url_inicial: str, confirmacao: str = "",
           texto_inicial: str = "", exemplos: bool = False) -> dict:
    """Salva os passos mesmo sem marcas; recusa apenas um roteiro que não pode ser executado.

    rede pode ser o nome de um roteiro de imagem ("instagram-imagem"). O retorno leva sempre
    quantos (arquivos escolhidos, 0 sem a escolha) e publicou (o clique de publicar foi anotado),
    que dizem se o post da gravação pode ter saído.
    """
    a = analisar(rede, passos, exemplos)
    limpos = a.pop("passos_limpos")
    arquivo = next((p for p in limpos if p["tipo"] == "arquivo"), None)
    a["quantos"] = int(arquivo.get("quantos") or 0) if arquivo else 0
    a["publicou"] = a["publicar_em"] is not None
    de_imagem = midia_do_roteiro(rede) == "imagem"
    if arquivo is None:
        if any(p["tipo"] == "soltou" for p in limpos):
            log.warning("%s: gravação não salva, o arquivo foi arrastado para a página", rede)
            return {"ok": False, "motivo": "arrastou", "mensagem": ARRASTOU, "passos": len(limpos), **a}
        log.warning("%s: gravação não salva, a escolha %s não foi anotada", rede,
                    "das imagens" if de_imagem else "do vídeo")
        return {"ok": False, "passos": len(limpos), **a}
    if de_imagem and arquivo.get("aceita_imagem") is False:
        log.warning("%s: gravação não salva, o campo de arquivo da página não aceita imagem", rede)
        return {"ok": False, "motivo": "sem_fotos", "mensagem": SEM_FOTOS, "passos": len(limpos), **a}
    if exemplos:
        # o nome do arquivo na tela vira separador: "exemplo-1.jpg foi compartilhada" quebraria no
        # ponto, e o "jpg foi compartilhada" que sobra viraria a linha conferir
        confirmacao = _EXEMPLO_NA_TELA.sub(" | ", confirmacao)
    texto = gerar(rede, limpos, url_inicial, confirmacao, a["publicar_em"], a["publicar_incerto"], texto_inicial,
                  evitar=_tem_nome_de_exemplo if exemplos else None)
    # o mesmo filtro do roteiro editado à mão: o que eu não consigo executar não vai para o disco
    _, erros = roteiro.analisar(texto, rede)
    if erros:
        log.warning("%s: gravação descartada, o roteiro sairia com erro: %s", rede, "; ".join(erros[:3]))
        return {"ok": False, "passos": len(limpos), **a, "erros": erros, "texto": texto}
    destino = gravar_roteiro(cfg, rede, texto)
    if a["faltando"] or a["estranhas"]:
        log.warning("%s: roteiro salvo mesmo sem todas as marcas (campos sem identificação %s, "
                    "marcas desconhecidas %s)", rede, a["faltando"] or "nenhum", a["estranhas"] or "nenhuma")
    log.info("%s: roteiro de %d passos escrito em %s", rede, len(limpos), destino)
    return {"ok": True, "passos": len(limpos), "papeis": sorted({p["papel"] for p in limpos if p.get("papel")}),
            **a, "arquivo": str(destino), "texto": texto, "resumo": roteiro.resumo(texto, rede)}


def arquivo_padrao(rede: str) -> Path:
    return PASTA_PADRAO / f"{rede}.txt"


def _ler_roteiro(arquivo: Path, rede: str) -> str | None:
    if not arquivo.is_file():
        return None
    try:
        texto = arquivo.read_text(encoding="utf-8-sig")  # um BOM viraria a linha 1 "\ufeff#..."
    except (OSError, UnicodeDecodeError) as e:
        log.warning("%s: não consegui ler o roteiro %s (%s)", rede, arquivo, e)
        return None
    return texto if texto.strip() else None


def carregar_com_origem(cfg: Config, rede: str) -> tuple[str | None, str | None]:
    """O roteiro que vale e de onde ele vem: "perfil" (o seu, em dados/roteiros) ou "padrao"
    (o que vem com a instalação). (None, None) quando não há nenhum: o envio usa os passos fixos.
    O roteiro de imagem ("<rede>-imagem") não tem padrão: só vale o gravado no perfil."""
    texto = _ler_roteiro(arquivo_roteiro(cfg, rede), rede)
    if texto is not None:
        return texto, "perfil"
    if roteiro.midia_do_roteiro(rede) == "imagem":
        return None, None
    texto = _ler_roteiro(arquivo_padrao(rede), rede)
    if texto is not None:
        return texto, "padrao"
    return None, None


def carregar(cfg: Config, rede: str) -> str | None:
    """O texto do roteiro que vale: o do perfil ou, sem ele, o padrão ("" não conta)."""
    return carregar_com_origem(cfg, rede)[0]
