'use strict';

const $ = id => document.getElementById(id);
let state = null;
let section = 'Ads Manager';
let selected = null;
let saveEditor = null;
let activePhone = '+77056945245';
let activeProduct = 'centrifuges';
let activeLang = 'kz';
let activeDetails = '';
let pendingAudienceProduct = null;

const sections = [
  'Ads Manager',
  'Live Outreach & Voice SDR',
  'Product Catalog & Offers',
  'Target Audience & ICP',
  'AI & CRM Connectors',
  'Telephony & Script Studio',
  'AI Product Intelligence',
  'Lead Discovery',
  'Outreach Analytics'
];

function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}

function add(parent, ...nodes) {
  for (const n of nodes) parent.append(n);
  return parent;
}

function button(text, action, cls = 'primary-btn') {
  const b = el('button', text, cls);
  b.type = 'button';
  b.onclick = async () => {
    b.disabled = true;
    try {
      await action();
    } catch (e) {
      notice(e.message);
    } finally {
      b.disabled = false;
    }
  };
  return b;
}

function notice(text) {
  const n = $('notice');
  n.textContent = text;
  n.hidden = false;
  setTimeout(() => n.hidden = true, 9000);
}

async function api(path, method = 'GET', data) {
  const r = await fetch('/platform/api' + path, {
    method,
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      'X-Control-Request': '1'
    },
    body: data === undefined ? undefined : JSON.stringify(data)
  });
  const body = await r.json();
  if (!r.ok) {
    if (r.status === 401) {
      $('shell').hidden = true;
      $('login').hidden = false;
    }
    throw Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail));
  }
  return body;
}

async function refresh(renderPage = true) {
  state = await api('/state');
  $('shell').hidden = false;
  $('login').hidden = true;
  $('connection').textContent = 'Live · ' + new Date().toLocaleTimeString();
  
  if (state.capabilities?.telephony_host) {
    $('telephony-badge').textContent = '📞 PBX: ' + state.capabilities.telephony_host;
  }
  if (!selected && state.products?.length) {
    selected = state.products[0].id;
  }
  if (renderPage) render();
}

function paragraph(parent, text, cls) {
  parent.append(el('p', text, cls));
}

function card(title, cls = '') {
  const c = el('article', undefined, 'card ' + cls);
  c.append(el('h3', title));
  return c;
}

function badge(text, cls = '') {
  return el('span', text, 'badge ' + cls);
}

function details(parent, title, value) {
  const d = el('details');
  add(d, el('summary', title), el('pre', typeof value === 'string' ? value : JSON.stringify(value, null, 2)));
  parent.append(d);
}

function list(parent, title, values) {
  parent.append(el('h4', title));
  const ul = el('ul');
  for (const value of values || []) ul.append(el('li', value));
  parent.append(ul);
}

function lines(s) {
  return (s || '').split('\n').map(x => x.trim()).filter(Boolean);
}

function edit(title, build, save) {
  $('editor-title').textContent = title;
  $('editor-fields').replaceChildren();
  build($('editor-fields'));
  saveEditor = save;
  $('editor').showModal();
}

function values() {
  const result = {};
  for (const n of $('editor-fields').querySelectorAll('input,textarea,select')) {
    result[n.name] = n.type === 'checkbox' ? n.checked : n.value;
  }
  return result;
}

function field(parent, key, label, value = '', type = 'text') {
  const l = el('label', label);
  const n = el(type === 'textarea' ? 'textarea' : 'input');
  n.name = key;
  if (type !== 'textarea') n.type = type;
  if (type === 'checkbox') n.checked = Boolean(value);
  else n.value = value ?? '';
  l.append(n);
  parent.append(l);
  return n;
}

function current() {
  return state.products?.find(p => p.id === selected);
}

function jobs() {
  return (state.jobs || []).filter(j => j.product_id === selected);
}

/* =====================================================================
   RENDER FUNCTION
   ===================================================================== */
function render() {
  document.querySelectorAll('nav button').forEach(b => {
    b.classList.toggle('active', b.dataset.section === section);
  });
  $('title').textContent = section;
  const root = $('content');
  root.replaceChildren();

  if (section === 'Ads Manager' && window.AdsManagerUI) {
    window.AdsManagerUI.render(root, { api, el, add, button, badge, notice, paragraph });
    return;
  }

  if (window.PlatformStudioUI) {
    const studioHelpers = {
      api, el, add, button, badge, notice, paragraph, edit, field,
      onRefreshState: refresh,
      onSelectForAudience: (prod) => {
        pendingAudienceProduct = prod;
        section = 'Target Audience & ICP';
        render();
      },
      onOpenAdsManager: () => {
        section = 'Ads Manager';
        render();
      }
    };
    if (section === 'Product Catalog & Offers') {
      window.PlatformStudioUI.renderProductCatalog(root, studioHelpers);
      return;
    }
    if (section === 'Target Audience & ICP') {
      window.PlatformStudioUI.renderAudienceAnalyzer(root, studioHelpers, pendingAudienceProduct);
      return;
    }
    if (section === 'AI & CRM Connectors') {
      window.PlatformStudioUI.renderConnectors(root, studioHelpers);
      return;
    }
    if (section === 'Telephony & Script Studio') {
      window.PlatformStudioUI.renderTelephonyAndScript(root, studioHelpers);
      return;
    }
  }

  /* ------------------------------------------------------------------
     1. LIVE OUTREACH & VOICE SDR
     ------------------------------------------------------------------ */
  if (section === 'Live Outreach & Voice SDR') {
    paragraph(root, 'Direct control cockpit for Asterisk PBX voice calls and Meta WhatsApp Cloud commercial offer delivery with dynamic PDF generation in Kazakh and Russian.', 'lead');

    const cockpit = el('div', undefined, 'outreach-cockpit');

    // Left Panel: Lead & Outreach Controls
    const left = el('div', undefined, 'cockpit-panel');
    left.append(el('h3', '📞 Оперативті хабарласу / Voice SDR & WhatsApp'));

    // Target Phone
    const phoneLabel = el('label', 'Мақсатты телефон нөмірі / Target Phone (E.164)');
    const phoneInput = el('input');
    phoneInput.type = 'tel';
    phoneInput.value = activePhone;
    phoneInput.placeholder = '+77056945245';
    phoneInput.oninput = e => { activePhone = e.target.value.trim(); };
    phoneLabel.append(phoneInput);
    left.append(phoneLabel);

    // Quick Presets
    const pills = el('div', undefined, 'quick-pills');
    const presets = [
      { label: 'Басшылық (+7 705 694-52-45)', phone: '+77056945245' },
      { label: 'Дәурен (+7 701 721-11-56)', phone: '+77017211156' },
      { label: 'PBX Линиясы (+7 700 912-12-12)', phone: '+77009121212' }
    ];
    for (const p of presets) {
      const pill = el('button', p.label, 'pill-btn' + (activePhone === p.phone ? ' active' : ''));
      pill.type = 'button';
      pill.onclick = () => {
        activePhone = p.phone;
        phoneInput.value = p.phone;
        document.querySelectorAll('.quick-pills .pill-btn').forEach(b => b.classList.remove('active'));
        pill.classList.add('active');
      };
      pills.append(pill);
    }
    left.append(pills);

    // Language Toggle
    left.append(el('label', 'Тіл таңдау / Language'));
    const langDiv = el('div', undefined, 'lang-toggle');
    const kzBtn = el('button', '🇰🇿 Қазақша', activeLang === 'kz' ? 'active' : '');
    kzBtn.type = 'button';
    kzBtn.onclick = () => { activeLang = 'kz'; render(); };
    const ruBtn = el('button', '🇷🇺 Русский', activeLang === 'ru' ? 'active' : '');
    ruBtn.type = 'button';
    ruBtn.onclick = () => { activeLang = 'ru'; render(); };
    add(langDiv, kzBtn, ruBtn);
    left.append(langDiv);

    // Custom Note / Context
    const detailsLabel = el('label', 'Қосымша мәліметтер / Call Context (Optional)');
    const detailsInput = el('input');
    detailsInput.placeholder = 'Клиника басшысына арналған арнайы коммерциялық ұсыныс';
    detailsInput.value = activeDetails;
    detailsInput.oninput = e => { activeDetails = e.target.value; };
    detailsLabel.append(detailsInput);
    left.append(detailsLabel);

    // Action Buttons
    const actions = el('div', undefined, 'actions');
    const callBtn = button('📞 Қоңырау шалу / Start AI Call', async () => {
      if (!activePhone) throw Error('Телефон нөмірін енгізіңіз');
      const catalog = state.catalog || [];
      const item = catalog.find(c => c.id === activeProduct) || catalog[0];
      const prodName = activeLang === 'kz' ? item.name : item.name_ru;
      notice('Calling ' + activePhone + ' via Asterisk PBX (185.22.64.70)...');
      const res = await api('/outreach/call', 'POST', {
        phone: activePhone,
        product: prodName,
        lang: activeLang,
        details: activeDetails
      });
      notice('Call initiated! Mode: ' + res.mode + ' · ' + activePhone);
      await refresh();
    }, 'call-btn');

    const offerBtn = button('📄 WhatsApp арқылы КП жіберу', async () => {
      if (!activePhone) throw Error('Телефон нөмірін енгізіңіз');
      notice('Sending WhatsApp PDF offer to ' + activePhone + ' via Meta Cloud API...');
      const res = await api('/outreach/offer', 'POST', {
        phone: activePhone,
        product: activeProduct,
        lang: activeLang
      });
      notice('WhatsApp offer sent successfully! WAMID: ' + res.wamid);
      await refresh();
    }, 'whatsapp-btn');

    add(actions, callBtn, offerBtn);
    left.append(actions);
    cockpit.append(left);

    // Right Panel: Active Product Picker
    const right = el('div', undefined, 'cockpit-panel');
    right.append(el('h3', '📦 Тауар каталогы / Product to Pitch'));
    const selector = el('div', undefined, 'product-selector');
    const catalog = state.catalog || [];
    for (const item of catalog) {
      const opt = el('div', undefined, 'product-option' + (activeProduct === item.id ? ' selected' : ''));
      const titleRow = el('div', undefined, 'product-title');
      titleRow.append(el('span', activeLang === 'kz' ? item.name : item.name_ru));
      titleRow.append(el('span', item.price_range, 'product-price'));
      opt.append(titleRow);
      opt.append(el('div', item.category, 'product-meta'));
      opt.append(el('div', item.specs, 'product-meta'));

      // PDF links
      const links = el('div', undefined, 'actions');
      const pdfKz = el('a', '📄 КП (KZ)', 'badge cyan-badge');
      pdfKz.href = item.pdf_kz;
      pdfKz.target = '_blank';
      const pdfRu = el('a', '📄 КП (RU)', 'badge cyan-badge');
      pdfRu.href = item.pdf_ru;
      pdfRu.target = '_blank';
      add(links, pdfKz, pdfRu);
      opt.append(links);

      opt.onclick = (e) => {
        if (e.target.tagName === 'A') return;
        activeProduct = item.id;
        document.querySelectorAll('.product-option').forEach(o => o.classList.remove('selected'));
        opt.classList.add('selected');
      };
      selector.append(opt);
    }
    right.append(selector);
    cockpit.append(right);
    root.append(cockpit);

    // Live Outreach History Table
    const histCard = card('📋 Соңғы қоңыраулар мен жіберілген ұсыныстар / Outreach Log Feed');
    const outreach = state.outreach || { calls: [], offers: [] };
    const allEvents = [
      ...(outreach.calls || []).map(c => ({ type: 'call', ...c })),
      ...(outreach.offers || []).map(o => ({ type: 'offer', ...o }))
    ].sort((a, b) => (b.at || '').localeCompare(a.at || ''));

    if (allEvents.length) {
      const table = el('table');
      const thead = el('thead');
      const trh = el('tr');
      for (const th of ['Уақыты / Time', 'Арна / Channel', 'Нөмір / Phone', 'Тауар / Product', 'Тілі / Lang', 'Мәртебесі / Status', 'Нәтиже / Output']) {
        trh.append(el('th', th));
      }
      thead.append(trh);
      table.append(thead);

      const tbody = el('tbody');
      for (const ev of allEvents.slice(0, 15)) {
        const tr = el('tr');
        tr.append(el('td', (ev.at || '').replace('T', ' ').slice(0, 19)));
        const chBadge = ev.type === 'call' ? badge('📞 Voice SDR', 'live-badge') : badge('💬 WhatsApp PDF', 'cyan-badge');
        const tdCh = el('td');
        tdCh.append(chBadge);
        tr.append(tdCh);
        tr.append(el('td', ev.phone));
        tr.append(el('td', ev.product));
        tr.append(el('td', ev.lang === 'kz' ? '🇰🇿 KZ' : '🇷🇺 RU'));
        tr.append(el('td', ev.status));
        tr.append(el('td', ev.response || ev.wamid || ev.error || 'ok'));
        tbody.append(tr);
      }
      table.append(tbody);
      histCard.append(table);
    } else {
      paragraph(histCard, 'No outreach events recorded yet. Place your first test call above.', 'muted');
    }
    root.append(histCard);
    return;
  }

  /* ------------------------------------------------------------------
     2. PRODUCT CATALOG & OFFERS
     ------------------------------------------------------------------ */
  if (section === 'Product Catalog & Offers') {
    paragraph(root, 'Manage enterprise product profiles, verify official commercial offer PDFs, and configure pricing and advantage claims.', 'lead');
    root.append(button('＋ Жаңа тауар қосу / New Product', () => editProduct()));

    const grid = el('div', undefined, 'grid');
    for (const p of state.products || []) {
      const c = card(p.payload.brief.name);
      add(c, badge(p.payload.brief.buyer_paths.join(' · '), 'cyan-badge'));
      paragraph(c, p.payload.brief.description);
      paragraph(c, (p.payload.brief.market || 'Kazakhstan') + ' · revision ' + p.revision, 'muted');
      const acts = el('div', undefined, 'actions');
      acts.append(button('Select & Analyze', () => {
        selected = p.id;
        section = 'AI Product Intelligence';
        render();
      }, 'secondary-btn'));
      acts.append(button('Edit', () => editProduct(p), 'secondary-btn'));
      c.append(acts);
      grid.append(c);
    }
    root.append(grid);

    // Official Catalogs Card
    const catCard = card('Ресми коммерциялық ұсыныстар (PDF) / Official Offers');
    const catGrid = el('div', undefined, 'grid');
    for (const item of state.catalog || []) {
      const c = card(item.name);
      paragraph(c, item.category);
      paragraph(c, 'Бағасы: ' + item.price_range, 'product-price');
      paragraph(c, item.specs, 'muted');
      const acts = el('div', undefined, 'actions');
      const aKz = el('a', '📄 Ашу (Қазақша)', 'badge cyan-badge');
      aKz.href = item.pdf_kz;
      aKz.target = '_blank';
      const aRu = el('a', '📄 Открыть (Русский)', 'badge cyan-badge');
      aRu.href = item.pdf_ru;
      aRu.target = '_blank';
      add(acts, aKz, aRu);
      c.append(acts);
      catGrid.append(c);
    }
    catCard.append(catGrid);
    root.append(catCard);
    return;
  }

  /* ------------------------------------------------------------------
     3. AI PRODUCT INTELLIGENCE
     ------------------------------------------------------------------ */
  if (section === 'AI Product Intelligence') {
    paragraph(root, 'Gemini Flash AI engine analyzes authorized seller product briefs and generates buyer psychology profiles, value propositions, and objection rebuttals.', 'lead');
    if (!current()) {
      paragraph(root, 'Тауарды таңдаңыз / Select a product first.', 'empty');
      return;
    }
    root.append(button('Analyze ' + current().payload.brief.name, async () => {
      const j = await api('/products/' + selected + '/analyze', 'POST');
      notice('Analysis queued · job #' + j.id);
      await refresh();
    }));

    for (const job of jobs().filter(j => j.kind === 'analysis')) {
      const c = card('Analysis #' + job.id + ' · Revision ' + job.revision);
      add(c, badge(job.status, job.status === 'completed' ? 'live-badge' : ''));
      if (job.error) paragraph(c, job.error);
      const a = job.result?.analysis;
      if (a) {
        paragraph(c, a.value_proposition);
        for (const [key, label] of [
          ['problems_solved', 'Problems Addressed'],
          ['use_cases', 'Use Cases'],
          ['purchase_barriers', 'Purchase Barriers'],
          ['missing_information', 'Missing Information']
        ]) list(c, label, a[key]);
        paragraph(c, 'Confidence: AI Hypotheses · Model: ' + (job.result.model || 'gemini-flash-latest'), 'muted');
        details(c, 'Evidence basis', job.payload);
        c.append(button('Review Target Profiles', () => {
          section = 'Target Audience & ICP';
          render();
        }, 'secondary-btn'));
      }
      root.append(c);
    }
    return;
  }

  /* ------------------------------------------------------------------
     4. TARGET AUDIENCE & ICP
     ------------------------------------------------------------------ */
  if (section === 'Target Audience & ICP') {
    paragraph(root, 'Review AI-generated buyer roles, motivations, buying signals, and qualification criteria before approving discovery.', 'lead');
    for (const s of (state.segments || []).filter(s => s.product_id === selected)) {
      const c = card(s.payload.name);
      add(c, badge(s.payload.path + ' · ' + (s.approved ? 'Approved' : 'Review Required'), s.approved ? 'live-badge' : ''));
      paragraph(c, s.payload.characteristics);
      list(c, 'Buyer Roles', s.payload.roles);
      list(c, 'Motivations', s.payload.motivations);
      list(c, 'Buying Signals', s.payload.signals);
      list(c, 'Search Strategy', s.payload.queries);
      list(c, 'Qualification Criteria', s.payload.criteria);
      c.append(button('Review / Approve', () => editSegment(s), 'secondary-btn'));
      root.append(c);
    }
    return;
  }

  /* ------------------------------------------------------------------
     5. LEAD DISCOVERY
     ------------------------------------------------------------------ */
  if (section === 'Lead Discovery') {
    paragraph(root, 'Autonomous prospect discovery over GDELT and verified public registers. Verified page excerpts only.', 'lead');
    for (const s of (state.segments || []).filter(s => s.product_id === selected && s.approved)) {
      const c = card(s.payload.name);
      c.append(button('Start Discovery · Target 20', async () => {
        const j = await api('/campaigns', 'POST', { segment_id: s.id, target: 20 });
        notice('Campaign queued · job #' + j.id);
        await refresh();
      }));
      root.append(c);
    }
    for (const j of jobs().filter(j => j.kind === 'discovery')) {
      const c = card('Campaign #' + j.id);
      add(c, badge(j.status, j.status === 'completed' ? 'live-badge' : ''));
      paragraph(c, 'Target: ' + j.payload.target + ' · Found: ' + (j.result?.found ?? 'pending'));
      if (j.error) paragraph(c, j.error);
      details(c, 'Workflow details', j.workflow || j.result || {});
      root.append(c);
    }
    return;
  }

  /* ------------------------------------------------------------------
     6. CRM INTEGRATION
     ------------------------------------------------------------------ */
  if (section === 'CRM Integration') {
    paragraph(root, 'Bitrix24 integration and contact synchronization status.', 'lead');
    const c = card('Bitrix24 CRM Connection');
    paragraph(c, 'Webhook Configured: ' + state.capabilities.crm_configured);
    paragraph(c, 'Writes Status: Authorized');
    c.append(button('Verify Live Connection', async () => {
      details(c, 'Live Connection Check', await api('/crm-status'));
    }, 'secondary-btn'));
    root.append(c);

    for (const h of state.handoffs || []) {
      details(root, 'Prepared Handoff · ' + h.candidate_id, h.payload);
    }
    return;
  }

  /* ------------------------------------------------------------------
     7. OUTREACH ANALYTICS
     ------------------------------------------------------------------ */
  if (section === 'Outreach Analytics') {
    paragraph(root, 'Operational metrics, model token usage, and system audit trail.', 'lead');
    const grid = el('div', undefined, 'grid');
    for (const [label, value] of Object.entries(state.job_totals || {})) {
      const c = card(label + ' Jobs');
      c.append(el('div', String(value), 'metric'));
      grid.append(c);
    }
    root.append(grid);

    const c = card('Model & Telephony Quota');
    paragraph(c, 'Daily Request Cap: ' + state.capabilities.daily_ai_call_limit);
    paragraph(c, 'Voice Gateway: ' + (state.capabilities.telephony_host || '185.22.64.70') + ' (Asterisk Live)');
    details(c, 'Usage Records', state.usage);
    root.append(c);

    details(root, 'System Audit Trail', state.audit);
  }
}

/* =====================================================================
   PRODUCT & SEGMENT EDITORS
   ===================================================================== */
function editProduct(p) {
  edit(p ? 'Edit Product' : 'New Product', f => {
    const v = p?.payload || {}, b = v.brief || {};
    field(f, 'name', 'Product or Service', b.name);
    field(f, 'description', 'Description', b.description, 'textarea');
    field(f, 'category', 'Category', b.category);
    field(f, 'market', 'Target Market', b.market);
    field(f, 'problem_hint', 'Known Customer Problem (Optional)', b.problem_hint, 'textarea');
    field(f, 'organization', 'Organizations / B2B', (b.buyer_paths || ['organization', 'individual']).includes('organization'), 'checkbox');
    field(f, 'individual', 'Individuals / Retail', (b.buyer_paths || ['organization', 'individual']).includes('individual'), 'checkbox');
    field(f, 'pricing', 'Pricing / Terms', v.pricing);
    field(f, 'advantages', 'Advantages — one per line', (v.advantages || []).join('\n'), 'textarea');
    field(f, 'problems', 'Customer Problems — one per line', (v.customer_problems || []).join('\n'), 'textarea');
    field(f, 'references', 'Public Reference URLs — one per line', (v.references || []).join('\n'), 'textarea');
    field(f, 'materials', 'Public Supporting Material', v.materials, 'textarea');
    field(f, 'public', 'Authorize Analysis by AI Engine', v.public_analysis_allowed, 'checkbox');
  }, async v => {
    const payload = {
      brief: {
        name: v.name,
        description: v.description,
        category: v.category,
        market: v.market,
        problem_hint: v.problem_hint || null,
        buyer_paths: ['organization', 'individual'].filter(x => v[x])
      },
      pricing: v.pricing,
      advantages: lines(v.advantages),
      customer_problems: lines(v.problems),
      references: lines(v.references),
      materials: v.materials,
      public_analysis_allowed: v.public
    };
    const saved = await api(p ? '/products/' + p.id : '/products', p ? 'PUT' : 'POST', payload);
    selected = saved.id;
    await refresh();
  });
}

function editSegment(s) {
  edit('Review Target Segment & Search Strategy', f => {
    const v = s.payload;
    field(f, 'name', 'Segment', v.name);
    field(f, 'characteristics', 'Ideal Customer Profile (ICP)', v.characteristics, 'textarea');
    for (const [key, label] of [
      ['roles', 'Buyer Roles'],
      ['motivations', 'Buyer Motivations'],
      ['signals', 'Buying Signals'],
      ['queries', 'Search Queries'],
      ['criteria', 'Qualification Criteria'],
      ['source_urls', 'Source URLs']
    ]) field(f, key, label + ' — one per line', (v[key] || []).join('\n'), 'textarea');
    field(f, 'approved', 'Approve this segment for discovery', s.approved, 'checkbox');
  }, async v => {
    const segment = { ...s.payload, name: v.name, characteristics: v.characteristics };
    for (const key of ['roles', 'motivations', 'signals', 'queries', 'criteria', 'source_urls']) {
      segment[key] = lines(v[key]);
    }
    await api('/segments/' + s.id, 'PUT', { segment, approved: v.approved });
    await refresh();
  });
}

/* =====================================================================
   BOOTSTRAP & EVENT LISTENERS
   ===================================================================== */
for (const name of sections) {
  const b = el('button', name);
  b.dataset.section = name;
  b.onclick = () => { section = name; render(); };
  $('nav').append(b);
}

$('login-form').onsubmit = async e => {
  e.preventDefault();
  try {
    await api('/login', 'POST', { password: $('password').value });
    $('password').value = '';
    await refresh();
  } catch (err) {
    notice(err.message);
  }
};

$('logout').onclick = async () => {
  await api('/logout', 'POST');
  state = null;
  $('shell').hidden = true;
  $('login').hidden = false;
};

$('cancel').onclick = () => $('editor').close();

$('editor-form').onsubmit = async e => {
  e.preventDefault();
  const b = e.submitter;
  b.disabled = true;
  try {
    await saveEditor(values());
    $('editor').close();
    notice('Saved successfully');
  } catch (err) {
    notice(err.message);
  } finally {
    b.disabled = false;
  }
};

refresh().catch(() => {});
setInterval(() => {
  if (state && !$('editor').open && !window.adsDrawerOpen) {
    if (section === 'Ads Manager' && window.AdsManagerUI) {
      window.AdsManagerUI.poll();
      refresh(false).catch(() => {
        $('connection').textContent = 'Connection Unavailable';
      });
    } else {
      refresh(false).catch(() => {
        $('connection').textContent = 'Connection Unavailable';
      });
    }
  }
}, 8000);
