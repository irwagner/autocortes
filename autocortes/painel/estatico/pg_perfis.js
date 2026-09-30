/* AutoCortes - painel: Perfis (um nicho por pasta, com contas, tema e agenda próprios) */
"use strict";

const PgPerfis = { dados: null };

const prfSlug = (p) => (p.principal ? "principal" : p.slug);

function prfSituacaoHtml(p) {
  const s = p.situacao || {};
  if (p.erro) return h`<span class="status-conta"><span>${ic("erro")}</span><span><b>Config com problema</b><br><small>${p.erro}</small></span></span>`;
  if (s.rodando) {
    const motor = s.motor ? "trabalhando" : "motor desligado";
    return h`<span class="status-conta"><span>${ic("ok")}</span><span><b>Aberto</b> · ${motor}${p.atual ? h` · <b>você está aqui</b>` : ""}<br><small>${p.url}</small></span></span>`;
  }
  if (s.outro) {
    return h`<span class="status-conta"><span>${ic("alerta")}</span><span><b>Porta ocupada</b><br><small>${s.perfil_na_porta} já responde na porta ${p.porta}</small></span></span>`;
  }
  return h`<span class="status-conta"><span>${ic("pausa")}</span><span><b>Fechado</b><br><small>Nada é editado nem postado</small></span></span>`;
}

function prfCartaoHtml(p) {
  const s = p.situacao || {};
  const slug = prfSlug(p);
  const redes = (p.redes || []).map((r) => (REDES[r] ? REDES[r].rotulo : r)).join(", ");
  return h`<article class="card card-perfil ${s.rodando ? "aberto" : ""}">
    <div class="card-topo">
      <h3>${ic(p.principal ? "estrela" : "caixa")}${p.nome}</h3>
      <div class="grupo-botoes">
        ${p.principal ? badge(["Principal", "neutro"], true) : ""}
        ${p.autoiniciar ? badge(["Abre junto", "info"], true) : ""}
        ${p.simulacao ? badge(["Simulação", "aviso"], true) : badge(["Publicando", "ok"], true)}
      </div>
    </div>
    ${prfSituacaoHtml(p)}
    <ul class="lista-simples">
      <li><span>Redes ligadas</span><b>${redes || "nenhuma"}</b></li>
      <li><span>Tema</span><b>${p.modelo || "-"}</b></li>
      <li><span>Painel</span><b>porta ${p.porta}</b></li>
      <li><span>Navegador</span><b>porta ${p.porta_navegador}</b></li>
      <li><span>Pasta</span><b title="${p.pasta}">${p.pasta_curta === "." ? "a instalação" : p.pasta_curta}</b></li>
    </ul>
    <div class="rodape-card">
      <div class="grupo-botoes">
        ${s.rodando
          ? h`<a class="btn pequeno" href="${p.url}" target="_blank" rel="noopener">${ic("externo")}Abrir o painel</a>
             ${p.atual ? "" : h`<button class="btn pequeno" data-acao="prf-fechar" data-slug="${slug}">${ic("pausa")}Fechar</button>`}`
          : h`<button class="btn pequeno primario" data-acao="prf-abrir" data-slug="${slug}" ${p.erro ? raw("disabled") : ""}>${ic("play")}Abrir</button>`}
      </div>
      <div class="grupo-botoes">
        <button class="btn pequeno fantasma" data-acao="prf-editar" data-slug="${slug}">${ic("lapis")}Ajustar</button>
        ${p.principal ? "" : h`<button class="btn pequeno fantasma" data-acao="prf-excluir" data-slug="${slug}" ${s.rodando ? raw("disabled title=\"Feche o perfil antes de excluir\"") : ""}>${ic("lixo")}Excluir</button>`}
      </div>
    </div>
  </article>`;
}

function prfPaginaHtml(d) {
  const limite = d.instalacao.max_simultaneos;
  return h`<div class="card descricao-aba">
      <p class="nota">Cada perfil é um nicho com a sua pasta: filmes, contas das redes, tema, agenda e histórico separados, e uma janela do Chrome só dele, para as contas não se misturarem. Cada um roda no seu processo, com o painel numa porta própria.</p>
    </div>
    ${d.conflitos.length ? h`<div class="aviso-inline erro">${ic("alerta")}<div>${d.conflitos.map((c) => h`<div>${c}</div>`)}</div></div>` : ""}
    <div class="barra-ferramentas">
      <div class="prf-limite">
        <label for="prf-limite">Perfis trabalhando ao mesmo tempo</label>
        <input type="number" id="prf-limite" min="0" max="${d.max}" value="${limite}" inputmode="numeric">
        <button class="btn pequeno" data-acao="prf-limite">${ic("check")}Aplicar</button>
        <small class="nota">0 = sem limite. Cada perfil aberto usa FFmpeg e whisper por conta própria: no seu PC, 2 ou 3 é o razoável. Abertos agora: ${d.abertos}.</small>
      </div>
      <div class="grupo-botoes">
        <button class="btn pequeno" data-acao="prf-atualizar">${ic("refazer")}Atualizar</button>
        <button class="btn primario" data-acao="prf-novo" ${d.perfis.length >= d.max ? raw("disabled") : ""}>${ic("mais")}Novo perfil</button>
      </div>
    </div>
    <div class="cards-redes">${d.perfis.map(prfCartaoHtml)}</div>
    <p class="nota rodape-card">O login do TikTok usa a porta 8765 em todos os perfis, porque ela é registrada no app: conecte um perfil por vez. As ferramentas e os modelos de transcrição são compartilhados pela instalação.</p>`;
}

async function prfCarregar() {
  const d = await api("/perfis");
  PgPerfis.dados = d;
  montarSeMudou($("#conteudo"), prfPaginaHtml(d));
  return d;
}

const prfDe = (slug) => (PgPerfis.dados ? PgPerfis.dados.perfis.find((p) => prfSlug(p) === slug) : null);

/* ------------------------------------------------------------ ações */

App.acoes["prf-atualizar"] = async (el) => {
  await ocupado(el, prfCarregar);
};

App.acoes["prf-limite"] = async (el) => {
  const campo = $("#prf-limite");
  await ocupado(el, () => post("/perfis/opcoes", { max_simultaneos: Number(campo.value) }));
  toast("Limite de perfis ao mesmo tempo salvo");
  await prfCarregar();
};

App.acoes["prf-novo"] = async () => {
  abrirModal({
    titulo: "Novo perfil",
    largura: 520,
    corpo: h`<p class="nota">Crio a pasta do perfil com config próprio, uma pasta de filmes vazia e uma porta livre para o painel. Ele começa em modo simulação, sem nenhuma rede ligada: conecte as contas dentro dele.</p>
      <div class="form">
        ${linhaCampo("Nome do nicho", "Aparece no painel, ex.: Motivacional, Receitas, Games",
          h`<input type="text" id="prf-nome" maxlength="60" placeholder="Motivacional" autofocus>`, true)}
      </div>
      <div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button><button class="btn primario" data-acao="prf-criar">${ic("mais")}Criar perfil</button></div>`,
  });
};

App.acoes["prf-criar"] = async (el) => {
  const nome = String($("#prf-nome").value || "").trim();
  if (!nome) {
    toast("Escreva o nome do nicho", "erro");
    return;
  }
  const r = await ocupado(el, () => post("/perfis/novo", { nome }));
  fecharModal();
  toast(`Perfil '${r.perfil.nome}' criado`, "ok", 6000);
  await prfCarregar();
  abrirModal({
    titulo: `Perfil '${r.perfil.nome}' criado`,
    largura: 560,
    corpo: h`<p>Agora, dentro dele:</p>
      <ol class="passos">
        <li>Clique em <b>Abrir</b> e depois em <b>Abrir o painel</b>.</li>
        <li>Ponha os vídeos em <code>${r.perfil.pasta_filmes}</code>.</li>
        <li>Em Redes sociais, conecte as contas <b>desse</b> nicho e ligue as redes.</li>
        <li>No Estúdio, escolha o tema. Quando estiver do seu jeito, desligue a simulação.</li>
      </ol>
      <div class="acoes-modal"><button class="btn primario" data-acao="fechar-modal">Entendi</button></div>`,
  });
};

App.acoes["prf-abrir"] = async (el) => {
  const p = prfDe(el.dataset.slug);
  const r = await ocupado(el, () => post(`/perfis/${el.dataset.slug}/abrir`));
  toast(r.iniciado ? `Perfil '${p.nome}' aberto` : `O perfil '${p.nome}' ${r.motivo}`, "ok", 6000);
  await prfCarregar();
};

App.acoes["prf-fechar"] = async (el) => {
  const p = prfDe(el.dataset.slug);
  const certo = await confirmar({
    titulo: `Fechar '${p.nome}'?`,
    texto: "O perfil para de editar e de postar até você abrir de novo. Um trabalho em andamento é interrompido com cuidado.",
    botao: "Fechar o perfil",
  });
  if (!certo) return;
  const r = await ocupado(el, () => post(`/perfis/${el.dataset.slug}/fechar`));
  toast(r.parado ? `Perfil '${p.nome}' fechado` : `O perfil '${p.nome}' ${r.motivo}`);
  await prfCarregar();
};

App.acoes["prf-editar"] = async (el) => {
  const p = prfDe(el.dataset.slug);
  abrirModal({
    titulo: `Ajustar '${p.nome}'`,
    largura: 560,
    corpo: h`<div class="form">
        ${linhaCampo("Nome", "", h`<input type="text" id="prf-ed-nome" maxlength="60" value="${p.nome}" autofocus>`)}
        ${p.principal ? "" : linhaCampo("Abrir junto com o principal", "Sobe este perfil quando o AutoCortes abre, se o limite permitir",
          h`<label class="switch"><input type="checkbox" id="prf-ed-auto" ${p.autoiniciar ? raw("checked") : ""}><span></span></label>`)}
        ${linhaCampo("Porta do painel", "Precisa ser diferente da dos outros perfis",
          h`<input type="number" id="prf-ed-porta" min="1024" max="65535" value="${p.porta}">`)}
        ${linhaCampo("Porta do navegador", "A janela do Chrome deste perfil. Repetida entre perfis, as contas se misturam",
          h`<input type="number" id="prf-ed-nav" min="1024" max="65535" value="${p.porta_navegador}">`)}
      </div>
      <p class="nota">O resto (redes, tema, agenda, cortes) se ajusta dentro do painel do próprio perfil.</p>
      <div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button><button class="btn primario" data-acao="prf-salvar" data-slug="${prfSlug(p)}">${ic("check")}Salvar</button></div>`,
  });
};

App.acoes["prf-salvar"] = async (el) => {
  const auto = $("#prf-ed-auto");
  const corpo = {
    nome: String($("#prf-ed-nome").value || "").trim(),
    porta: Number($("#prf-ed-porta").value),
    porta_navegador: Number($("#prf-ed-nav").value),
  };
  if (auto) corpo.autoiniciar = auto.checked;
  const r = await ocupado(el, () => post(`/perfis/${el.dataset.slug}/salvar`, corpo));
  fecharModal();
  toast(r.perfil.aviso || "Perfil salvo", r.perfil.aviso ? "info" : "ok", r.perfil.aviso ? 7000 : 4500);
  await prfCarregar();
};

App.acoes["prf-excluir"] = async (el) => {
  const p = prfDe(el.dataset.slug);
  const c = await api(`/perfis/${el.dataset.slug}/conteudo`);
  abrirModal({
    titulo: `Excluir '${p.nome}'?`,
    largura: 560,
    corpo: h`<div class="aviso-inline erro">${ic("alerta")}<div>Isto apaga a pasta <code>${p.pasta}</code> inteira, sem volta: ${plural(c.filmes, "vídeo", "vídeos")} na pasta de filmes, ${plural(c.cortes, "corte editado", "cortes editados")}, o histórico e ${c.tem_contas ? "as contas conectadas" : "as configurações"} deste nicho.</div></div>
      <p class="nota">As contas nas redes continuam existindo: só o acesso salvo aqui é apagado.</p>
      <div class="form">
        ${linhaCampo("Escreva o nome do perfil para confirmar", `Escreva: ${p.nome}`,
          h`<input type="text" id="prf-conf" maxlength="60" placeholder="${p.nome}" autofocus>`, true)}
      </div>
      <div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button><button class="btn perigo" data-acao="prf-excluir-ok" data-slug="${prfSlug(p)}">${ic("lixo")}Excluir para sempre</button></div>`,
  });
};

App.acoes["prf-excluir-ok"] = async (el) => {
  const p = prfDe(el.dataset.slug);
  await ocupado(el, () => post(`/perfis/${el.dataset.slug}/excluir`, { confirmacao: String($("#prf-conf").value || "") }));
  fecharModal();
  toast(`Perfil '${p.nome}' excluído`, "info", 6000);
  await prfCarregar();
};

/* ------------------------------------------------------------ página */

App.paginas.perfis = {
  titulo: "Perfis",
  subtitulo: "Vários nichos na mesma instalação, cada um com suas contas",
  async render(el, token) {
    const d = await api("/perfis");
    if (!App.ativa(token)) return;
    PgPerfis.dados = d;
    montar(el, prfPaginaHtml(d));
  },
  async recarregar() {
    await prfCarregar();
  },
};
