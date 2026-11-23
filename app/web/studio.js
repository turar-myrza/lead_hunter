'use strict';

/* =========================================================================
   Lead Hunter Platform Studio UI (v2.8.0)
   - Dedicated AI Connectors vs. CRM & ERP Sync Tabs
   - Multi-Company Legal Entity Manager (Advertise from Selected Entity)
   - Document Templates Library & Uploader (Offer, Inquiry, Contract, etc.)
   - Declarative Component Mounts (Zero DOM duplication on Grid/List toggle)
   - LiteLLM Unified Router & Tier Connectors (Research, Reasoning, Test, Local)
   - SSRF-Safe Prompt-to-Product Web Scraper & Pydantic Schema Extractor
   - SQLite FTS5 BM25 Market Seed Explorer & Prompt-to-Audience Analyzer
   - End-to-End Bitrix24 CRM Handoff & Pipeline Funnel Analytics
   ========================================================================= */

window.PlatformStudioUI = (() => {
  let viewMode = 'grid';
  let selectedProductIds = new Set();
  let lastProductDraft = null;
  let lastAudienceAnalysis = null;
  let selectedProposedIdx = new Set();
  let studioSettings = null;
  let aiProviders = [];
  let studioProducts = [];
  let companyEntities = [];
  let campaignsCache = [];
  let scriptLang = 'kz';
  let scriptProductKey = '';
  let lastScriptPreview = null;

  function pcm16Base64ToWavUrl(b64, sampleRate = 24000) {
    const bin = atob(b64);
    const pcmLen = bin.length;
    const buf = new ArrayBuffer(44 + pcmLen);
    const view = new DataView(buf);
    const writeStr = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
    writeStr(0, 'RIFF');
    view.setUint32(4, 36 + pcmLen, true);
    writeStr(8, 'WAVE');
    writeStr(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeStr(36, 'data');
    view.setUint32(40, pcmLen, true);
    const bytes = new Uint8Array(buf, 44);
    for (let i = 0; i < pcmLen; i++) bytes[i] = bin.charCodeAt(i);
    return URL.createObjectURL(new Blob([buf], { type: 'audio/wav' }));
  }

  async function playVoicePreview(api, notice, voiceName, lang, text) {
    notice(`Generating ${lang.toUpperCase()} audio preview for voice "${voiceName}"...`);
    const res = await api('/studio/voice/preview', 'POST', { voice_name: voiceName, lang, text: text || '' });
    if (res.mode === 'gemini_audio' && res.audio_base64) {
      let url;
      if ((res.mime_type || '').toLowerCase().includes('l16') || (res.mime_type || '').toLowerCase().includes('pcm')) {
        url = pcm16Base64ToWavUrl(res.audio_base64, 24000);
      } else {
        url = `data:${res.mime_type || 'audio/mp3'};base64,${res.audio_base64}`;
      }
      const audio = new Audio(url);
      audio.play().catch(e => notice('Audio playback blocked by browser: ' + e.message));
      notice(`Playing Gemini voice "${voiceName}" (${lang.toUpperCase()})`);
      return;
    }
    if ('speechSynthesis' in window) {
      window.speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(res.text);
      u.lang = res.lang || (lang === 'kz' ? 'kk-KZ' : 'ru-RU');
      u.rate = 1.0;
      window.speechSynthesis.speak(u);
      notice(`Playing browser TTS preview (${u.lang})`);
    } else {
      notice('Preview text: ' + res.text);
    }
  }

  /* =========================================================================
     1. PRODUCT CATALOG & PROMPT-TO-PRODUCT GENERATOR
     ========================================================================= */
  async function renderProductCatalog(root, h) {
    const { api, el, add, button, badge, notice, paragraph, edit, field, onSelectForAudience, onRefreshState } = h;
    
    // Strict clean mount: clear root once at start
    root.replaceChildren();

    paragraph(root, 'Add products manually, sync from Bitrix24 / MoySklad, or enter a prompt so AI researches real specs and pricing from the internet.', 'lead');

    const loading = el('p', 'Loading catalog...', 'muted');
    root.append(loading);
    try {
      const [pRes, cRes] = await Promise.all([
        api('/studio/products'),
        api('/studio/companies').catch(() => ({ items: [] })),
      ]);
      studioProducts = pRes.items || [];
      companyEntities = cRes.items || [];
    } catch (e) {
      loading.textContent = 'Failed to load catalog: ' + e.message;
      return;
    }
    loading.remove();

    // Card 1: Prompt-to-Product Box
    const promptBox = el('div', undefined, 'card studio-highlight');
    promptBox.append(el('h3', '✨ Prompt-to-Product AI Web Research'));
    paragraph(promptBox, 'Enter any product or service name/description. AI searches the internet for real specifications, models, and KZT price ranges to fill your product card.', 'muted');

    const promptRow = el('div', undefined, 'studio-inline-row');
    const pInput = el('input');
    pInput.type = 'text';
    pInput.placeholder = 'e.g. High-speed laboratory centrifuge DLAB DM0412 or Bitrix24 CRM Implementation...';
    const pUrl = el('input');
    pUrl.type = 'url';
    pUrl.placeholder = 'Optional URL to scrape (https://...)';
    pUrl.style.maxWidth = '260px';
    const pLang = el('select');
    add(pLang, el('option', 'RU', ''), el('option', 'KZ', ''));
    pLang.options[0].value = 'ru';
    pLang.options[1].value = 'kz';

    const draftContainer = el('div', undefined, 'studio-draft-wrapper');

    const genBtn = button('✨ Research & Fill Card', async () => {
      const q = pInput.value.trim();
      if (q.length < 3) throw Error('Enter at least 3 characters describing the product');
      notice('AI is researching real product specs from the internet...');
      const r = await api('/studio/products/from-prompt', 'POST', {
        prompt: q,
        url: pUrl.value.trim(),
        lang: pLang.value,
        auto_save: false,
      });
      lastProductDraft = r;
      notice(`Product researched using ${r.model_used || 'AI'} (${r.latency_ms || 0}ms)`);
      renderDraft();
    }, 'primary-btn');
    add(promptRow, pInput, pUrl, pLang, genBtn);
    promptBox.append(promptRow, draftContainer);

    function renderDraft() {
      draftContainer.replaceChildren();
      if (!lastProductDraft || !lastProductDraft.draft) return;
      const d = lastProductDraft.draft;
      const draftCard = el('div', undefined, 'studio-draft-box');
      draftCard.append(el('h4', `Draft Ready (${lastProductDraft.model_used || 'AI'})`));
      const fName = el('input'); fName.value = d.name || '';
      const fNameRu = el('input'); fNameRu.value = d.name_ru || '';
      const fCat = el('input'); fCat.value = d.category || '';
      const fPrice = el('input'); fPrice.value = d.price_range || '';
      const fModels = el('input'); fModels.value = (d.models || []).join(', ');
      const fSpecs = el('textarea'); fSpecs.value = d.specs || '';
      const fCerts = el('input'); fCerts.value = d.certifications || '';
      const fUse = el('input'); fUse.value = d.target_use_case || '';

      const grid = el('div', undefined, 'studio-form-grid');
      const wrap = (lbl, node) => { const l = el('label', lbl); l.append(node); return l; };
      add(grid,
        wrap('Name (KZ)', fName),
        wrap('Name (RU)', fNameRu),
        wrap('Category', fCat),
        wrap('Price Range (KZT)', fPrice),
        wrap('Models (comma-separated)', fModels),
        wrap('Certifications (CE, ISO, GOST)', fCerts),
        wrap('Target Use Case', fUse)
      );
      draftCard.append(grid, wrap('Technical Specifications', fSpecs));

      if (lastProductDraft.sources?.length) {
        const sRow = el('div', undefined, 'studio-sources');
        sRow.append(el('span', 'Sources: ', 'muted'));
        for (const s of lastProductDraft.sources.slice(0, 4)) {
          const a = el('a', s.title || s.uri, 'badge cyan-badge');
          a.href = s.uri; a.target = '_blank';
          sRow.append(a);
        }
        draftCard.append(sRow);
      }

      const dActions = el('div', undefined, 'actions');
      add(dActions,
        button('💾 Save to Product Catalog', async () => {
          await api('/studio/products', 'POST', {
            name: fName.value.trim(),
            name_ru: fNameRu.value.trim(),
            category: fCat.value.trim(),
            price_range: fPrice.value.trim(),
            models: fModels.value.split(',').map(x => x.trim()).filter(Boolean),
            specs: fSpecs.value.trim(),
            certifications: fCerts.value.trim(),
            target_use_case: fUse.value.trim(),
            pdf_kz: d.pdf_kz || '',
            pdf_ru: d.pdf_ru || '',
            source: 'ai_prompt',
          });
          lastProductDraft = null;
          notice('Product saved to catalog!');
          if (onRefreshState) await onRefreshState(false);
          renderProductCatalog(root, h);
        }, 'primary-btn'),
        button('Discard Draft', async () => {
          lastProductDraft = null;
          renderDraft();
        }, 'secondary-btn')
      );
      draftCard.append(dActions);
      draftContainer.append(draftCard);
    }
    renderDraft();
    root.append(promptBox);

    // Toolbar: View switcher, Manual Add, CRM Sync, Audience Action
    const bar = el('div', undefined, 'studio-toolbar');
    const leftTools = el('div', undefined, 'actions');
    const gridBtn = el('button', '▦ Grid View', 'primary-btn');
    gridBtn.type = 'button';
    const listBtn = el('button', '☰ List View', 'secondary-btn');
    listBtn.type = 'button';

    gridBtn.onclick = () => {
      viewMode = 'grid';
      gridBtn.className = 'primary-btn';
      listBtn.className = 'secondary-btn';
      renderItems();
    };
    listBtn.onclick = () => {
      viewMode = 'list';
      gridBtn.className = 'secondary-btn';
      listBtn.className = 'primary-btn';
      renderItems();
    };

    const addBtn = button('+ Add Manual Product', async () => {
      openProductModal(null, h, () => renderProductCatalog(root, h));
    }, 'secondary-btn');

    const syncB24 = button('🔄 Sync Bitrix24', async () => {
      const r = await api('/studio/products/sync-crm', 'POST', { source: 'bitrix24', limit: 20 });
      notice(`Imported ${r.imported_count} products from Bitrix24`);
      if (onRefreshState) await onRefreshState(false);
      renderProductCatalog(root, h);
    }, 'secondary-btn');

    const syncMS = button('🔄 Sync MoySklad', async () => {
      const r = await api('/studio/products/sync-crm', 'POST', { source: 'moysklad', limit: 20 });
      notice(`Imported ${r.imported_count} products from MoySklad`);
      if (onRefreshState) await onRefreshState(false);
      renderProductCatalog(root, h);
    }, 'secondary-btn');

    add(leftTools, gridBtn, listBtn, addBtn, syncB24, syncMS);

    const rightTools = el('div', undefined, 'actions');
    add(bar, leftTools, rightTools);
    root.append(bar);

    // Dedicated Dynamic Container for Items
    const itemsContainer = el('div', undefined, 'studio-items-container');
    root.append(itemsContainer);

    function updateActionToolbar() {
      rightTools.replaceChildren();
      const selectedArr = studioProducts.filter(p => selectedProductIds.has(p.id));
      if (selectedArr.length) {
        rightTools.append(button(`🎯 Find Target Audiences (${selectedArr.length})`, async () => {
          if (onSelectForAudience) onSelectForAudience(selectedArr[0]);
        }, 'call-btn'));
      }
    }

    function renderItems() {
      itemsContainer.replaceChildren();
      updateActionToolbar();

      if (viewMode === 'grid') {
        const grid = el('div', undefined, 'grid');
        for (const item of studioProducts) {
          const c = el('article', undefined, 'card studio-product-card' + (selectedProductIds.has(item.id) ? ' is-checked' : ''));
          const head = el('div', undefined, 'studio-card-head');
          const chk = el('input');
          chk.type = 'checkbox';
          chk.checked = selectedProductIds.has(item.id);
          chk.onchange = () => {
            if (chk.checked) selectedProductIds.add(item.id);
            else selectedProductIds.delete(item.id);
            c.classList.toggle('is-checked', chk.checked);
            updateActionToolbar();
          };
          const titleWrap = el('div');
          titleWrap.append(el('h3', item.name));
          if (item.name_ru && item.name_ru !== item.name) titleWrap.append(el('div', item.name_ru, 'muted'));
          add(head, chk, titleWrap, badge(item.source || 'manual', 'cyan-badge'));
          c.append(head);

          if (item.category) c.append(el('div', 'Category: ' + item.category, 'product-meta'));
          if (item.price_range) c.append(el('div', item.price_range, 'product-price'));
          if (item.specs) c.append(el('p', item.specs, 'product-meta'));
          if (item.certifications) c.append(el('div', '🏅 Certs: ' + item.certifications, 'muted'));
          if (item.models?.length) c.append(el('div', 'Models: ' + item.models.join(' · '), 'muted'));

          const acts = el('div', undefined, 'actions');
          add(acts,
            button('Edit', async () => openProductModal(item, h, () => renderProductCatalog(root, h)), 'secondary-btn'),
            button('📄 Offer', async () => {
              const prev = await api(`/studio/products/${item.id}/offer-preview?lang=ru`);
              const win = window.open('', '_blank', 'width=840,height=720');
              if (win) {
                win.document.write(prev.html);
                win.document.close();
              } else {
                notice('Popup blocked — allow popups to preview Commercial Offer');
              }
            }, 'secondary-btn'),
            button('🎯 Find Clients', async () => { if (onSelectForAudience) onSelectForAudience(item); }, 'primary-btn'),
            button('Delete', async () => {
              await api('/studio/products/' + item.id, 'DELETE');
              selectedProductIds.delete(item.id);
              notice('Product deleted');
              if (onRefreshState) await onRefreshState(false);
              renderProductCatalog(root, h);
            }, 'secondary-btn')
          );
          c.append(acts);
          grid.append(c);
        }
        itemsContainer.append(grid);
      } else {
        const wrap = el('div', undefined, 'card');
        const tbl = el('table');
        const thead = el('thead');
        const hr = el('tr');
        for (const col of ['', 'Product (KZ / RU)', 'Category', 'Price Range', 'Source', 'Actions']) hr.append(el('th', col));
        thead.append(hr);
        tbl.append(thead);
        const tbody = el('tbody');
        for (const item of studioProducts) {
          const tr = el('tr');
          const tdChk = el('td');
          const chk = el('input');
          chk.type = 'checkbox';
          chk.checked = selectedProductIds.has(item.id);
          chk.onchange = () => {
            if (chk.checked) selectedProductIds.add(item.id);
            else selectedProductIds.delete(item.id);
            updateActionToolbar();
          };
          tdChk.append(chk);

          const tdName = el('td');
          tdName.append(el('strong', item.name));
          if (item.name_ru) tdName.append(el('div', item.name_ru, 'muted'));

          const tdActs = el('td');
          const acts = el('div', undefined, 'actions');
          add(acts,
            button('Edit', async () => openProductModal(item, h, () => renderProductCatalog(root, h)), 'secondary-btn'),
            button('🎯 Clients', async () => { if (onSelectForAudience) onSelectForAudience(item); }, 'primary-btn')
          );
          tdActs.append(acts);

          add(tr, tdChk, tdName, el('td', item.category || '—'), el('td', item.price_range || '—'), el('td', item.source || 'manual'), tdActs);
          tbody.append(tr);
        }
        tbl.append(tbody);
        wrap.append(tbl);
        itemsContainer.append(wrap);
      }
    }

    renderItems();
  }

  function openProductModal(existing, h, onDone) {
    const { api, edit, field, el, onRefreshState } = h;
    edit(existing ? 'Edit Product Card' : 'Add Product Manually', f => {
      field(f, 'name', 'Product Name (KZ)', existing?.name || '');
      field(f, 'name_ru', 'Product Name (RU)', existing?.name_ru || '');
      field(f, 'category', 'Category', existing?.category || '');
      field(f, 'price_range', 'Price Range (KZT)', existing?.price_range || '');
      field(f, 'models', 'Models (comma-separated)', (existing?.models || []).join(', '));
      field(f, 'specs', 'Specifications & Certifications', existing?.specs || '', 'textarea');
      field(f, 'target_use_case', 'Target Use Case', existing?.target_use_case || '');
      field(f, 'pdf_kz', 'Commercial Offer PDF URL (KZ)', existing?.pdf_kz || '');
      field(f, 'pdf_ru', 'Commercial Offer PDF URL (RU)', existing?.pdf_ru || '');
      const cLbl = el('label', 'Advertising Company Entity');
      const cSel = el('select'); cSel.name = 'company_id';
      const optDef = el('option', 'Use Active Default Company Entity'); optDef.value = '0'; cSel.append(optDef);
      for (const c of companyEntities) {
        const o = el('option', `${c.company_name}${c.is_default ? ' (Default)' : ''}`);
        o.value = String(c.id);
        if (String(existing?.company_id || 0) === String(c.id)) o.selected = true;
        cSel.append(o);
      }
      cLbl.append(cSel);
      f.append(cLbl);
    }, async v => {
      const payload = {
        name: v.name.trim(),
        name_ru: v.name_ru.trim() || v.name.trim(),
        category: v.category.trim(),
        price_range: v.price_range.trim(),
        models: v.models.split(',').map(x => x.trim()).filter(Boolean),
        specs: v.specs.trim(),
        target_use_case: v.target_use_case.trim(),
        pdf_kz: v.pdf_kz.trim(),
        pdf_ru: v.pdf_ru.trim(),
        company_id: parseInt(v.company_id || '0', 10) || 0,
        source: existing?.source || 'manual',
      };
      if (existing) await api('/studio/products/' + existing.id, 'PUT', payload);
      else await api('/studio/products', 'POST', payload);
      if (onRefreshState) await onRefreshState(false);
      onDone();
    });
  }

  /* =========================================================================
     2. PROMPT-TO-AUDIENCE AI CLIENT ANALYZER
     ========================================================================= */
  async function renderAudienceAnalyzer(root, h, preselectedProduct = null) {
    const { api, el, add, button, badge, notice, paragraph, onOpenAdsManager } = h;
    
    // Strict clean mount
    root.replaceChildren();

    paragraph(root, 'Choose a product or describe your business category. AI will analyze what types of clients buy it in Kazakhstan and let you push selected ranges directly into Ads Manager.', 'lead');

    try {
      const [pRes, cRes] = await Promise.all([api('/studio/products'), api('/ads/campaigns')]);
      studioProducts = pRes.items || [];
      campaignsCache = cRes.items || [];
    } catch (e) {
      root.append(el('p', 'Failed to load data: ' + e.message, 'muted'));
      return;
    }

    const box = el('div', undefined, 'card studio-highlight');
    box.append(el('h3', '🧠 AI Target Client & Category Analyzer'));

    const pSelect = el('select');
    const noneOpt = el('option', '-- Describe category manually (no product selected) --');
    noneOpt.value = '';
    pSelect.append(noneOpt);
    for (const p of studioProducts) {
      const o = el('option', `${p.name_ru || p.name} (${p.category || p.slug})`);
      o.value = p.slug;
      if (preselectedProduct && (preselectedProduct.slug === p.slug || preselectedProduct.id === p.id)) o.selected = true;
      pSelect.append(o);
    }

    const promptArea = el('textarea');
    promptArea.placeholder = 'Optional instructions for AI: e.g. Focus on private diagnostic labs, state hospitals, and regional medical distributors in Almaty and Astana...';

    const lbl1 = el('label', 'Select Product from Catalog'); lbl1.append(pSelect);
    const lbl2 = el('label', 'Describe Target Category or Buyer Constraints (Prompt)'); lbl2.append(promptArea);

    const resultsContainer = el('div', undefined, 'studio-analysis-results');

    const analyzeBtn = button('🧠 Analyze Target Audiences with AI', async () => {
      notice('AI is analyzing B2B/B2G/B2C client segments...');
      const res = await api('/studio/audiences/analyze-prompt', 'POST', {
        product_key: pSelect.value,
        prompt: promptArea.value.trim(),
      });
      lastAudienceAnalysis = res;
      selectedProposedIdx = new Set((res.segments || []).map((_, idx) => idx));
      notice(`Found ${(res.segments || []).length} target client ranges (${res.model_used || 'AI'})`);
      renderResults();
    }, 'primary-btn');

    add(box, lbl1, lbl2, el('div', undefined, 'actions'));
    box.lastChild.append(analyzeBtn);
    root.append(box, resultsContainer);

    function renderResults() {
      resultsContainer.replaceChildren();
      if (!lastAudienceAnalysis || !lastAudienceAnalysis.segments?.length) return;

      const resCard = el('div', undefined, 'card');
      resCard.append(el('h3', 'Proposed Customer Ranges (Select to Apply)'));
      if (lastAudienceAnalysis.summary) paragraph(resCard, lastAudienceAnalysis.summary, 'muted');

      const segGrid = el('div', undefined, 'grid');
      lastAudienceAnalysis.segments.forEach((seg, idx) => {
        const sc = el('article', undefined, 'card studio-product-card' + (selectedProposedIdx.has(idx) ? ' is-checked' : ''));
        const head = el('div', undefined, 'studio-card-head');
        const chk = el('input');
        chk.type = 'checkbox';
        chk.checked = selectedProposedIdx.has(idx);
        chk.onchange = () => {
          if (chk.checked) selectedProposedIdx.add(idx);
          else selectedProposedIdx.delete(idx);
          sc.classList.toggle('is-checked', chk.checked);
        };
        add(head, chk, el('h4', seg.name || 'Segment'), badge((seg.segment || 'b2b').toUpperCase(), 'cyan-badge'));
        sc.append(head);
        if (seg.description) sc.append(el('p', seg.description, 'product-meta'));
        if (seg.estimated_volume) sc.append(el('div', '📊 Est. Market: ' + seg.estimated_volume, 'product-price'));
        if (typeof seg.matched_seed_count === 'number' && seg.matched_seed_count > 0) {
          sc.append(el('div', `⚡ ${seg.matched_seed_count} verified seeds matched in DB`, 'badge live-badge'));
        }
        if (seg.decision_makers?.length) sc.append(el('div', '👤 Roles: ' + seg.decision_makers.join(', '), 'product-meta'));
        if (seg.buying_signals?.length) sc.append(el('div', '⚡ Signals: ' + seg.buying_signals.join('; '), 'product-meta'));
        if (seg.keywords?.length) sc.append(el('div', '🔍 Keywords: ' + seg.keywords.join(', '), 'muted'));
        segGrid.append(sc);
      });
      resCard.append(segGrid);

      const applyRow = el('div', undefined, 'studio-inline-row');
      const campSel = el('select');
      for (const c of campaignsCache) {
        const o = el('option', `Campaign #${c.id}: ${c.name} (${c.lang.toUpperCase()})`);
        o.value = c.id;
        campSel.append(o);
      }
      const applyBtn = button('🚀 Create Selected Audiences in Ads Manager', async () => {
        const chosen = lastAudienceAnalysis.segments.filter((_, i) => selectedProposedIdx.has(i));
        if (!chosen.length) throw Error('Select at least one customer range');
        let cid = parseInt(campSel.value, 10);
        if (!cid) {
          const newCamp = await api('/ads/campaigns', 'POST', {
            name: 'AI Campaign: ' + (pSelect.value || 'Custom'),
            product_key: pSelect.value || 'centrifuges',
            objective: 'call_then_offer',
            lang: 'kz',
          });
          cid = newCamp.id;
        }
        const applied = await api('/studio/audiences/apply', 'POST', { campaign_id: cid, segments: chosen });
        notice(`Created ${applied.created_count} audiences in Ads Manager! Opening Ads Manager...`);
        if (onOpenAdsManager) onOpenAdsManager();
      }, 'call-btn');

      add(applyRow, campSel, applyBtn);
      resCard.append(applyRow);
      resultsContainer.append(resCard);
    }

    renderResults();

    // Card 2: Live FTS5 BM25 Market Seed Explorer
    const ftsCard = el('div', undefined, 'card');
    ftsCard.append(el('h3', '⚡ Live FTS5 Market Seed Explorer (BM25 Sub-50ms)'));
    paragraph(ftsCard, 'Search indexed Kazakhstan B2B/B2G procurement registries & company seeds by keyword, activity, or region.', 'muted');

    const ftsBar = el('div', undefined, 'studio-inline-row');
    const qInput = el('input');
    qInput.type = 'text';
    qInput.placeholder = 'Search companies, BIN, medical, lab, equipment...';
    const regSel = el('select');
    for (const [val, txt] of [['', 'All Regions'], ['almaty', 'Almaty'], ['astana', 'Astana'], ['shymkent', 'Shymkent']]) {
      const o = el('option', txt); o.value = val; regSel.append(o);
    }
    const ftsResults = el('div');
    const runFtsSearch = async () => {
      const url = `/studio/audiences/search-seeds?q=${encodeURIComponent(qInput.value.trim())}&region=${encodeURIComponent(regSel.value)}&limit=25`;
      const res = await api(url);
      ftsResults.replaceChildren();
      ftsResults.append(el('p', `⚡ Query took ${res.query_ms} ms · Showing ${res.count} of ${res.total_indexed} indexed seeds`, 'muted'));
      if (res.items?.length) {
        const tbl = el('table');
        const thead = el('thead');
        const hr = el('tr');
        for (const col of ['Company', 'BIN', 'City', 'Category / Activity', 'Phone', 'Source']) hr.append(el('th', col));
        thead.append(hr);
        tbl.append(thead);
        const tbody = el('tbody');
        for (const r of res.items) {
          const tr = el('tr');
          add(tr,
            el('td', r.company),
            el('td', r.bin || '—'),
            el('td', r.city || r.region || '—'),
            el('td', (r.category ? r.category + ' · ' : '') + (r.activity || '—')),
            el('td', r.phone || '—'),
            el('td', r.source || 'seed')
          );
          tbody.append(tr);
        }
        tbl.append(tbody);
        ftsResults.append(tbl);
      }
    };
    const searchBtn = button('🔍 Search Seeds', runFtsSearch, 'primary-btn');
    add(ftsBar, qInput, regSel, searchBtn);
    ftsCard.append(ftsBar, ftsResults);
    root.append(ftsCard);
    runFtsSearch().catch(() => {});
  }

  /* =========================================================================
     3A. AI MODEL & TOKEN CONNECTORS (DEDICATED TAB)
     ========================================================================= */
  async function renderAIConnectors(root, h) {
    const { api, el, add, button, badge, notice, paragraph } = h;
    root.replaceChildren();

    paragraph(root, 'Configure API access tokens for proprietary LLMs (Gemini, OpenAI, DeepSeek, Anthropic) or self-hosted local models (Ollama, vLLM, LM Studio) with automatic rate-limit fallback and live cost tracking.', 'lead');

    let envInfo = {};
    let usageInfo = {};
    try {
      const [mRes, uRes] = await Promise.all([
        api('/studio/connectors/models'),
        api('/studio/ai-usage').catch(() => ({ total_calls: 0, total_tokens: 0, estimated_cost_usd: 0, avg_latency_ms: 0 })),
      ]);
      aiProviders = mRes.items || [];
      envInfo = mRes;
      usageInfo = uRes || {};
    } catch (e) {
      root.append(el('p', 'Failed to load AI connectors: ' + e.message, 'muted'));
      return;
    }

    const metric = (lbl, val, sub) => {
      const b = el('div', undefined, 'studio-stat-box');
      b.append(el('span', lbl, 'muted'));
      b.append(el('strong', val, 'stat-number'));
      if (sub) b.append(el('small', sub, 'muted'));
      return b;
    };

    // Telemetry & Spend Banner
    const teleCard = el('div', undefined, 'card studio-highlight');
    teleCard.append(el('h3', '📊 AI Router Telemetry & Token Spend'));
    const tGrid = el('div', undefined, 'studio-summary-bar');
    add(tGrid,
      metric('Total AI Invocations', String(usageInfo.total_calls || 0)),
      metric('Total Tokens Processed', (usageInfo.total_tokens || 0).toLocaleString(), `${(usageInfo.prompt_tokens || 0).toLocaleString()} in / ${(usageInfo.completion_tokens || 0).toLocaleString()} out`),
      metric('Estimated Total Spend', `$${(usageInfo.estimated_cost_usd || 0).toFixed(4)}`, `≈ ${(usageInfo.estimated_cost_kzt || 0).toFixed(2)} ₸`),
      metric('Average Latency', `${usageInfo.avg_latency_ms || 0} ms`, 'across active providers')
    );
    teleCard.append(tGrid);
    root.append(teleCard);

    // AI Model & Token Connectors
    const aiCard = el('div', undefined, 'card');
    const aiHead = el('div', undefined, 'studio-toolbar');
    const titleDiv = el('div');
    titleDiv.append(el('h3', '🤖 AI Agent Model & Token Connectors'));
    if (envInfo.env_gemini_configured) {
      titleDiv.append(badge(`System Fallback Active: ${envInfo.env_gemini_model || 'gemini-flash-latest'}`, 'live-badge'));
    }
    const addModelBtn = button('+ Add AI Model / Local Endpoint', async () => {
      openProviderModal(null, h, () => renderAIConnectors(root, h));
    }, 'primary-btn');
    add(aiHead, titleDiv, addModelBtn);
    aiCard.append(aiHead);

    if (!aiProviders.length) {
      paragraph(aiCard, 'No custom AI tokens added yet. Add a Gemini, OpenAI, DeepSeek, Anthropic, or local Ollama/vLLM model below.', 'muted');
    } else {
      const tbl = el('table');
      const thead = el('thead');
      const hr = el('tr');
      for (const col of ['Name', 'Provider', 'Model', 'Base URL / Token', 'Purpose', 'Status', 'Actions']) hr.append(el('th', col));
      thead.append(hr);
      tbl.append(thead);
      const tbody = el('tbody');
      for (const p of aiProviders) {
        const tr = el('tr');
        const tdActs = el('td');
        const acts = el('div', undefined, 'actions');
        const pingStatus = el('span', '', 'muted');
        add(acts,
          button('⚡ Ping', async () => {
            pingStatus.textContent = '⏳ Pinging...';
            try {
              const r = await api(`/studio/connectors/models/${p.id}/ping`, 'POST');
              pingStatus.textContent = `🟢 ${r.latency_ms}ms · ${r.prompt_tokens + r.completion_tokens} tok · $${r.cost_usd.toFixed(5)}`;
              notice(`Ping OK (${r.latency_ms}ms): ${r.reply}`);
            } catch (err) {
              pingStatus.textContent = '🔴 ' + err.message;
              notice('Ping failed: ' + err.message);
            }
          }, 'primary-btn'),
          button('Edit', async () => openProviderModal(p, h, () => renderAIConnectors(root, h)), 'secondary-btn'),
          button('Delete', async () => {
            await api(`/studio/connectors/models/${p.id}`, 'DELETE');
            notice('AI connector removed');
            renderAIConnectors(root, h);
          }, 'secondary-btn')
        );
        tdActs.append(acts, pingStatus);
        add(tr,
          el('td', p.name),
          el('td', p.provider.toUpperCase()),
          el('td', p.model_name),
          el('td', (p.base_url ? p.base_url + ' · ' : '') + (p.api_key_masked || 'no key')),
          el('td', p.purpose),
          el('td', p.is_active ? '🟢 Active' : '⚪ Disabled'),
          tdActs
        );
        tbody.append(tr);
      }
      tbl.append(tbody);
      aiCard.append(tbl);
    }
    root.append(aiCard);
  }

  /* =========================================================================
     3B. CRM, ERP & CMS SYNC TAB (DEDICATED TAB)
     ========================================================================= */
  async function renderCrmSync(root, h) {
    const { api, el, add, button, notice, paragraph } = h;
    root.replaceChildren();

    paragraph(root, 'Connect Bitrix24, MoySklad, 1C, and E-Commerce CMS feeds for one-click product catalog sync and autonomous hot-lead Deal/Contact creation.', 'lead');

    let funnelInfo = {};
    try {
      const [sRes, fRes] = await Promise.all([
        api('/studio/settings'),
        api('/studio/analytics').catch(() => ({ products: 0, seeds_indexed: 0, leads: 0, calls_today: 0, calls_qualified: 0, deals_created_in_crm: 0 })),
      ]);
      studioSettings = sRes;
      funnelInfo = fRes || {};
    } catch (e) {
      root.append(el('p', 'Failed to load CRM connectors: ' + e.message, 'muted'));
      return;
    }

    const metric = (lbl, val, sub) => {
      const b = el('div', undefined, 'studio-stat-box');
      b.append(el('span', lbl, 'muted'));
      b.append(el('strong', val, 'stat-number'));
      if (sub) b.append(el('small', sub, 'muted'));
      return b;
    };

    // Pipeline Funnel & Bitrix24 CRM Analytics
    const funnelCard = el('div', undefined, 'card');
    funnelCard.append(el('h3', '📈 End-to-End Autonomous Funnel & CRM Handoff'));
    const fGrid = el('div', undefined, 'studio-summary-bar');
    add(fGrid,
      metric('Catalog Products', String(funnelInfo.products || 0), `${(funnelInfo.seeds_indexed || 0).toLocaleString()} indexed market seeds`),
      metric('Discovered Leads', String(funnelInfo.leads || 0), `${funnelInfo.audiences || 0} active audiences`),
      metric('AI Calls Today', String(funnelInfo.calls_today || 0), `${funnelInfo.calls_total || 0} total · ${funnelInfo.offers_sent || 0} WhatsApp offers`),
      metric('CRM Deals Created', String(funnelInfo.deals_created_in_crm || 0), `${funnelInfo.calls_qualified || 0} qualified (${funnelInfo.qualification_rate_pct || 0}% rate)`)
    );
    funnelCard.append(fGrid);
    root.append(funnelCard);

    // CRM / ERP / CMS Connectors
    const crm = studioSettings?.connectors || {};
    const crmCard = el('div', undefined, 'card studio-highlight');
    crmCard.append(el('h3', '🔄 CRM, ERP & CMS Product Platform Connectors'));
    const fB24 = el('input'); fB24.value = crm.bitrix24_webhook || ''; fB24.placeholder = 'https://yourportal.bitrix24.kz/rest/1/xxxxx/';
    const fMS = el('input'); fMS.value = crm.moysklad_token || ''; fMS.placeholder = 'MoySklad Bearer API Token';
    const f1C = el('input'); f1C.value = crm.onec_odata_url || ''; f1C.placeholder = 'https://1c.company.kz/odata/standard.odata/';
    const fCMS = el('input'); fCMS.value = crm.cms_feed_url || ''; fCMS.placeholder = 'https://shop.kz/wp-json/wc/v3/products or YML feed';

    const wrap = (lbl, node) => { const l = el('label', lbl); l.append(node); return l; };
    const grid = el('div', undefined, 'studio-form-grid');
    add(grid,
      wrap('Bitrix24 Inbound Webhook URL', fB24),
      wrap('MoySklad (МойСклад) API Token', fMS),
      wrap('1C:Enterprise OData Endpoint', f1C),
      wrap('CMS / E-Commerce Product Feed URL', fCMS)
    );
    crmCard.append(grid);
    const crmActs = el('div', undefined, 'actions');
    add(crmActs,
      button('💾 Save CRM Connectors', async () => {
        await api('/studio/settings/connectors', 'PUT', {
          bitrix24_webhook: fB24.value.trim(),
          moysklad_token: fMS.value.trim(),
          onec_odata_url: f1C.value.trim(),
          cms_feed_url: fCMS.value.trim(),
        });
        notice('CRM / ERP connectors saved');
        renderCrmSync(root, h);
      }, 'primary-btn'),
      button('🔄 Import from Bitrix24', async () => {
        const r = await api('/studio/products/sync-crm', 'POST', { source: 'bitrix24', limit: 20 });
        notice(`Imported ${r.imported_count} products from Bitrix24`);
      }, 'secondary-btn'),
      button('🔄 Import from MoySklad', async () => {
        const r = await api('/studio/products/sync-crm', 'POST', { source: 'moysklad', limit: 20 });
        notice(`Imported ${r.imported_count} products from MoySklad`);
      }, 'secondary-btn')
    );
    crmCard.append(crmActs);
    root.append(crmCard);
  }

  function openProviderModal(existing, h, onDone) {
    const { api, edit, field, el } = h;
    edit(existing ? 'Edit AI Model Connector' : 'Add AI Model / Local LLM Connector', f => {
      field(f, 'name', 'Display Name (e.g. Gemini Flash, Local Ollama Qwen)', existing?.name || '');
      const lbl = el('label', 'Provider Type');
      const sel = el('select');
      sel.name = 'provider';
      for (const [val, txt] of [
        ['gemini', 'Google Gemini (Cloud + Web Search Grounding)'],
        ['openai', 'OpenAI (GPT-4o / o3-mini)'],
        ['deepseek', 'DeepSeek API (DeepSeek-V3 / R1)'],
        ['anthropic', 'Anthropic Claude'],
        ['local_openai', 'Local / Self-Hosted (Ollama, vLLM, LM Studio — OpenAI API)'],
      ]) {
        const o = el('option', txt); o.value = val;
        if ((existing?.provider || 'gemini') === val) o.selected = true;
        sel.append(o);
      }
      lbl.append(sel);
      f.append(lbl);

      field(f, 'model_name', 'Model Name (e.g. gemini-2.5-flash, deepseek-chat, qwen2.5:14b)', existing?.model_name || 'gemini-2.5-flash');
      field(f, 'api_key', 'API Token / Key (leave blank for local Ollama)', existing?.api_key_masked || '');
      field(f, 'base_url', 'Custom Base URL (required for Local Ollama/vLLM, e.g. http://192.168.1.50:11434/v1)', existing?.base_url || '');

      const pLbl = el('label', 'Assigned Purpose');
      const pSel = el('select');
      pSel.name = 'purpose';
      for (const [val, txt] of [
        ['all', 'All Operations / Universal Fallback'],
        ['research_tier', 'Research Tier (Gemini Search Grounding / Web Scraping)'],
        ['reasoning_tier', 'Reasoning Tier (DeepSeek / Gemini Pro / Audience ICP)'],
        ['test_tier', 'Test Tier (Flash-Lite / Ollama / Low Token Cost)'],
        ['voice_scripting', 'Voice Call Script Generation'],
      ]) {
        const o = el('option', txt); o.value = val;
        if ((existing?.purpose || 'all') === val) o.selected = true;
        pSel.append(o);
      }
      pLbl.append(pSel);
      f.append(pLbl);
      field(f, 'is_active', 'Enable this model connector', existing ? existing.is_active : true, 'checkbox');
    }, async v => {
      const payload = {
        name: v.name.trim(),
        provider: v.provider,
        model_name: v.model_name.trim(),
        api_key: v.api_key.trim(),
        base_url: v.base_url.trim(),
        purpose: v.purpose,
        is_active: Boolean(v.is_active),
      };
      if (existing) await api('/studio/connectors/models/' + existing.id, 'PUT', payload);
      else await api('/studio/connectors/models', 'POST', payload);
      onDone();
    });
  }

  /* =========================================================================
     4A. MULTI-COMPANY ENTITIES (ADVERTISE FROM CHOSEN ENTITY)
     ========================================================================= */
  async function renderCompanyEntities(root, h) {
    const { api, el, add, button, badge, notice, paragraph, onRefreshState } = h;
    root.replaceChildren();

    paragraph(root, 'Save multiple legal entities (ТОО, ИП, branches) and choose which company entity is currently active for advertising products, AI voice introductions, and commercial offers.', 'lead');

    let entities = [];
    try {
      const res = await api('/studio/companies');
      entities = res.items || [];
    } catch (e) {
      root.append(el('p', 'Failed to load company entities: ' + e.message, 'muted'));
      return;
    }

    const headBar = el('div', undefined, 'studio-toolbar');
    headBar.append(el('h3', `🏢 Saved Legal Entities (${entities.length})`));
    headBar.append(button('+ Add New Company Entity', async () => {
      openCompanyModal(null, h, async () => {
        if (onRefreshState) await onRefreshState(false);
        renderCompanyEntities(root, h);
      });
    }, 'primary-btn'));
    root.append(headBar);

    const grid = el('div', undefined, 'grid');
    for (const c of entities) {
      const card = el('article', undefined, 'card studio-product-card' + (c.is_default ? ' is-checked' : ''));
      const head = el('div', undefined, 'studio-card-head');
      head.append(el('h3', c.company_name));
      if (c.is_default) head.append(badge('✓ Active Default', 'live-badge'));
      card.append(head);

      if (c.bin) card.append(el('div', `BIN: ${c.bin} · ${c.city || ''}`, 'product-meta'));
      if (c.manager_name || c.manager_phone) card.append(el('div', `Manager: ${c.manager_name || '—'} (${c.manager_phone || '—'})`, 'product-meta'));
      if (c.description) card.append(el('p', c.description, 'muted'));

      const acts = el('div', undefined, 'actions');
      if (!c.is_default) {
        acts.append(button('✓ Set Active Entity', async () => {
          await api(`/studio/companies/${c.id}/activate`, 'POST');
          notice(`Active advertising entity switched to ${c.company_name}`);
          if (onRefreshState) await onRefreshState(false);
          renderCompanyEntities(root, h);
        }, 'primary-btn'));
      }
      add(acts,
        button('✏️ Edit Company', async () => openCompanyModal(c, h, async () => {
          if (onRefreshState) await onRefreshState(false);
          renderCompanyEntities(root, h);
        }), 'secondary-btn'),
        button('Delete', async () => {
          await api(`/studio/companies/${c.id}`, 'DELETE');
          notice('Company entity removed');
          if (onRefreshState) await onRefreshState(false);
          renderCompanyEntities(root, h);
        }, 'secondary-btn')
      );
      card.append(acts);
      grid.append(card);
    }
    root.append(grid);
  }

  function openCompanyModal(existing, h, onDone) {
    const { api, edit, field } = h;
    edit(existing ? 'Edit Company Entity' : 'Add Legal Company Entity', f => {
      field(f, 'company_name', 'Company Name (e.g. ТОО «Аврора-2014»)', existing?.company_name || '');
      field(f, 'bin', 'BIN / IIN', existing?.bin || '');
      field(f, 'city', 'City', existing?.city || 'Алматы');
      field(f, 'website', 'Website', existing?.website || '');
      field(f, 'manager_name', 'Manager Name (for Call Transfer)', existing?.manager_name || '');
      field(f, 'manager_phone', 'Manager Phone', existing?.manager_phone || '');
      field(f, 'description', 'Value Proposition / Description', existing?.description || '', 'textarea');
      field(f, 'default_pdf_kz', 'Default Offer PDF URL (KZ)', existing?.default_pdf_kz || '');
      field(f, 'default_pdf_ru', 'Default Offer PDF URL (RU)', existing?.default_pdf_ru || '');
      field(f, 'is_default', 'Set as Active Default Advertising Entity', existing ? existing.is_default : false, 'checkbox');
    }, async v => {
      const payload = {
        company_name: v.company_name.trim(),
        bin: v.bin.trim(),
        city: v.city.trim(),
        website: v.website.trim(),
        manager_name: v.manager_name.trim(),
        manager_phone: v.manager_phone.trim(),
        description: v.description.trim(),
        default_pdf_kz: v.default_pdf_kz.trim(),
        default_pdf_ru: v.default_pdf_ru.trim(),
        is_default: Boolean(v.is_default),
      };
      if (existing) await api('/studio/companies/' + existing.id, 'PUT', payload);
      else await api('/studio/companies', 'POST', payload);
      onDone();
    });
  }

  /* =========================================================================
     4B. DOCUMENT TEMPLATES LIBRARY & UPLOADER
     ========================================================================= */
  async function renderDocumentTemplates(root, h) {
    const { api, el, add, button, badge, notice, paragraph } = h;
    root.replaceChildren();

    paragraph(root, 'Upload multiple variations of Commercial Offers, Inquiries, Contracts, or Presentations (.docx, .pdf, .html, .txt, .md) and choose which variation is currently active for outreach.', 'lead');

    let templates = [];
    let entities = [];
    try {
      const [tRes, cRes] = await Promise.all([api('/studio/templates'), api('/studio/companies')]);
      templates = tRes.items || [];
      entities = cRes.items || [];
    } catch (e) {
      root.append(el('p', 'Failed to load templates: ' + e.message, 'muted'));
      return;
    }

    // Upload Form Card
    const upCard = el('div', undefined, 'card studio-highlight');
    upCard.append(el('h3', '📤 Upload New Document Template Variation'));
    const wrap = (lbl, node) => { const l = el('label', lbl); l.append(node); return l; };

    const tName = el('input'); tName.placeholder = 'e.g. Medical Offer Q2 (Formal) or Standard Contract v3';
    const tType = el('select');
    for (const [val, txt] of [
      ['offer', 'Commercial Offer (КП)'],
      ['inquiry', 'Inquiry / Request (Запрос)'],
      ['contract', 'Contract (Договор)'],
      ['presentation', 'Presentation / Deck'],
      ['other', 'Other Document'],
    ]) { const o = el('option', txt); o.value = val; tType.append(o); }

    const tLang = el('select');
    for (const [val, txt] of [['ru', 'Russian (RU)'], ['kz', 'Kazakh (KZ)']]) {
      const o = el('option', txt); o.value = val; tLang.append(o);
    }

    const tComp = el('select');
    const optAll = el('option', 'All / Active Default Company'); optAll.value = '0'; tComp.append(optAll);
    for (const c of entities) { const o = el('option', c.company_name); o.value = String(c.id); tComp.append(o); }

    const tFile = el('input'); tFile.type = 'file'; tFile.accept = '.docx,.pdf,.html,.htm,.txt,.md';
    const tContent = el('textarea'); tContent.placeholder = 'Optional HTML / Markdown / Text body (supports {{company_name}}, {{product_name}}, {{price}}, {{specs}})';
    const tActive = el('input'); tActive.type = 'checkbox'; tActive.checked = true;

    const uGrid = el('div', undefined, 'studio-form-grid');
    add(uGrid,
      wrap('Template Name', tName),
      wrap('Document Category', tType),
      wrap('Language', tLang),
      wrap('Linked Company Entity', tComp),
      wrap('Upload File (.docx, .pdf, .html, .txt, .md)', tFile)
    );
    upCard.append(uGrid, wrap('Text / HTML Template Body (Optional)', tContent));
    const chkLbl = el('label', ' Set as currently active template for this category & language');
    chkLbl.prepend(tActive);
    upCard.append(chkLbl);

    upCard.append(button('📤 Upload & Save Template', async () => {
      if (tName.value.trim().length < 2) throw Error('Enter a template name');
      const fd = new FormData();
      fd.append('name', tName.value.trim());
      fd.append('doc_type', tType.value);
      fd.append('lang', tLang.value);
      fd.append('company_id', tComp.value);
      fd.append('is_active', tActive.checked ? 'true' : 'false');
      fd.append('content_text', tContent.value.trim());
      if (tFile.files && tFile.files[0]) {
        fd.append('file', tFile.files[0]);
      }
      const resp = await fetch('/platform/api/studio/templates/upload', {
        method: 'POST',
        headers: { 'X-Control-Request': '1' },
        credentials: 'same-origin',
        body: fd,
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        throw Error(err.detail || 'Upload failed');
      }
      notice('Document template uploaded!');
      renderDocumentTemplates(root, h);
    }, 'primary-btn'));
    root.append(upCard);

    // Templates Table Card
    const listCard = el('div', undefined, 'card');
    listCard.append(el('h3', `📄 Saved Template Variations (${templates.length})`));
    if (!templates.length) {
      paragraph(listCard, 'No custom document templates uploaded yet.', 'muted');
    } else {
      const tbl = el('table');
      const thead = el('thead');
      const hr = el('tr');
      for (const col of ['Name', 'Category', 'Lang', 'File', 'Status', 'Actions']) hr.append(el('th', col));
      thead.append(hr);
      tbl.append(thead);
      const tbody = el('tbody');
      for (const t of templates) {
        const tr = el('tr');
        const tdActs = el('td');
        const acts = el('div', undefined, 'actions');
        if (!t.is_active) {
          acts.append(button('✓ Set Active', async () => {
            await api(`/studio/templates/${t.id}/activate`, 'POST');
            notice(`Template "${t.name}" is now active for ${t.doc_type.toUpperCase()} (${t.lang.toUpperCase()})`);
            renderDocumentTemplates(root, h);
          }, 'primary-btn'));
        }
        if (t.file_name) {
          const dl = el('a', '⬇ Download', 'secondary-btn');
          dl.href = `/platform/api/studio/templates/${t.id}/download`;
          dl.target = '_blank';
          acts.append(dl);
        }
        acts.append(button('Delete', async () => {
          await api(`/studio/templates/${t.id}`, 'DELETE');
          notice('Template deleted');
          renderDocumentTemplates(root, h);
        }, 'secondary-btn'));
        tdActs.append(acts);
        add(tr,
          el('td', t.name),
          el('td', t.doc_type.toUpperCase()),
          el('td', t.lang.toUpperCase()),
          el('td', t.file_name || (t.content_text ? 'Inline Text/HTML' : '—')),
          el('td', t.is_active ? '🟢 Currently Used' : '⚪ Standby'),
          tdActs
        );
        tbody.append(tr);
      }
      tbl.append(tbody);
      listCard.append(tbl);
    }
    root.append(listCard);
  }

  /* =========================================================================
     5. TELEPHONY ENGINE, VOICE AUDIO PREVIEW & CALL SCRIPT STUDIO
     ========================================================================= */
  async function renderTelephonyAndScript(root, h) {
    const { api, el, add, button, notice, paragraph, onRefreshState } = h;
    
    // Strict clean mount
    root.replaceChildren();

    paragraph(root, 'Configure Asterisk PBX concurrency & call forwarding queue, listen to Kazakh/Russian voice samples, and preview/edit the exact AI call script.', 'lead');

    try {
      const [sRes, pRes] = await Promise.all([api('/studio/settings'), api('/studio/products')]);
      studioSettings = sRes;
      studioProducts = pRes.items || [];
      if (!scriptProductKey && studioProducts.length) scriptProductKey = studioProducts[0].slug;
    } catch (e) {
      root.append(el('p', 'Failed to load settings: ' + e.message, 'muted'));
      return;
    }

    const tel = studioSettings.telephony || {};
    const voices = studioSettings.voices || [];
    const wrap = (lbl, node) => { const l = el('label', lbl); l.append(node); return l; };

    // Card 1: Calling Engine & Voice Audio Preview
    const telCard = el('div', undefined, 'card');
    telCard.append(el('h3', '📞 Calling Engine, Concurrency, Forwarding Queue & Voice Preview'));
    const tConc = el('input'); tConc.type = 'number'; tConc.min = 1; tConc.max = 20; tConc.value = tel.concurrent_calls || 1;
    const tCap = el('input'); tCap.type = 'number'; tCap.min = 1; tCap.max = 5000; tCap.value = tel.daily_call_cap || 200;
    const tHours = el('input'); tHours.value = tel.work_hours || '09:00-19:00 Asia/Almaty';
    const tExt = el('input'); tExt.value = tel.transfer_extension || '055';
    const tVoice = el('select');
    for (const v of voices) {
      const o = el('option', v.label); o.value = v.id;
      if ((tel.voice_name || 'Kore') === v.id) o.selected = true;
      tVoice.append(o);
    }

    const tGrid = el('div', undefined, 'studio-form-grid');
    add(tGrid,
      wrap('Concurrent Calling Channels (1–20)', tConc),
      wrap('Daily Call Safety Cap', tCap),
      wrap('Allowed Calling Window', tHours),
      wrap('Call Forwarding Queue / SIP Extension', tExt),
      wrap('AI Voice Character (Gemini Live)', tVoice)
    );
    telCard.append(tGrid);

    const vActs = el('div', undefined, 'actions');
    add(vActs,
      button('🔊 Listen Preview (Қазақша)', async () => {
        await playVoicePreview(api, notice, tVoice.value, 'kz', '');
      }, 'secondary-btn'),
      button('🔊 Listen Preview (Русский)', async () => {
        await playVoicePreview(api, notice, tVoice.value, 'ru', '');
      }, 'secondary-btn'),
      button('💾 Save Telephony Settings', async () => {
        await api('/studio/settings/telephony', 'PUT', {
          ...tel,
          concurrent_calls: parseInt(tConc.value, 10) || 1,
          daily_call_cap: parseInt(tCap.value, 10) || 200,
          work_hours: tHours.value.trim(),
          transfer_extension: tExt.value.trim(),
          voice_name: tVoice.value,
        });
        notice('Telephony & voice settings saved!');
        if (onRefreshState) await onRefreshState(false);
      }, 'primary-btn')
    );
    telCard.append(vActs);
    root.append(telCard);

    // Card 2: Call Script & Prompt Customizer (Zero Glitch Preview)
    const scCard = el('div', undefined, 'card studio-highlight');
    scCard.append(el('h3', '📝 Call Script & Prompt Studio (Exact AI Speech Preview)'));
    paragraph(scCard, 'Customize what the AI says during calls and preview the exact isolated prompt before calling. Hardcoded WhatsApp CRM prompts are completely removed.', 'muted');

    const pSel = el('select');
    for (const p of studioProducts) {
      const o = el('option', `${p.name_ru || p.name} (${p.price_range || p.slug})`);
      o.value = p.slug;
      if (scriptProductKey === p.slug) o.selected = true;
      pSel.append(o);
    }
    pSel.onchange = () => { scriptProductKey = pSel.value; };

    const lSel = el('select');
    add(lSel, el('option', '🇰🇿 Kazakh (KZ)'), el('option', '🇷🇺 Russian (RU)'));
    lSel.options[0].value = 'kz'; lSel.options[1].value = 'ru';
    lSel.value = scriptLang;
    lSel.onchange = () => { scriptLang = lSel.value; renderTelephonyAndScript(root, h); };

    const gInput = el('textarea');
    gInput.placeholder = 'Leave empty to use auto-generated greeting for the chosen company & product...';
    gInput.value = scriptLang === 'kz' ? (tel.custom_greeting_kz || '') : (tel.custom_greeting_ru || '');

    const prInput = el('textarea');
    prInput.placeholder = 'Optional custom sales rules, qualification questions, or objection responses...';
    prInput.value = scriptLang === 'kz' ? (tel.custom_prompt_kz || '') : (tel.custom_prompt_ru || '');

    const sGrid = el('div', undefined, 'studio-form-grid');
    add(sGrid, wrap('Product to Pitch', pSel), wrap('Call Language', lSel));
    scCard.append(sGrid, wrap('Custom Opening Greeting (Optional)', gInput), wrap('Custom Call Instructions / Prompt (Optional)', prInput));

    const scriptDraftContainer = el('div', undefined, 'studio-script-draft');

    const scActs = el('div', undefined, 'actions');
    add(scActs,
      button('👁️ Preview Exact AI Call Script', async () => {
        const res = await api('/studio/script/preview', 'POST', {
          product_key: pSel.value,
          lang: lSel.value,
          custom_greeting: gInput.value.trim(),
          custom_prompt: prInput.value.trim(),
        });
        lastScriptPreview = res;
        notice('Preview generated! Review the exact greeting and prompt below.');
        renderScriptDraft();
      }, 'primary-btn'),
      button('💾 Save Script as Default', async () => {
        const updated = { ...tel, voice_name: tVoice.value };
        if (lSel.value === 'kz') {
          updated.custom_greeting_kz = gInput.value.trim();
          updated.custom_prompt_kz = prInput.value.trim();
        } else {
          updated.custom_greeting_ru = gInput.value.trim();
          updated.custom_prompt_ru = prInput.value.trim();
        }
        await api('/studio/settings/telephony', 'PUT', updated);
        notice(`Saved default ${lSel.value.toUpperCase()} call script!`);
      }, 'secondary-btn')
    );
    scCard.append(scActs, scriptDraftContainer);

    function renderScriptDraft() {
      scriptDraftContainer.replaceChildren();
      if (!lastScriptPreview) return;
      const prevBox = el('div', undefined, 'studio-draft-box');
      prevBox.append(el('h4', `Opening Greeting (${lastScriptPreview.lang.toUpperCase()} · Voice: ${lastScriptPreview.voice_name})`));
      prevBox.append(el('p', '“' + lastScriptPreview.greeting + '”', 'studio-quote'));
      prevBox.append(el('h4', 'Full Isolated System Instruction Sent to PBX Gateway'));
      prevBox.append(el('pre', lastScriptPreview.system_prompt));
      prevBox.append(button('🔊 Listen to This Greeting', async () => {
        await playVoicePreview(api, notice, lastScriptPreview.voice_name, lastScriptPreview.lang, lastScriptPreview.greeting);
      }, 'secondary-btn'));
      scriptDraftContainer.append(prevBox);
    }

    renderScriptDraft();
    root.append(scCard);
  }

  return {
    renderProductCatalog,
    renderAudienceAnalyzer,
    renderAIConnectors,
    renderCrmSync,
    renderCompanyEntities,
    renderDocumentTemplates,
    renderTelephonyAndScript,
  };
})();
