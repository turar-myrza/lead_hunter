'use strict';

window.adsDrawerOpen = false;

window.AdsManagerUI = (function () {
  const S = {
    tab: 'campaigns', // 'campaigns' | 'audiences' | 'leads'
    meta: null,
    campaigns: [],
    audiences: [],
    leads: { items: [], total: 0 },
    selectedCampaigns: new Set(),
    selectedAudiences: new Set(),
    selectedLeads: new Set(),
    search: '',
    statusFilter: 'all',
    loaded: false,
    ctx: null,
    root: null,
    dynamicEl: null,
    searchTimer: null,
    drawerBackdrop: null,
    drawerPanel: null,
  };

  function el(tag, text, cls) {
    const n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (cls) n.className = cls;
    return n;
  }

  function add(parent, ...nodes) {
    for (const n of nodes) if (n) parent.append(n);
    return parent;
  }

  function formatMoney(v) {
    if (v === null || v === undefined || v === '') return '—';
    const num = Number(v);
    if (Number.isNaN(num)) return String(v);
    return Math.round(num).toLocaleString('ru-RU') + ' ₸';
  }

  function makeSwitch(checked, onChange) {
    const lbl = el('label', undefined, 'am-switch');
    const inp = el('input');
    inp.type = 'checkbox';
    inp.checked = Boolean(checked);
    inp.onchange = async () => {
      inp.disabled = true;
      try {
        await onChange(inp.checked);
      } catch (e) {
        inp.checked = !inp.checked;
        S.ctx.notice(e.message);
      } finally {
        inp.disabled = false;
      }
    };
    add(lbl, inp, el('span', undefined, 'am-slider'));
    return lbl;
  }

  function makeCheckbox(checked, onChange) {
    const inp = el('input');
    inp.type = 'checkbox';
    inp.checked = Boolean(checked);
    inp.onchange = () => onChange(inp.checked);
    return inp;
  }

  function queryParamsForAudiences() {
    if (!S.selectedCampaigns.size) return '';
    return '?campaign_ids=' + encodeURIComponent(Array.from(S.selectedCampaigns).join(','));
  }

  function queryParamsForLeads() {
    const p = new URLSearchParams();
    if (S.selectedAudiences.size) {
      p.set('audience_ids', Array.from(S.selectedAudiences).join(','));
    } else if (S.selectedCampaigns.size) {
      p.set('campaign_ids', Array.from(S.selectedCampaigns).join(','));
    }
    if (S.statusFilter && S.statusFilter !== 'all') p.set('status', S.statusFilter);
    if (S.search.trim()) p.set('q', S.search.trim());
    p.set('limit', '200');
    const qs = p.toString();
    return qs ? '?' + qs : '';
  }

  async function loadAll(renderAfter = true) {
    const { api } = S.ctx;
    const [meta, camps, auds, leads] = await Promise.all([
      api('/ads/meta'),
      api('/ads/campaigns'),
      api('/ads/audiences' + queryParamsForAudiences()),
      api('/ads/leads' + queryParamsForLeads()),
    ]);
    S.meta = meta;
    S.campaigns = camps.items || [];
    S.audiences = auds.items || [];
    S.leads = leads || { items: [], total: 0 };
    S.loaded = true;
    const bv = document.getElementById('brand-version');
    if (bv && meta.version) bv.textContent = 'Universal Cockpit v' + meta.version;
    if (renderAfter && S.dynamicEl) renderDynamic();
  }

  function ensureDrawerNodes() {
    if (S.drawerPanel) return;
    S.drawerBackdrop = el('div', undefined, 'am-drawer-backdrop');
    S.drawerBackdrop.hidden = true;
    S.drawerBackdrop.onclick = closeDrawer;

    S.drawerPanel = el('aside', undefined, 'am-drawer');
    S.drawerPanel.hidden = true;
    add(document.body, S.drawerBackdrop, S.drawerPanel);
  }

  function openDrawer(titleText, buildBody, buildFooter) {
    ensureDrawerNodes();
    window.adsDrawerOpen = true;
    S.drawerPanel.replaceChildren();

    const head = el('div', undefined, 'am-drawer-head');
    const closeBtn = el('button', '✕', 'secondary-btn am-row-btn');
    closeBtn.type = 'button';
    closeBtn.onclick = closeDrawer;
    add(head, el('h2', titleText), closeBtn);

    const body = el('div', undefined, 'am-drawer-body');
    buildBody(body);

    const foot = el('div', undefined, 'am-drawer-foot');
    buildFooter(foot);

    add(S.drawerPanel, head, body, foot);
    S.drawerBackdrop.hidden = false;
    S.drawerPanel.hidden = false;
    requestAnimationFrame(() => S.drawerPanel.classList.add('open'));
  }

  function closeDrawer() {
    window.adsDrawerOpen = false;
    if (!S.drawerPanel) return;
    S.drawerPanel.classList.remove('open');
    setTimeout(() => {
      if (!window.adsDrawerOpen) {
        S.drawerPanel.hidden = true;
        S.drawerBackdrop.hidden = true;
      }
    }, 220);
  }

  /* ===================================================================
     CAMPAIGN DRAWER
     =================================================================== */
  function openCampaignDrawer(existing = null) {
    const form = {
      name: existing ? existing.name : '',
      product_key: existing ? existing.product_key : ((S.meta?.catalog?.[0]?.id) || 'centrifuges'),
      objective: existing ? existing.objective : 'call_then_offer',
      lang: existing ? existing.lang : 'kz',
    };

    function draw() {
      openDrawer(existing ? 'Edit Campaign' : 'Create Campaign', body => {
        const nameLbl = el('label', 'Campaign Name');
        const nameInp = el('input');
        nameInp.value = form.name;
        nameInp.placeholder = 'e.g. Центрифугалар B2B Q4';
        nameInp.oninput = e => { form.name = e.target.value; };
        nameLbl.append(nameInp);
        body.append(nameLbl);

        body.append(el('label', 'Product / Service We Sell'));
        const prodGrid = el('div', undefined, 'am-card-grid');
        for (const item of S.meta?.catalog || []) {
          const cardBtn = el('button', undefined, 'am-choice-card' + (form.product_key === item.id ? ' selected' : ''));
          cardBtn.type = 'button';
          add(cardBtn, el('div', item.name, 'product-title'), el('div', item.price_range, 'product-price'));
          cardBtn.onclick = () => { form.product_key = item.id; draw(); };
          prodGrid.append(cardBtn);
        }
        body.append(prodGrid);

        body.append(el('label', 'Campaign Objective'));
        const objWrap = el('div', undefined, 'product-selector');
        for (const obj of S.meta?.objectives || []) {
          const oBtn = el('button', undefined, 'am-choice-card' + (form.objective === obj.id ? ' selected' : ''));
          oBtn.type = 'button';
          add(oBtn, el('div', obj.label));
          oBtn.onclick = () => { form.objective = obj.id; draw(); };
          objWrap.append(oBtn);
        }
        body.append(objWrap);

        body.append(el('label', 'Outreach Language'));
        const langWrap = el('div', undefined, 'lang-toggle');
        const kzBtn = el('button', '🇰🇿 Қазақша', form.lang === 'kz' ? 'active' : '');
        kzBtn.type = 'button';
        kzBtn.onclick = () => { form.lang = 'kz'; draw(); };
        const ruBtn = el('button', '🇷🇺 Русский', form.lang === 'ru' ? 'active' : '');
        ruBtn.type = 'button';
        ruBtn.onclick = () => { form.lang = 'ru'; draw(); };
        add(langWrap, kzBtn, ruBtn);
        body.append(langWrap);
      }, foot => {
        const cancelBtn = el('button', 'Cancel', 'secondary-btn');
        cancelBtn.type = 'button';
        cancelBtn.onclick = closeDrawer;

        const saveBtn = S.ctx.button(existing ? 'Save Changes' : 'Create Campaign', async () => {
          if (existing) {
            await S.ctx.api('/ads/campaigns/' + existing.id, 'PUT', form);
            S.ctx.notice('Campaign updated');
          } else {
            const created = await S.ctx.api('/ads/campaigns', 'POST', form);
            S.selectedCampaigns = new Set([created.id]);
            S.ctx.notice('Campaign created');
          }
          closeDrawer();
          await loadAll(true);
        }, 'primary-btn');
        add(foot, cancelBtn, saveBtn);
      });
    }
    draw();
  }

  /* ===================================================================
     VISUAL FILTER DRAWER FOR AUDIENCES
     =================================================================== */
  function openAudienceDrawer(existing = null) {
    const defaultCampId = existing
      ? existing.campaign_id
      : (Array.from(S.selectedCampaigns)[0] || (S.campaigns[0] && S.campaigns[0].id) || '');

    if (!defaultCampId) {
      S.ctx.notice('Create a Campaign first before adding an Audience Rule Set.');
      return;
    }

    const rules = existing?.rules || {};
    const form = {
      campaign_id: Number(defaultCampId),
      name: existing ? existing.name : '',
      segment: existing ? existing.segment : 'b2b',
      auto_discovery: existing ? Boolean(existing.auto_discovery) : true,
      rules: {
        regions: Array.isArray(rules.regions) && rules.regions.length ? [...rules.regions] : ['almaty'],
        keywords: Array.isArray(rules.keywords) ? [...rules.keywords] : [],
        min_contract_kzt: rules.min_contract_kzt ?? '',
        max_contract_kzt: rules.max_contract_kzt ?? '',
        year: rules.year || 2026,
        sources: Array.isArray(rules.sources) && rules.sources.length ? [...rules.sources] : ['csv_seed'],
        require_phone: rules.require_phone !== undefined ? Boolean(rules.require_phone) : true,
        manual_rows: rules.manual_rows || '',
      },
    };
    if (form.segment === 'b2c') form.rules.sources = ['manual_import'];

    function payloadFromForm() {
      const minVal = form.rules.min_contract_kzt === '' ? null : Number(form.rules.min_contract_kzt);
      const maxVal = form.rules.max_contract_kzt === '' ? null : Number(form.rules.max_contract_kzt);
      return {
        campaign_id: Number(form.campaign_id),
        name: form.name,
        segment: form.segment,
        auto_discovery: form.auto_discovery,
        rules: {
          regions: form.rules.regions.length ? form.rules.regions : ['all'],
          keywords: form.rules.keywords,
          min_contract_kzt: minVal,
          max_contract_kzt: maxVal,
          year: Number(form.rules.year),
          sources: form.segment === 'b2c' ? ['manual_import'] : form.rules.sources,
          require_phone: form.rules.require_phone,
          manual_rows: form.rules.manual_rows || null,
        },
      };
    }

    function draw() {
      openDrawer(existing ? 'Edit Audience Rule Set' : 'New Audience Rule Set (Visual Filters)', body => {
        // Campaign dropdown
        const campLbl = el('label', 'Parent Campaign');
        const campSel = el('select');
        for (const c of S.campaigns) {
          const opt = el('option', c.name + ' (' + c.service + ')');
          opt.value = String(c.id);
          campSel.append(opt);
        }
        campSel.value = String(form.campaign_id);
        campSel.onchange = e => { form.campaign_id = Number(e.target.value); };
        campLbl.append(campSel);
        body.append(campLbl);

        // Name
        const nameLbl = el('label', 'Audience Rule Set Name');
        const nameInp = el('input');
        nameInp.value = form.name;
        nameInp.placeholder = 'e.g. Almaty Private Clinics (B2B)';
        nameInp.oninput = e => { form.name = e.target.value; };
        nameLbl.append(nameInp);
        body.append(nameLbl);

        // Segment cards (B2B / B2G / B2C)
        body.append(el('label', 'Audience Segment'));
        const segGrid = el('div', undefined, 'am-card-grid');
        for (const seg of S.meta?.segments || []) {
          const sBtn = el('button', undefined, 'am-choice-card' + (form.segment === seg.id ? ' selected' : ''));
          sBtn.type = 'button';
          add(sBtn, el('div', seg.label, 'product-title'), el('div', seg.desc, 'am-subtext'));
          sBtn.onclick = () => {
            form.segment = seg.id;
            if (seg.id === 'b2c') {
              form.rules.sources = ['manual_import'];
            } else if (seg.id === 'b2g' && !form.rules.sources.includes('goszakup')) {
              form.rules.sources = ['goszakup', 'csv_seed'];
            }
            draw();
          };
          segGrid.append(sBtn);
        }
        body.append(segGrid);

        if (form.segment === 'b2c') {
          body.append(el('div', 'Policy: B2C audiences only accept manual_import CSV rows. Individual web scraping is disabled.', 'am-policy-note'));
        }

        // Region multi-select chips
        body.append(el('label', 'Regions (Multi-select)'));
        const regWrap = el('div', undefined, 'quick-pills');
        for (const reg of S.meta?.regions || []) {
          const active = form.rules.regions.includes(reg.id);
          const chip = el('button', reg.label, 'pill-btn' + (active ? ' active' : ''));
          chip.type = 'button';
          chip.onclick = () => {
            if (reg.id === 'all') {
              form.rules.regions = ['all'];
            } else {
              const next = form.rules.regions.filter(r => r !== 'all');
              const idx = next.indexOf(reg.id);
              if (idx >= 0) next.splice(idx, 1);
              else next.push(reg.id);
              form.rules.regions = next.length ? next : ['all'];
            }
            draw();
          };
          regWrap.append(chip);
        }
        body.append(regWrap);

        // Keywords tag input
        const kwLbl = el('label', 'Keywords (press Enter to add filter tag, max 10)');
        const kwInp = el('input');
        kwInp.placeholder = 'e.g. шприц, центрифуга, клиника';
        kwInp.onkeydown = e => {
          if (e.key === 'Enter') {
            e.preventDefault();
            const val = kwInp.value.trim();
            if (val.length >= 2 && val.length <= 60 && form.rules.keywords.length < 10 && !form.rules.keywords.includes(val)) {
              form.rules.keywords.push(val);
              draw();
            }
          }
        };
        kwLbl.append(kwInp);
        body.append(kwLbl);

        const tagsWrap = el('div', undefined, 'am-tags');
        for (const kw of form.rules.keywords) {
          const tag = el('span', kw + ' ', 'am-tag');
          const rm = el('button', '×', 'am-chip-clear');
          rm.type = 'button';
          rm.onclick = () => {
            form.rules.keywords = form.rules.keywords.filter(x => x !== kw);
            draw();
          };
          tag.append(rm);
          tagsWrap.append(tag);
        }
        body.append(tagsWrap);

        // Contract sum Min / Max + Slider
        body.append(el('label', 'Contract Sum Bounds (KZT)'));
        const sumRow = el('div', undefined, 'am-row-2');
        const minInp = el('input');
        minInp.type = 'number';
        minInp.min = '0';
        minInp.placeholder = 'Min KZT (e.g. 50000)';
        minInp.value = form.rules.min_contract_kzt;
        minInp.oninput = e => { form.rules.min_contract_kzt = e.target.value; };

        const maxInp = el('input');
        maxInp.type = 'number';
        maxInp.min = '0';
        maxInp.placeholder = 'Max KZT';
        maxInp.value = form.rules.max_contract_kzt;
        maxInp.oninput = e => { form.rules.max_contract_kzt = e.target.value; };
        add(sumRow, minInp, maxInp);
        body.append(sumRow);

        const slider = el('input');
        slider.type = 'range';
        slider.min = '0';
        slider.max = '10000000';
        slider.step = '50000';
        slider.value = form.rules.min_contract_kzt || '0';
        slider.oninput = e => {
          form.rules.min_contract_kzt = e.target.value === '0' ? '' : e.target.value;
          minInp.value = form.rules.min_contract_kzt;
        };
        body.append(slider);

        // Year
        const yrLbl = el('label', 'Procurement / Tender Year');
        const yrSel = el('select');
        for (const y of [2024, 2025, 2026, 2027]) {
          const o = el('option', String(y));
          o.value = String(y);
          yrSel.append(o);
        }
        yrSel.value = String(form.rules.year);
        yrSel.onchange = e => { form.rules.year = Number(e.target.value); };
        yrLbl.append(yrSel);
        body.append(yrLbl);

        // Sources with live status badges
        body.append(el('label', 'Data Sources'));
        const srcStatus = S.meta?.source_status || {};
        for (const src of S.meta?.sources || []) {
          const row = el('div', undefined, 'actions');
          const lbl = el('label');
          const chk = el('input');
          chk.type = 'checkbox';
          const lockedB2C = form.segment === 'b2c' && src.id !== 'manual_import';
          chk.disabled = lockedB2C;
          chk.checked = form.rules.sources.includes(src.id);
          chk.onchange = () => {
            const cur = new Set(form.rules.sources);
            if (chk.checked) cur.add(src.id);
            else if (cur.size > 1) cur.delete(src.id);
            form.rules.sources = Array.from(cur);
            draw();
          };
          lbl.append(chk, document.createTextNode(' ' + src.label + ' '));
          const stText = srcStatus[src.id];
          if (stText) {
            lbl.append(el('span', stText, stText === 'ok' ? 'wl-ok' : 'err-badge'));
          } else if (src.id === 'csv_seed') {
            lbl.append(el('span', 'ready', 'wl-ok'));
          }
          row.append(lbl);
          body.append(row);
        }

        // Manual CSV textarea if manual_import selected
        if (form.rules.sources.includes('manual_import')) {
          const manLbl = el('label', 'Manual CSV Import (columns: company,bin,city,phone,director,category)');
          const manTxt = el('textarea');
          manTxt.value = form.rules.manual_rows;
          manTxt.placeholder = 'company,bin,city,phone,director,category\nТест ЖШС,000000000000,Almaty,+77056945245,Иванов,Центрифугалар';
          manTxt.oninput = e => { form.rules.manual_rows = e.target.value; };
          manLbl.append(manTxt);
          body.append(manLbl);
        }

        // Require phone & Auto discovery switches
        const optRow = el('div', undefined, 'am-row-2');
        const reqBox = el('div');
        reqBox.append(el('label', 'Require Phone'), makeSwitch(form.rules.require_phone, v => { form.rules.require_phone = v; }));
        const autoBox = el('div');
        autoBox.append(el('label', 'Auto Discovery (30m)'), makeSwitch(form.auto_discovery, v => { form.auto_discovery = v; }));
        add(optRow, reqBox, autoBox);
        body.append(optRow);
      }, foot => {
        const cancelBtn = el('button', 'Cancel', 'secondary-btn');
        cancelBtn.type = 'button';
        cancelBtn.onclick = closeDrawer;

        const saveBtn = S.ctx.button('Save', async () => {
          const p = payloadFromForm();
          if (existing) {
            await S.ctx.api('/ads/audiences/' + existing.id, 'PUT', p);
            S.ctx.notice('Audience rule set updated');
          } else {
            await S.ctx.api('/ads/audiences', 'POST', p);
            S.ctx.notice('Audience rule set created');
          }
          closeDrawer();
          await loadAll(true);
        }, 'secondary-btn');

        const saveRunBtn = S.ctx.button('Save & Run Discovery', async () => {
          const p = payloadFromForm();
          let saved;
          if (existing) {
            saved = await S.ctx.api('/ads/audiences/' + existing.id, 'PUT', p);
          } else {
            saved = await S.ctx.api('/ads/audiences', 'POST', p);
          }
          await S.ctx.api('/ads/audiences/' + saved.id + '/discover', 'POST');
          S.selectedAudiences = new Set([saved.id]);
          S.tab = 'leads';
          S.ctx.notice('Audience saved & discovery queued — worker updates leads within seconds.');
          closeDrawer();
          await loadAll(true);
        }, 'primary-btn');

        add(foot, cancelBtn, saveBtn, saveRunBtn);
      });
    }
    draw();
  }

  /* ===================================================================
     QUEUE DIALOG / DRAWER
     =================================================================== */
  function openQueueDrawer() {
    const ids = Array.from(S.selectedLeads);
    if (!ids.length) {
      S.ctx.notice('Select one or more leads first.');
      return;
    }
    const form = { gap_seconds: 90, lang: '' };
    openDrawer('Queue Selected Leads (' + ids.length + ')', body => {
      body.append(el('p', 'Auto-dialer calls enabled leads strictly present in PBX whitelist during Mon–Fri 09:00–19:00 Asia/Almaty.', 'lead'));
      const gapLbl = el('label', 'Pause Between Calls (seconds: 30–600)');
      const gapInp = el('input');
      gapInp.type = 'number';
      gapInp.min = '30';
      gapInp.max = '600';
      gapInp.value = '90';
      gapInp.oninput = e => { form.gap_seconds = Number(e.target.value || 90); };
      gapLbl.append(gapInp);
      body.append(gapLbl);

      const langLbl = el('label', 'Override Language (Optional)');
      const langSel = el('select');
      add(langSel, el('option', 'Use campaign default', ''), el('option', 'Қазақша (kz)', ''), el('option', 'Русский (ru)', ''));
      langSel.options[0].value = '';
      langSel.options[1].value = 'kz';
      langSel.options[2].value = 'ru';
      langSel.onchange = e => { form.lang = e.target.value; };
      langLbl.append(langSel);
      body.append(langLbl);
    }, foot => {
      const cancelBtn = el('button', 'Cancel', 'secondary-btn');
      cancelBtn.type = 'button';
      cancelBtn.onclick = closeDrawer;

      const goBtn = S.ctx.button('Enqueue Calls', async () => {
        const res = await S.ctx.api('/ads/leads/queue', 'POST', {
          lead_ids: ids,
          gap_seconds: form.gap_seconds,
          lang: form.lang || null,
        });
        const skippedMsg = res.skipped?.length ? ' · Skipped ' + res.skipped.length + ' (whitelist/gate)' : '';
        S.ctx.notice('Queued ' + res.queued + ' call(s)' + skippedMsg);
        closeDrawer();
        await loadAll(true);
      }, 'call-btn');
      add(foot, cancelBtn, goBtn);
    });
  }

  /* ===================================================================
     RENDER HELPERS
     =================================================================== */
  function makeTabBtn(id, label, selSet) {
    const btn = el('button', undefined, 'am-tab' + (S.tab === id ? ' active' : ''));
    btn.type = 'button';
    btn.append(el('span', label));
    if (selSet.size) {
      const chip = el('span', selSet.size + ' selected', 'am-chip');
      const clr = el('button', '×', 'am-chip-clear');
      clr.type = 'button';
      clr.title = 'Clear selection';
      clr.onclick = async e => {
        e.stopPropagation();
        selSet.clear();
        await loadAll(true);
      };
      chip.append(clr);
      btn.append(chip);
    }
    btn.onclick = () => {
      S.tab = id;
      loadAll(true);
    };
    return btn;
  }

  function renderCampaignsTable(cardWrap) {
    const table = el('table', undefined, 'am-table');
    const thead = el('thead');
    const trh = el('tr');
    const masterChk = makeCheckbox(
      S.campaigns.length && S.campaigns.every(c => S.selectedCampaigns.has(c.id)),
      async checked => {
        if (checked) S.campaigns.forEach(c => S.selectedCampaigns.add(c.id));
        else S.selectedCampaigns.clear();
        await loadAll(true);
      }
    );
    const thChk = el('th');
    thChk.append(masterChk);
    trh.append(thChk);
    for (const h of ['On/Off', 'Campaign', 'Product / Service', 'Objective', 'Lang', 'Audiences', 'Leads', 'Called', 'Offers', 'Queued', 'Status']) {
      trh.append(el('th', h));
    }
    thead.append(trh);
    table.append(thead);

    const tbody = el('tbody');
    for (const c of S.campaigns) {
      const tr = el('tr');
      const tdChk = el('td');
      tdChk.append(makeCheckbox(S.selectedCampaigns.has(c.id), async chk => {
        if (chk) S.selectedCampaigns.add(c.id);
        else S.selectedCampaigns.delete(c.id);
        await loadAll(true);
      }));
      tr.append(tdChk);

      const tdSw = el('td');
      tdSw.append(makeSwitch(c.status === 'active', async en => {
        await S.ctx.api('/ads/campaigns/' + c.id + '/toggle', 'POST', { enabled: en });
        await loadAll(true);
      }));
      tr.append(tdSw);

      const tdName = el('td');
      const nameLink = el('button', c.name, 'am-link-btn');
      nameLink.type = 'button';
      nameLink.onclick = () => {
        S.selectedCampaigns = new Set([c.id]);
        S.selectedAudiences.clear();
        S.tab = 'audiences';
        loadAll(true);
      };
      tdName.append(nameLink);
      tr.append(tdName);

      tr.append(el('td', c.service));
      tr.append(el('td', c.objective));
      tr.append(el('td', c.lang === 'kz' ? '🇰🇿 KZ' : '🇷🇺 RU'));
      tr.append(el('td', c.audiences_count));
      tr.append(el('td', c.leads_count));
      tr.append(el('td', c.called_count));
      tr.append(el('td', c.offers_count));
      tr.append(el('td', c.queued_count));

      const tdSt = el('td');
      tdSt.append(S.ctx.badge(c.status, c.status === 'active' ? 'live-badge' : ''));
      tr.append(tdSt);
      tbody.append(tr);
    }
    table.append(tbody);
    cardWrap.append(table);
    if (!S.campaigns.length) {
      cardWrap.append(el('p', 'No campaigns yet. Click "+ Create" to start a campaign.', 'lead'));
    }
  }

  function renderAudiencesTable(cardWrap) {
    const table = el('table', undefined, 'am-table');
    const thead = el('thead');
    const trh = el('tr');
    const masterChk = makeCheckbox(
      S.audiences.length && S.audiences.every(a => S.selectedAudiences.has(a.id)),
      async checked => {
        if (checked) S.audiences.forEach(a => S.selectedAudiences.add(a.id));
        else S.selectedAudiences.clear();
        await loadAll(true);
      }
    );
    const thChk = el('th');
    thChk.append(masterChk);
    trh.append(thChk);
    for (const h of ['On/Off', 'Audience Rule Set', 'Segment', 'Campaign', 'Regions', 'Keywords', 'Sources', 'Auto', 'Last Run / Result', 'Leads']) {
      trh.append(el('th', h));
    }
    thead.append(trh);
    table.append(thead);

    const tbody = el('tbody');
    for (const a of S.audiences) {
      const tr = el('tr');
      const tdChk = el('td');
      tdChk.append(makeCheckbox(S.selectedAudiences.has(a.id), async chk => {
        if (chk) S.selectedAudiences.add(a.id);
        else S.selectedAudiences.delete(a.id);
        await loadAll(true);
      }));
      tr.append(tdChk);

      const tdSw = el('td');
      tdSw.append(makeSwitch(a.status === 'active', async en => {
        await S.ctx.api('/ads/audiences/' + a.id + '/toggle', 'POST', { enabled: en });
        await loadAll(true);
      }));
      tr.append(tdSw);

      const tdName = el('td');
      const nameLink = el('button', a.name, 'am-link-btn');
      nameLink.type = 'button';
      nameLink.onclick = () => {
        S.selectedAudiences = new Set([a.id]);
        S.tab = 'leads';
        loadAll(true);
      };
      tdName.append(nameLink);
      tr.append(tdName);

      const tdSeg = el('td');
      tdSeg.append(el('span', a.segment, 'seg-pill seg-' + a.segment));
      tr.append(tdSeg);

      tr.append(el('td', a.campaign_name));
      tr.append(el('td', (a.rules?.regions || []).join(', ')));
      tr.append(el('td', (a.rules?.keywords || []).join(', ') || '—'));
      tr.append(el('td', (a.rules?.sources || []).join(', ')));
      tr.append(el('td', a.auto_discovery ? '30m' : 'Manual'));

      const tdRes = el('td');
      if (a.run_requested) {
        tdRes.append(S.ctx.badge('Discovery queued...', 'cyan-badge'));
      } else if (a.last_result) {
        const txt = 'found ' + (a.last_result.found ?? 0) + ', new ' + (a.last_result.new ?? 0);
        tdRes.append(el('div', txt));
        const errs = a.last_result.errors || {};
        for (const [k, v] of Object.entries(errs)) {
          tdRes.append(el('span', k + ': ' + v, 'err-badge'));
        }
        if (a.last_run) tdRes.append(el('div', a.last_run.replace('T', ' ').slice(0, 16), 'am-subtext'));
      } else {
        tdRes.append(el('span', 'Not run yet', 'am-subtext'));
      }
      tr.append(tdRes);
      tr.append(el('td', a.leads_count));
      tbody.append(tr);
    }
    table.append(tbody);
    cardWrap.append(table);
    if (!S.audiences.length) {
      cardWrap.append(el('p', 'No audience rule sets match current campaign filter. Click "+ Create" to define B2B/B2G/B2C rules.', 'lead'));
    }
  }

  function renderLeadsTable(cardWrap) {
    const items = S.leads.items || [];
    const table = el('table', undefined, 'am-table');
    const thead = el('thead');
    const trh = el('tr');
    const masterChk = makeCheckbox(
      items.length && items.every(l => S.selectedLeads.has(l.id)),
      async checked => {
        if (checked) items.forEach(l => S.selectedLeads.add(l.id));
        else S.selectedLeads.clear();
        renderDynamic();
      }
    );
    const thChk = el('th');
    thChk.append(masterChk);
    trh.append(thChk);
    for (const h of ['On/Off', 'Company', 'BIN', 'City', 'Category', 'Service We Sell', 'Director', 'Phone / Whitelist', 'Contract Sum', 'ICP Score', 'Source', 'Status', 'Actions']) {
      trh.append(el('th', h));
    }
    thead.append(trh);
    table.append(thead);

    const tbody = el('tbody');
    for (const l of items) {
      const tr = el('tr');
      const tdChk = el('td');
      tdChk.append(makeCheckbox(S.selectedLeads.has(l.id), chk => {
        if (chk) S.selectedLeads.add(l.id);
        else S.selectedLeads.delete(l.id);
        renderDynamic();
      }));
      tr.append(tdChk);

      const tdSw = el('td');
      tdSw.append(makeSwitch(l.enabled, async en => {
        await S.ctx.api('/ads/leads/' + l.id + '/toggle', 'POST', { enabled: en });
        await loadAll(true);
      }));
      tr.append(tdSw);

      const tdComp = el('td');
      tdComp.append(el('div', l.company));
      tdComp.append(el('div', l.audience_name + ' · ' + l.segment.toUpperCase(), 'am-subtext'));
      tr.append(tdComp);

      tr.append(el('td', l.bin || '—'));
      tr.append(el('td', l.city || '—'));
      tr.append(el('td', l.category || '—'));
      tr.append(el('td', l.service));
      tr.append(el('td', l.director || '—'));

      const tdPh = el('td');
      tdPh.append(el('div', l.phone_e164 || l.phone_raw || '—'));
      if (l.phone_e164) {
        tdPh.append(el('span', l.whitelisted ? '✓ PBX Whitelisted' : '✗ Not whitelisted', l.whitelisted ? 'wl-ok' : 'wl-no'));
      }
      tr.append(tdPh);

      tr.append(el('td', formatMoney(l.contract_sum_kzt)));

      const tdIcp = el('td');
      const barWrap = el('div', undefined, 'icp-bar-wrap');
      const barFill = el('div', undefined, 'icp-bar-fill');
      barFill.style.width = Math.max(0, Math.min(100, Number(l.icp_score || 0))) + '%';
      barWrap.append(barFill);
      tdIcp.append(barWrap, el('span', (l.icp_score || 0) + '%', 'am-subtext'));
      tr.append(tdIcp);

      const tdSrc = el('td');
      if (l.source_url) {
        const a = el('a', l.source || 'link');
        a.href = l.source_url;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        tdSrc.append(a);
      } else {
        tdSrc.append(el('span', l.source || '—'));
      }
      tr.append(tdSrc);

      const tdSt = el('td');
      const stCls = l.status === 'called' || l.status === 'offer_sent' ? 'live-badge' : (l.status === 'queued' ? 'cyan-badge' : '');
      tdSt.append(S.ctx.badge(l.status, stCls));
      if (l.last_result) tdSt.append(el('div', String(l.last_result).slice(0, 48), 'am-subtext'));
      tr.append(tdSt);

      const tdAct = el('td');
      const actDiv = el('div', undefined, 'actions');
      const callBtn = S.ctx.button('📞', async () => {
        S.ctx.notice('Calling ' + l.phone_e164 + '...');
        const res = await S.ctx.api('/ads/leads/' + l.id + '/call', 'POST');
        S.ctx.notice('Call initiated to ' + res.phone);
        await loadAll(true);
      }, 'call-btn am-row-btn');
      callBtn.title = 'Call now via PBX';

      const offerBtn = S.ctx.button('📄', async () => {
        S.ctx.notice('Sending WhatsApp PDF to ' + l.phone_e164 + '...');
        const res = await S.ctx.api('/ads/leads/' + l.id + '/offer', 'POST');
        S.ctx.notice('WhatsApp PDF sent · WAMID: ' + res.wamid);
        await loadAll(true);
      }, 'whatsapp-btn am-row-btn');
      offerBtn.title = 'Send WhatsApp Offer';

      add(actDiv, callBtn, offerBtn);
      tdAct.append(actDiv);
      tr.append(tdAct);
      tbody.append(tr);
    }
    table.append(tbody);
    cardWrap.append(table);
    if (!items.length) {
      cardWrap.append(el('p', 'No leads found for current filters. Select an Audience Rule Set and click "▶ Run discovery".', 'lead'));
    }
  }

  function renderDynamic() {
    const dyn = S.dynamicEl;
    if (!dyn) return;
    dyn.replaceChildren();

    // 3 Tabs
    const tabs = el('div', undefined, 'am-tabs');
    add(
      tabs,
      makeTabBtn('campaigns', 'Кампаниялар / Campaigns (' + S.campaigns.length + ')', S.selectedCampaigns),
      makeTabBtn('audiences', 'Аудиториялар / Audiences (' + S.audiences.length + ')', S.selectedAudiences),
      makeTabBtn('leads', 'Лидтер / Leads (' + (S.leads.total ?? 0) + ')', S.selectedLeads)
    );
    dyn.append(tabs);

    // Toolbar
    const toolbar = el('div', undefined, 'am-toolbar');
    const leftGrp = el('div', undefined, 'am-toolbar-group');

    leftGrp.append(S.ctx.button('+ Create', () => {
      if (S.tab === 'campaigns') openCampaignDrawer(null);
      else openAudienceDrawer(null);
    }, 'am-create-btn'));

    if (S.tab === 'campaigns' && S.selectedCampaigns.size === 1) {
      const cid = Array.from(S.selectedCampaigns)[0];
      const camp = S.campaigns.find(c => c.id === cid);
      if (camp) {
        leftGrp.append(S.ctx.button('Duplicate', async () => {
          const dup = await S.ctx.api('/ads/campaigns/' + cid + '/duplicate', 'POST');
          S.selectedCampaigns = new Set([dup.id]);
          S.ctx.notice('Campaign duplicated (paused)');
          await loadAll(true);
        }, 'secondary-btn'));
        leftGrp.append(S.ctx.button('Edit', () => openCampaignDrawer(camp), 'secondary-btn'));
      }
    }

    if (S.tab === 'audiences') {
      if (S.selectedAudiences.size === 1) {
        const aid = Array.from(S.selectedAudiences)[0];
        const aud = S.audiences.find(a => a.id === aid);
        if (aud) leftGrp.append(S.ctx.button('Edit', () => openAudienceDrawer(aud), 'secondary-btn'));
      }
      if (S.selectedAudiences.size >= 1) {
        leftGrp.append(S.ctx.button('▶ Run discovery', async () => {
          const ids = Array.from(S.selectedAudiences);
          for (const id of ids) await S.ctx.api('/ads/audiences/' + id + '/discover', 'POST');
          S.ctx.notice('Discovery requested for ' + ids.length + ' audience(s)');
          await loadAll(true);
        }, 'primary-btn'));
      }
    }

    if (S.tab === 'leads') {
      if (S.selectedLeads.size >= 1) {
        leftGrp.append(S.ctx.button('📞 Call selected', async () => {
          const id = Array.from(S.selectedLeads)[0];
          const res = await S.ctx.api('/ads/leads/' + id + '/call', 'POST');
          S.ctx.notice('Call initiated to ' + res.phone);
          await loadAll(true);
        }, 'call-btn'));
        leftGrp.append(S.ctx.button('⏱ Queue selected', openQueueDrawer, 'primary-btn'));
        leftGrp.append(S.ctx.button('📄 Send offer', async () => {
          const ids = Array.from(S.selectedLeads);
          let okCount = 0;
          for (const id of ids) {
            await S.ctx.api('/ads/leads/' + id + '/offer', 'POST');
            okCount++;
          }
          S.ctx.notice('Sent WhatsApp PDF offer to ' + okCount + ' lead(s)');
          await loadAll(true);
        }, 'whatsapp-btn'));
      }
      leftGrp.append(S.ctx.button('⬇ Export CSV', () => {
        window.open('/platform/api/ads/leads/export.csv' + queryParamsForLeads(), '_self');
      }, 'secondary-btn'));
    }

    // Queue status & controls on the right
    const rightGrp = el('div', undefined, 'am-toolbar-group');
    const qInfo = S.meta?.queue || { paused: false, counts: {} };
    const queuedCnt = qInfo.counts?.queued || 0;
    const winLabel = S.meta?.dial_window_open ? '🟢 09–19 Almaty' : '⏸ Outside 09–19';
    rightGrp.append(S.ctx.badge((qInfo.paused ? 'Queue Paused' : 'Queue Running') + ' · ' + queuedCnt + ' queued', qInfo.paused ? '' : 'live-badge'));
    rightGrp.append(S.ctx.badge(winLabel + ' · ' + (S.meta?.calls_today ?? 0) + '/' + (S.meta?.daily_call_cap ?? 200)));

    if (qInfo.paused) {
      rightGrp.append(S.ctx.button('Resume', async () => {
        await S.ctx.api('/ads/queue/resume', 'POST');
        await loadAll(true);
      }, 'secondary-btn am-row-btn'));
    } else {
      rightGrp.append(S.ctx.button('Pause', async () => {
        await S.ctx.api('/ads/queue/pause', 'POST');
        await loadAll(true);
      }, 'secondary-btn am-row-btn'));
    }
    if (queuedCnt > 0) {
      rightGrp.append(S.ctx.button('Clear', async () => {
        await S.ctx.api('/ads/queue/clear', 'POST');
        await loadAll(true);
      }, 'secondary-btn am-row-btn'));
    }

    add(toolbar, leftGrp, rightGrp);
    dyn.append(toolbar);

    // Table card
    const tableCard = el('div', undefined, 'am-table-card');
    if (S.tab === 'campaigns') renderCampaignsTable(tableCard);
    else if (S.tab === 'audiences') renderAudiencesTable(tableCard);
    else renderLeadsTable(tableCard);
    dyn.append(tableCard);
  }

  function render(root, ctx) {
    S.root = root;
    S.ctx = ctx;
    const wrap = el('div', undefined, 'am-wrap');

    // Persistent search + status filter bar
    const searchBar = el('div', undefined, 'am-search-bar');
    const searchInp = el('input');
    searchInp.type = 'search';
    searchInp.placeholder = 'Search company, BIN, phone...';
    searchInp.value = S.search;
    searchInp.oninput = e => {
      S.search = e.target.value;
      clearTimeout(S.searchTimer);
      S.searchTimer = setTimeout(() => {
        if (S.tab === 'leads') loadAll(true);
      }, 280);
    };

    const stSel = el('select');
    for (const [val, lbl] of [
      ['all', 'All Lead Statuses'],
      ['new', 'Status: new'],
      ['queued', 'Status: queued'],
      ['called', 'Status: called'],
      ['offer_sent', 'Status: offer_sent'],
      ['blocked_whitelist', 'Status: blocked_whitelist'],
      ['failed', 'Status: failed'],
    ]) {
      const opt = el('option', lbl);
      opt.value = val;
      stSel.append(opt);
    }
    stSel.value = S.statusFilter;
    stSel.onchange = e => {
      S.statusFilter = e.target.value;
      S.tab = 'leads';
      loadAll(true);
    };
    add(searchBar, searchInp, stSel);
    wrap.append(searchBar);

    S.dynamicEl = el('div', undefined, 'am-wrap');
    wrap.append(S.dynamicEl);
    root.append(wrap);

    if (S.loaded) renderDynamic();
    loadAll(true).catch(err => ctx.notice(err.message));
  }

  function poll() {
    if (!S.ctx || window.adsDrawerOpen) return;
    loadAll(true).catch(() => {});
  }

  return { render, poll };
})();
