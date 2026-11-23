'use strict';

/* =========================================================================
   Lead Hunter Autonomous B2B Operations Cockpit (v2.1.0)
   Clean, unified shell — all legacy views removed.
   ========================================================================= */

const $ = id => document.getElementById(id);
let state = null;
let section = '1. Products (Prompt AI)';
let saveEditor = null;
let activePhone = '+77056945245';
let activeProduct = 'centrifuges';
let activeLang = 'kz';
let activeDetails = '';
let pendingAudienceProduct = null;

const navGroups = [
  {
    label: 'PIPELINE BUILDER',
    items: [
      { id: '1. Products (Prompt AI)', icon: '📦', label: '1. Products (Prompt AI)' },
      { id: '2. Target Audiences (AI)', icon: '🎯', label: '2. Target Audiences (AI)' },
      { id: 'Ads Manager', icon: '🚀', label: '3. Campaigns & Leads' },
    ]
  },
  {
    label: 'VOICE OUTREACH',
    items: [
      { id: 'Live Voice SDR', icon: '📞', label: 'Live Voice SDR & Test' },
      { id: 'Telephony & Script Studio', icon: '🎙️', label: 'Telephony & Call Scripts' },
    ]
  },
  {
    label: 'INTEGRATIONS & ORG',
    items: [
      { id: 'AI & CRM Connectors', icon: '🔌', label: 'AI Models & CRM Sync' },
      { id: 'Company Profile & Offers', icon: '🏢', label: 'Company Profile & PDFs' },
      { id: 'Analytics & Call Logs', icon: '📊', label: 'Analytics & Call Logs' },
    ]
  }
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
  setTimeout(() => { n.hidden = true; }, 9000);
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
  let body;
  const rawText = await r.text();
  try {
    body = rawText ? JSON.parse(rawText) : {};
  } catch {
    body = { detail: rawText || `HTTP ${r.status}` };
  }
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

/* =========================================================================
   PIPELINE STEPPER BAR (Top visual guide for the 4-stage workflow)
   ========================================================================= */
function renderStepper(root) {
  const bar = el('div', undefined, 'pipeline-stepper');
  const steps = [
    { id: '1. Products (Prompt AI)', num: '1', label: 'Products (Prompt AI / CRM)' },
    { id: '2. Target Audiences (AI)', num: '2', label: 'Target Audiences (AI)' },
    { id: 'Ads Manager', num: '3', label: 'Leads & Dial Queue' },
    { id: 'Telephony & Script Studio', num: '4', label: 'Voice & Call Script' },
  ];
  for (const st of steps) {
    const btn = el('button', undefined, 'stepper-pill' + (section === st.id ? ' active' : ''));
    btn.type = 'button';
    btn.append(el('span', st.num, 'stepper-num'), el('span', st.label));
    btn.onclick = () => { section = st.id; render(); };
    bar.append(btn);
  }
  root.append(bar);
}

/* =========================================================================
   RENDER ROUTER
   ========================================================================= */
function render() {
  document.querySelectorAll('nav button').forEach(b => {
    b.classList.toggle('active', b.dataset.section === section);
  });
  $('title').textContent = section;
  const root = $('content');
  root.replaceChildren();

  renderStepper(root);

  const studioHelpers = {
    api, el, add, button, badge, notice, paragraph, edit, field,
    onRefreshState: refresh,
    onSelectForAudience: (prod) => {
      pendingAudienceProduct = prod;
      section = '2. Target Audiences (AI)';
      render();
    },
    onOpenAdsManager: () => {
      section = 'Ads Manager';
      render();
    }
  };

  if (section === '1. Products (Prompt AI)' && window.PlatformStudioUI) {
    window.PlatformStudioUI.renderProductCatalog(root, studioHelpers);
    return;
  }

  if (section === '2. Target Audiences (AI)' && window.PlatformStudioUI) {
    window.PlatformStudioUI.renderAudienceAnalyzer(root, studioHelpers, pendingAudienceProduct);
    return;
  }

  if (section === 'Ads Manager' && window.AdsManagerUI) {
    window.AdsManagerUI.render(root, { api, el, add, button, badge, notice, paragraph });
    return;
  }

  if (section === 'AI & CRM Connectors' && window.PlatformStudioUI) {
    window.PlatformStudioUI.renderConnectors(root, studioHelpers);
    return;
  }

  if (section === 'Telephony & Script Studio' && window.PlatformStudioUI) {
    window.PlatformStudioUI.renderTelephonyAndScript(root, studioHelpers);
    return;
  }

  if (section === 'Company Profile & Offers' && window.PlatformStudioUI) {
    window.PlatformStudioUI.renderCompanyProfile(root, studioHelpers);
    return;
  }

  /* ------------------------------------------------------------------
     LIVE VOICE SDR & TEST CALL COCKPIT
     ------------------------------------------------------------------ */
  if (section === 'Live Voice SDR') {
    const companyName = state?.company?.company_name || 'CRM Consulting';
    const voiceName = state?.telephony?.voice_name || 'Kore';
    paragraph(root, `Instant test dialer & WhatsApp PDF sender. Active Company: "${companyName}" · Voice: "${voiceName}" (zero legacy prompt leakage).`, 'lead');

    const cockpit = el('div', undefined, 'outreach-cockpit');

    const left = el('div', undefined, 'cockpit-panel');
    left.append(el('h3', '📞 Instant AI Test Call & WhatsApp Offer'));

    const phoneLabel = el('label', 'Target Phone (E.164)');
    const phoneInput = el('input');
    phoneInput.type = 'tel';
    phoneInput.value = activePhone;
    phoneInput.placeholder = '+77056945245';
    phoneInput.oninput = e => { activePhone = e.target.value.trim(); };
    phoneLabel.append(phoneInput);
    left.append(phoneLabel);

    const pills = el('div', undefined, 'quick-pills');
    const presets = [
      { label: 'Management (+7 705 694-52-45)', phone: '+77056945245' },
      { label: 'Dauren (+7 701 721-11-56)', phone: '+77017211156' },
      { label: 'PBX Line (+7 700 912-12-12)', phone: '+77009121212' }
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

    left.append(el('label', 'Call Language'));
    const langDiv = el('div', undefined, 'lang-toggle');
    const kzBtn = el('button', '🇰🇿 Қазақша', activeLang === 'kz' ? 'active' : '');
    kzBtn.type = 'button';
    kzBtn.onclick = () => { activeLang = 'kz'; render(); };
    const ruBtn = el('button', '🇷🇺 Русский', activeLang === 'ru' ? 'active' : '');
    ruBtn.type = 'button';
    ruBtn.onclick = () => { activeLang = 'ru'; render(); };
    add(langDiv, kzBtn, ruBtn);
    left.append(langDiv);

    const detailsLabel = el('label', 'Extra Call Context (Optional)');
    const detailsInput = el('input');
    detailsInput.placeholder = 'Special discount, delivery terms, or contact note...';
    detailsInput.value = activeDetails;
    detailsInput.oninput = e => { activeDetails = e.target.value; };
    detailsLabel.append(detailsInput);
    left.append(detailsLabel);

    const actions = el('div', undefined, 'actions');
    const callBtn = button('📞 Start AI Test Call', async () => {
      if (!activePhone) throw Error('Enter a target phone number');
      const catalog = state.catalog || [];
      const item = catalog.find(c => (c.slug || c.id) === activeProduct) || catalog[0];
      const prodName = item ? (activeLang === 'kz' ? item.name : (item.name_ru || item.name)) : activeProduct;
      notice(`Calling ${activePhone} as "${companyName}" pitching "${prodName}"...`);
      const res = await api('/outreach/call', 'POST', {
        phone: activePhone,
        product: prodName,
        lang: activeLang,
        details: activeDetails || (item ? `${item.price_range || ''} ${item.specs || ''}`.trim() : '')
      });
      notice('Call initiated! Mode: ' + res.mode + ' · ' + activePhone);
      await refresh();
    }, 'call-btn');

    const offerBtn = button('📄 Send WhatsApp Offer (PDF)', async () => {
      if (!activePhone) throw Error('Enter a target phone number');
      notice('Sending WhatsApp PDF offer to ' + activePhone + '...');
      const res = await api('/outreach/offer', 'POST', {
        phone: activePhone,
        product: activeProduct,
        lang: activeLang
      });
      notice('WhatsApp offer sent! WAMID: ' + res.wamid);
      await refresh();
    }, 'whatsapp-btn');

    const editScriptBtn = button('📝 Edit Call Script', async () => {
      section = 'Telephony & Script Studio';
      render();
    }, 'secondary-btn');

    add(actions, callBtn, offerBtn, editScriptBtn);
    left.append(actions);
    cockpit.append(left);

    const right = el('div', undefined, 'cockpit-panel');
    right.append(el('h3', '📦 Select Product to Pitch'));
    const selector = el('div', undefined, 'product-selector');
    const catalog = state.catalog || [];
    for (const item of catalog) {
      const key = item.slug || item.id;
      const opt = el('div', undefined, 'product-option' + (activeProduct === key ? ' selected' : ''));
      const titleRow = el('div', undefined, 'product-title');
      titleRow.append(el('span', activeLang === 'kz' ? item.name : (item.name_ru || item.name)));
      titleRow.append(el('span', item.price_range || '', 'product-price'));
      opt.append(titleRow);
      if (item.category) opt.append(el('div', item.category, 'product-meta'));
      if (item.specs) opt.append(el('div', item.specs, 'product-meta'));

      opt.onclick = () => {
        activeProduct = key;
        document.querySelectorAll('.product-option').forEach(o => o.classList.remove('selected'));
        opt.classList.add('selected');
      };
      selector.append(opt);
    }
    right.append(selector);
    cockpit.append(right);
    root.append(cockpit);
    return;
  }

  /* ------------------------------------------------------------------
     ANALYTICS & CALL LOGS
     ------------------------------------------------------------------ */
  if (section === 'Analytics & Call Logs') {
    paragraph(root, 'Live operational telemetry, recent AI voice calls, WhatsApp deliveries, and system audit trail.', 'lead');

    const metrics = el('div', undefined, 'metrics');
    const outreach = state.outreach || { calls: [], offers: [] };
    const mData = [
      ['Catalog Products', (state.catalog || []).length],
      ['AI Calls Logged', (outreach.calls || []).length],
      ['WhatsApp Offers', (outreach.offers || []).length],
      ['Daily Cap', state.telephony?.daily_call_cap || 200],
      ['Voice Profile', state.telephony?.voice_name || 'Kore'],
    ];
    for (const [label, val] of mData) {
      const m = el('div', undefined, 'metric');
      m.append(el('span', label), el('strong', String(val)));
      metrics.append(m);
    }
    root.append(metrics);

    const histCard = card('📋 Recent Voice Calls & WhatsApp Offers');
    const allEvents = [
      ...(outreach.calls || []).map(c => ({ type: 'call', ...c })),
      ...(outreach.offers || []).map(o => ({ type: 'offer', ...o }))
    ].sort((a, b) => (b.at || '').localeCompare(a.at || ''));

    if (allEvents.length) {
      const table = el('table');
      const thead = el('thead');
      const trh = el('tr');
      for (const th of ['Time', 'Channel', 'Phone', 'Product', 'Lang', 'Status', 'Output']) trh.append(el('th', th));
      thead.append(trh);
      table.append(thead);

      const tbody = el('tbody');
      for (const ev of allEvents.slice(0, 30)) {
        const tr = el('tr');
        tr.append(el('td', (ev.at || '').replace('T', ' ').slice(0, 19)));
        const tdCh = el('td');
        tdCh.append(ev.type === 'call' ? badge('📞 Voice SDR', 'live-badge') : badge('💬 WhatsApp PDF', 'cyan-badge'));
        tr.append(tdCh);
        tr.append(el('td', ev.phone));
        tr.append(el('td', ev.product));
        tr.append(el('td', ev.lang === 'kz' ? '🇰🇿 KZ' : '🇷🇺 RU'));
        tr.append(el('td', ev.status));
        tr.append(el('td', (ev.response || ev.wamid || ev.error || 'ok').slice(0, 120)));
        tbody.append(tr);
      }
      table.append(tbody);
      histCard.append(table);
    } else {
      paragraph(histCard, 'No calls or offers logged yet.', 'muted');
    }
    root.append(histCard);
  }
}

/* =========================================================================
   BOOTSTRAP & GROUPED SIDEBAR NAVIGATION
   ========================================================================= */
for (const group of navGroups) {
  const gTitle = el('div', group.label, 'nav-group-label');
  $('nav').append(gTitle);
  for (const item of group.items) {
    const b = el('button', `${item.icon}  ${item.label}`);
    b.dataset.section = item.id;
    b.onclick = () => { section = item.id; render(); };
    $('nav').append(b);
  }
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
    }
    refresh(false).catch(() => {
      $('connection').textContent = 'Connection Unavailable';
    });
  }
}, 10000);
