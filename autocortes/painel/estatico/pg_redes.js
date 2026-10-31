/* AutoCortes - painel: Redes sociais (contas, forma de envio e opções de cada rede) */
"use strict";

const PgRedes = { dados: null };
const Login = { rede: null, ativo: false, timer: null };
const PERMISSOES_IG = "instagram_basic, instagram_content_publish, instagram_manage_insights, pages_show_list, pages_read_engagement, business_management";
// as do login da Página do Facebook (plataformas/facebook.py)
const RED_FB_PERMISSOES = "pages_show_list, pages_read_engagement, pages_manage_posts, business_management";
// curtos para caber no seletor; o que cada um implica vem no aviso e no guia logo abaixo
const NOME_ENVIO = {
  oficial: () => "API oficial",
  upload_post: () => "Upload-Post (pago)",
  navegador: () => "Navegador (arriscado)",
  manual: () => "À mão (pelo app)",
};
// redes que só aceitam a postagem à mão
const ENVIO_FIXO = { kwai: "À mão, pelo app do Kwai" };

const PRIVACIDADE_TIKTOK = [
  ["PUBLIC_TO_EVERYONE", "Todos"], ["FOLLOWER_OF_CREATOR", "Seguidores"],
  ["MUTUAL_FOLLOW_FRIENDS", "Amigos (seguidores mútuos)"], ["SELF_ONLY", "Só eu"],
];

function link(url, texto) {
  return h`<a href="${url}" target="_blank" rel="noopener noreferrer">${texto}</a>`;
}

function guiaOficial(rede) {
  if (rede === "youtube") {
    return h`<details class="guia"><summary>${ic("info")}Como conseguir as chaves do YouTube</summary><ol>
      <li>Em ${link("https://console.cloud.google.com/", "console.cloud.google.com")}, crie um projeto e ative a "YouTube Data API v3".</li>
      <li>Na tela de consentimento OAuth, escolha público Externo, adicione seu e-mail como usuário de teste e publique o app. Em modo "Teste", o login expira a cada 7 dias.</li>
      <li>Em Credenciais, crie um "ID do cliente OAuth" do tipo "App para computador".</li>
      <li>Cole o Client ID e o Client secret aqui, salve e clique em Conectar conta.</li>
    </ol></details>`;
  }
  if (rede === "tiktok") {
    const retorno = (App.meta && App.meta.tiktok_redirect) || "http://127.0.0.1:8765/callback/";
    return h`<details class="guia"><summary>${ic("info")}Como conseguir as chaves do TikTok</summary><ol>
      <li>Em ${link("https://developers.tiktok.com/", "developers.tiktok.com")}, abra Manage apps e crie um app.</li>
      <li>Adicione "Login Kit" e "Content Posting API", com "Direct Post" ligado.</li>
      <li>No Login Kit, plataforma Desktop, cadastre este endereço de retorno:<div class="copiavel"><code>${retorno}</code><button class="btn pequeno icone" data-acao="copiar" data-texto="${retorno}" title="Copiar" aria-label="Copiar o endereço">${ic("copiar")}</button></div></li>
      <li>Cole o Client key e o Client secret aqui, salve e clique em Conectar conta.</li>
    </ol></details>`;
  }
  if (rede === "facebook") {
    return h`<details class="guia"><summary>${ic("info")}Como conectar a Página do Facebook</summary><ol>
      <li>Use o app da Meta do Instagram ou crie um do tipo Empresa em ${link("https://developers.facebook.com/apps/", "developers.facebook.com")}, e adicione o caso de uso de gerenciar a Página.</li>
      <li>Cole o App ID e a chave secreta aqui e salve. Em branco, eu uso os do Instagram.</li>
      <li>Clique em Conectar conta, gere o token no ${link("https://developers.facebook.com/tools/explorer/", "Graph API Explorer")} com as permissões <code class="permissoes">${RED_FB_PERMISSOES}</code> e escolha a Página.</li>
      <li>Deixe o app no modo publicado: em desenvolvimento, o Reel só aparece para quem tem função no app.</li>
    </ol></details>`;
  }
  return h`<details class="guia"><summary>${ic("info")}Como preparar o Instagram</summary><ol>
    <li>A conta precisa ser profissional (Criador ou Empresa) e estar ligada a uma Página do Facebook.</li>
    <li>Em ${link("https://developers.facebook.com/apps/", "developers.facebook.com")}, crie um app do tipo Empresa e adicione o produto Instagram (API com login do Facebook).</li>
    <li>Cole o App ID e a chave secreta aqui e salve.</li>
    <li>Clique em Conectar conta e siga os passos para gerar o token.</li>
  </ol></details>`;
}

function guiaManual(rede) {
  const passos = {
    kwai: h`<li>No horário, a tarefa aparece no Início com o vídeo e a legenda.</li><li>Passe o vídeo para o celular (baixe ou use a pasta sincronizada abaixo).</li><li>No app do Kwai, toque na câmera, depois em Álbum, escolha o vídeo, cole a legenda e publique.</li><li>Volte ao painel e marque "Já postei".</li>`,
    bilibili: h`<li>No horário, a tarefa aparece no Início com o vídeo, o título, a descrição e as tags.</li><li>Abra a ${link("https://member.bilibili.com/platform/upload/video/frame", "página de envio do Bilibili")} e escolha o vídeo.</li><li>Em 类型, marque 转载 (repost) e informe a fonte; em 分区, escolha 影视 > 影视剪辑.</li><li>Cole os textos, clique em 立即投稿 e marque "Já postei" no painel.</li>`,
  }[rede] || h`<li>No horário, a tarefa aparece no Início com o vídeo e os textos no formato da rede.</li><li>Baixe o vídeo (ou pegue na pasta sincronizada), poste pelo app ou pelo site e cole os textos.</li><li>Volte ao painel e marque "Já postei". Se preferir, agende direto na rede.</li>`;
  return h`<details class="guia"><summary>${ic("info")}Como funciona a postagem à mão</summary><ol>${passos}</ol></details>`;
}

function guiaNavegador(rede) {
  if (rede === "facebook") {
    return h`<details class="guia"><summary>${ic("info")}Como funciona o envio pelo navegador</summary><ol>
      <li>Clique em <b>Conectar</b> e entre na sua conta do Facebook na janela do Chrome do AutoCortes (perfil em <code>dados/chrome</code>, separado do seu navegador).</li>
      <li>Informe a Página logo abaixo: o link dela é o mais seguro; com o nome, eu pergunto antes de usar.</li>
      <li>Clique em <b>Testar a sessão</b>: eu confiro se a janela está agindo como a Página (e troco para ela, se puder).</li>
      <li>Em <b>Aprender a postar</b>, grave o roteiro já agindo como a Página: poste um Reel à mão na janela.</li>
      <li>Antes de valer, ligue o <b>ensaio</b> no cartão "Postagem pelo navegador": ele preenche tudo e não publica.</li>
    </ol></details>`;
  }
  const onde = { youtube: "YouTube Studio", tiktok: "TikTok Studio", instagram: "Instagram", bilibili: "Bilibili" }[rede];
  return h`<details class="guia"><summary>${ic("info")}Como funciona o envio pelo navegador</summary><ol>
    <li>Clique em <b>Conectar</b>: abre uma janela do Chrome com um perfil só do AutoCortes (em <code>dados/chrome</code>), separado do seu navegador do dia a dia.</li>
    <li>Entre na conta do ${REDES[rede].rotulo} nessa janela, resolvendo o 2FA se ele pedir. A sessão fica salva ali e sobrevive a reiniciar o PC.</li>
    <li>Clique em <b>Testar</b>: eu abro a página do ${onde} e confirmo que a sessão está de pé.</li>
    <li>No horário, o AutoCortes preenche a página de envio e publica, com pausas entre os passos.</li>
    <li>Antes de valer, ligue o <b>ensaio</b> no cartão "Postagem pelo navegador": ele preenche tudo, não publica e guarda uma imagem da tela.</li>
  </ol></details>`;
}

function avisoRede(rede, envio) {
  const privacidadeTiktok = valor("tiktok.privacidade");
  if (envio === "navegador") {
    return `Isto contraria os termos do ${REDES[rede].rotulo}, que só autorizam a API oficial, e pode custar a conta. Quando a rede muda o layout da página, o envio falha até eu ajustar. Use numa conta que você pode perder.`;
  }
  if (rede === "kwai") {
    return "O Kwai não tem API de postagem para criadores nem envio pelo site: você posta pelo app, com o vídeo e a legenda que o AutoCortes separa.";
  }
  if (rede === "bilibili") {
    return "A API de envio do Bilibili é só para empresas chinesas cadastradas. O corte é de um filme de terceiros: marque 转载 (repost) com a fonte, e esse tipo de vídeo não entra no programa de incentivo do Bilibili.";
  }
  if (envio === "manual") {
    return `Nada é enviado sozinho: no horário, o ${REDES[rede].rotulo} ganha uma tarefa no Início com o vídeo e os textos.`;
  }
  if (rede === "youtube" && envio === "oficial") {
    return "Projetos sem a auditoria da API do YouTube sobem os vídeos como privados. Para sair público, peça a auditoria (grátis, sem prazo) ou use o Upload-Post.";
  }
  if (rede === "tiktok" && envio === "oficial") {
    return "Sem auditoria, o TikTok só aceita posts visíveis só para você, com a conta privada, e não audita ferramentas de uso próprio. Para postar em público, use o Upload-Post.";
  }
  if (rede === "tiktok" && privacidadeTiktok === "SELF_ONLY") {
    return "A privacidade está em \"Só eu\". Troque para \"Todos\" para publicar em público.";
  }
  if (rede === "instagram" && envio === "oficial") {
    return "Precisa de conta profissional ligada a uma Página do Facebook. A legenda do post leva no máximo 5 hashtags.";
  }
  return "";
}

// tipo da pausa que o motor pôs na rede (publicador.ler_bloqueios)
const TITULO_PAUSA_REDE = {
  cota: "Pausada: limite de uso da rede",
  recusas: "Pausada: posts recusados em seguida",
  rede: "Sem resposta da rede: tento de novo sozinho",
  configuracao: "Falta configurar",
  bloqueio: "Parada: a rede bloqueou ou pediu verificação",
};

function quandoVoltaRede(b) {
  if (!b) return "";
  if (b.ate === null || b.ate === undefined) return b.tipo === "configuracao" ? "" : " Volta quando você testar a conta.";
  return ` Volta ${fmtQuando(b.ate)}.`;
}

function statusContaHtml(r, envio) {
  let classe;
  let icone;
  let titulo;
  let sub;
  if (!r.ativo) {
    classe = "pendente"; icone = "pausa"; titulo = "Desativada"; sub = "Ligue a chave acima para postar nesta rede.";
  } else if (r.bloqueada) {
    const b = r.bloqueio || {};
    const passa = b.tipo === "rede";
    classe = passa ? "pendente" : "erro"; icone = passa ? "relogio" : "erro";
    titulo = TITULO_PAUSA_REDE[b.tipo] || "Pausada por um erro";
    sub = `${r.bloqueada}.${quandoVoltaRede(b)}`;
  } else if (r.via === "manual") {
    classe = "ok"; icone = "ok"; titulo = "Postagem à mão";
    sub = r.tarefas ? `${plural(r.tarefas, "tarefa esperando", "tarefas esperando")} você no Início` : "Os horários viram tarefas no Início";
  } else if (r.via === "navegador") {
    classe = r.pronta ? "ok" : "pendente";
    icone = r.pronta ? "ok" : "alerta";
    titulo = r.pronta ? r.conta || "Sessão salva no navegador" : "Sem sessão no navegador";
    sub = r.pronta ? "pelo navegador, com a sua sessão" : r.motivo;
    if (r.gravando && r.gravando_midia === "imagem") { classe = "pendente"; icone = "microfone"; titulo = "Gravando o roteiro de imagem"; sub = "Poste as imagens de exemplo à mão na janela que abriu"; }
    else if (r.gravando) { classe = "pendente"; icone = "microfone"; titulo = "Gravando o roteiro"; sub = "Poste um vídeo à mão na janela que abriu"; }
    else if (r.roteiro && r.roteiro.tem) {
      const erros = (r.roteiro.erros || []).length;
      const avisos = (r.roteiro.avisos || []).length;
      sub += ` · ${r.roteiro.origem === "padrao" ? "roteiro padrão" : "roteiro"} de ${plural(r.roteiro.linhas, "ação", "ações")}`;
      if (erros) { classe = "erro"; icone = "erro"; sub += ` · ${plural(erros, "linha com problema", "linhas com problema")}`; }
      else if (avisos) { classe = "pendente"; icone = "alerta"; sub += ` · ${plural(avisos, "ponto arriscado", "pontos arriscados")} (veja o roteiro)`; }
    } else if (r.pronta) sub += " · sem roteiro gravado: uso os passos que eu escrevi";
  } else if (r.pronta) {
    classe = "ok"; icone = "ok"; titulo = r.conta || "Pronta para postar"; sub = ROTULO_ENVIO[r.via] || r.via;
  } else {
    classe = "pendente"; icone = "alerta"; titulo = "Não conectada"; sub = r.motivo;
  }
  if (r.ativo && r.conferir) sub += ` · ${plural(r.conferir, "post", "posts")} a conferir no Início`;
  const trocando = envio !== r.via ? h`<small class="aviso-inline">${ic("alerta")}Salve para trocar a forma de envio.</small>` : "";
  return h`<div class="status-conta ${classe}">${ic(icone)}<div><b>${titulo}</b><small title="${sub}">${sub}</small>${trocando}</div></div>`;
}

function camposOficiais(rede) {
  if (rede === "youtube") {
    return [
      campo({ chave: "youtube.client_id", tipo: "texto", rotulo: "Client ID", placeholder: "....apps.googleusercontent.com" }),
      campo({ chave: "youtube.client_secret", tipo: "senha", rotulo: "Client secret" }),
    ];
  }
  if (rede === "tiktok") {
    return [
      campo({ chave: "tiktok.client_key", tipo: "texto", rotulo: "Client key" }),
      campo({ chave: "tiktok.client_secret", tipo: "senha", rotulo: "Client secret" }),
    ];
  }
  if (rede === "facebook") {
    return [
      campo({ chave: "facebook.app_id", tipo: "texto", rotulo: "App ID", ajuda: "Em branco, uso o app do Instagram." }),
      campo({ chave: "facebook.app_secret", tipo: "senha", rotulo: "Chave secreta do app" }),
    ];
  }
  return [
    campo({ chave: "instagram.app_id", tipo: "texto", rotulo: "App ID" }),
    campo({ chave: "instagram.app_secret", tipo: "senha", rotulo: "Chave secreta do app" }),
  ];
}

function opcoesRede(rede) {
  if (rede === "youtube") {
    return [
      campo({ chave: "youtube.max_segundos", tipo: "numero", min: 15, max: 180, sufixo: "s", rotulo: "Duração máxima", ajuda: "O YouTube só recebe Shorts: vídeo vertical ou quadrado de até 3 min. Cortes mais longos que isto ficam só para as outras redes." }),
      campo({ chave: "youtube.privacidade", tipo: "select", rotulo: "Privacidade", opcoes: [["public", "Público"], ["unlisted", "Não listado"], ["private", "Privado"]] }),
      campo({ chave: "youtube.categoria", tipo: "select", rotulo: "Categoria", opcoes: [["1", "Filmes e animação"], ["24", "Entretenimento"], ["22", "Pessoas e blogs"]] }),
      campo({ chave: "youtube.adicionar_shorts", tipo: "switch", rotulo: "Colocar #shorts no título" }),
      campo({ chave: "youtube.tags", tipo: "chips", rotulo: "Tags do vídeo", larga: true, placeholder: "Digite e tecle Enter" }),
    ];
  }
  if (rede === "bilibili") {
    return [
      campo({ chave: "bilibili.tags", tipo: "chips", rotulo: "Tags fixas", ajuda: "Vão em todo post, junto com as hashtags do corte (sem o #). Em chinês ajudam na busca: 电影 é filme e 影视剪辑 é corte de filme.", larga: true, placeholder: "Digite e tecle Enter" }),
    ];
  }
  if (rede === "kwai") return [];
  if (rede === "tiktok") {
    if (valor("tiktok.envio") === "manual") return []; // você escolhe essas opções no app, ao postar
    return [
      campo({ chave: "tiktok.privacidade", tipo: "select", rotulo: "Quem pode ver", opcoes: PRIVACIDADE_TIKTOK }),
      campo({ chave: "tiktok.modo", tipo: "select", rotulo: "Como postar", ajuda: "Rascunho manda para a caixa de entrada do app para você finalizar no celular.", opcoes: [["direto", "Publicar direto"], ["rascunho", "Mandar como rascunho"]] }),
      campo({ chave: "tiktok.permitir_comentarios", tipo: "switch", rotulo: "Permitir comentários" }),
      campo({ chave: "tiktok.permitir_duet", tipo: "switch", rotulo: "Permitir dueto" }),
      campo({ chave: "tiktok.permitir_stitch", tipo: "switch", rotulo: "Permitir costura (stitch)" }),
    ];
  }
  if (rede === "facebook") {
    return [
      campo({ chave: "facebook.max_segundos", tipo: "numero", min: 5, max: 90, sufixo: "s", rotulo: "Duração máxima", ajuda: "A Página recebe Reels de 3 a 90 s. Cortes mais longos que isto ficam só para as outras redes." }),
    ];
  }
  const opcoes = [];
  if (valor("instagram.envio") !== "manual") {
    opcoes.push(
      campo({ chave: "instagram.compartilhar_no_feed", tipo: "switch", rotulo: "Mostrar também no feed" }),
      campo({ chave: "instagram.trial_reels", tipo: "switch", rotulo: "Reel de teste", ajuda: "Aparece primeiro só para quem não segue a conta. Pede uma conta pública com seguidores." }),
      campo({ chave: "instagram.trial_graduacao", tipo: "select", rotulo: "Depois do teste", opcoes: [["SS_PERFORMANCE", "Liberar sozinho se for bem"], ["MANUAL", "Eu libero no app"]] }),
    );
  }
  return opcoes;
}

function botoesRede(rede, r, envio) {
  const b = [];
  if (envio === "manual") {
    if (r.tarefas) b.push(h`<a class="btn" href="#/inicio">${ic("lista")}Ver as tarefas</a>`);
  } else if (envio === "navegador") {
    b.push(h`<button class="btn ${r.pronta ? "" : "primario"}" data-acao="rede-conectar" data-rede="${rede}">${ic("externo")}${r.pronta ? "Abrir o navegador" : "Conectar"}</button>`);
    b.push(h`<button class="btn" data-acao="rede-testar" data-rede="${rede}">${ic("ok")}Testar a sessão</button>`);
    const tem = r.roteiro && r.roteiro.tem;
    // o roteiro padrão vem com a instalação: o seu (gravado ou editado) passa a valer no lugar dele
    const meu = tem && r.roteiro.origem === "perfil";
    const gravar = meu ? "Gravar de novo" : tem ? "Gravar o meu" : "Aprender a postar";
    b.push(h`<button class="btn ${r.pronta && !tem ? "primario" : ""}" data-acao="rede-gravar" data-rede="${rede}">${ic("microfone")}${gravar}</button>`);
    b.push(h`<button class="btn" data-acao="rede-roteiro" data-rede="${rede}">${ic("terminal")}Ver o roteiro</button>`);
    if (meu) b.push(h`<button class="btn fantasma" data-acao="rede-roteiro-apagar" data-rede="${rede}" data-padrao="${r.roteiro.tem_padrao ? "1" : ""}">${r.roteiro.tem_padrao ? "Voltar ao roteiro padrão" : "Apagar o roteiro"}</button>`);
    if (r.conta) b.push(h`<button class="btn fantasma" data-acao="rede-desconectar" data-rede="${rede}">Esquecer a conta</button>`);
  } else if (envio === "upload_post") {
    b.push(h`<button class="btn" data-acao="rede-testar" data-rede="${rede}">${ic("ok")}Testar conexão</button>`);
  } else {
    b.push(h`<button class="btn ${r.pronta ? "" : "primario"}" data-acao="rede-conectar" data-rede="${rede}">${ic("link")}${r.pronta ? "Conectar de novo" : "Conectar conta"}</button>`);
    if (r.pronta) b.push(h`<button class="btn" data-acao="rede-testar" data-rede="${rede}">${ic("ok")}Testar</button>`);
    if (r.pronta || r.conta) b.push(h`<button class="btn fantasma" data-acao="rede-desconectar" data-rede="${rede}">Desconectar</button>`);
  }
  return b;
}

function envioHtml(rede, info, envio) {
  const permitidos = (App.meta && App.meta.envios && App.meta.envios[rede]) || ["oficial", "upload_post", "manual"];
  if (permitidos.length === 1) {
    return linhaCampo("Forma de envio", "Esta rede não tem API de postagem aberta.", h`<span class="valor-fixo">${ENVIO_FIXO[rede] || NOME_ENVIO[permitidos[0]](info)}</span>`);
  }
  return campo({ chave: `${rede}.envio`, tipo: "select", rotulo: "Forma de envio", opcoes: permitidos.map((v) => [v, NOME_ENVIO[v](info)]) });
}

function notaEnvio(rede, info, envio) {
  if (envio === "oficial") return guiaOficial(rede);
  if (envio === "manual") return guiaManual(rede);
  if (envio === "navegador") return guiaNavegador(rede);
  return h`<p class="nota">A conta do ${info.rotulo} é conectada no site do Upload-Post, dentro do perfil informado abaixo.</p>`;
}

/* ------------------------------------------------------------ vídeos e imagens de cada rede */

const redImgRedes = () => (App.meta && App.meta.redes_imagem) || ["tiktok", "instagram"];
const RED_IMG_DIREITOS = "O quadro do filme tem os mesmos direitos do vídeo. Desde 13/03/2026 a Meta trata como não original juntar trechos com mudanças pequenas (bordas, legendas), e isso vale também para imagens.";
const RED_IMG_DICA = {
  instagram: "Na gravação, no passo de recorte, escolha 4:5 ou Original antes de Avançar; sem isso o Instagram corta em quadrado.",
  tiktok: "Na gravação, escolha a opção de fotos do TikTok Studio. Se ela não aparecer, o TikTok desta conta ainda não aceita fotos pelo computador.",
};

/** As caixas Vídeos e Imagens da rede, salvas juntas pelo salvar da rede (dá para trocar uma pela outra de uma vez). */
function redImgCaixasHtml(rede, r, envio) {
  const info = REDES[rede];
  const videos = campo({ chave: `${rede}.videos`, tipo: "switch", rotulo: "Vídeos", ajuda: "Os cortes em vídeo vertical, nos horários de vídeo da agenda." });
  if (!redImgRedes().includes(rede)) {
    const motivo = (App.meta && App.meta.imagem_indisponivel && App.meta.imagem_indisponivel[rede]) || r.imagens_motivo || "Esta rede ainda não posta imagens.";
    // sem data-campo: a chave nem existe no config desta rede
    const desligado = h`<label class="switch inativo"><input type="checkbox" disabled aria-label="Imagens no ${info.rotulo}"><span></span></label>`;
    return h`<div class="rede-posta"><div class="rotulo-campo">O que esta rede posta</div>${formulario([videos, linhaCampo("Imagens", motivo, desligado)])}</div>`;
  }
  const ligada = Boolean(valor(`${rede}.imagens`));
  const estado = r.estado_imagens || {};
  const tem = Boolean(r.roteiro_imagem && r.roteiro_imagem.tem);
  let nota = "Momentos, frases e carrosséis tirados dos cortes, nos horários de imagem da agenda.";
  let inativo = false;
  // ligada continua podendo desligar; o que impede é só ligar
  if (envio !== "navegador") { nota = "As imagens saem só pelo navegador: troque o envio para Navegador."; inativo = true; }
  else if (estado.sem_fotos && !ligada) { nota = estado.sem_fotos; inativo = true; }
  else if (!tem && !ligada) { nota = "Grave o roteiro de imagem abaixo antes de ligar."; inativo = true; }
  else if (r.imagens && r.imagens_disponivel && !r.imagens_prontas && r.imagens_prontas_motivo) {
    nota = `As imagens não saem agora: ${r.imagens_prontas_motivo}.`;
  }
  const imagens = campo({ chave: `${rede}.imagens`, tipo: "switch", rotulo: "Imagens", ajuda: nota, inativo });
  return h`<div class="rede-posta"><div class="rotulo-campo">O que esta rede posta</div>${formulario([videos, imagens])}
    <p class="nota">${RED_IMG_DIREITOS}</p></div>`;
}

/** O roteiro de imagem da rede (só pelo navegador): gravar, ver e apagar, sem roteiro padrão. */
function redImgRoteiroHtml(rede, r) {
  const rot = r.roteiro_imagem || {};
  const estado = r.estado_imagens || {};
  const erros = (rot.erros || []).length;
  const avisos = (rot.avisos || []).length;
  let selo;
  if (estado.sem_fotos) selo = ["Sem fotos nesta conta", "erro"];
  else if (!rot.tem) selo = ["Não gravado", "neutro"];
  else if (erros) selo = [plural(erros, "linha com problema", "linhas com problema"), "erro"];
  else if (estado.parado) selo = ["Parado", "erro"];
  else if (estado.sem_carrossel) selo = ["O carrossel ficou de fora", "aviso"];
  else if (avisos) selo = [plural(avisos, "ponto arriscado", "pontos arriscados"), "aviso"];
  else selo = ["Gravado", "ok"];
  const problema = estado.sem_fotos || estado.parado || estado.sem_carrossel;
  const botoes = [h`<button class="btn ${rot.tem ? "" : "primario"}" data-acao="rede-gravar" data-rede="${rede}" data-midia="imagem">${ic("microfone")}${rot.tem ? "Gravar de novo" : "Gravar o roteiro de imagem"}</button>`];
  if (rot.tem) {
    botoes.push(h`<button class="btn" data-acao="rede-roteiro" data-rede="${rede}" data-midia="imagem">${ic("terminal")}Ver o roteiro</button>`);
    botoes.push(h`<button class="btn fantasma" data-acao="rede-roteiro-apagar" data-rede="${rede}" data-midia="imagem">Apagar</button>`);
  }
  return h`<div class="card-interno"><div class="card-topo"><h3>${ic("imagem")}Roteiro de imagem</h3>${badge(selo, true)}</div>
    <p class="nota">Gravado enquanto você posta à mão o próximo carrossel desta rede. Esse post sai de verdade e fica em "A conferir" no Início.${rot.tem ? ` Tem ${plural(rot.linhas || 0, "ação", "ações")}.` : ""}</p>
    ${problema ? h`<div class="aviso-rede">${ic("alerta")}<span>${problema}</span></div>` : ""}
    ${RED_IMG_DICA[rede] ? h`<p class="nota">${RED_IMG_DICA[rede]}</p>` : ""}
    <div class="grupo-botoes">${botoes}</div></div>`;
}

function redImgPorSemana(r) {
  const partes = [];
  if (r.videos !== false) partes.push(plural(r.por_semana || 0, "vídeo", "vídeos"));
  if (r.imagens_por_semana) partes.push(plural(r.imagens_por_semana, "imagem", "imagens"));
  return partes.length ? `${partes.join(" e ")} por semana na agenda` : "Nada por semana na agenda";
}

function cardRedeHtml(rede, r) {
  const info = REDES[rede];
  const envio = valor(`${rede}.envio`);
  if (!valor(`${rede}.ativo`)) {
    // desligada: cartão curto, sem o formulário e os avisos (a página passava de 2.600 px)
    const via = ROTULO_ENVIO[envio] || envio;
    return h`<div class="card card-rede desligada" style="--cor:${info.cor}">
      <div class="card-topo"><div class="rede-titulo">${logoRede(rede)}<div><b>${info.nome}</b><small>Desligada · ${via}${r.conta ? ` · ${r.conta}` : ""}</small></div></div>${CONTROLES.switch({ chave: `${rede}.ativo`, rotulo: `Postar no ${info.rotulo}` })}</div>
    </div>`;
  }
  const aviso = avisoRede(rede, envio);
  const opcoes = opcoesRede(rede);
  const rodape = [];
  if (r.ativo) rodape.push(`${r.postados_24h} nas últimas 24 h`);
  // rede só de imagens: sem a linha do próximo vídeo
  if (r.ativo && r.proximo) rodape.push(`${r.proximo_imagem ? "próximo vídeo" : "próximo"}: ${fmtQuando(r.proximo)}`);
  if (r.ativo && r.proximo_imagem) rodape.push(`próxima imagem: ${fmtQuando(r.proximo_imagem)}`);
  const roteiroImagem = envio === "navegador" && redImgRedes().includes(rede);
  return h`<div class="card card-rede" style="--cor:${info.cor}">
    <div class="card-topo"><div class="rede-titulo">${logoRede(rede)}<div><b>${info.nome}</b><small>${redImgPorSemana(r)}</small></div></div>${CONTROLES.switch({ chave: `${rede}.ativo`, rotulo: `Postar no ${info.rotulo}` })}</div>
    ${statusContaHtml(r, envio)}
    ${aviso ? h`<div class="aviso-rede">${ic("alerta")}<span>${aviso}</span></div>` : ""}
    ${rede === "facebook" ? h`${redFbAvisosHtml()}${redFbPaginaHtml(r, envio)}` : ""}
    ${rede === "instagram" ? h`<p class="nota">A Página do Facebook agora tem cartão próprio, o "${REDES.facebook.nome}": ligue lá para os Reels saírem também na Página.</p>` : ""}
    ${redImgCaixasHtml(rede, r, envio)}
    ${formulario([envioHtml(rede, info, envio), ...(envio === "oficial" ? camposOficiais(rede) : [])])}
    ${notaEnvio(rede, info, envio)}
    ${roteiroImagem ? redImgRoteiroHtml(rede, r) : ""}
    ${opcoes.length ? h`<details class="guia"><summary>${ic("ajustes")}Opções dos posts</summary><div class="form-interno">${formulario(opcoes)}</div></details>` : ""}
    <div class="rodape-card"><div class="grupo-botoes">${botoesRede(rede, r, envio)}</div><small class="texto-suave">${rodape.join(" · ")}</small></div>
  </div>`;
}

/* ------------------------------------------------------------ Página do Facebook */

// sugestao: a Página com o nome informado que a última conferência viu ativa (falta você confirmar)
// janela: número da janela "antes de gravar" aberta agora (a resposta de uma antiga não redesenha a nova)
const redFbEstado = { sugestao: null, janela: 0 };

function redFbAvisosHtml() {
  const max = valor("facebook.max_segundos") || 90;
  return h`<div class="aviso-rede">${ic("alerta")}<span>Depois de ligar o Facebook aqui, desligue no Instagram o compartilhamento automático dos Reels no Facebook, em Central de Contas, senão o mesmo vídeo sai duas vezes na Página.</span></div>
    <p class="nota">Os cortes são de filmes de terceiros: poste só o que você tem direito de usar. Desde 13/03/2026 a Meta trata como não original juntar trechos com mudanças pequenas (bordas, legendas), o que pesa na distribuição e na monetização. A Página recebe Reels de até ${max} s: cortes mais longos ficam fora do Facebook.</p>`;
}

function redFbSituacao(p) {
  if (p.origem) return [`Página ${p.nome || p.id}, ${p.origem}`, "ok"];
  if (p.confirmada) return [`Confirmada: ${p.nome ? `${p.nome} (id ${p.id})` : `id ${p.id}`}`, "ok"];
  return ["Ainda não confirmada", "aviso"];
}

function redFbPaginaHtml(r, envio) {
  const p = r.pagina || {};
  let entrada;
  if (p.so_leitura) {
    const de = envio === "upload_post" ? "Vem da escolha na lista do Upload-Post (Escolher a Página)." : "Vem da Página escolhida ao conectar (Conectar conta).";
    entrada = linhaCampo("Página", de, h`<span class="valor-fixo">${p.nome || p.entrada || "nenhuma escolhida"}</span>`);
  } else {
    entrada = campo({ chave: "facebook.pagina", tipo: "texto", rotulo: "Página", ajuda: "O link da Página (facebook.com/SuaPagina ou profile.php?id=...) ou o nome exato dela. Antes de cada post eu confiro se a janela está agindo como ela, e não como o seu perfil pessoal ou outra Página.", placeholder: "https://www.facebook.com/SuaPagina", larga: true });
  }
  const linhas = [];
  const u = p.ultima;
  if (envio === "navegador" && u) {
    linhas.push(h`<p class="nota">${badge([u.confere ? "Confere" : "Não confere", u.confere ? "ok" : "erro"], true)} Última conferência ${fmtQuando(u.em)}: ${u.motivo}</p>`);
  }
  const s = !p.confirmada && redFbEstado.sugestao;
  if (envio === "navegador" && s) {
    linhas.push(h`<div class="aviso-rede">${ic("info")}<span>Agindo como a Página ${s.pagina_nome} (id ${s.pagina_id}). É esta?</span><button class="btn pequeno primario" data-acao="fb-usar" data-id="${s.pagina_id}">${ic("check")}Usar esta Página</button></div>`);
  }
  const botoes = [];
  if (envio === "navegador") {
    botoes.push(h`<button class="btn" data-acao="fb-perfil">${ic("olho")}Ver quem está ativo</button>`);
    botoes.push(h`<button class="btn" data-acao="fb-perfil" data-trocar="1">${ic("refazer")}Trocar para a Página</button>`);
  } else if (envio === "upload_post") {
    botoes.push(h`<button class="btn" data-acao="fb-pagina-up">${ic("lista")}Escolher a Página</button>`);
  }
  return h`<div class="card-interno"><div class="card-topo"><h3>${ic("pessoas")}Página que recebe os Reels</h3>${badge(redFbSituacao(p), true)}</div>
    <p class="nota">No Facebook você pode ter várias Páginas e o perfil pessoal: eu só posto agindo como a Página daqui.</p>
    ${formulario([entrada])}
    ${linhas}
    ${botoes.length ? h`<div class="grupo-botoes">${botoes}</div>` : ""}</div>`;
}

App.acoes["fb-perfil"] = async (el) => {
  if (!(await salvarDaRede(el, "facebook"))) return;
  const trocar = el.dataset.trocar === "1";
  toast(trocar ? "Trocando para a Página na janela do navegador..." : "Conferindo com que perfil a janela está agindo...", "info");
  const r = await ocupado(el, () => post("/redes/facebook/perfil", { trocar }));
  redFbEstado.sugestao = r.sugestao || null;
  toast(`Facebook: ${r.motivo}`, r.confere ? "ok" : "info", 8000);
  await App.paginas.redes.recarregar();
};

App.acoes["fb-usar"] = async (el) => {
  const r = await ocupado(el, () => post("/redes/facebook/perfil", { usar: el.dataset.id }));
  redFbEstado.sugestao = null;
  toast(`Facebook: ${r.motivo}`, "ok", 7000);
  if (el.dataset.depois === "gravar" && modal.aberto) { montar(corpoModal(), redFbAntesHtml(r)); return; }
  await App.paginas.redes.recarregar();
};

/* Upload-Post: a Página do perfil que recebe os Reels */
App.acoes["fb-pagina-up"] = async (el) => {
  if (!(await salvarDaRede(el, "facebook"))) return;
  const r = await ocupado(el, () => post("/redes/facebook/pagina"));
  const paginas = r.paginas || [];
  abrirModal({
    titulo: "Qual Página recebe os Reels?",
    largura: 520,
    corpo: paginas.length
      ? h`<div class="contas-ig">${paginas.map((p, i) => h`<label><input type="radio" name="fb-pagina" value="${p.id}" ${(r.atual ? p.id === r.atual : i === 0) ? raw("checked") : ""}><span><b>${p.nome}</b> <span class="texto-suave">id ${p.id}</span></span></label>`)}</div><div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button><button class="btn primario" data-acao="fb-pagina-up-usar">${ic("check")}Usar esta Página</button></div>`
      : h`<p class="nota">Nenhuma Página do Facebook está conectada no perfil do Upload-Post. Conecte no site do Upload-Post e tente de novo.</p><div class="acoes-modal"><button class="btn primario" data-acao="fechar-modal">Fechar</button></div>`,
  });
};

App.acoes["fb-pagina-up-usar"] = async (el) => {
  const marcado = document.querySelector("input[name=fb-pagina]:checked");
  if (!marcado) return;
  await ocupado(el, () => post("/redes/facebook/pagina", { id: marcado.value }));
  fecharModal();
  toast("Página do Facebook escolhida", "ok");
  await App.paginas.redes.recarregar();
};

/* API oficial: o token colado e a escolha da Página, como no Instagram */
function redFbConectar() {
  abrirModal({
    titulo: "Conectar a Página do Facebook",
    largura: 640,
    corpo: h`<ol class="passos">
        <li>Abra o ${link("https://developers.facebook.com/tools/explorer/", "Graph API Explorer")}.</li>
        <li>Em "App da Meta", escolha o seu app (App ID ${valor("facebook.app_id") || valor("instagram.app_id") || "?"}).</li>
        <li>Em "Usuário ou Página", escolha "Obter token de acesso do usuário".</li>
        <li>Adicione as permissões: <code class="permissoes">${RED_FB_PERMISSOES}</code></li>
        <li>Clique em "Generate Access Token", autorize a Página e copie o token.</li>
      </ol>
      <label class="campo"><span>Token de acesso</span><textarea id="fb-token" rows="3" placeholder="Cole o token aqui" spellcheck="false" autocomplete="off"></textarea></label>
      <p class="nota">O token é trocado por um de longa duração e fica salvo só neste computador.</p>
      <div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button><button class="btn primario" data-acao="fb-enviar-token">${ic("link")}Conectar</button></div>`,
  });
}

App.acoes["fb-enviar-token"] = async (el) => {
  const token = $("#fb-token").value.trim();
  if (!token) { toast("Cole o token gerado no Graph API Explorer", "erro"); return; }
  const s = await ocupado(el, () => post("/redes/facebook/login", { token }));
  if (s.estado === "ok") {
    fecharModal();
    toast(`Facebook conectado: ${s.mensagem}`, "ok", 7000);
    await App.paginas.redes.recarregar();
    atualizarEstado();
    return;
  }
  if (s.estado === "escolher") {
    tituloModal("Qual Página vai receber os Reels?");
    montar(corpoModal(), h`<div class="contas-ig">${s.contas.map((c, i) => h`<label><input type="radio" name="fb-conta" value="${c.indice}" ${i === 0 ? raw("checked") : ""}><span><b>${c.pagina || "?"}</b></span></label>`)}</div><div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button><button class="btn primario" data-acao="fb-escolher">${ic("check")}Usar esta Página</button></div>`);
  }
};

App.acoes["fb-escolher"] = async (el) => {
  const marcado = document.querySelector("input[name=fb-conta]:checked");
  if (!marcado) return;
  const s = await ocupado(el, () => post("/redes/facebook/conta", { indice: Number(marcado.value) }));
  fecharModal();
  toast(`Facebook conectado: ${s.mensagem}`, "ok", 7000);
  await App.paginas.redes.recarregar();
  atualizarEstado();
};

/* Antes de gravar o roteiro do Facebook: a janela precisa estar agindo como a Página (na gravação, quem troca é você) */
function redFbAntesHtml(r, erro = "") {
  if (!r && !erro) {
    return h`<div class="login-espera"><span class="giro"></span><p>Conferindo com que perfil a janela do navegador está agindo...</p><div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button></div></div>`;
  }
  const confere = Boolean(r && r.confere);
  const s = r && r.sugestao;
  return h`<div class="login-espera">
    ${erro ? h`<p class="erro-texto">${erro}</p>` : h`<p>${badge([confere ? "Confere" : "Não confere", confere ? "ok" : "erro"], true)} ${r.motivo}</p>`}
    ${s ? h`<div class="aviso-rede">${ic("info")}<span>Agindo como a Página ${s.pagina_nome} (id ${s.pagina_id}). É esta?</span><button class="btn pequeno primario" data-acao="fb-usar" data-id="${s.pagina_id}" data-depois="gravar">${ic("check")}Usar esta Página</button></div>` : ""}
    <p class="nota">O roteiro sai da Página que estiver ativa na janela: grave agindo como a Página que recebe os Reels, e não como o seu perfil pessoal.</p>
    <div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button><button class="btn" data-acao="fb-antes-trocar">${ic("refazer")}Trocar para a Página</button><button class="btn primario" data-acao="rede-gravar" data-rede="facebook" data-conferido="1" ${confere ? "" : raw("disabled")}>${ic("microfone")}Começar a gravar</button></div></div>`;
}

async function redFbConferirAntes(trocar) {
  const janela = redFbEstado.janela;
  let r = null;
  let erro = "";
  try {
    r = await post("/redes/facebook/perfil", { trocar });
  } catch (e) {
    erro = (e && e.message) || String(e);
  }
  if (janela !== redFbEstado.janela || !modal.aberto) return; // a janela fechou ou outra tomou o lugar
  montar(corpoModal(), redFbAntesHtml(r, erro));
}

async function redFbAntesDeGravar() {
  const janela = ++redFbEstado.janela;
  abrirModal({
    titulo: "Antes de gravar o roteiro do Facebook",
    largura: 560,
    corpo: redFbAntesHtml(null),
    aoFechar: () => { if (redFbEstado.janela === janela) redFbEstado.janela++; },
  });
  await redFbConferirAntes(false);
}

App.acoes["fb-antes-trocar"] = async (el) => {
  montar(corpoModal(), redFbAntesHtml(null));
  await redFbConferirAntes(true);
};

function publicacaoHtml(d) {
  if (d.simulacao) {
    return h`<div class="card destaque-card"><div class="estado-postagens">${ic("info", "grande")}<div><b>Modo simulação ligado</b><p class="nota">Os vídeos são editados e os horários seguem a agenda, mas nada é enviado. Quando as contas estiverem conectadas e testadas, ative a publicação real.</p></div></div><div class="linha-botoes"><button class="btn primario" data-acao="modo-real">${ic("aviao")}Ativar a publicação real</button></div></div>`;
  }
  return h`<div class="card destaque-card real"><div class="estado-postagens">${ic("aviao", "grande")}<div><b>Publicando de verdade</b><p class="nota">Nos horários da agenda, cada rede ativa recebe o próximo corte da fila.</p></div></div><div class="linha-botoes"><button class="btn" data-acao="modo-simulacao">${ic("pausa")}Voltar ao modo simulação</button></div></div>`;
}

function uploadPostHtml() {
  const usando = ORDEM_REDES.filter((r) => valor(`${r}.envio`) === "upload_post");
  return h`<div class="card"><div class="card-topo"><h3>${ic("aviao")}Upload-Post (opcional)</h3>${usando.length ? badge([`Usado por ${usando.map((r) => REDES[r].rotulo).join(", ")}`, "info"], true) : badge(["Não usado", "neutro"], true)}</div>
    <p class="nota">Serviço pago que já passou pela auditoria das redes e publica em público, inclusive no TikTok. Seus vídeos e o acesso às contas passam por ele (${link("https://www.upload-post.com/privacy-policy", "política de privacidade")}). O TikTok não está no plano grátis.</p>
    ${formulario([
      campo({ chave: "upload_post.api_key", tipo: "senha", rotulo: "Chave da API" }),
      campo({ chave: "upload_post.perfil", tipo: "texto", rotulo: "Nome do perfil", placeholder: "o perfil criado no Upload-Post" }),
    ])}
    <details class="guia"><summary>${ic("info")}Como configurar</summary><ol>
      <li>Crie a conta em ${link("https://www.upload-post.com/", "upload-post.com")} e gere a chave da API.</li>
      <li>Em ${link("https://app.upload-post.com/manage-users", "Manage users")}, crie um perfil e conecte nele as redes.</li>
      <li>Preencha a chave e o nome do perfil aqui e salve.</li>
      <li>Em cada rede abaixo, escolha "Upload-Post" na forma de envio, salve e clique em Testar conexão.</li>
    </ol></details>
  </div>`;
}

function manualHtml(d) {
  const usando = ORDEM_REDES.filter((r) => valor(`${r}.envio`) === "manual" && valor(`${r}.ativo`));
  const maximo = (App.meta && App.meta.max_tarefas) || 5;
  return h`<div class="card"><div class="card-topo"><h3>${ic("lista")}Postagem à mão</h3>${usando.length ? badge([`Usada por ${usando.map((r) => REDES[r].rotulo).join(", ")}`, "info"], true) : badge(["Não usada", "neutro"], true)}</div>
    <p class="nota">Para redes sem API aberta (Kwai e Bilibili) ou quando você prefere postar pelo app. No horário da agenda, o AutoCortes cria uma tarefa no Início com o vídeo e os textos no formato da rede, e você marca "Já postei" depois. Cada rede junta no máximo ${maximo} tarefas esperando; com mais que isso, os horários seguintes ficam sem tarefa nova.${d.simulacao ? " As tarefas saem também no modo simulação, porque quem posta é você." : ""}</p>
    ${formulario([
      campo({ chave: "manual.pasta", tipo: "texto", rotulo: "Pasta sincronizada (opcional)", ajuda: "Cada tarefa copia o vídeo e um .txt com os textos para esta pasta, numa subpasta por rede. Use uma pasta do OneDrive ou do Google Drive para pegar no celular. As cópias somem quando a tarefa é feita ou pulada.", placeholder: "ex.: C:\\Users\\voce\\OneDrive\\AutoCortes", larga: true }),
    ])}
  </div>`;
}

function navegadorHtml() {
  const usando = ORDEM_REDES.filter((r) => valor(`${r}.envio`) === "navegador" && valor(`${r}.ativo`));
  const ensaio = valor("navegador.ensaio");
  return h`<div class="card card-navegador"><div class="card-topo"><h3>${ic("externo")}Postagem pelo navegador</h3>${usando.length ? badge([`Usada por ${usando.map((r) => REDES[r].rotulo).join(", ")}`, "aviso"], true) : badge(["Não usada", "neutro"], true)}</div>
    <div class="aviso-rede">${ic("alerta")}<span>Contraria os termos das redes, que só autorizam as APIs oficiais, e pode custar a conta. O AutoCortes não disfarça nada: sem forjar fingerprint, sem resolver captcha e sem proxy. Se a rede pedir verificação, o envio para e avisa você.</span></div>
    <p class="nota">O AutoCortes abre o Chrome com um perfil separado, em <code>dados/chrome</code>, onde você entra na conta uma vez. Depois ele preenche a página de envio da rede como se fosse você. Funciona no YouTube, TikTok, Instagram e Bilibili; o Kwai não tem envio pelo site.</p>
    ${ensaio ? h`<div class="aviso-rede">${ic("info")}<span>Ensaio ligado: os envios preenchem tudo e <b>não publicam</b>, guardando uma imagem da tela em <code>dados/navegador</code>. A postagem fica registrada como ensaio, e o corte continua na fila para quando você desligar o ensaio.</span></div>` : ""}
    ${formulario([
      campo({ chave: "navegador.ensaio", tipo: "switch", rotulo: "Ensaio (não publica)", ajuda: "Preenche a página e para antes de publicar. Deixe ligado no primeiro teste de cada rede." }),
      campo({ chave: "navegador.visivel", tipo: "switch", rotulo: "Mostrar a janela", ajuda: "Precisa estar ligado para você entrar na conta. Desligado, o navegador roda escondido." }),
      campo({ chave: "navegador.programa", tipo: "texto", rotulo: "Caminho do navegador", ajuda: "Em branco, acha o Chrome e, se não houver, o Edge.", placeholder: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", larga: true }),
      campo({ chave: "navegador.porta", tipo: "numero", min: 1024, max: 65535, rotulo: "Porta do DevTools", ajuda: "Só em 127.0.0.1. Troque se essa porta já estiver em uso." }),
      campo({ chave: "navegador.pausa_min_seg", tipo: "numero", min: 0, max: 30, passo: 0.1, sufixo: "s", rotulo: "Pausa mínima entre passos" }),
      campo({ chave: "navegador.pausa_max_seg", tipo: "numero", min: 0, max: 60, passo: 0.1, sufixo: "s", rotulo: "Pausa máxima entre passos" }),
      campo({ chave: "navegador.tempo_limite_seg", tipo: "numero", min: 30, max: 1800, sufixo: "s", rotulo: "Tempo limite de cada passo" }),
    ])}
  </div>`;
}

async function carregarRedes(token = App.navegacao) {
  const d = await api("/redes");
  if (!App.ativa(token)) return null;
  PgRedes.dados = d;
  App.config = d.config;
  App.meta = d.meta;
  return d;
}

function desenharRedes() {
  const d = PgRedes.dados;
  const el = $("#conteudo");
  if (!d || !el || App.nomePagina !== "redes") return;
  montar(el, h`${publicacaoHtml(d)}
    <div class="cards-redes">${ORDEM_REDES.map((r) => cardRedeHtml(r, d.redes[r]))}</div>
    <div class="grade-2">${uploadPostHtml()}${manualHtml(d)}</div>
    ${navegadorHtml()}
    <p class="nota rodape-card">O caminho seguro é a API oficial de cada rede, ou o Upload-Post. O envio pelo navegador contraria os termos e pode custar a conta: você escolhe rede por rede. Cortes de filmes são conteúdo de terceiros: poste só o que você tem direito de usar, e saiba que as redes tratam trechos com pouca edição como conteúdo não original na monetização e nas recomendações.</p>`);
}

/* ------------------------------------------------------------ login */

function pararLogin() {
  Login.ativo = false;
  clearTimeout(Login.timer);
}

async function acompanharLogin(rede) {
  if (!Login.ativo || Login.rede !== rede) return;
  let s;
  try {
    s = await api(`/redes/${rede}/login`);
  } catch (e) {
    Login.timer = setTimeout(() => acompanharLogin(rede), 3000);
    return;
  }
  if (!Login.ativo) return;
  if (s.estado === "ok") {
    pararLogin();
    fecharModal();
    toast(`${REDES[rede].rotulo} conectado: ${s.mensagem}`, "ok", 7000);
    await App.paginas.redes.recarregar();
    atualizarEstado();
  } else if (s.estado === "erro") {
    pararLogin();
    montar(corpoModal(), h`<div class="login-espera">${ic("erro", "grande")}<p class="erro-texto">${s.mensagem}</p><div class="acoes-modal"><button class="btn primario" data-acao="fechar-modal">Fechar</button></div></div>`);
  } else if (s.estado === "aguardando") {
    Login.timer = setTimeout(() => acompanharLogin(rede), 2000);
  } else {
    pararLogin();
  }
}

async function conectarOAuth(rede) {
  const s = await post(`/redes/${rede}/login`);
  const url = urlSegura(s.url);
  if (url) window.open(url, "_blank", "noopener");
  Login.rede = rede;
  Login.ativo = true;
  abrirModal({
    titulo: `Conectar o ${REDES[rede].rotulo}`,
    largura: 520,
    corpo: h`<div class="login-espera"><span class="giro"></span><p>Autorize o acesso na aba que abriu. Esta janela fecha sozinha quando terminar.</p>${url ? h`<p class="nota">A aba não abriu? ${link(url, "Abrir a página de autorização")}</p>` : ""}<div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button></div></div>`,
    aoFechar: () => {
      if (Login.ativo && Login.rede === rede) post(`/redes/${rede}/cancelar`).catch(() => {});
      pararLogin();
    },
  });
  Login.timer = setTimeout(() => acompanharLogin(rede), 2000);
}

function conectarInstagram() {
  abrirModal({
    titulo: "Conectar o Instagram",
    largura: 640,
    corpo: h`<ol class="passos">
        <li>Abra o ${link("https://developers.facebook.com/tools/explorer/", "Graph API Explorer")}.</li>
        <li>Em "App da Meta", escolha o seu app (App ID ${valor("instagram.app_id") || "?"}).</li>
        <li>Em "Usuário ou Página", escolha "Obter token de acesso do usuário".</li>
        <li>Adicione as permissões: <code class="permissoes">${PERMISSOES_IG}</code></li>
        <li>Clique em "Generate Access Token", autorize a Página e a conta do Instagram e copie o token.</li>
      </ol>
      <label class="campo"><span>Token de acesso</span><textarea id="ig-token" rows="3" placeholder="Cole o token aqui" spellcheck="false" autocomplete="off"></textarea></label>
      <p class="nota">O token é trocado por um de longa duração e fica salvo só neste computador.</p>
      <div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button><button class="btn primario" data-acao="ig-enviar-token">${ic("link")}Conectar</button></div>`,
  });
}

App.acoes["ig-enviar-token"] = async (el) => {
  const token = $("#ig-token").value.trim();
  if (!token) { toast("Cole o token gerado no Graph API Explorer", "erro"); return; }
  const s = await ocupado(el, () => post("/redes/instagram/login", { token }));
  if (s.estado === "ok") {
    fecharModal();
    toast(`Instagram conectado: ${s.mensagem}`, "ok", 7000);
    await App.paginas.redes.recarregar();
    atualizarEstado();
    return;
  }
  if (s.estado === "escolher") {
    tituloModal("Qual conta vai receber os Reels?");
    montar(corpoModal(), h`<div class="contas-ig">${s.contas.map((c, i) => h`<label><input type="radio" name="ig-conta" value="${c.indice}" ${i === 0 ? raw("checked") : ""}><span><b>@${c.usuario || "?"}</b> <span class="texto-suave">Página ${c.pagina || "?"}</span></span></label>`)}</div><div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button><button class="btn primario" data-acao="ig-escolher">${ic("check")}Usar esta conta</button></div>`);
  }
};

App.acoes["ig-escolher"] = async (el) => {
  const marcado = document.querySelector("input[name=ig-conta]:checked");
  if (!marcado) return;
  const s = await ocupado(el, () => post("/redes/instagram/conta", { indice: Number(marcado.value) }));
  fecharModal();
  toast(`Instagram conectado: ${s.mensagem}`, "ok", 7000);
  await App.paginas.redes.recarregar();
  atualizarEstado();
};

function avisoNavegadorHtml(rede, s) {
  return h`<div class="login-espera"><p>${ic("externo")}Abri a janela do Chrome do AutoCortes no login do ${REDES[rede].rotulo}.</p>
    <ol class="passos">
      <li>Entre na conta nessa janela (e resolva o 2FA, se pedir).</li>
      <li>A sessão fica salva no perfil do AutoCortes, não no seu Chrome normal.</li>
      <li>Volte aqui e clique em <b>Testar a sessão</b>.</li>
    </ol>
    <p class="nota">A janela não abriu? Confira o caminho do Chrome no cartão "Postagem pelo navegador".${s && s.url ? h` Endereço: <code>${s.url}</code>` : ""}</p>
    <div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Fechar</button><button class="btn primario" data-acao="rede-testar" data-rede="${rede}">${ic("ok")}Testar a sessão</button></div></div>`;
}

/** Antes de conectar, testar ou gravar: salva só o que é desta rede e da forma de envio dela.
    Salvar tudo levava junto o que você ainda estava testando no Estúdio ou nas Configurações. */
async function salvarDaRede(el, rede) {
  const partes = [rede, "navegador", "upload_post"];
  if (vazio(pendentesDe(partes))) return true;
  return ocupado(el, () => salvarConfig(partes));
}

App.acoes["rede-conectar"] = async (el) => {
  const rede = el.dataset.rede;
  if (!(await salvarDaRede(el, rede))) return;
  if (valor(`${rede}.envio`) === "navegador") {
    const s = await ocupado(el, () => post(`/redes/${rede}/login`));
    abrirModal({ titulo: `Entrar no ${REDES[rede].rotulo} pelo navegador`, largura: 560, corpo: avisoNavegadorHtml(rede, s) });
    return;
  }
  if (rede === "instagram") { conectarInstagram(); return; }
  if (rede === "facebook") { redFbConectar(); return; }
  await ocupado(el, () => conectarOAuth(rede));
};

App.acoes["rede-testar"] = async (el) => {
  const rede = el.dataset.rede;
  if (!(await salvarDaRede(el, rede))) return;
  if (valor(`${rede}.envio`) === "navegador") toast("Abrindo a página da rede no navegador...", "info");
  let r;
  try {
    r = await ocupado(el, () => post(`/redes/${rede}/testar`));
  } catch (e) {
    // Facebook pelo nome: a Página ativa tem esse título, e o cartão pergunta se é ela
    if (rede === "facebook" && e && e.dados && e.dados.sugestao) {
      redFbEstado.sugestao = e.dados.sugestao;
      if (modal.aberto) fecharModal();
      await App.paginas.redes.recarregar();
    }
    throw e;
  }
  if (rede === "facebook") redFbEstado.sugestao = null;
  if (modal.aberto) fecharModal();
  toast(`${REDES[rede].rotulo}: ${r.conta}`, "ok", 7000);
  await App.paginas.redes.recarregar();
};

/* ------------------------------------------------------------ aprender a postar */

// sessao: número da gravação aberta agora. A janela de uma gravação antiga, ao fechar, não pode
// cancelar a gravação que acabou de começar ("Gravar de novo" abria e já cancelava a nova).
// midia: "video" ou "imagem"; tipo: o tipo das imagens de exemplo (o post que sai na gravação de imagem)
const Gravacao = { rede: null, timer: null, sessao: 0, passos: 0, midia: "video", tipo: null };

function marcasHtml(marcas) {
  const nomes = { titulo: "Título", descricao: "Descrição", legenda: "Legenda", tags: "Tags", fonte: "Fonte (转载来源)" };
  return h`<div class="marcas">${Object.entries(marcas || {}).map(([papel, marca]) => h`<button type="button" class="tag-copiar" data-acao="copiar" data-texto="${marca}" title="Copiar ${marca}"><b>${nomes[papel] || papel}</b><code>${marca}</code>${ic("copiar")}</button>`)}</div>`;
}

const ICONE_PASSO = { arquivo: "video", clicar: "seta-dir", digitar: "texto", tecla: "terminal" };

function camposGravacaoHtml(s) {
  const achados = new Set((s.lista || []).map((p) => p.papel).filter(Boolean));
  // o título do post de fotos do TikTok é opcional: sem ele, a legenda basta
  const opcional = (papel) => s.midia === "imagem" && papel === "titulo";
  return h`<div class="marcas">${Object.keys(s.marcas || {}).map((papel) => badge([
    `${NOME_PAPEL[papel] || papel}${opcional(papel) ? " (opcional)" : ""}: ${achados.has(papel) ? "reconhecido" : "aguardando"}`,
    achados.has(papel) ? "ok" : "neutro",
  ], true))}</div>`;
}

function logGravacaoHtml(s) {
  const lista = s.lista || [];
  if (!lista.length) {
    return h`<div class="grava-log vazio-log">${ic("microfone")}<span>Nada anotado ainda. Faça o primeiro passo na janela do Chrome.</span></div>`;
  }
  const icone = (p) => (p.tipo === "arquivo" && s.midia === "imagem" ? "imagem" : ICONE_PASSO[p.tipo] || "lista");
  return h`<div class="grava-log">${lista.map((p) => h`<div class="grava-passo${p.fragil ? " fragil" : ""}${p.estranha ? " ruim" : ""}"><span class="n">${p.n}</span>${ic(icone(p))}<span class="txt">${p.texto}</span>${p.papel ? badge([NOME_PAPEL[p.papel] || p.papel, "ok"], true) : ""}${p.fragil ? h`<span class="marca-fragil" title="Sem âncora estável: esse passo é o que quebra mais fácil se a página mudar">${ic("alerta")}</span>` : ""}</div>`)}</div>`;
}

function gravandoHtml(rede, s) {
  const marcas = Object.keys(s.marcas || {}).length;
  return h`<div class="gravando">
    <p><span class="ponto-grava"></span>Gravando. Faça a postagem na janela do Chrome que abriu, do começo até publicar.</p>
    <ol class="passos">
      <li>Escolha o vídeo normalmente (qualquer vídeo seu serve).</li>
      ${marcas ? h`<li>Você pode <b>colar estas marcas nos campos de texto</b> (clique para copiar), para eu preencher cada um com o texto do corte:${marcasHtml(s.marcas)}</li>`
    : h`<li>Escreva a legenda normalmente, como você postaria de verdade. Aqui tem um campo de texto só, então eu descubro sozinho.</li>`}
      <li>Ajuste o resto como você quiser (privacidade, capa, opções) — eu repito igual.</li>
      <li>Clique em publicar e espere a confirmação aparecer na tela.</li>
      <li>Volte aqui e clique em <b>Terminei</b>.</li>
    </ol>
    ${rede === "youtube" ? h`<p class="nota">No YouTube, cole <code>@@TAGS@@</code> no campo <b>Tags</b>, que aparece ao clicar em <b>Mostrar mais</b>. Título e descrição recebem suas marcas nos campos correspondentes.</p>` : ""}
    ${gravaAcompanharHtml(rede, s)}</div>`;
}

/** O que a gravação de imagem pede: o próximo post de verdade, escolhido pelo botão da página e com a legenda real. */
function redImgGravandoHtml(rede, s) {
  const c = s.exemplo_corte || {};
  const qual = c.parte ? `${c.filme} - Parte ${c.parte}` : c.filme || "um corte";
  const n = s.exemplo_quantos || 1;
  const usar = s.exemplo_tipo === "carrossel"
    ? `Use as ${n} imagens desta pasta (são o próximo carrossel de '${qual}'), na ordem, e cole @@LEGENDA@@ na legenda.`
    : `Use a imagem desta pasta (é o próximo post de imagem de '${qual}') e cole @@LEGENDA@@ na legenda.`;
  const marcas = Object.fromEntries(Object.entries(s.marcas || {}).filter(([papel]) => papel !== "legenda"));
  return h`<div class="gravando">
    <p><span class="ponto-grava"></span>Gravando o roteiro de imagem. Faça a postagem na janela do Chrome que abriu, do começo até publicar.</p>
    <div class="aviso-rede">${ic("alerta")}<span><b>Este post vai sair de verdade na sua conta.</b> ${usar} Fica em: <code class="caminho">${s.exemplos || ""}</code></span></div>
    <ol class="passos">
      <li><b>Escolha as imagens pelo botão de escolher arquivo da própria página (não arraste).</b> Arrastadas para a página, eu não consigo repetir.</li>
      ${RED_IMG_DICA[rede] ? h`<li>${RED_IMG_DICA[rede]}</li>` : ""}
      <li>Cole <code>@@LEGENDA@@</code> no campo da legenda. Antes de publicar, troque pela legenda de verdade:
        <button type="button" class="btn pequeno" data-acao="copiar" data-texto="${s.exemplo_legenda || ""}">${ic("copiar")}Copiar a legenda</button></li>
      ${Object.keys(marcas).length ? h`<li>Se quiser, cole também estas marcas (clique para copiar):${marcasHtml(marcas)}</li>` : ""}
      <li>Se a página preencher um campo com o nome do arquivo (exemplo-1), apague e cole a marca dele (no TikTok, <code>@@TITULO@@</code> no título): este post sai de verdade.</li>
      <li>Espere o envio das imagens terminar antes de clicar em publicar. Clicando antes, o roteiro sai com o clique cedo demais.</li>
      <li>Clique em publicar, espere a confirmação aparecer e volte aqui em <b>Terminei</b>.</li>
    </ol>
    ${gravaAcompanharHtml(rede, s)}</div>`;
}

/** O acompanhamento ao vivo e os botões, iguais nas gravações de vídeo e de imagem. */
function gravaAcompanharHtml(rede, s) {
  return h`<div class="rotulo-campo">Reconhecimento dos campos</div>
    <div id="grava-campos">${camposGravacaoHtml(s)}</div>
    <div class="rotulo-campo">O que eu anotei até agora</div>
    <div id="grava-log">${logGravacaoHtml(s)}</div>
    <div class="grava-estado">${ic("lista")}<span id="grava-contagem">${plural(s.passos || 0, "passo anotado", "passos anotados")}</span><small id="grava-url">${(s.url || "").slice(0, 60)}</small></div>
    <div id="grava-erro">${s.erro ? h`<p class="erro-texto">${s.erro}</p>` : ""}</div>
    <p class="nota">As marcas são opcionais: o roteiro é salvo mesmo se algum campo ficar sem identificação. Textos capturados sem marca reconhecida serão repetidos como foram digitados.</p>
    <p class="nota">Se um clique não aparecer na lista, repita ele na janela. O ${ic("alerta")} marca o passo sem âncora estável, que é o que quebra mais fácil. Campo de senha nunca é gravado.</p>
    <div class="acoes-modal">
      <button class="btn fantasma" data-acao="grava-cancelar" data-rede="${rede}">Cancelar</button>
      <button class="btn" data-acao="grava-desfazer" data-rede="${rede}">${ic("refazer")}Desfazer o último</button>
      <button class="btn" data-acao="grava-recomecar" data-rede="${rede}">Recomeçar</button>
      <button class="btn primario" data-acao="grava-fim" data-rede="${rede}">${ic("check")}Terminei</button>
    </div>`;
}

function pararGravacao() {
  clearTimeout(Gravacao.timer);
  Gravacao.rede = null;
  Gravacao.passos = 0;
}

async function acompanharGravacao(rede, sessao) {
  if (Gravacao.rede !== rede || Gravacao.sessao !== sessao) return;
  let s;
  try {
    s = await api(`/redes/${rede}/gravar`);
  } catch (e) {
    Gravacao.timer = setTimeout(() => acompanharGravacao(rede, sessao), 3000);
    return;
  }
  if (Gravacao.rede !== rede || Gravacao.sessao !== sessao) return;
  desenharGravacao(s);
  if (s.erro) return;
  Gravacao.timer = setTimeout(() => acompanharGravacao(rede, sessao), 1500);
}

function desenharGravacao(s) {
  Gravacao.passos = s.passos || 0;
  const contagem = $("#grava-contagem");
  if (contagem) contagem.textContent = plural(s.passos || 0, "passo anotado", "passos anotados");
  const url = $("#grava-url");
  if (url) url.textContent = (s.url || "").slice(0, 60);
  const log = $("#grava-log");
  if (log) {
    const antes = log.scrollHeight - log.scrollTop <= log.clientHeight + 30;
    if (montarSeMudou(log, logGravacaoHtml(s)) && antes) log.scrollTop = log.scrollHeight;
  }
  montarSeMudou($("#grava-erro"), s.erro ? h`<p class="erro-texto">${s.erro}</p>` : "");
  montarSeMudou($("#grava-campos"), camposGravacaoHtml(s));
}

/** As imagens de exemplo levam até uns 2 min para sair: o botão diz o que está acontecendo. */
async function redImgPreparando(el, trabalho) {
  const antes = el.innerHTML;
  montar(el, h`${ic("relogio")}Preparando as imagens de exemplo...`);
  toast("Preparando as imagens de exemplo (o próximo post de imagem desta rede)...", "info", 7000);
  try {
    return await ocupado(el, trabalho);
  } finally {
    if (el.isConnected) el.innerHTML = antes;
  }
}

/** Na gravação de imagem com algum passo anotado: o post pode ter saído na rede. */
function redImgPerguntarPublicou() {
  return confirmar({
    titulo: "Você chegou a publicar?",
    texto: "Se o post saiu na rede, eu deixo ele em \"A conferir\" no Início, para não postar as mesmas imagens de novo.",
    botao: "Sim, publiquei",
    cancelar: "Não publiquei",
  });
}

async function redImgCancelarNoServidor(rede, publicou) {
  const r = await post(`/redes/${rede}/gravar/cancelar`, { publicou });
  if (r.conferir) toast("O post da gravação ficou em \"A conferir\" no Início: diga lá se ele saiu.", "info", 8000);
  return r;
}

App.acoes["rede-gravar"] = async (el) => {
  const rede = el.dataset.rede;
  const midia = el.dataset.midia === "imagem" ? "imagem" : "video";
  if (!(await salvarDaRede(el, rede))) return;
  // no Facebook, antes: com que perfil a janela está agindo (o "Começar a gravar" de lá volta aqui)
  if (rede === "facebook" && midia === "video" && el.dataset.conferido !== "1") { await redFbAntesDeGravar(); return; }
  redFbEstado.janela++; // a conferência que ainda estiver respondendo não redesenha a janela da gravação
  // a janela anterior (o resultado de outra gravação) fecha antes de a nova começar
  fecharModal(true);
  let s;
  if (midia === "imagem") {
    const token = App.navegacao;
    s = await redImgPreparando(el, () => post(`/redes/${rede}/gravar`, { midia }));
    if (!App.ativa(token)) {
      // você saiu da página enquanto as imagens saíam: sem a janela, ninguém termina a gravação
      redImgCancelarNoServidor(rede, false).catch(() => {});
      toast("Gravação cancelada: você saiu de Redes sociais antes de as imagens ficarem prontas.", "info", 7000);
      return;
    }
  } else {
    s = await ocupado(el, () => post(`/redes/${rede}/gravar`));
  }
  const sessao = ++Gravacao.sessao;
  Gravacao.rede = rede;
  Gravacao.passos = s.passos || 0;
  Gravacao.midia = midia;
  Gravacao.tipo = s.exemplo_tipo || null;
  abrirModal({
    titulo: midia === "imagem" ? `Gravar o roteiro de imagem do ${REDES[rede].rotulo}` : `Aprender a postar no ${REDES[rede].rotulo}`,
    largura: 660,
    corpo: midia === "imagem" ? redImgGravandoHtml(rede, s) : gravandoHtml(rede, s),
    // fechar sem querer (Esc, clique fora) cancelaria a gravação com tudo que já foi anotado
    protegido: () => Gravacao.sessao === sessao && Gravacao.rede === rede && Gravacao.passos > 0,
    aoFechar: () => {
      if (Gravacao.sessao !== sessao) return; // outra gravação já tomou o lugar desta
      const aberta = Gravacao.rede === rede;
      const perguntar = aberta && midia === "imagem" && Gravacao.passos > 0;
      pararGravacao();
      if (!aberta) return;
      if (!perguntar) { post(`/redes/${rede}/gravar/cancelar`).catch(() => {}); return; }
      redImgPerguntarPublicou().then((publicou) => {
        if (Gravacao.sessao === sessao) redImgCancelarNoServidor(rede, publicou).catch(() => {});
      });
    },
  });
  Gravacao.timer = setTimeout(() => acompanharGravacao(rede, sessao), 2000);
};

const NOME_PAPEL = { titulo: "título", descricao: "descrição", legenda: "legenda", tags: "tags", fonte: "fonte" };

App.acoes["grava-fim"] = async (el) => {
  const rede = el.dataset.rede;
  const midia = Gravacao.midia;
  const r = await ocupado(el, () => post(`/redes/${rede}/gravar/fim`));
  pararGravacao();
  if (midia === "imagem") { await redImgFimDaGravacao(rede, r); return; }
  const nomes = (lista) => (lista || []).map((p) => NOME_PAPEL[p] || p).join(", ");
  if (!r.ok) {
    const motivos = [];
    if (r.estranhas && r.estranhas.length) {
      motivos.push(h`<p class="erro-texto">Você colou <code>${r.estranhas[0]}</code>, que eu não reconheço. Use uma das marcas que eu mostrei: clique nelas para copiar, em vez de digitar.</p>`);
    }
    if (r.faltando && r.faltando.length) {
      motivos.push(h`<p class="erro-texto">Não achei onde vai ${nomes(r.faltando)}. Cole a marca desse campo no lugar do texto.</p>`);
      if (rede === "youtube" && r.faltando.includes("tags")) {
        motivos.push(h`<p class="nota">Abra <b>Mostrar mais</b> no YouTube e cole <code>@@TAGS@@</code> no campo <b>Tags</b>.</p>`);
      }
    }
    if (!r.tem_arquivo) {
      motivos.push(h`<p class="erro-texto">Não vi você escolher o vídeo. Escolha o vídeo na janela do AutoCortes, não em outra.</p>`);
    }
    if (r.erros && r.erros.length) {
      motivos.push(h`<p class="erro-texto">${r.erros.map((erro) => h`${erro}<br>`)}</p>`);
    }
    const d = r.diagnostico;
    if (d) {
      motivos.push(h`<details class="guia"><summary>${ic("info")}O que eu estava vendo na página (me mande isto se travar de novo)</summary><div class="diag">
        <div>Campos de texto na página: <b>${d.campos}</b></div>
        <div>Com algo escrito: ${(d.com_texto || []).length ? (d.com_texto || []).map((c) => h`<code>${c.seletor}</code> (${c.tamanho})`) : "nenhum"}</div>
        <div>Quadros (iframe): ${(d.quadros || []).length ? (d.quadros || []).map((q) => h`<code>${q}</code>`) : "nenhum"}</div>
      </div></details>`);
    }
    tituloModal(`A gravação do ${REDES[rede].rotulo} não deu`);
    montar(corpoModal(), h`<div class="login-espera">${ic("alerta", "grande")}
      <p><b>${plural(r.passos, "passo anotado", "passos anotados")}</b>, mas o roteiro não serve, então eu <b>não salvei</b>.</p>
      ${motivos}
      ${marcasHtml(Object.fromEntries((r.faltando || []).map((p) => [p, `@@${p.toUpperCase()}@@`])))}
      <p class="nota">O roteiro que já existia (se havia um) continua valendo.</p>
      <div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Fechar</button><button class="btn primario" data-acao="rede-gravar" data-rede="${rede}">${ic("microfone")}Gravar de novo</button></div></div>`);
    toast("Gravação descartada: veja o que faltou", "erro", 7000);
    await App.paginas.redes.recarregar();
    return;
  }
  const incompleto = (r.faltando || []).length > 0 || (r.estranhas || []).length > 0;
  tituloModal(`Roteiro do ${REDES[rede].rotulo} gravado`);
  montar(corpoModal(), h`<div class="login-espera">${ic("ok", "grande")}
    <p><b>${plural(r.passos, "passo gravado", "passos gravados")}</b>, incluindo a escolha do vídeo.</p>
    <p class="nota">Campos identificados: ${nomes(r.papeis) || "nenhum"}.${r.inferido ? h` O campo de <b>${NOME_PAPEL[r.inferido] || r.inferido}</b> eu descobri por eliminação, por ser o único campo de texto.` : ""}</p>
    ${incompleto ? h`<p class="nota"><b>Salvei mesmo sem reconhecer todas as marcas.</b>${(r.faltando || []).length ? h` Campos sem identificação: ${nomes(r.faltando)}.` : ""}${(r.estranhas || []).length ? h` Marcas não reconhecidas: ${r.estranhas.join(", ")}.` : ""} Textos capturados sem marca reconhecida ficam fixos no roteiro. Você pode ajustar em <b>Ver o roteiro</b>.</p>` : ""}
    <p class="nota">Agora ligue o ensaio e poste um corte: ele repete estes passos e para antes de publicar.</p>
    <div class="acoes-modal">${incompleto ? h`<button class="btn" data-acao="rede-roteiro" data-rede="${rede}">Ver o roteiro</button>` : ""}<button class="btn primario" data-acao="fechar-modal">Entendi</button></div></div>`);
  toast(`Roteiro do ${REDES[rede].rotulo} gravado com ${r.passos} passos${incompleto ? " · alguns campos sem identificação" : ""}`, incompleto ? "info" : "ok", 7000);
  await App.paginas.redes.recarregar();
};

App.acoes["grava-desfazer"] = async (el) => {
  const s = await ocupado(el, () => post(`/redes/${el.dataset.rede}/gravar/desfazer`));
  desenharGravacao(s);
};

App.acoes["grava-recomecar"] = async (el) => {
  const rede = el.dataset.rede;
  const ok = await confirmar({ titulo: "Recomeçar a gravação?", texto: "Eu esqueço tudo que anotei até agora. A janela do Chrome continua aberta: recomece a postagem por lá.", botao: "Recomeçar" });
  if (!ok) return;
  const s = await ocupado(el, () => post(`/redes/${rede}/gravar/recomecar`));
  desenharGravacao(s);
  toast("Gravação zerada. Comece de novo na janela do Chrome.", "info");
};

App.acoes["grava-cancelar"] = async (el) => {
  const rede = el.dataset.rede;
  // na de imagem, o post pode ter saído: com sim, ele fica "a conferir" no Início
  const publicou = Gravacao.midia === "imagem" && Gravacao.passos > 0 ? await redImgPerguntarPublicou() : false;
  const r = await ocupado(el, () => post(`/redes/${rede}/gravar/cancelar`, publicou ? { publicou } : {}));
  pararGravacao();
  fecharModal();
  toast(r.conferir ? "Gravação cancelada. O post ficou em \"A conferir\" no Início: diga lá se ele saiu." : "Gravação cancelada", "info", r.conferir ? 8000 : 4500);
  await App.paginas.redes.recarregar();
};

/** O resultado da gravação de imagem: o roteiro (ou o porquê de não ter salvo) e o post que ficou a conferir. */
async function redImgFimDaGravacao(rede, r) {
  const rotulo = REDES[rede].rotulo;
  const qual = Gravacao.tipo === "carrossel" ? "Este carrossel" : "Este post";
  const conferir = r.conferir ? h`<p class="nota"><b>${qual} ficou em "A conferir" no Início: diga lá se ele saiu.</b></p>` : "";
  if (!r.ok) {
    const motivos = [];
    if (r.mensagem) motivos.push(h`<p class="erro-texto">${r.mensagem}</p>`);
    else if (!r.tem_arquivo) motivos.push(h`<p class="erro-texto">Não vi você escolher as imagens. Escolha pelo botão de escolher arquivo da página, na janela do AutoCortes.</p>`);
    if (r.erros && r.erros.length) motivos.push(h`<p class="erro-texto">${r.erros.map((erro) => h`${erro}<br>`)}</p>`);
    tituloModal(`A gravação do roteiro de imagem do ${rotulo} não deu`);
    montar(corpoModal(), h`<div class="login-espera">${ic("alerta", "grande")}
      <p><b>${plural(r.passos || 0, "passo anotado", "passos anotados")}</b>, mas o roteiro não serve, então eu <b>não salvei</b>.</p>
      ${motivos}${conferir}
      <p class="nota">O roteiro de imagem que já existia (se havia um) continua valendo.</p>
      <div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Fechar</button>${r.motivo === "sem_fotos" ? "" : h`<button class="btn primario" data-acao="rede-gravar" data-rede="${rede}" data-midia="imagem">${ic("microfone")}Gravar de novo</button>`}</div></div>`);
    toast("Gravação de imagem descartada: veja o porquê", "erro", 7000);
    await App.paginas.redes.recarregar();
    return;
  }
  tituloModal(`Roteiro de imagem do ${rotulo} gravado`);
  montar(corpoModal(), h`<div class="login-espera">${ic("ok", "grande")}
    <p><b>${plural(r.passos, "passo gravado", "passos gravados")}</b>, incluindo a escolha das imagens${r.quantos ? ` (${plural(r.quantos, "imagem", "imagens")})` : ""}.</p>
    ${r.so_uma ? h`<p class="nota">O campo de arquivo que você usou aceita uma imagem por vez: o carrossel fica de fora nesta rede, e os outros tipos continuam.</p>` : ""}
    ${conferir}
    <p class="nota">Agora ligue Imagens no cartão da rede. Com o ensaio ligado, o próximo post de imagem preenche tudo e para antes de publicar.</p>
    <div class="acoes-modal"><button class="btn" data-acao="rede-roteiro" data-rede="${rede}" data-midia="imagem">Ver o roteiro</button><button class="btn primario" data-acao="fechar-modal">Entendi</button></div></div>`);
  toast(`Roteiro de imagem do ${rotulo} gravado com ${r.passos} passos`, "ok", 7000);
  await App.paginas.redes.recarregar();
}

/* ------------------------------------------------------------ ver e editar o roteiro */

function ajudaRoteiroHtml(comandos) {
  return h`<details class="guia"><summary>${ic("lista")}Comandos que valem no roteiro</summary><div class="ajuda-roteiro">
    ${(comandos || []).map((c) => h`<div class="cmd"><button type="button" class="tag-copiar" data-acao="roteiro-inserir" data-linha="${c.exemplo}" title="Inserir esta linha no roteiro"><code>${c.exemplo}</code>${ic("mais")}</button><small>${c.o_que}</small></div>`)}
  </div></details>`;
}

// o texto do roteiro como está salvo: a janela pergunta antes de fechar com mudança por salvar
const RoteiroAberto = { rede: null, texto: "" };

function problemasRoteiroHtml(r) {
  if (!r) return "";
  if (r.erros && r.erros.length) {
    return h`<div class="aviso-rede">${ic("alerta")}<span>${r.erros.map((x) => h`${x}<br>`)}</span></div>`;
  }
  if (r.avisos && r.avisos.length) {
    return h`<div class="aviso-rede">${ic("info")}<span><b>Funciona, mas é arriscado:</b><br>${r.avisos.map((x) => h`${x}<br>`)}</span></div>`;
  }
  return "";
}

function roteiroHtml(rede, d) {
  const r = d.resumo;
  const linhas = Math.max(14, Math.min(30, (d.texto || "").split("\n").length + 2));
  const padrao = d.origem === "padrao";
  const imagem = d.midia === "imagem";
  const vazio = imagem ? "Nenhum roteiro de imagem ainda. Grave um em Gravar o roteiro de imagem, ou escreva aqui."
    : "Nenhum roteiro ainda. Grave um em Aprender a postar, ou escreva aqui.";
  return h`<div class="editor-roteiro">
    ${padrao ? h`<div class="aviso-rede">${ic("info")}<span>Este é o <b>roteiro padrão</b> do AutoCortes, que vem com a instalação e é atualizado junto com ela. Se você mudar e salvar, ele vira o seu roteiro e passa a valer no lugar do padrão. O seu fica em: <code class="caminho">${d.arquivo}</code></span></div>` : ""}
    <p class="nota">Uma ação por linha, na ordem em que eu faço. Linha que começa com <code>#</code> é comentário e não faz nada, então dá para desligar um passo sem apagar. Precisa de um tempo em algum ponto? Ponha <code>esperar 5</code> ali. Ctrl+S salva.</p>
    ${imagem ? h`<p class="nota">No roteiro de imagem, a linha <code>imagens</code> entrega todas as imagens do post de uma vez, na ordem do carrossel. Não existe roteiro padrão de imagem.</p>` : ""}
    ${ajudaRoteiroHtml(d.comandos)}
    <label class="campo"><span>${imagem ? "Roteiro de imagem" : "Roteiro"} do ${REDES[rede].rotulo}</span><textarea id="roteiro-texto" rows="${linhas}" spellcheck="false" placeholder="${vazio}">${d.texto || ""}</textarea></label>
    <div id="roteiro-erros">${problemasRoteiroHtml(r)}</div>
    <p class="nota">${r ? h`${plural(r.linhas, "ação", "ações")}${r.papeis && r.papeis.length ? ` · escreve ${r.papeis.join(", ")}` : ""}` : "vazio"} · arquivo: <code class="caminho">${padrao ? d.arquivo_padrao : d.arquivo}</code></p>
    <div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Fechar</button><button class="btn primario" data-acao="roteiro-salvar" data-rede="${rede}" data-midia="${imagem ? "imagem" : "video"}">${ic("check")}Salvar o roteiro</button></div>
  </div>`;
}

App.acoes["rede-roteiro"] = async (el) => {
  const rede = el.dataset.rede;
  const midia = el.dataset.midia === "imagem" ? "imagem" : "video";
  const d = await ocupado(el, () => api(`/redes/${rede}/roteiro${midia === "imagem" ? "?midia=imagem" : ""}`));
  RoteiroAberto.rede = rede;
  RoteiroAberto.texto = d.texto || "";
  abrirModal({
    titulo: midia === "imagem" ? `Roteiro de imagem do ${REDES[rede].rotulo}` : `Roteiro do ${REDES[rede].rotulo}`,
    largura: 860,
    corpo: roteiroHtml(rede, d),
    protegido: () => {
      const area = $("#roteiro-texto");
      return Boolean(area) && area.value.replace(/\r\n/g, "\n") !== RoteiroAberto.texto.replace(/\r\n/g, "\n");
    },
    aoSalvar: () => salvarRoteiroAberto(rede, null, midia),
    aoFechar: () => { RoteiroAberto.rede = null; RoteiroAberto.texto = ""; },
  });
};

App.acoes["roteiro-inserir"] = (el) => {
  const area = $("#roteiro-texto");
  if (!area) return;
  const linha = el.dataset.linha;
  const pos = area.selectionStart ?? area.value.length;
  const quebra = area.value.slice(0, pos).match(/\n?$/) ? "" : "\n";
  area.value = area.value.slice(0, pos) + quebra + linha + "\n" + area.value.slice(pos);
  area.focus();
  area.selectionStart = area.selectionEnd = pos + quebra.length + linha.length + 1;
};

async function salvarRoteiroAberto(rede, el = null, midia = "video") {
  const texto = ($("#roteiro-texto") || {}).value || "";
  try {
    const r = await ocupado(el, () => post(`/redes/${rede}/roteiro`, midia === "imagem" ? { texto, midia } : { texto }));
    RoteiroAberto.texto = texto;
    montar($("#roteiro-erros"), problemasRoteiroHtml(r.resumo));
    const avisos = (r.resumo && r.resumo.avisos) || [];
    toast(`Roteiro salvo: ${plural(r.resumo.linhas, "ação", "ações")}${avisos.length ? ` · ${plural(avisos.length, "ponto arriscado", "pontos arriscados")}` : ""}`,
      avisos.length ? "info" : "ok");
    await App.paginas.redes.recarregar();
  } catch (e) {
    const erros = (e.dados && e.dados.erros) || [String(e.message)];
    montar($("#roteiro-erros"), h`<div class="aviso-rede">${ic("alerta")}<span>${erros.map((x) => h`${x}<br>`)}</span></div>`);
    toast("Não salvei: veja o que está errado", "erro");
  }
}

App.acoes["roteiro-salvar"] = (el) => salvarRoteiroAberto(el.dataset.rede, el, el.dataset.midia);

App.acoes["rede-roteiro-apagar"] = async (el) => {
  const rede = el.dataset.rede;
  if (el.dataset.midia === "imagem") {
    const ok = await confirmar({
      titulo: `Apagar o roteiro de imagem do ${REDES[rede].rotulo}?`,
      texto: "Sem ele, as imagens desta rede param até você gravar de novo (não há roteiro padrão de imagem). Os vídeos continuam.",
      botao: "Apagar",
      perigo: true,
    });
    if (!ok) return;
    await ocupado(el, () => post(`/redes/${rede}/roteiro/apagar`, { midia: "imagem" }));
    toast("Roteiro de imagem apagado", "info");
    await App.paginas.redes.recarregar();
    return;
  }
  const padrao = Boolean(el.dataset.padrao);
  const ok = await confirmar({
    titulo: padrao ? `Voltar ao roteiro padrão do ${REDES[rede].rotulo}?` : `Apagar o roteiro do ${REDES[rede].rotulo}?`,
    texto: padrao
      ? "Apago o roteiro que você gravou ou editou e volto a usar o roteiro padrão do AutoCortes, que vem com a instalação."
      : "Sem o roteiro gravado, volto a usar os passos que eu escrevi, que podem não bater com a página.",
    botao: padrao ? "Voltar ao padrão" : "Apagar",
    perigo: true,
  });
  if (!ok) return;
  const r = await ocupado(el, () => post(`/redes/${rede}/roteiro/apagar`));
  toast(r.volta === "padrao" ? "Voltei ao roteiro padrão" : "Roteiro apagado", "info");
  await App.paginas.redes.recarregar();
};

App.acoes["rede-desconectar"] = async (el) => {
  const rede = el.dataset.rede;
  const ok = await confirmar({ titulo: `Desconectar o ${REDES[rede].rotulo}?`, texto: "O login salvo é apagado deste computador. Para voltar a postar, conecte a conta de novo.", botao: "Desconectar", perigo: true });
  if (!ok) return;
  await ocupado(el, () => post(`/redes/${rede}/desconectar`));
  toast(`${REDES[rede].rotulo} desconectado`);
  await App.paginas.redes.recarregar();
  atualizarEstado();
};

App.acoes["modo-real"] = async () => {
  const d = PgRedes.dados;
  const prontas = d ? ORDEM_REDES.filter((r) => d.redes[r].ativo && d.redes[r].pronta && d.redes[r].via !== "manual") : [];
  const texto = prontas.length
    ? `A partir de agora, os cortes são publicados de verdade nos horários da agenda (${prontas.map((r) => REDES[r].rotulo).join(", ")}).`
    : "Nenhuma rede ativa está conectada ainda. Nada será publicado até você conectar uma conta (as redes à mão continuam só com tarefas).";
  const ok = await confirmar({ titulo: "Ativar a publicação real?", texto, botao: "Ativar" });
  if (!ok) return;
  definirValor("geral.simulacao", false);
  if (await salvarConfig(["geral.simulacao"])) toast("Publicação real ativada");
};

App.acoes["modo-simulacao"] = async () => {
  definirValor("geral.simulacao", true);
  if (await salvarConfig(["geral.simulacao"])) toast("Modo simulação ligado: nada será publicado", "info");
};

App.acoes.copiar = async (el) => {
  try {
    await navigator.clipboard.writeText(el.dataset.texto);
    toast("Copiado");
  } catch (e) {
    toast("Não consegui copiar. Selecione o texto e copie com Ctrl+C.", "erro");
  }
};

/* ------------------------------------------------------------ página */

App.paginas.redes = {
  titulo: "Redes sociais",
  subtitulo: "Contas conectadas, forma de envio e opções de cada rede",
  async render(el, token) {
    const d = await carregarRedes(token);
    if (!d) return;
    desenharRedes();
  },
  aoMudar(caminho) {
    // facebook.pagina fica de fora: é um campo de texto, e redesenhar a cada tecla apagaria o que você digita
    // (o salvar recarrega o cartão com a Página nova)
    if (/\.(envio|ativo|privacidade|videos|imagens)$/.test(caminho) || caminho === "navegador.ensaio") desenharRedes();
  },
  async aoSalvar() {
    await this.recarregar();
  },
  async recarregar() {
    const d = await carregarRedes();
    if (d) desenharRedes();
  },
  sair() {
    pararLogin();
    pararGravacao();
  },
};
