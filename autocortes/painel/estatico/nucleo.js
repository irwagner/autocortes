/* AutoCortes - painel: núcleo (API, HTML seguro, ícones, formatação, janelas e campos) */
"use strict";

const App = {
  token: (document.querySelector('meta[name="ac-token"]') || {}).content || "",
  paginas: {},
  acoes: {},
  estado: null,
  config: null,
  meta: null,
  pendente: {},
  rotulos: {},
  pagina: null,
  nomePagina: "",
  navegacao: 0,
  offline: false,
  encerrado: false,
  enviosAtivos: false,
  ativa(token) { return token === this.navegacao; },
};

const LIMPAR = "__limpar__";
const REDES = {
  youtube: { rotulo: "YouTube", nome: "YouTube Shorts", cor: "#ff3b3b" },
  tiktok: { rotulo: "TikTok", nome: "TikTok", cor: "#25f4ee" },
  instagram: { rotulo: "Instagram", nome: "Instagram Reels", cor: "#e1306c" },
  kwai: { rotulo: "Kwai", nome: "Kwai", cor: "#ff7a00" },
  bilibili: { rotulo: "Bilibili", nome: "Bilibili", cor: "#00a1d6" },
  // sem agenda própria: o Reel do Instagram repetido na Página
  facebook: { rotulo: "Facebook", nome: "Página do Facebook", cor: "#1877f2" },
};
const ORDEM_REDES = ["youtube", "tiktok", "instagram", "kwai", "bilibili"];
const ROTULO_ENVIO = { oficial: "API oficial", upload_post: "via Upload-Post", manual: "à mão" };
const DIAS = [["seg", "Seg"], ["ter", "Ter"], ["qua", "Qua"], ["qui", "Qui"], ["sex", "Sex"], ["sab", "Sáb"], ["dom", "Dom"]];
const IDIOMAS = [
  ["", "Detectar automaticamente"], ["pt", "Português"], ["en", "Inglês"], ["es", "Espanhol"], ["fr", "Francês"],
  ["it", "Italiano"], ["de", "Alemão"], ["ja", "Japonês"], ["ko", "Coreano"], ["zh", "Chinês"], ["ru", "Russo"],
];

/* ------------------------------------------------------------ HTML seguro */
class Html { constructor(t) { this.t = t; } toString() { return this.t; } }
const raw = (t) => new Html(t === null || t === undefined ? "" : String(t));
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (v) => String(v).replace(/[&<>"']/g, (c) => ESC[c]);

function paraHtml(v) {
  if (v === null || v === undefined || v === false) return "";
  if (v instanceof Html) return v.t;
  if (Array.isArray(v)) return v.map(paraHtml).join("");
  return esc(v);
}
function h(partes, ...valores) {
  let s = partes[0];
  for (let i = 0; i < valores.length; i++) s += paraHtml(valores[i]) + partes[i + 1];
  return new Html(s);
}
function montar(el, html) { if (el) el.innerHTML = paraHtml(html); }
function elementoDe(html) {
  const t = document.createElement("template");
  t.innerHTML = paraHtml(html).trim();
  return t.content.firstElementChild;
}
const $ = (seletor, raiz = document) => raiz.querySelector(seletor);

/* ------------------------------------------------------------ ícones */
const ICONES = {
  casa: '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
  calendario: '<rect x="3" y="4.5" width="18" height="16.5" rx="2.5"/><path d="M3 9.5h18M8 2.5v4M16 2.5v4"/>',
  tesoura: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.1 15.9M14.5 14.5 20 20M8.1 8.1 12 12"/>',
  filme: '<rect x="3" y="3" width="18" height="18" rx="2.5"/><path d="M7 3v18M17 3v18M3 7.5h4M3 12h18M3 16.5h4M17 7.5h4M17 16.5h4"/>',
  rede: '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.6 13.5 6.8 4M15.4 6.5l-6.8 4"/>',
  ajustes: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
  terminal: '<path d="m4 17 6-5-6-5M12 19h8"/>',
  play: '<path d="M7 4.5v15l12-7.5z" fill="currentColor" stroke="none"/>',
  pausa: '<path d="M8 5v14M16 5v14" stroke-width="3"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  lixo: '<path d="M3 6h18M8 6V4h8v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
  refazer: '<path d="M21 12a9 9 0 0 1-15.5 6.3L3 16M3 12a9 9 0 0 1 15.5-6.3L21 8M21 3v5h-5M3 21v-5h5"/>',
  estrela: '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.3 6.4 20.2l1.1-6.2L3 9.6l6.2-.9z"/>',
  enviar: '<path d="M12 16V4M7 9l5-5 5 5M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>',
  pasta: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  relogio: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  alerta: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
  erro: '<circle cx="12" cy="12" r="9"/><path d="m15 9-6 6M9 9l6 6"/>',
  ok: '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
  raio: '<path d="M13 2 3 14h9l-1 8 10-12h-9z"/>',
  lapis: '<path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  externo: '<path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  mais: '<path d="M12 5v14M5 12h14"/>',
  copiar: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  olho: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  oculto: '<path d="M9.9 4.2A10 10 0 0 1 12 4c6.5 0 10 8 10 8a17 17 0 0 1-2.2 3.3M6.6 6.6A17 17 0 0 0 2 12s3.5 8 10 8a9.7 9.7 0 0 0 5.4-1.6M2 2l20 20M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
  baixar: '<path d="M12 4v12M7 11l5 5 5-5M4 20h16"/>',
  varinha: '<path d="M15 4V2M15 16v-2M8 9h2M20 9h2M17.8 11.8 19 13M15 9h.01M17.8 6.2 19 5M3 21l9-9M12.2 6.2 11 5"/>',
  lista: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  aviao: '<path d="m22 2-7 20-4-9-9-4z"/><path d="M22 2 11 13"/>',
  sair: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
  chave: '<circle cx="7.5" cy="15.5" r="5.5"/><path d="m21 2-9.6 9.6M15.5 7.5l3 3L22 7l-3-3"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
  imagem: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
  texto: '<path d="M4 7V4h16v3M9 20h6M12 4v16"/>',
  microfone: '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M19 10v1a7 7 0 0 1-14 0v-1M12 18v4M8 22h8"/>',
  cpu: '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
  video: '<rect x="2" y="6" width="14" height="12" rx="2"/><path d="m22 8-6 4 6 4z"/>',
  caixa: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1z"/>',
  "seta-dir": '<path d="m9 18 6-6-6-6"/>',
  interruptor: '<rect x="2" y="6" width="20" height="12" rx="6"/><circle cx="16" cy="12" r="3"/>',
  grafico: '<path d="M3 21h18M7 17V10M12 17V5M17 17v-4"/>',
  busca: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
};
function ic(nome, classe = "") {
  return raw(`<svg class="ic ${classe}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${ICONES[nome] || ""}</svg>`);
}

// Marca do TikTok: caminho do Simple Icons (CC0)
const TIKTOK_PATH = "M12.525.02c1.31-.02 2.61-.01 3.91-.02.08 1.53.63 3.09 1.75 4.17 1.12 1.11 2.7 1.62 4.24 1.79v4.03c-1.44-.05-2.89-.35-4.2-.97-.57-.26-1.1-.59-1.62-.93-.01 2.92.01 5.84-.02 8.75-.08 1.4-.54 2.79-1.35 3.94-1.31 1.92-3.58 3.17-5.91 3.21-1.43.08-2.86-.31-4.08-1.03-2.02-1.19-3.44-3.37-3.65-5.71-.02-.5-.03-1-.01-1.49.18-1.9 1.12-3.72 2.58-4.96 1.66-1.44 3.98-2.13 6.15-1.72.02 1.48-.04 2.96-.04 4.44-.99-.32-2.15-.23-3.02.37-.63.41-1.11 1.04-1.36 1.75-.21.51-.15 1.07-.14 1.61.24 1.64 1.82 3.02 3.5 2.87 1.12-.01 2.19-.66 2.77-1.61.19-.33.4-.67.41-1.06.1-1.79.06-3.57.07-5.36.01-4.03-.01-8.05.02-12.07z";
const LOGOS = {
  youtube: '<rect x="1.5" y="4.8" width="21" height="14.4" rx="4.4" fill="#ff0033"/><path d="M10 9.1v5.8l5-2.9z" fill="#fff"/>',
  tiktok: `<g transform="translate(12 12) scale(.8) translate(-12 -12)"><path d="${TIKTOK_PATH}" fill="#25f4ee" transform="translate(-.9 -.6)"/><path d="${TIKTOK_PATH}" fill="#fe2c55" transform="translate(.9 .6)"/><path d="${TIKTOK_PATH}" fill="#fff"/></g>`,
  instagram: '<rect x="3" y="3" width="18" height="18" rx="5.5" fill="none" stroke="url(#grad-ig)" stroke-width="2.2"/><circle cx="12" cy="12" r="4.1" fill="none" stroke="url(#grad-ig)" stroke-width="2.2"/><circle cx="17.2" cy="6.8" r="1.25" fill="#e1306c"/>',
  kwai: '<rect x="2" y="2" width="20" height="20" rx="6" fill="#ff7a00"/><circle cx="7.6" cy="6.9" r="1.7" fill="#fff"/><circle cx="11.8" cy="6.9" r="1.7" fill="#fff"/><rect x="5.2" y="9.3" width="9.4" height="7.4" rx="2" fill="#fff"/><path d="M15.4 12 18.8 9.9v6.2L15.4 14z" fill="#fff"/>',
  bilibili: '<path d="M7.2 3.2 9.6 5.7M16.8 3.2 14.4 5.7" stroke="#00a1d6" stroke-width="1.9" stroke-linecap="round"/><rect x="2.6" y="6.2" width="18.8" height="14" rx="3.6" fill="none" stroke="#00a1d6" stroke-width="2.1"/><path d="M8.3 11.4 9.4 13.6M15.7 11.4 14.6 13.6" stroke="#00a1d6" stroke-width="2" stroke-linecap="round"/>',
  facebook: '<circle cx="12" cy="12" r="11" fill="#1877f2"/><path d="M9.1 24v-8.3H6.6V12h2.5v-1.6c0-4.1 1.8-6 5.9-6 .8 0 2 .1 2.6.3v3.3c-.4 0-1-.1-1.4-.1-1.6 0-2.4.6-2.4 2.4V12h3.9l-.7 3.7h-3.2V24z" fill="#fff" clip-path="url(#corte-fb)"/>',
};
function logoRede(rede, classe = "") {
  return raw(`<svg class="rede-logo ${classe}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${LOGOS[rede] || ""}</svg>`);
}

/* ------------------------------------------------------------ formatação */
const DIAS_CURTOS = ["dom", "seg", "ter", "qua", "qui", "sex", "sáb"];
const DIAS_LONGOS = ["Domingo", "Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado"];
const dois = (n) => String(n).padStart(2, "0");

function inicioDia(ms) { const d = new Date(ms); d.setHours(0, 0, 0, 0); return d; }
function fmtHora(ts) { const d = new Date(ts * 1000); return `${dois(d.getHours())}:${dois(d.getMinutes())}`; }
function rotuloDia(ts) {
  const d = inicioDia(ts * 1000);
  const dif = Math.round((d - inicioDia(Date.now())) / 864e5);
  if (dif === 0) return "hoje";
  if (dif === 1) return "amanhã";
  if (dif === -1) return "ontem";
  return `${DIAS_CURTOS[d.getDay()]} ${dois(d.getDate())}/${dois(d.getMonth() + 1)}`;
}
const fmtQuando = (ts) => `${rotuloDia(ts)}, ${fmtHora(ts)}`;
function fmtRelativo(ts) {
  const s = Math.round(ts - Date.now() / 1000);
  const abs = Math.abs(s);
  let t;
  if (abs < 60) t = "instantes";
  else if (abs < 3600) t = `${Math.round(abs / 60)} min`;
  else if (abs < 86400) {
    const hh = Math.floor(abs / 3600);
    const mm = Math.round((abs % 3600) / 60);
    t = mm ? `${hh} h ${mm} min` : `${hh} h`;
  } else {
    const d = Math.round(abs / 86400);
    t = d === 1 ? "1 dia" : `${d} dias`;
  }
  return s >= 0 ? `em ${t}` : `há ${t}`;
}
function fmtDuracao(seg) { const s = Math.round(seg || 0); return s < 60 ? `${s} s` : `${Math.floor(s / 60)}:${dois(s % 60)}`; }
function fmtTempo(seg) {
  const s = Math.max(0, Math.round(seg || 0));
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  return hh ? `${hh}:${dois(mm)}:${dois(ss)}` : `${mm}:${dois(ss)}`;
}
function fmtBytes(n) {
  if (!n) return "—";
  const unidades = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < unidades.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i >= 2 ? 1 : 0).replace(".", ",")} ${unidades[i]}`;
}
const fmtNum = (n, casas = 0) => Number(n || 0).toLocaleString("pt-BR", { maximumFractionDigits: casas });
function fmtDias(d) {
  if (d === null || d === undefined) return "—";
  if (d < 1) return "menos de 1 dia";
  const n = Math.floor(d);
  return `${n} ${n === 1 ? "dia" : "dias"}`;
}
const plural = (n, um, varios) => `${fmtNum(n)} ${n === 1 ? um : varios}`;
const urlSegura = (u) => (typeof u === "string" && /^https:\/\//i.test(u) ? u : null);
function midia(url) { return url ? `${url}${url.includes("?") ? "&" : "?"}t=${encodeURIComponent(App.token)}` : ""; }
function formatar(formato, v) {
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  if (formato === "x") return `${n.toFixed(2).replace(".", ",")}x`;
  if (formato === "pct") return `${Math.round(n * 100)}%`;
  if (formato === "px") return `${Math.round(n)} px`;
  if (formato === "dec1") return n.toFixed(1).replace(".", ",");
  if (formato === "min") return `${Math.round(n)} min`;
  if (formato === "s") return `${Math.round(n)} s`;
  return String(Math.round(n));
}

/** Troca o conteúdo só quando o HTML muda (evita piscar e perder cliques nas atualizações ao vivo). */
function montarSeMudou(el, html) {
  if (!el) return false;
  const s = paraHtml(html);
  if (el._htmlAtual === s) return false;
  el._htmlAtual = s;
  el.innerHTML = s;
  return true;
}

function adiar(fn, ms) {
  let t = null;
  const f = (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  f.cancelar = () => clearTimeout(t); // ao sair da página, nada dispara depois
  return f;
}

function consultaDaRota() {
  return new URLSearchParams(location.hash.split("?")[1] || "");
}

/* ------------------------------------------------------------ estados */
const ESTADO_POST = {
  publicado: ["Publicado", "ok"], simulado: ["Simulado", "info"], falhou: ["Falhou", "erro"],
  pulado: ["Recusado", "aviso"], interrompido: ["Interrompido", "aviso"], enviando: ["Enviando", "acento"],
  aguardando: ["Postar à mão", "acento"],
};
const estadoPost = (estado) => ESTADO_POST[estado] || [estado || "?", "neutro"];
const ESTADO_HORARIO = {
  agendado: ["Agendado", "neutro"], pendente: ["Agora", "acento"], perdido: ["Não postado", "aviso"], pausado: ["Pausado", "aviso"],
};
const ESTADO_FILME = {
  novo: ["Na fila", "neutro"], analisando: ["Analisando", "acento"], analisado: ["Analisado", "ok"],
  erro: ["Erro", "erro"], ausente: ["Arquivo não encontrado", "aviso"], ignorado: ["Ignorado", "neutro"],
};
const ESTADO_CORTE = {
  candidato: ["Trecho encontrado", "neutro"], renderizando: ["Editando", "acento"], revisao: ["Aguardando aprovação", "aviso"],
  pronto: ["Na fila", "info"], concluido: ["Publicado", "ok"], erro: ["Erro", "erro"], descartado: ["Descartado", "neutro"],
};
function badge(par, mini = false) {
  const [texto, tipo] = par || ["?", "neutro"];
  return h`<span class="badge ${tipo}${mini ? " mini" : ""}">${texto}</span>`;
}
function vazioBloco(icone, titulo, texto = "", grande = false) {
  return h`<div class="vazio${grande ? " grande" : ""}">${ic(icone)}<b>${titulo}</b>${texto}</div>`;
}
function miniaturaHtml(url, icone = "tesoura") {
  return h`<span class="miniatura">${url ? h`<img src="${midia(url)}" alt="" loading="lazy">` : ic(icone)}</span>`;
}

/* ------------------------------------------------------------ API */
class ErroApi extends Error {
  constructor(mensagem, status, dados) { super(mensagem); this.status = status; this.dados = dados || {}; }
}
async function api(caminho, corpo) {
  const init = { method: corpo === undefined ? "GET" : "POST", headers: { "X-AutoCortes": App.token } };
  if (corpo !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(corpo);
  }
  let resp;
  try {
    resp = await fetch("/api" + caminho, init);
  } catch (e) {
    marcarOffline(true);
    throw new ErroApi("Sem conexão com o AutoCortes. Ele ainda está aberto?", 0);
  }
  marcarOffline(false);
  let dados = {};
  try { dados = await resp.json(); } catch (e) { dados = {}; }
  if (!resp.ok) {
    if (resp.status === 403 && /Sessão/.test(dados.erro || "")) mostrarFaixa("sessao");
    throw new ErroApi(dados.erro || `Erro ${resp.status}`, resp.status, dados);
  }
  return dados;
}
const post = (caminho, corpo = {}) => api(caminho, corpo);

function mostrarFaixa(tipo) {
  const el = $("#faixa");
  if (tipo === "offline") {
    montar(el, h`<div class="faixa">${ic("alerta")}<span>Não consegui falar com o AutoCortes. Se você fechou a janela dele, abra de novo pelo <b>AutoCortes.bat</b>.</span><button class="btn pequeno" data-acao="reconectar">Tentar de novo</button></div>`);
  } else if (tipo === "sessao") {
    montar(el, h`<div class="faixa">${ic("alerta")}<span>O AutoCortes foi reiniciado. Recarregue a página para continuar.</span><button class="btn pequeno" data-acao="recarregar">Recarregar</button></div>`);
  } else {
    montar(el, "");
  }
}
function marcarOffline(sim) {
  if (sim === App.offline) return;
  App.offline = sim;
  mostrarFaixa(sim ? "offline" : "");
}

/* ------------------------------------------------------------ avisos e janelas */
function toast(texto, tipo = "ok", ms = 4500) {
  const icone = tipo === "erro" ? "erro" : tipo === "info" ? "info" : "ok";
  const el = elementoDe(h`<div class="toast ${tipo}">${ic(icone)}<span>${texto}</span></div>`);
  $("#toasts").appendChild(el);
  setTimeout(() => { el.classList.add("saindo"); setTimeout(() => el.remove(), 350); }, ms);
}
function erroToast(e) {
  if (e && e.dados && Array.isArray(e.dados.erros) && e.dados.erros.length) return mostrarErros(e.dados.erros);
  toast((e && e.message) || String(e), "erro", 7000);
}
async function ocupado(botao, trabalho) {
  if (!botao) return trabalho();
  botao.classList.add("carregando");
  botao.disabled = true;
  try { return await trabalho(); } finally { botao.classList.remove("carregando"); botao.disabled = false; }
}

const modal = { aberto: false, aoFechar: null, foco: null };
function abrirModal({ titulo, corpo = "", largura = 640, aoFechar = null }) {
  fecharModal(true);
  modal.foco = document.activeElement;
  modal.aoFechar = aoFechar;
  montar($("#modal-raiz"), h`<div class="modal-fundo" data-fundo="modal"><div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-titulo" style="width:min(${largura}px, 96vw)"><div class="modal-topo"><h2 id="modal-titulo">${titulo}</h2><button class="btn fantasma icone" data-acao="fechar-modal" aria-label="Fechar">${ic("x")}</button></div><div class="modal-corpo" id="modal-corpo">${corpo}</div></div></div>`);
  modal.aberto = true;
  setTimeout(() => {
    const alvo = $("#modal-raiz [autofocus]") || $("#modal-raiz .modal-corpo input, #modal-raiz .modal-corpo textarea")
      || $("#modal-raiz [data-acao=fechar-modal]");
    if (alvo) alvo.focus();
  }, 30);
  return corpoModal();
}
function fecharModal(silencioso = false) {
  if (!modal.aberto) return;
  modal.aberto = false;
  const fn = modal.aoFechar;
  modal.aoFechar = null;
  document.querySelectorAll("#modal-raiz video").forEach((v) => { try { v.pause(); } catch (e) { /* ignora */ } });
  montar($("#modal-raiz"), "");
  if (fn) { try { fn(); } catch (e) { console.error(e); } }
  if (!silencioso && modal.foco && modal.foco.isConnected) modal.foco.focus();
}
const corpoModal = () => $("#modal-corpo");
function tituloModal(texto) { const el = $("#modal-titulo"); if (el) el.textContent = texto; }

function confirmar({ titulo, texto, botao = "Confirmar", perigo = false, cancelar = "Cancelar" }) {
  return new Promise((resolve) => {
    const caixa = elementoDe(h`<div class="modal-fundo sobreposto" data-fundo="confirmar"><div class="modal" role="alertdialog" aria-modal="true" aria-labelledby="confirmar-titulo" style="width:min(480px, 96vw)"><div class="modal-topo"><h2 id="confirmar-titulo">${titulo}</h2></div><div class="modal-corpo"><p class="texto-modal">${texto}</p><div class="acoes-modal"><button class="btn fantasma" data-resposta="nao">${cancelar}</button><button class="btn ${perigo ? "perigo" : "primario"}" data-resposta="sim">${botao}</button></div></div></div></div>`);
    const anterior = document.activeElement;
    const tecla = (e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); fim(false); } };
    const fim = (sim) => {
      caixa.remove();
      document.removeEventListener("keydown", tecla, true);
      if (anterior && anterior.isConnected) anterior.focus();
      resolve(sim);
    };
    caixa.addEventListener("click", (e) => {
      const b = e.target.closest("[data-resposta]");
      if (b) fim(b.dataset.resposta === "sim");
      else if (e.target === caixa) fim(false);
    });
    document.addEventListener("keydown", tecla, true);
    document.body.appendChild(caixa);
    caixa.querySelector("[data-resposta=sim]").focus();
  });
}

function mostrarErros(erros) {
  const legiveis = erros.map((t) => String(t).replace(/\[(\w+)\]\.(\w+)/g, (m, s, k) => {
    const rotulo = App.rotulos[`${s}.${k}`];
    return rotulo ? `"${rotulo}"` : m;
  }));
  abrirModal({
    titulo: "Não deu para salvar",
    largura: 560,
    corpo: h`<p class="texto-modal">Corrija estes pontos e tente de novo:</p><ul class="lista-erros">${legiveis.map((t) => h`<li>${t}</li>`)}</ul><div class="acoes-modal"><button class="btn primario" data-acao="fechar-modal">Entendi</button></div>`,
  });
}

/* ------------------------------------------------------------ configuração pendente */
function obter(obj, caminho) {
  return caminho.split(".").reduce((o, k) => (o === null || o === undefined ? undefined : o[k]), obj);
}
function vazio(o) {
  if (o === null || typeof o !== "object" || Array.isArray(o)) return false;
  return Object.values(o).every(vazio);
}
function valor(caminho) {
  const p = obter(App.pendente, caminho);
  return p !== undefined ? p : obter(App.config, caminho);
}
function definirPendente(caminho, v) {
  const partes = caminho.split(".");
  let o = App.pendente;
  for (const p of partes.slice(0, -1)) {
    if (o[p] === null || typeof o[p] !== "object") o[p] = {};
    o = o[p];
  }
  o[partes[partes.length - 1]] = v;
}
function removerPendente(caminho) {
  const partes = caminho.split(".");
  let o = App.pendente;
  for (const p of partes.slice(0, -1)) { o = o[p]; if (!o) return; }
  delete o[partes[partes.length - 1]];
}
function definirValor(caminho, v) {
  if (JSON.stringify(obter(App.config, caminho)) === JSON.stringify(v)) removerPendente(caminho);
  else definirPendente(caminho, v);
  atualizarBarraSalvar();
  if (App.pagina && App.pagina.aoMudar) App.pagina.aoMudar(caminho, v);
}
function contarPendentes(o = App.pendente) {
  let n = 0;
  for (const v of Object.values(o)) n += v !== null && typeof v === "object" && !Array.isArray(v) ? contarPendentes(v) : 1;
  return n;
}
function atualizarBarraSalvar() {
  const barra = $("#barra-salvar");
  if (!barra) return;
  const n = contarPendentes();
  barra.classList.toggle("visivel", n > 0);
  const texto = $("#barra-salvar-texto");
  if (texto) texto.textContent = n === 1 ? "1 alteração não salva" : `${n} alterações não salvas`;
}
const ROTULOS_REINICIO = { "painel.porta": "a porta do painel", "geral.pasta_dados": "a pasta de dados" };
const rotuloReinicio = (chave) => ROTULOS_REINICIO[chave] || chave;

async function carregarConfig() {
  const r = await api("/config");
  App.config = r.config;
  App.meta = r.meta;
  return r;
}
async function garantirConfig() { if (!App.config || !App.meta) await carregarConfig(); }
async function salvarConfig() {
  if (vazio(App.pendente)) return true;
  try {
    // o servidor recusa mudanças do visual feitas para outro modelo (trocado em outra janela)
    const modelo = App.config && App.config.edicao ? App.config.edicao.modelo : undefined;
    const r = await post("/config", { alteracoes: App.pendente, modelo_esperado: App.pendente.edicao ? modelo : undefined });
    App.config = r.config;
    App.meta = r.meta;
    App.pendente = {};
    atualizarBarraSalvar();
    toast("Configurações salvas");
    if (r.reiniciar && r.reiniciar.length) {
      toast(`Feche e abra o AutoCortes de novo para aplicar: ${r.reiniciar.map(rotuloReinicio).join(" e ")}.`, "info", 9000);
    }
    if (App.pagina && App.pagina.aoSalvar) await App.pagina.aoSalvar();
    if (typeof atualizarEstado === "function") atualizarEstado();
    return true;
  } catch (e) {
    erroToast(e);
    return false;
  }
}

/* ------------------------------------------------------------ campos de formulário */
const mesmoArquivo = (a, b) => String(a).replace(/\\/g, "/").toLowerCase() === String(b).replace(/\\/g, "/").toLowerCase();

const CONTROLES = {
  switch: ({ chave, rotulo }) => h`<label class="switch"><input type="checkbox" data-campo="${chave}" data-tipo="switch" ${valor(chave) ? raw("checked") : ""} aria-label="${rotulo}"><span></span></label>`,
  numero: ({ chave, rotulo, min, max, passo = 1, sufixo }) => h`<input type="number" class="curto" data-campo="${chave}" data-tipo="numero" value="${valor(chave)}" min="${min ?? ""}" max="${max ?? ""}" step="${passo}" aria-label="${rotulo}">${sufixo ? h`<span class="sufixo">${sufixo}</span>` : ""}`,
  range: ({ chave, rotulo, min, max, passo = 1, formato = "" }) => h`<div class="range"><input type="range" data-campo="${chave}" data-tipo="range" data-formato="${formato}" value="${valor(chave)}" min="${min}" max="${max}" step="${passo}" aria-label="${rotulo}"><output>${formatar(formato, valor(chave))}</output></div>`,
  texto: ({ chave, rotulo, placeholder = "", id = "" }) => h`<input type="text" ${id ? raw(`id="${id}"`) : ""} data-campo="${chave}" data-tipo="texto" value="${valor(chave) ?? ""}" placeholder="${placeholder}" aria-label="${rotulo}" spellcheck="false" autocomplete="off">`,
  textarea: ({ chave, rotulo, linhas = 3, placeholder = "", id = "" }) => h`<textarea ${id ? raw(`id="${id}"`) : ""} data-campo="${chave}" data-tipo="texto" rows="${linhas}" placeholder="${placeholder}" aria-label="${rotulo}" spellcheck="false">${valor(chave) ?? ""}</textarea>`,
  senha: ({ chave, rotulo }) => {
    const pendente = obter(App.pendente, chave);
    const salvo = App.config && App.config._segredos && App.config._segredos[chave];
    const limpar = pendente === LIMPAR;
    const dica = limpar ? "Será removida ao salvar" : salvo ? "•••••••• salva (digite para trocar)" : "Cole aqui";
    return h`<div class="segredo"><input type="password" data-campo="${chave}" data-tipo="senha" autocomplete="off" value="${limpar || pendente === undefined ? "" : pendente}" placeholder="${dica}" aria-label="${rotulo}">${salvo && !limpar ? h`<button type="button" class="link pequeno" data-acao="limpar-segredo" data-chave="${chave}">Remover a chave salva</button>` : ""}</div>`;
  },
  select: ({ chave, rotulo, opcoes, numero = false }) => {
    const atual = String(valor(chave) ?? "");
    const lista = opcoes.some(([v]) => String(v) === atual) ? opcoes : [...opcoes, [atual, atual || "(vazio)"]];
    return h`<select data-campo="${chave}" data-tipo="select" ${numero ? raw("data-numero") : ""} aria-label="${rotulo}">${lista.map(([v, r]) => h`<option value="${v}" ${String(v) === atual ? raw("selected") : ""}>${r}</option>`)}</select>`;
  },
  fonte: ({ chave, rotulo, fontes = [] }) => {
    const atual = String(valor(chave) || "");
    const lista = fontes.some((f) => mesmoArquivo(f.arquivo, atual)) ? fontes : [...fontes, { arquivo: atual, nome: valor("edicao.fonte_nome") || atual }];
    return h`<select data-campo="${chave}" data-tipo="fonte" aria-label="${rotulo}">${lista.map((f) => h`<option value="${f.arquivo}" data-nome="${f.nome}" ${mesmoArquivo(f.arquivo, atual) ? raw("selected") : ""}>${f.nome}</option>`)}</select>`;
  },
  cor: ({ chave, rotulo }) => {
    const v = String(valor(chave) || "FFFFFF").replace("#", "").toUpperCase();
    return h`<div class="cor"><input type="color" data-campo="${chave}" data-tipo="cor" value="#${v.toLowerCase()}" aria-label="${rotulo}"><code>#${v}</code></div>`;
  },
  chips: (def) => chipsHtml(def),
};

function chipsHtml({ chave, placeholder = "Digite e tecle Enter", prefixo = "" }) {
  const itens = valor(chave) || [];
  return h`<div class="chips" data-chips="${chave}" data-prefixo="${prefixo}" data-dica="${placeholder}">${itens.map((t, i) => h`<span class="chip">${t}<button type="button" data-acao="chip-remover" data-indice="${i}" aria-label="Remover ${t}">${ic("x")}</button></span>`)}<input type="text" data-chips-entrada placeholder="${placeholder}" aria-label="Adicionar item" spellcheck="false" autocomplete="off"></div>`;
}
function redesenharChips(caixa, focar) {
  const novo = elementoDe(chipsHtml({ chave: caixa.dataset.chips, placeholder: caixa.dataset.dica, prefixo: caixa.dataset.prefixo }));
  caixa.replaceWith(novo);
  if (focar) novo.querySelector("[data-chips-entrada]").focus();
}
function adicionarChips(caixa, texto, focar = true) {
  if (!caixa) return;
  const chave = caixa.dataset.chips;
  const prefixo = caixa.dataset.prefixo || "";
  const novos = texto.split(/[,;\n]/).map((s) => s.trim()).filter(Boolean)
    .map((s) => (prefixo ? prefixo + s.replace(/^#+/, "").replace(/\s+/g, "") : s))
    .filter((s) => s !== prefixo);
  if (!novos.length) return;
  const atual = [...(valor(chave) || [])];
  for (const n of novos) if (!atual.some((a) => a.toLowerCase() === n.toLowerCase())) atual.push(n);
  definirValor(chave, atual);
  redesenharChips(caixa, focar);
}

function linhaCampo(rotulo, ajuda, controle, larga = false) {
  return h`<div class="linha-campo${larga ? " larga" : ""}"><div class="rotulo"><b>${rotulo}</b>${ajuda ? h`<small>${ajuda}</small>` : ""}</div><div class="controle">${controle}</div></div>`;
}
function campo(def) {
  App.rotulos[def.chave] = def.rotulo;
  const controle = (CONTROLES[def.tipo || "texto"] || CONTROLES.texto)(def);
  return linhaCampo(def.rotulo, def.ajuda, h`${controle}${def.depois || ""}`, def.larga);
}
const secao = (titulo) => h`<div class="form-secao">${titulo}</div>`;
const formulario = (itens) => h`<div class="form">${itens}</div>`;
