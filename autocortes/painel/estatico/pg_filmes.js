/* AutoCortes - painel: Filmes (envio por arrastar e soltar, análise e dados de cada filme) */
"use strict";

const PgFilmes = { dados: null, ultimaAssinatura: "", enviando: 0 };

function descricaoLegenda(f) {
  const fonte = String(f.legenda || "");
  if (fonte.startsWith("arquivo")) return "legenda do arquivo .srt";
  if (fonte.startsWith("embutida")) return "legenda embutida no vídeo";
  if (fonte === "whisper") return `transcrição automática${f.idioma ? ` (${f.idioma})` : ""}`;
  if (fonte) return "sem legenda";
  return "";
}

function atividadeDoFilme(f) {
  const atividades = (App.estado && App.estado.atividades) || [];
  return atividades.find((a) => a.texto.includes(`'${f.titulo}'`)) || null;
}

function filmeHtml(f) {
  const estado = ESTADO_FILME[f.status] || [f.status, "neutro"];
  const c = f.cortes;
  const numeros = [];
  if (f.duracao) numeros.push(h`<span>Duração <b>${fmtTempo(f.duracao)}</b></span>`);
  if (f.tamanho) numeros.push(h`<span><b>${fmtBytes(f.tamanho)}</b></span>`);
  if (f.status === "analisado" || c.candidatos || c.prontos || c.publicados) {
    numeros.push(h`<span>Trechos <b>${c.candidatos}</b></span>`);
    if (c.revisao) numeros.push(h`<span>Aguardando aprovação <b>${c.revisao}</b></span>`);
    numeros.push(h`<span>Na fila <b>${c.prontos}</b></span>`);
    numeros.push(h`<span>Publicados <b>${c.publicados}</b></span>`);
  }
  const legenda = descricaoLegenda(f);
  if (legenda) numeros.push(h`<span>${legenda}</span>`);
  const atividade = f.status === "analisando" ? atividadeDoFilme(f) : null;
  let andamento = "";
  if (f.status === "analisando") {
    const pct = atividade && atividade.pct !== null ? atividade.pct : null;
    andamento = h`<div class="filme-progresso"><span class="giro"></span><span>${atividade ? atividade.texto : "Analisando..."}</span>${pct !== null ? h`<div class="barra"><i style="width:${pct}%"></i></div><b>${Math.round(pct)}%</b>` : ""}</div>`;
  } else if (f.status === "novo") {
    andamento = h`<div class="filme-progresso texto-suave">${ic("relogio")}<span>Na fila para análise${App.estado && !App.estado.motor.rodando ? " (o motor está desligado)" : ""}</span></div>`;
  }
  const esgotado = f.esgotado && f.status === "analisado" ? badge(["Todos os trechos aproveitados", "neutro"], true) : "";
  const botoes = [];
  if (c.candidatos || c.prontos || c.publicados || c.revisao) botoes.push(h`<a class="btn pequeno" href="#/cortes?filme=${f.id}">${ic("tesoura")}Ver cortes</a>`);
  botoes.push(h`<button class="btn pequeno" data-acao="filme-dados" data-filme="${f.id}">${ic("lapis")}Dados</button>`);
  if (f.status !== "analisando" && f.status !== "ausente") botoes.push(h`<button class="btn pequeno" data-acao="filme-reanalisar" data-filme="${f.id}">${ic("refazer")}Reanalisar</button>`);
  if (f.status === "ignorado") botoes.push(h`<button class="btn pequeno" data-acao="filme-acao" data-tipo="reativar" data-filme="${f.id}">${ic("play")}Reativar</button>`);
  else if (f.status !== "ausente") botoes.push(h`<button class="btn pequeno fantasma" data-acao="filme-acao" data-tipo="ignorar" data-filme="${f.id}" title="Parar de usar este filme">${ic("pausa")}Ignorar</button>`);
  botoes.push(h`<button class="btn pequeno icone fantasma" data-acao="filme-acao" data-tipo="mostrar" data-filme="${f.id}" title="Mostrar no Explorer" aria-label="Mostrar no Explorer">${ic("pasta")}</button>`);
  return h`<div class="card filme">
    <div class="filme-capa">${f.miniatura ? h`<img src="${midia(f.miniatura)}" alt="" loading="lazy">` : ic("filme")}</div>
    <div class="filme-info">
      <div class="filme-titulo"><b>${f.titulo}</b>${f.ano ? h`<span class="ano">${f.ano}</span>` : ""}${badge(estado, true)}${esgotado}</div>
      <div class="arquivo" title="${f.arquivo}">${f.arquivo}</div>
      ${numeros.length ? h`<div class="filme-numeros">${numeros}</div>` : ""}
      ${andamento}
      ${f.status === "erro" && f.erro ? h`<p class="erro-texto">${f.erro}</p>` : ""}
    </div>
    <div class="filme-acoes">${botoes}</div>
  </div>`;
}

function listaFilmesHtml(r) {
  if (!r.filmes.length) {
    return vazioBloco("filme", "Nenhum filme ainda", "Arraste um filme para a área acima ou coloque os arquivos na pasta de filmes. O AutoCortes procura arquivos novos a cada poucos minutos.", true);
  }
  return h`<div class="lista-filmes">${r.filmes.map(filmeHtml)}</div>`;
}

function zonaEnvioHtml(r) {
  return h`<div class="card zona-envio" id="zona-envio" data-acao="escolher-arquivos" role="button" tabindex="0" aria-label="Enviar filmes ou legendas">
      <div class="zona-conteudo">${ic("enviar", "grande")}<div><b>Arraste filmes ou legendas (.srt) para cá</b><small>ou clique para escolher. Os arquivos são copiados para ${r.pasta}</small></div></div>
    </div>
    <input type="file" id="arquivo-filmes" multiple hidden accept="${[...r.extensoes, ".srt"].join(",")}">
    <div class="envios" id="envios"></div>`;
}

async function carregarFilmes(token = App.navegacao) {
  const r = await api("/filmes");
  if (!App.ativa(token) || App.nomePagina !== "filmes") return;
  PgFilmes.dados = r;
  montarSeMudou($("#lista-filmes"), listaFilmesHtml(r));
}

/* ------------------------------------------------------------ envio de arquivos */

function linhaEnvio(nome) {
  const linha = elementoDe(h`<div class="envio"><span title="${nome}">${nome}</span><div class="barra"><i style="width:0%"></i></div><small>0%</small></div>`);
  $("#envios").appendChild(linha);
  return linha;
}

function marcarEnvio(linha, classe, texto, pct = null) {
  if (classe) linha.classList.add(classe);
  if (pct !== null) linha.querySelector("i").style.width = `${pct}%`;
  linha.querySelector("small").textContent = texto;
}

function enviarArquivo(arquivo, linha) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/filmes/enviar");
    xhr.setRequestHeader("X-AutoCortes", App.token);
    xhr.setRequestHeader("X-Nome-Arquivo", encodeURIComponent(arquivo.name));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) {
        const pct = Math.floor((100 * ev.loaded) / ev.total);
        marcarEnvio(linha, null, `${pct}%`, pct);
      }
    };
    xhr.onload = () => {
      let dados = {};
      try { dados = JSON.parse(xhr.responseText || "{}"); } catch (e) { dados = {}; }
      if (xhr.status === 200) {
        marcarEnvio(linha, "feito", "Enviado", 100);
        resolve(dados);
      } else {
        marcarEnvio(linha, "falhou", dados.erro || `Erro ${xhr.status}`);
        resolve(null);
      }
    };
    xhr.onerror = () => { marcarEnvio(linha, "falhou", "Falha de conexão"); resolve(null); };
    xhr.send(arquivo);
  });
}

async function enviarArquivos(lista) {
  const r = PgFilmes.dados;
  const aceitas = new Set([...(r ? r.extensoes : []), ".srt"]);
  const arquivos = [...lista];
  if (!arquivos.length) return;
  PgFilmes.enviando++;
  App.enviosAtivos = true;
  let ok = 0;
  try {
    for (const arquivo of arquivos) {
      const ext = (arquivo.name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
      const linha = linhaEnvio(arquivo.name);
      if (!aceitas.has(ext)) { marcarEnvio(linha, "falhou", "Formato não aceito"); continue; }
      if (await enviarArquivo(arquivo, linha)) ok++;
    }
  } finally {
    PgFilmes.enviando--;
    App.enviosAtivos = PgFilmes.enviando > 0;
  }
  if (ok) {
    toast(`${plural(ok, "arquivo enviado", "arquivos enviados")}. A análise começa em instantes.`);
    await carregarFilmes().catch(() => {});
    atualizarEstado();
  }
}

function prepararZonaEnvio() {
  const zona = $("#zona-envio");
  const seletor = $("#arquivo-filmes");
  if (!zona || !seletor) return;
  seletor.addEventListener("change", () => {
    const arquivos = [...seletor.files];
    seletor.value = "";
    enviarArquivos(arquivos);
  });
  let profundidade = 0;
  zona.addEventListener("dragenter", (ev) => { ev.preventDefault(); profundidade++; zona.classList.add("sobre"); });
  zona.addEventListener("dragover", (ev) => { ev.preventDefault(); ev.dataTransfer.dropEffect = "copy"; });
  zona.addEventListener("dragleave", () => { profundidade = Math.max(0, profundidade - 1); if (!profundidade) zona.classList.remove("sobre"); });
  zona.addEventListener("drop", (ev) => {
    ev.preventDefault();
    profundidade = 0;
    zona.classList.remove("sobre");
    enviarArquivos(ev.dataTransfer.files);
  });
}

App.acoes["escolher-arquivos"] = () => {
  const seletor = $("#arquivo-filmes");
  if (seletor) seletor.click();
};

App.acoes["varrer-filmes"] = async (el) => {
  const r = await ocupado(el, () => post("/filmes/varrer"));
  toast(r.novos ? `${plural(r.novos, "filme novo encontrado", "filmes novos encontrados")}` : "Nenhum filme novo na pasta. Arquivos copiados há menos de 1 minuto esperam um pouco.", r.novos ? "ok" : "info", 6000);
  await carregarFilmes();
};

/* ------------------------------------------------------------ ações de cada filme */

function filmePorId(id) {
  return PgFilmes.dados && PgFilmes.dados.filmes.find((f) => String(f.id) === String(id));
}

App.acoes["filme-acao"] = async (el) => {
  const f = filmePorId(el.dataset.filme);
  const tipo = el.dataset.tipo;
  if (tipo === "ignorar") {
    const ok = await confirmar({ titulo: `Ignorar "${f ? f.titulo : "o filme"}"?`, texto: "O filme deixa de ser analisado e os cortes dele não são mais postados. Dá para reativar depois.", botao: "Ignorar" });
    if (!ok) return;
  }
  await ocupado(el, () => post(`/filmes/${el.dataset.filme}/acao`, { acao: tipo }));
  if (tipo !== "mostrar") {
    toast(tipo === "ignorar" ? "Filme ignorado" : "Filme reativado");
    await carregarFilmes();
    atualizarEstado();
  }
};

App.acoes["filme-reanalisar"] = (el) => {
  const f = filmePorId(el.dataset.filme);
  if (!f) return;
  abrirModal({
    titulo: `Reanalisar "${f.titulo}"`,
    largura: 560,
    corpo: h`<p class="texto-modal">Os trechos encontrados que ainda não foram editados são refeitos. Cortes já editados, publicados ou descartados continuam como estão.</p>
      <div class="opcoes-rede">
        <button class="btn" data-acao="filme-reanalisar-ok" data-filme="${f.id}" data-tipo="reanalisar">${ic("texto")}Refazer legendas e trechos<span class="texto-suave">rápido, reaproveita cenas e volume</span></button>
        <button class="btn" data-acao="filme-reanalisar-ok" data-filme="${f.id}" data-tipo="reanalisar_tudo">${ic("refazer")}Analisar tudo do zero<span class="texto-suave">use se o arquivo mudou</span></button>
      </div>
      <div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button></div>`,
  });
};

App.acoes["filme-reanalisar-ok"] = async (el) => {
  await ocupado(el, () => post(`/filmes/${el.dataset.filme}/acao`, { acao: el.dataset.tipo }));
  fecharModal();
  toast("O filme voltou para a fila de análise");
  await carregarFilmes();
  atualizarEstado();
};

App.acoes["filme-dados"] = (el) => {
  const f = filmePorId(el.dataset.filme);
  if (!f) return;
  const meta = f.meta || {};
  const idioma = meta.idioma || "";
  abrirModal({
    titulo: "Dados do filme",
    largura: 600,
    corpo: h`<p class="nota">Arquivo: ${f.arquivo}. Os dados ficam num arquivo .json ao lado do filme.</p>
      <label class="campo"><span>Título</span><input type="text" id="fd-titulo" maxlength="150" value="${meta.titulo || ""}" placeholder="${f.titulo}"></label>
      <label class="campo"><span>Ano</span><input type="number" id="fd-ano" min="1880" max="2100" class="curto" value="${meta.ano || ""}" placeholder="${f.ano || ""}"></label>
      <label class="campo"><span>Hashtags deste filme</span><input type="text" id="fd-hashtags" value="${(meta.hashtags || []).join(" ")}" placeholder="#terror #classico"></label>
      <label class="campo"><span>Idioma falado</span><select id="fd-idioma">${IDIOMAS.map(([v, t]) => h`<option value="${v}" ${v === idioma ? raw("selected") : ""}>${t}</option>`)}</select></label>
      <p class="nota">O idioma ajuda a transcrição automática. Para refazer as legendas com o novo idioma, use Reanalisar.</p>
      <div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button><button class="btn primario" data-acao="filme-dados-salvar" data-filme="${f.id}">${ic("check")}Salvar</button></div>`,
  });
};

App.acoes["filme-dados-salvar"] = async (el) => {
  const hashtags = $("#fd-hashtags").value.split(/[\s,;]+/).map((t) => t.trim()).filter(Boolean)
    .map((t) => (t.startsWith("#") ? t : `#${t}`));
  await ocupado(el, () => post(`/filmes/${el.dataset.filme}/meta`, {
    titulo: $("#fd-titulo").value,
    ano: $("#fd-ano").value,
    hashtags,
    idioma: $("#fd-idioma").value,
  }));
  fecharModal();
  toast("Dados do filme salvos");
  await carregarFilmes();
};

/* ------------------------------------------------------------ página */

App.paginas.filmes = {
  titulo: "Filmes",
  subtitulo: "Os filmes de onde saem os cortes",
  async render(el, token) {
    const r = await api("/filmes");
    if (!App.ativa(token)) return;
    PgFilmes.dados = r;
    montar(el, h`${zonaEnvioHtml(r)}
      <div class="barra-ferramentas">
        <p class="nota">Pasta: ${r.pasta}</p>
        <div class="grupo-botoes"><button class="btn pequeno" data-acao="varrer-filmes">${ic("busca")}Procurar filmes novos</button><button class="btn pequeno" data-acao="abrir-pasta" data-alvo="filmes">${ic("pasta")}Abrir a pasta</button></div>
      </div>
      <div id="lista-filmes"></div>
      <p class="nota rodape-card">Use filmes que você tem direito de publicar: domínio público, Creative Commons, próprios ou licenciados. Cortes de filmes protegidos geram bloqueios e podem derrubar a conta.</p>`);
    montarSeMudou($("#lista-filmes"), listaFilmesHtml(r));
    prepararZonaEnvio();
  },
  aoEstado(e) {
    if (!PgFilmes.dados || !$("#lista-filmes")) return;
    const assinatura = JSON.stringify([e.numeros.filmes, e.numeros.filmes_na_fila, e.numeros.filmes_erro, e.numeros.candidatos,
      e.atividades.map((a) => [a.texto, a.pct === null ? null : Math.floor(a.pct / 5)])]);
    if (assinatura !== PgFilmes.ultimaAssinatura) {
      PgFilmes.ultimaAssinatura = assinatura;
      carregarFilmes().catch(() => {});
    }
  },
  async recarregar() {
    await carregarFilmes();
  },
};
