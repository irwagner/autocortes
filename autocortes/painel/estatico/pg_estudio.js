/* AutoCortes - painel: Estúdio (visual dos cortes: modelos, moldura, vídeo, título e legenda) */
"use strict";

const PgEstudio = {
  dados: null,      // /estudio: modelos, molduras e as chaves do visual
  geo: null,        // /estudio/geometria: onde fica cada coisa com as mudanças ainda não salvas
  erroGeo: null,
  guias: true,
  modo: "editar",   // editar | real
  previa: { url: null, gerando: false, sujo: false, pedido: 0, erro: null },
  pedidoGeo: 0,
  arrasto: null,
  focar: null,      // camada que ganha o foco quando a geometria voltar (depois de "Automática", por exemplo)
};

// 1x mostra o quadro inteiro na largura da área; 1,25x é o padrão do AutoCortes
const EST_TAMANHOS = [
  ["Menor", 0.8, "Mais estreito que a área, com fundo dos lados"],
  ["Inteiro", 1, "O quadro inteiro, na largura da área"],
  ["Padrão", 1.25, "Um pouco maior, cortando um pouco das laterais"],
  ["Maior", 1.7, "Bem maior, cortando mais das laterais"],
];
const EST_FORMATOS = [
  ["desfocado", "Vídeo com fundo desfocado"], ["preto", "Vídeo com fundo preto"],
  ["preencher", "Vídeo preenchendo a área (corta as laterais)"],
];
const EST_LUGARES = [
  ["automatico", "Automático: abaixo do vídeo se couber na área livre"], ["abaixo", "Sempre abaixo do vídeo"],
  ["sobre", "Sobre a parte de baixo do vídeo"],
];
const EST_CHAVE_POS = { video: "edicao.posicao_video", titulo: "edicao.posicao_topo", legenda: "edicao.posicao_legenda" };
const EST_ROTULO_POS = { video: "Altura do vídeo", titulo: "Altura do título", legenda: "Altura da legenda" };
// mudanças que mostram ou escondem campos
const EST_REDESENHA = new Set(["edicao.layout", "edicao.legendas", "edicao.moldura", "edicao.barra_progresso"]);

const estLarguraTela = () => (PgEstudio.geo && PgEstudio.geo.largura) || 1080;
const estCq = (px) => `${((px / estLarguraTela()) * 100).toFixed(3)}cqw`; // pixels do vídeo em unidades do celular
const estPct = (v, total) => `${((v / total) * 100).toFixed(4)}%`;
const estCor = (c) => String(c || "FFFFFF").replace("#", "");
// nome da fonte entre aspas no CSS: um apóstrofo no nome não quebra o estilo
const estFamilia = (nome) => `"${String(nome || "").replace(/["\\]/g, "\\$&")}"`;

// libass mede a fonte pela altura da linha (subida + descida); o navegador, pelo "em"
const EST_FATOR_FONTE = {};
function estFatorFonte(nome) {
  if (EST_FATOR_FONTE[nome]) return EST_FATOR_FONTE[nome];
  let fator = 0.85;
  try {
    const ctx = document.createElement("canvas").getContext("2d");
    ctx.font = `100px ${estFamilia(nome)}`;
    const m = ctx.measureText("Hg");
    const altura = (m.fontBoundingBoxAscent || 0) + (m.fontBoundingBoxDescent || 0);
    if (altura > 40 && altura < 250) fator = 100 / altura;
  } catch (e) { /* fica o valor padrão */ }
  EST_FATOR_FONTE[nome] = fator;
  return fator;
}

/* ------------------------------------------------------------ tela do celular */

function estArea(g) {
  return g.geometria.area || { x: 0, y: 0, w: g.largura, h: g.altura };
}

/** Faixa em que cada camada pode ficar (a mesma que o servidor usa, com o texto inteiro dentro da área). */
function estLimites(tipo) {
  const g = PgEstudio.geo, geo = g.geometria, area = estArea(g), t = g.textos;
  let minimo, maximo;
  if (tipo === "video") {
    const meia = geo.frente_h / 2;
    [minimo, maximo] = [area.y + meia, area.y + area.h - meia];
  } else if (tipo === "titulo") {
    const altura = t.topo.linhas.length * t.topo.tamanho * (t.topo.escala / 100) * 1.4;
    [minimo, maximo] = [area.y + altura, area.y + area.h];
  } else {
    const meia = t.legenda.tamanho * 0.72 + t.legenda.contorno;
    [minimo, maximo] = [area.y + meia, area.y + area.h - meia];
  }
  if (minimo > maximo) minimo = maximo = (minimo + maximo) / 2;
  return [minimo, maximo];
}

function estLimitar(tipo, y) {
  const [minimo, maximo] = estLimites(tipo);
  return Math.max(minimo, Math.min(maximo, y));
}

/** Slider vertical: o valor é a altura (sobe com a seta para cima); o texto lido é a distância do topo. */
function estAriaHtml(tipo, y) {
  const H = PgEstudio.geo.altura;
  const [minimo, maximo] = estLimites(tipo);
  return raw(`data-arrastar="${tipo}" tabindex="0" role="slider" aria-orientation="vertical" aria-label="${EST_ROTULO_POS[tipo]}" aria-valuemin="${Math.round(H - maximo)}" aria-valuemax="${Math.round(H - minimo)}" aria-valuenow="${Math.round(H - y)}" aria-valuetext="${Math.round(y)} px do topo"`);
}

function estAtualizarAria(alvo, y) {
  alvo.setAttribute("aria-valuenow", String(Math.round(PgEstudio.geo.altura - y)));
  alvo.setAttribute("aria-valuetext", `${Math.round(y)} px do topo`);
}

function estCamadasHtml() {
  const g = PgEstudio.geo;
  if (!g) return h`<div class="est-carregando"><span class="giro"></span></div>`;
  const W = g.largura, H = g.altura, geo = g.geometria;
  const quadro = midia(g.quadro.url);
  const partes = [];
  if (geo.layout === "desfocado") {
    const blur = Number(valor("edicao.desfoque_fundo")) || 20;
    const escurecer = Number(valor("edicao.escurecer_fundo")) || 0;
    partes.push(h`<div class="est-fundo" style="background-image:url('${quadro}');filter:blur(${estCq(blur * 3.2)}) brightness(${(1 - escurecer).toFixed(2)}) saturate(1.15)"></div>`);
  }
  const arrastavel = geo.layout !== "preencher";
  const larguraFundo = ((geo.escala_w / geo.frente_w) * 100).toFixed(3);
  const alturaFundo = ((geo.escala_h / geo.frente_h) * 100).toFixed(3);
  const centro = geo.frente_y + geo.frente_h / 2;
  partes.push(h`<div class="est-filme ${arrastavel ? "est-arrastavel" : ""}" ${arrastavel ? estAriaHtml("video", centro) : ""} title="${arrastavel ? "Arraste para subir ou descer o vídeo" : ""}" style="left:${estPct(geo.frente_x, W)};top:${estPct(geo.frente_y, H)};width:${estPct(geo.frente_w, W)};height:${estPct(geo.frente_h, H)};background-image:url('${quadro}');background-size:${larguraFundo}% ${alturaFundo}%"></div>`);
  if (valor("edicao.barra_progresso")) {
    partes.push(h`<div class="est-barra" id="est-barra" style="left:${estPct(geo.barra_x, W)};top:${estPct(geo.barra_y, H)};width:${estPct(geo.barra_w * 0.35, W)};height:${estPct(10, H)};background:#${estCor(valor("edicao.cor_barra"))}"></div>`);
  }
  return h`${partes}`;
}

function estTextosHtml() {
  const g = PgEstudio.geo;
  if (!g) return "";
  const W = g.largura, H = g.altura, geo = g.geometria, t = g.textos;
  const partes = [];
  if (t.topo.linhas.length) {
    const tam = (t.topo.tamanho * t.topo.escala) / 100;
    partes.push(h`<div class="est-texto est-titulo est-arrastavel" ${estAriaHtml("titulo", geo.topo_y)} title="Arraste para subir ou descer o título" style="left:${estPct(t.centro_x, W)};top:${estPct(geo.topo_y, H)};font-family:${estFamilia(t.topo.fonte)};font-size:${estCq(tam * estFatorFonte(t.topo.fonte))};line-height:${estCq(tam)};color:#${estCor(t.topo.cor)};-webkit-text-stroke:${estCq(10)} #000;text-shadow:${estCq(2)} ${estCq(2)} 0 rgba(0,0,0,.6)">${t.topo.linhas.map((linha, i) => h`${i ? raw("<br>") : ""}${linha}`)}</div>`);
  }
  if (valor("edicao.legendas") && t.legenda.palavras.length) {
    const l = t.legenda;
    const tam = (l.tamanho * l.escala) / 100;
    const palavras = l.palavras.map((p, i) => h`${i ? " " : ""}${i === l.destaque ? h`<span style="color:#${estCor(l.cor_destaque)}">${p}</span>` : p}`);
    partes.push(h`<div class="est-texto est-legenda est-arrastavel" ${estAriaHtml("legenda", geo.legenda_y)} title="Arraste para subir ou descer a legenda" style="left:${estPct(t.centro_x, W)};top:${estPct(geo.legenda_y, H)};font-family:${estFamilia(l.fonte)};font-size:${estCq(tam * estFatorFonte(l.fonte))};line-height:${estCq(tam)};color:#${estCor(l.cor)};-webkit-text-stroke:${estCq(l.contorno * 2)} #000;text-shadow:${estCq(3)} ${estCq(3)} 0 rgba(0,0,0,.6)">${palavras}</div>`);
  }
  return h`${partes}`;
}

function estGuiasHtml() {
  const g = PgEstudio.geo;
  if (!g || !PgEstudio.guias) return "";
  const W = g.largura, H = g.altura, s = g.seguro;
  return h`<div class="est-guia" style="left:0;top:0;width:100%;height:${estPct(s.topo, H)}"><span>Barra do app</span></div>
    <div class="est-guia" style="left:0;top:${estPct(s.base, H)};width:100%;height:${estPct(H - s.base, H)}"><span>Descrição e botões do app</span></div>
    <div class="est-linha-guia" style="left:${estPct(s.esquerda, W)};top:${estPct(s.topo, H)};height:${estPct(s.base - s.topo, H)}"></div>
    <div class="est-linha-guia" style="left:${estPct(s.direita, W)};top:${estPct(s.topo, H)};height:${estPct(s.base - s.topo, H)}"></div>`;
}

function estMolduraAjuste(m) {
  const g = PgEstudio.geo;
  if (!m || !m.largura || !m.altura || !g) return "fill";
  const alvo = g.largura / g.altura;
  return Math.abs(m.largura / m.altura - alvo) <= 0.03 * alvo ? "fill" : "contain";
}

function estDesenharVisual() {
  const visual = $("#est-visual");
  if (!visual) return;
  const g = PgEstudio.geo;
  const proporcao = g ? `${g.largura} / ${g.altura}` : "";
  if (PgEstudio.modo === "real") {
    const p = PgEstudio.previa;
    if (!$("#est-real", visual)) {
      montar(visual, h`<div class="celular real" id="est-real"><img id="est-real-img" alt="Como o corte vai sair"><div class="est-carregando" id="est-real-giro"><span class="giro"></span></div><div class="est-carregando est-falhou" id="est-real-erro" hidden>${ic("alerta")}<span>Não consegui gerar o quadro. O motivo está logo abaixo.</span></div></div>`);
    }
    if (proporcao) $("#est-real").style.aspectRatio = proporcao;
    const img = $("#est-real-img");
    if (p.url && img.getAttribute("src") !== p.url) img.setAttribute("src", p.url);
    img.hidden = !p.url;
    $("#est-real-giro").hidden = !p.gerando;
    $("#est-real-erro").hidden = p.gerando || !p.erro;
    return;
  }
  if (!$("#est-celular", visual)) {
    montar(visual, h`<div class="celular" id="est-celular" role="group" aria-label="Tela do celular com o visual do corte"><div class="est-camadas" id="est-camadas"></div><img class="est-moldura" id="est-moldura" alt="" hidden><div class="est-camadas" id="est-textos"></div><div class="est-camadas est-sem-toque" id="est-guias-camada"></div></div>`);
  }
  const celular = $("#est-celular");
  if (proporcao) celular.style.aspectRatio = proporcao;
  // o elemento com foco é recriado quando a geometria muda: guarda qual era para devolver o foco
  const ativo = document.activeElement;
  const focado = ativo && celular.contains(ativo) && ativo.dataset ? ativo.dataset.arrastar || null : null;
  montarSeMudou($("#est-camadas"), estCamadasHtml());
  const img = $("#est-moldura");
  const m = g && g.moldura;
  if (m && m.url) {
    const url = midia(m.url);
    if (img.getAttribute("src") !== url) img.setAttribute("src", url);
    img.style.objectFit = estMolduraAjuste(m);
    img.hidden = false;
  } else {
    img.hidden = true;
    img.removeAttribute("src");
  }
  montarSeMudou($("#est-textos"), estTextosHtml());
  montarSeMudou($("#est-guias-camada"), estGuiasHtml());
  // "focar" só vale se o foco não foi para outro lugar (ex.: o usuário clicou num campo)
  const agora = document.activeElement;
  const livre = !agora || agora === document.body || !agora.isConnected;
  const tipo = focado || (livre ? PgEstudio.focar : null);
  PgEstudio.focar = null;
  if (tipo) {
    const alvo = $(`#est-celular [data-arrastar="${tipo}"]`);
    if (alvo && document.activeElement !== alvo) alvo.focus({ preventScroll: true });
  }
}

function estAtualizarNota() {
  const el = $("#est-nota");
  if (!el) return;
  const g = PgEstudio.geo;
  const notas = [];
  // cada modo mostra o próprio erro: a prévia real tem o dela, a tela de editar tem o da geometria
  const erro = PgEstudio.modo === "real" ? PgEstudio.previa.erro : PgEstudio.erroGeo;
  if (erro) {
    montar(el, h`<span class="erro-texto">${erro}</span>`);
    return;
  }
  if (PgEstudio.modo === "real") {
    notas.push("Quadro gerado pelo mesmo editor dos cortes, com as mudanças ainda não salvas.");
  } else {
    notas.push("Arraste o vídeo, o título e a legenda para mudar de lugar (ou clique e use as setas do teclado; com Shift anda mais). Esta tela é aproximada: em \"Como vai sair\" aparece o quadro de verdade.");
  }
  if (g && !g.quadro.real) notas.push("A imagem é de teste; depois que um filme for analisado aparece um quadro dele.");
  if (g && g.moldura_problema) notas.push(`${g.moldura_problema}: o corte sai sem ela.`);
  montar(el, notas.join(" "));
}

function estNotaTamanhoHtml() {
  const g = PgEstudio.geo;
  if (!g || valor("edicao.layout") === "preencher" || g.geometria.layout !== "preencher") return "";
  return h`<p class="nota">Este vídeo já é alto (quase vertical): ele ocupa a área toda, e o tamanho só muda algo abaixo de 1x.</p>`;
}

async function estAtualizarGeo() {
  if (App.nomePagina !== "estudio") return;
  const pedido = ++PgEstudio.pedidoGeo;
  try {
    const g = await post("/estudio/geometria", { alteracoes: App.pendente });
    if (pedido !== PgEstudio.pedidoGeo || App.nomePagina !== "estudio") return;
    PgEstudio.geo = g;
    PgEstudio.erroGeo = null;
  } catch (e) {
    if (pedido !== PgEstudio.pedidoGeo) return;
    PgEstudio.erroGeo = (e.dados && Array.isArray(e.dados.erros) && e.dados.erros[0]) || e.message;
  }
  if (!PgEstudio.arrasto) estDesenharVisual();
  estAtualizarNota();
  montarSeMudou($("#est-nota-tamanho"), estNotaTamanhoHtml());
}
const estAgendarGeo = adiar(estAtualizarGeo, 120);

async function estGerarPrevia() {
  const p = PgEstudio.previa;
  if (App.nomePagina !== "estudio") return;
  if (p.gerando) { p.sujo = true; return; } // uma prévia por vez; a próxima sai quando esta terminar
  const pedido = ++p.pedido;
  p.gerando = true;
  p.sujo = false;
  p.erro = null;
  estDesenharVisual();
  try {
    const r = await post("/previa", { alteracoes: App.pendente });
    if (pedido !== p.pedido) return;
    p.url = midia(r.url);
    if (PgEstudio.erroGeo) estAgendarGeo(); // a configuração voltou a valer: a tela de editar também se refaz
  } catch (e) {
    if (pedido !== p.pedido) return;
    p.erro = (e.dados && Array.isArray(e.dados.erros) && e.dados.erros[0]) || e.message;
  } finally {
    if (pedido === p.pedido) p.gerando = false;
  }
  if (App.nomePagina !== "estudio") return;
  estDesenharVisual();
  estAtualizarNota();
  if (p.sujo && PgEstudio.modo === "real") estGerarPrevia();
}
const estAgendarPrevia = adiar(estGerarPrevia, 700);

/* ------------------------------------------------------------ arrastar */

function estPosicaoAtual(tipo) {
  const geo = PgEstudio.geo.geometria;
  if (tipo === "video") return geo.frente_y + geo.frente_h / 2;
  return tipo === "titulo" ? geo.topo_y : geo.legenda_y;
}

function estMoverVisual(alvo, tipo, y) {
  const g = PgEstudio.geo, geo = g.geometria;
  if (tipo === "video") {
    const topo = y - geo.frente_h / 2;
    alvo.style.top = estPct(topo, g.altura);
    const barra = $("#est-barra");
    if (barra) barra.style.top = estPct(topo + (geo.barra_y - geo.frente_y), g.altura);
  } else {
    alvo.style.top = estPct(y, g.altura);
  }
}

function estDefinirPosicao(tipo, y) {
  definirValor(EST_CHAVE_POS[tipo], Math.max(1, Math.round(y)));
}

function estInicioArrasto(ev) {
  const alvo = ev.target.closest("[data-arrastar]");
  if (!alvo || ev.button !== 0 || !PgEstudio.geo || PgEstudio.arrasto) return;
  ev.preventDefault();
  alvo.focus({ preventScroll: true });
  const tipo = alvo.dataset.arrastar;
  const escala = $("#est-celular").getBoundingClientRect().height / PgEstudio.geo.altura;
  const inicio = estPosicaoAtual(tipo);
  const arr = { tipo, y0: ev.clientY, inicio, atual: inicio, escala, id: ev.pointerId, fim: false };
  PgEstudio.arrasto = arr;
  try { alvo.setPointerCapture(ev.pointerId); } catch (e) { /* o ponteiro já foi solto */ }
  alvo.classList.add("est-arrastando");
  const mover = (e2) => {
    if (e2.pointerId !== arr.id || arr.fim) return;
    arr.atual = estLimitar(tipo, arr.inicio + (e2.clientY - arr.y0) / arr.escala);
    estMoverVisual(alvo, tipo, arr.atual);
    estAtualizarAria(alvo, arr.atual);
  };
  const terminar = (e2, gravar) => {
    if (e2.pointerId !== arr.id || arr.fim) return;
    arr.fim = true; // pointerup e lostpointercapture chegam os dois
    alvo.removeEventListener("pointermove", mover);
    alvo.removeEventListener("pointerup", soltar);
    alvo.removeEventListener("lostpointercapture", soltar);
    alvo.removeEventListener("pointercancel", cancelar);
    alvo.classList.remove("est-arrastando");
    PgEstudio.arrasto = null;
    if (gravar && Math.abs(arr.atual - arr.inicio) >= 1) estDefinirPosicao(tipo, arr.atual);
    else estDesenharVisual();
  };
  const soltar = (e2) => terminar(e2, true);
  const cancelar = (e2) => terminar(e2, false);
  alvo.addEventListener("pointermove", mover);
  alvo.addEventListener("pointerup", soltar);
  alvo.addEventListener("lostpointercapture", soltar);
  alvo.addEventListener("pointercancel", cancelar);
}

function estTeclaArrasto(ev) {
  const alvo = ev.target.closest && ev.target.closest("[data-arrastar]");
  if (!alvo || !PgEstudio.geo || !["ArrowUp", "ArrowDown", "PageUp", "PageDown"].includes(ev.key)) return;
  ev.preventDefault();
  const passo = ev.key.startsWith("Page") || ev.shiftKey ? 50 : 10;
  const direcao = ev.key === "ArrowUp" || ev.key === "PageUp" ? -1 : 1;
  const tipo = alvo.dataset.arrastar;
  // parte do valor já escolhido: várias teclas seguidas somam, mesmo antes de a geometria voltar
  const base = Number(valor(EST_CHAVE_POS[tipo])) || estPosicaoAtual(tipo);
  const y = estLimitar(tipo, base + direcao * passo);
  estMoverVisual(alvo, tipo, y);
  estAtualizarAria(alvo, y);
  estDefinirPosicao(tipo, y);
}

/* ------------------------------------------------------------ controles */

function estPosicaoHtml(tipo) {
  const v = Number(valor(EST_CHAVE_POS[tipo])) || 0;
  return linhaCampo(EST_ROTULO_POS[tipo], v ? `Escolhida: ${v} px do topo.` : "Automática. Arraste na tela para escolher.",
    v ? h`<button type="button" class="btn pequeno" data-acao="est-pos-auto" data-tipo="${tipo}">${ic("refazer")}Automática</button>` : h`<span class="texto-suave">Automática</span>`);
}
const estLinhaPosicao = (tipo) => h`<div id="est-pos-${tipo}">${estPosicaoHtml(tipo)}</div>`;

function estCardModelos() {
  const d = PgEstudio.dados;
  return h`<div class="card"><div class="card-topo"><h3>${ic("estrela")}Modelos</h3><button type="button" class="btn pequeno" data-acao="est-modelo-novo">${ic("mais")}Novo modelo</button></div>
    <div class="modelos-visuais">${d.modelos.map((m) => h`<div class="modelo-visual ${m.ativo ? "ativo" : ""}">
      <div class="mv-info"><b>${m.nome}</b><small>${m.resumo}</small></div>
      ${m.ativo ? badge(["Em uso", "ok"], true) : h`<button type="button" class="btn pequeno" data-acao="est-modelo-usar" data-nome="${m.nome}">Usar</button>`}
      <button type="button" class="btn pequeno icone fantasma" data-acao="est-modelo-renomear" data-nome="${m.nome}" title="Renomear" aria-label="Renomear ${m.nome}">${ic("lapis")}</button>
      ${m.ativo ? "" : h`<button type="button" class="btn pequeno icone fantasma" data-acao="est-modelo-excluir" data-nome="${m.nome}" title="Excluir" aria-label="Excluir ${m.nome}">${ic("lixo")}</button>`}
    </div>`)}</div>
    <p class="nota">O que você mudar aqui fica no modelo em uso (${d.ativo}) quando salvar. Ele vale para os cortes editados daqui em diante: os que já estão prontos continuam como estão. Para refazer um corte com o visual novo, use Editar de novo na página Cortes.</p></div>`;
}

function estMoldurasHtml() {
  const atual = String(valor("edicao.moldura") || "");
  const itens = [h`<button type="button" class="moldura-op ${atual ? "" : "ativa"}" data-acao="est-moldura" data-arquivo="" aria-pressed="${!atual}"><span class="mo-vazia">${ic("x")}</span><small>Sem moldura</small></button>`];
  for (const m of PgEstudio.dados.molduras) {
    const escolhida = mesmoArquivo(m.arquivo, atual);
    const usavel = m.existe && !m.erro && Boolean(m.janela);
    const dica = m.erro || (!m.existe ? "Arquivo não encontrado" : m.janela ? `Área do vídeo: ${m.janela.w} x ${m.janela.h} px` : "Sem área transparente para o vídeo");
    itens.push(h`<button type="button" class="moldura-op ${escolhida ? "ativa" : ""}" data-acao="est-moldura" data-arquivo="${m.arquivo}" aria-pressed="${escolhida}" ${usavel ? "" : raw("disabled")} title="${dica}">${m.url ? h`<img src="${midia(m.url)}" alt="" loading="lazy">` : h`<span class="mo-vazia">${ic("alerta")}</span>`}<small>${m.nome}</small></button>`);
  }
  return h`${itens}`;
}

function estCardMoldura() {
  return h`<div class="card"><div class="card-topo"><h3>${ic("imagem")}Moldura</h3><button type="button" class="btn pequeno" data-acao="est-moldura-enviar">${ic("enviar")}Enviar moldura</button><input type="file" id="est-arquivo-moldura" accept="image/png" hidden></div>
    <div class="molduras" id="est-molduras" role="group" aria-label="Escolha a moldura">${estMoldurasHtml()}</div>
    <p class="nota">Imagem PNG em pé (9:16, como 1080 x 1920) com a área do vídeo transparente. O vídeo, o título e a legenda ficam dentro dessa área. As molduras ficam em ${PgEstudio.dados.pasta_molduras}.</p></div>`;
}

function estBotoesTamanhoHtml() {
  const zoom = Number(valor("edicao.zoom"));
  return h`${EST_TAMANHOS.map(([rotulo, z, dica]) => {
    const ativo = Math.abs(zoom - z) < 0.001;
    return h`<button type="button" class="btn pequeno ${ativo ? "ativo" : ""}" data-acao="est-zoom" data-zoom="${z}" aria-pressed="${ativo}" title="${dica}">${rotulo}</button>`;
  })}`;
}

function estCardVideo() {
  const layout = valor("edicao.layout");
  const referencia = valor("edicao.moldura") ? "da área da moldura" : "da tela";
  return h`<div class="card"><div class="card-topo"><h3>${ic("video")}Vídeo</h3></div>${formulario([
    campo({ chave: "edicao.layout", tipo: "select", opcoes: EST_FORMATOS, rotulo: "Formato" }),
    ...(layout === "preencher" ? [] : [
      linhaCampo("Tamanho", `Inteiro (1x) mostra o quadro todo na largura ${referencia}; acima disso o vídeo cresce e corta as laterais.`, h`<div class="grupo-botoes" id="est-tamanhos" role="group" aria-label="Tamanho do vídeo">${estBotoesTamanhoHtml()}</div>`),
      campo({ chave: "edicao.zoom", tipo: "range", min: 0.3, max: 3, passo: 0.05, formato: "x", rotulo: "Tamanho exato" }),
      h`<div id="est-nota-tamanho">${estNotaTamanhoHtml()}</div>`,
      estLinhaPosicao("video"),
    ]),
    ...(layout === "desfocado" ? [
      campo({ chave: "edicao.desfoque_fundo", tipo: "range", min: 1, max: 60, rotulo: "Desfoque do fundo" }),
      campo({ chave: "edicao.escurecer_fundo", tipo: "range", min: 0, max: 0.9, passo: 0.01, formato: "pct", rotulo: "Escurecer o fundo" }),
    ] : []),
  ])}</div>`;
}

function estSelectFonteTitulo() {
  App.rotulos["edicao.fonte_topo_arquivo"] = "Fonte do título";
  const atual = String(valor("edicao.fonte_topo_arquivo") || "");
  const fontes = (App.meta && App.meta.fontes) || [];
  const lista = [{ arquivo: "", nome: "" }, ...fontes];
  if (atual && !fontes.some((f) => mesmoArquivo(f.arquivo, atual))) lista.push({ arquivo: atual, nome: valor("edicao.fonte_topo_nome") || atual });
  return h`<select data-campo="edicao.fonte_topo_arquivo" data-tipo="fonte" data-campo-nome="edicao.fonte_topo_nome" aria-label="Fonte do título">${lista.map((f) => h`<option value="${f.arquivo}" data-nome="${f.nome}" ${mesmoArquivo(f.arquivo, atual) ? raw("selected") : ""}>${f.arquivo ? f.nome : "Igual à da legenda"}</option>`)}</select>`;
}

function estCardTitulo() {
  return h`<div class="card"><div class="card-topo"><h3>${ic("texto")}Título no topo</h3></div>${formulario([
    campo({ chave: "edicao.texto_topo", tipo: "textarea", linhas: 2, larga: true, rotulo: "Texto", ajuda: "{filme}, {parte} e {ano} viram o nome, o número da parte e o ano. Enter quebra a linha; em branco, fica sem título." }),
    linhaCampo("Fonte", "", estSelectFonteTitulo()),
    campo({ chave: "edicao.tamanho_topo", tipo: "range", min: 20, max: 200, formato: "px", rotulo: "Tamanho" }),
    campo({ chave: "edicao.cor_topo", tipo: "cor", rotulo: "Cor" }),
    estLinhaPosicao("titulo"),
  ])}</div>`;
}

function estCardLegenda() {
  const ligada = Boolean(valor("edicao.legendas"));
  return h`<div class="card"><div class="card-topo"><h3>${ic("microfone")}Legenda</h3></div>${formulario([
    campo({ chave: "edicao.legendas", tipo: "switch", rotulo: "Mostrar a legenda", ajuda: "A fala aparece em blocos de poucas palavras, com a palavra falada destacada." }),
    ...(ligada ? [
      campo({ chave: "edicao.fonte_arquivo", tipo: "fonte", fontes: (App.meta && App.meta.fontes) || [], rotulo: "Fonte" }),
      campo({ chave: "edicao.tamanho_legenda", tipo: "range", min: 20, max: 200, formato: "px", rotulo: "Tamanho" }),
      campo({ chave: "edicao.cor_legenda", tipo: "cor", rotulo: "Cor" }),
      campo({ chave: "edicao.cor_destaque", tipo: "cor", rotulo: "Cor da palavra falada" }),
      campo({ chave: "edicao.contorno_legenda", tipo: "range", min: 0, max: 20, formato: "px", rotulo: "Contorno" }),
      campo({ chave: "edicao.maiusculas", tipo: "switch", rotulo: "Tudo em maiúsculas" }),
      campo({ chave: "edicao.palavras_por_bloco", tipo: "range", min: 1, max: 12, rotulo: "Palavras por vez" }),
      campo({ chave: "edicao.max_caracteres_bloco", tipo: "range", min: 4, max: 60, rotulo: "Letras por vez, no máximo", ajuda: "Blocos compridos são quebrados, e o texto encolhe para caber." }),
      campo({ chave: "edicao.legenda_lugar", tipo: "select", opcoes: EST_LUGARES, rotulo: "Onde fica" }),
      estLinhaPosicao("legenda"),
    ] : []),
  ])}</div>`;
}

function estCardAcabamento() {
  return h`<div class="card"><div class="card-topo"><h3>${ic("ajustes")}Acabamento</h3></div>${formulario([
    campo({ chave: "edicao.barra_progresso", tipo: "switch", rotulo: "Barra de progresso", ajuda: "Uma linha que cresce embaixo do vídeo enquanto o corte passa." }),
    ...(valor("edicao.barra_progresso") ? [campo({ chave: "edicao.cor_barra", tipo: "cor", rotulo: "Cor da barra" })] : []),
    campo({ chave: "edicao.fade", tipo: "switch", rotulo: "Entrada e saída suaves", ajuda: "O vídeo aparece e some aos poucos." }),
  ])}</div>`;
}

function estControlesHtml() {
  return h`${estCardModelos()}${estCardMoldura()}${estCardVideo()}${estCardTitulo()}${estCardLegenda()}${estCardAcabamento()}`;
}

/** Seletor para achar de novo o controle que tinha o foco depois de redesenhar. */
function estChaveFoco(el) {
  if (!el || !el.dataset) return null;
  if (el.dataset.campo) return `[data-campo="${el.dataset.campo}"]`;
  if (!el.dataset.acao) return el.id ? `#${CSS.escape(el.id)}` : null;
  for (const k of ["zoom", "arquivo", "tipo", "nome", "modo"]) {
    if (el.dataset[k] !== undefined) return `[data-acao="${el.dataset.acao}"][data-${k}="${CSS.escape(el.dataset[k])}"]`;
  }
  return `[data-acao="${el.dataset.acao}"]`;
}

function estFocarDeNovo(caixa, seletores) {
  for (const sel of seletores) {
    const alvo = sel && caixa.querySelector(sel);
    if (alvo && !alvo.disabled) { alvo.focus({ preventScroll: true }); return; }
  }
}

function estRedesenharControles(preferido = null) {
  const caixa = $("#est-controles");
  if (!caixa) return;
  const ativo = document.activeElement;
  const chave = ativo && caixa.contains(ativo) ? estChaveFoco(ativo) : null;
  montar(caixa, estControlesHtml());
  estFocarDeNovo(caixa, [preferido, chave]);
}

function estAtualizarPosicoes() {
  const caixa = $("#est-controles");
  if (!caixa) return;
  const ativo = document.activeElement;
  const chave = ativo && caixa.contains(ativo) ? estChaveFoco(ativo) : null;
  for (const tipo of Object.keys(EST_CHAVE_POS)) montarSeMudou($(`#est-pos-${tipo}`), estPosicaoHtml(tipo));
  montarSeMudou($("#est-molduras"), estMoldurasHtml());
  montarSeMudou($("#est-tamanhos"), estBotoesTamanhoHtml());
  if (chave && !caixa.contains(document.activeElement)) estFocarDeNovo(caixa, [chave]);
}

/* ------------------------------------------------------------ pendências do visual */

/** Chaves do visual (as que ficam no modelo) com mudança ainda não salva. */
function estVisualPendente() {
  const pendente = App.pendente.edicao || {};
  const chaves = new Set((PgEstudio.dados && PgEstudio.dados.chaves) || []);
  return Object.keys(pendente).filter((k) => chaves.has(k));
}

function estTirarPendentes(lista) {
  const pendente = App.pendente.edicao;
  if (!pendente) return;
  for (const k of lista) delete pendente[k];
  if (!Object.keys(pendente).length) delete App.pendente.edicao;
  atualizarBarraSalvar();
}

/** Salva só as mudanças do visual (as outras telas continuam com as delas pendentes). */
async function estSalvarVisual(lista) {
  const edicao = {};
  for (const k of lista) edicao[k] = App.pendente.edicao[k];
  try {
    const r = await post("/config", { alteracoes: { edicao }, modelo_esperado: App.config.edicao.modelo });
    App.config = r.config;
    App.meta = r.meta;
    estTirarPendentes(lista);
    toast(`Mudanças salvas no modelo ${PgEstudio.dados.ativo}`);
    return true;
  } catch (e) {
    erroToast(e);
    return false;
  }
}

function estEscolher({ titulo, texto, opcoes }) {
  return new Promise((resolve) => {
    let feito = false;
    const fim = (v) => { if (!feito) { feito = true; resolve(v); } };
    const corpo = abrirModal({
      titulo,
      largura: 540,
      aoFechar: () => fim(""),
      corpo: h`<p class="texto-modal">${texto}</p><div class="acoes-modal">${opcoes.map(([v, rotulo, classe]) => h`<button type="button" class="btn ${classe}" data-escolha="${v}">${rotulo}</button>`)}</div>`,
    });
    corpo.querySelectorAll("[data-escolha]").forEach((b) => b.addEventListener("click", () => { fim(b.dataset.escolha); fecharModal(); }));
  });
}

async function estResolverPendencias(acao) {
  const lista = estVisualPendente();
  if (!lista.length) return true;
  const escolha = await estEscolher({
    titulo: "Mudanças do visual sem salvar",
    texto: `O modelo ${PgEstudio.dados.ativo} tem mudanças que ainda não foram salvas. O que fazer com elas antes de ${acao}?`,
    opcoes: [["", "Cancelar", "fantasma"], ["descartar", "Descartar", ""], ["salvar", "Salvar e continuar", "primario"]],
  });
  if (escolha === "salvar") return estSalvarVisual(lista);
  if (escolha === "descartar") { estTirarPendentes(lista); return true; }
  return false;
}

function estPedirNome(titulo, texto, inicial) {
  return new Promise((resolve) => {
    let feito = false;
    const fim = (v) => { if (!feito) { feito = true; resolve(v); } };
    abrirModal({
      titulo,
      largura: 460,
      aoFechar: () => fim(null),
      corpo: h`<p class="nota">${texto}</p><label class="campo"><span>Nome</span><input type="text" id="est-nome" maxlength="60" value="${inicial}" autocomplete="off" spellcheck="false" autofocus></label><div class="acoes-modal"><button type="button" class="btn fantasma" data-acao="fechar-modal">Cancelar</button><button type="button" class="btn primario" id="est-nome-ok">Salvar</button></div>`,
    });
    const entrada = $("#est-nome");
    const ok = () => {
      const v = entrada.value.trim();
      if (!v) { entrada.focus(); return; }
      fim(v);
      fecharModal();
    };
    $("#est-nome-ok").addEventListener("click", ok);
    entrada.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); ok(); } });
  });
}

/* ------------------------------------------------------------ página */

function estAplicarDados(d) {
  PgEstudio.dados = { ativo: d.ativo, modelos: d.modelos, molduras: d.molduras, pasta_molduras: d.pasta_molduras, chaves: d.chaves || [] };
  if (d.config) App.config = d.config;
  if (d.meta) App.meta = d.meta;
}

function estPaginaHtml() {
  const real = PgEstudio.modo === "real";
  return h`<div class="estudio-grade">
    <div class="card estudio-tela">
      <div class="card-topo">
        <div class="abas" role="group" aria-label="Visualização">
          <button type="button" class="aba ${real ? "" : "ativa"}" aria-pressed="${!real}" data-acao="est-modo" data-modo="editar">${ic("lapis")}Editar</button>
          <button type="button" class="aba ${real ? "ativa" : ""}" aria-pressed="${real}" data-acao="est-modo" data-modo="real">${ic("olho")}Como vai sair</button>
        </div>
        <label class="marcar"><input type="checkbox" id="est-guias" ${PgEstudio.guias ? raw("checked") : ""}>O que os apps cobrem</label>
      </div>
      <div id="est-visual"></div>
      <p class="nota" id="est-nota"></p>
    </div>
    <div class="estudio-controles" id="est-controles">${estControlesHtml()}</div>
  </div>`;
}

function estRedesenharTudo(preferido = null) {
  estRedesenharControles(preferido);
  estAgendarGeo();
  if (PgEstudio.modo === "real") estAgendarPrevia();
}

async function estEnviarMoldura(arquivo) {
  if (!arquivo) return;
  if (!/\.png$/i.test(arquivo.name)) { toast("A moldura precisa ser uma imagem .png", "erro"); return; }
  if (arquivo.size > 30 * 1024 * 1024) { toast("Imagem grande demais (o máximo é 30 MB)", "erro"); return; }
  let resp;
  let dados = {};
  try {
    resp = await fetch("/api/molduras/enviar", {
      method: "POST",
      headers: { "X-AutoCortes": App.token, "X-Nome-Arquivo": encodeURIComponent(arquivo.name), "Content-Type": "application/octet-stream" },
      body: arquivo,
    });
    dados = await resp.json().catch(() => ({}));
  } catch (e) {
    toast("Não consegui enviar a moldura. O AutoCortes ainda está aberto?", "erro", 7000);
    return;
  }
  if (!resp.ok) { toast(dados.erro || `Erro ${resp.status}`, "erro", 8000); return; }
  estAplicarDados({ ...dados, config: null, meta: null });
  definirValor("edicao.moldura", dados.moldura.arquivo);
  toast(`Moldura ${dados.moldura.nome} enviada. Salve para usar nos próximos cortes.`);
  estRedesenharControles(`[data-acao="est-moldura"][data-arquivo="${CSS.escape(dados.moldura.arquivo)}"]`);
}

App.acoes["est-modo"] = (el) => {
  PgEstudio.modo = el.dataset.modo === "real" ? "real" : "editar";
  document.querySelectorAll('[data-acao="est-modo"]').forEach((b) => {
    const ativa = b.dataset.modo === PgEstudio.modo;
    b.classList.toggle("ativa", ativa);
    b.setAttribute("aria-pressed", String(ativa));
  });
  montar($("#est-visual"), "");
  estDesenharVisual();
  estAtualizarNota();
  if (PgEstudio.modo === "real") estGerarPrevia();
};

App.acoes["est-zoom"] = (el) => {
  definirValor("edicao.zoom", Number(el.dataset.zoom));
  estRedesenharControles(`[data-acao="est-zoom"][data-zoom="${el.dataset.zoom}"]`);
};

App.acoes["est-pos-auto"] = (el) => {
  PgEstudio.focar = el.dataset.tipo; // o botão some: o foco vai para a camada na tela
  definirValor(EST_CHAVE_POS[el.dataset.tipo], 0);
};

App.acoes["est-moldura"] = (el) => {
  definirValor("edicao.moldura", el.dataset.arquivo || "");
};

App.acoes["est-moldura-enviar"] = () => {
  const entrada = $("#est-arquivo-moldura");
  if (entrada) { entrada.value = ""; entrada.click(); }
};

App.acoes["est-modelo-usar"] = async (el) => {
  const nome = el.dataset.nome;
  if (!(await estResolverPendencias("trocar de modelo"))) return;
  const d = await ocupado(el, () => post("/modelos/usar", { nome }));
  estTirarPendentes(estVisualPendente()); // o visual agora é o do modelo escolhido
  estAplicarDados(d);
  toast(`Os próximos cortes vão sair com o modelo ${nome}`);
  estRedesenharTudo(`[data-acao="est-modelo-renomear"][data-nome="${CSS.escape(nome)}"]`);
};

App.acoes["est-modelo-novo"] = async (el) => {
  if (!(await estResolverPendencias("criar um modelo"))) return;
  const nome = await estPedirNome("Novo modelo", `Começa igual ao ${PgEstudio.dados.ativo}. Depois é só ajustar e salvar.`, "");
  if (!nome) return;
  const d = await ocupado(el, () => post("/modelos/criar", { nome, base: PgEstudio.dados.ativo }));
  estTirarPendentes(estVisualPendente());
  estAplicarDados(d);
  toast(`Modelo ${nome} criado e em uso`);
  estRedesenharTudo(`[data-acao="est-modelo-renomear"][data-nome="${CSS.escape(nome)}"]`);
};

App.acoes["est-modelo-renomear"] = async (el) => {
  const nome = el.dataset.nome;
  const novo = await estPedirNome("Renomear modelo", `Novo nome para ${nome}.`, nome);
  if (!novo || novo === nome) return;
  const d = await ocupado(el, () => post("/modelos/renomear", { nome, novo }));
  estAplicarDados(d);
  toast("Modelo renomeado");
  estRedesenharControles(`[data-acao="est-modelo-renomear"][data-nome="${CSS.escape(novo)}"]`);
};

App.acoes["est-modelo-excluir"] = async (el) => {
  const nome = el.dataset.nome;
  const ok = await confirmar({ titulo: `Excluir o modelo ${nome}?`, texto: "Os cortes que já saíram com ele não mudam.", botao: "Excluir", perigo: true });
  if (!ok) return;
  const d = await ocupado(el, () => post("/modelos/excluir", { nome }));
  estAplicarDados(d);
  toast("Modelo excluído");
  estRedesenharControles('[data-acao="est-modelo-novo"]');
};

App.paginas.estudio = {
  titulo: "Estúdio",
  subtitulo: "O visual dos cortes: moldura, tamanho do vídeo, título e legenda",
  async render(el, token) {
    const d = await api("/estudio");
    if (!App.ativa(token)) return;
    estAplicarDados(d);
    PgEstudio.geo = null;
    PgEstudio.arrasto = null;
    PgEstudio.focar = null;
    PgEstudio.previa = { url: null, gerando: false, sujo: false, pedido: 0, erro: null };
    montar(el, estPaginaHtml());
    const visual = $("#est-visual", el);
    visual.addEventListener("pointerdown", estInicioArrasto);
    visual.addEventListener("keydown", estTeclaArrasto);
    $("#est-guias", el).addEventListener("change", (ev) => {
      PgEstudio.guias = ev.target.checked;
      estDesenharVisual();
    });
    $("#est-controles", el).addEventListener("change", (ev) => {
      if (ev.target.id === "est-arquivo-moldura") estEnviarMoldura(ev.target.files && ev.target.files[0]).catch(erroToast);
    });
    estDesenharVisual();
    await estAtualizarGeo();
    if (PgEstudio.modo === "real") estGerarPrevia();
  },
  aoMudar(caminho) {
    if (App.nomePagina !== "estudio" || !caminho.startsWith("edicao.")) return;
    if (EST_REDESENHA.has(caminho)) estRedesenharControles();
    else estAtualizarPosicoes();
    estAgendarGeo();
    if (PgEstudio.modo === "real") estAgendarPrevia();
  },
  async aoSalvar() {
    const d = await api("/estudio");
    estAplicarDados(d);
    estRedesenharTudo();
  },
  async recarregar() {
    const d = await api("/estudio");
    estAplicarDados(d);
    estRedesenharTudo();
  },
  sair() {
    PgEstudio.arrasto = null;
    PgEstudio.pedidoGeo++;
    PgEstudio.previa.pedido++;
    estAgendarGeo.cancelar();
    estAgendarPrevia.cancelar();
  },
};
