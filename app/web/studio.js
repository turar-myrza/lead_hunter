'use strict';

/* =========================================================================
   Lead Hunter Platform Studio UI (v2.2.0)
   - Declarative Component Mounts (Zero DOM duplication on Grid/List toggle)
   - Prompt-to-Product Catalog (Grid/List View, Checkboxes, Inline Edit, CRM Sync)
   - Prompt-to-Audience AI Client Analyzer (Push to Ads Manager)
   - AI Model & Token Connectors (LiteLLM / Gemini, OpenAI, DeepSeek, Local Ollama/vLLM)
   - Telephony Engine, Voice Audio Preview (KZ/RU), Company Profile & Call Script Studio
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
      const res = await api('/studio/products');
      studioProducts = res.items || [];
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
    const pLang = el('select');
    add(pLang, el('option', 'RU', ''), el('option', 'KZ', ''));
    pLang.options[0].value = 'ru';
    pLang.options[1].value = 'kz';

    const draftContainer = el('div', undefined, 'studio-draft-wrapper');

    const genBtn = button('✨ Research & Fill Card', async () => {
      const q = pInput.value.trim();
      if (q.length < 3) throw Error('Enter at least 3 characters describing the product');
      notice('AI is researching real product specs from the internet...');
      const r = await api('/studio/products/from-prompt', 'POST', { prompt: q, lang: pLang.value, auto_save: false });
      lastProductDraft = r;
      notice('Product data researched using ' + (r.model_used || 'AI'));
      renderDraft();
    }, 'primary-btn');
    add(promptRow, pInput, pLang, genBtn);
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
      const fUse = el('input'); fUse.value = d.target_use_case || '';

      const grid = el('div', undefined, 'studio-form-grid');
      const wrap = (lbl, node) => { const l = el('label', lbl); l.append(node); return l; };
      add(grid,
        wrap('Name (KZ)', fName),
        wrap('Name (RU)', fNameRu),
        wrap('Category', fCat),
        wrap('Price Range (KZT)', fPrice),
        wrap('Models (comma-separated)', fModels),
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
          if (item.models?.length) c.append(el('div', 'Models: ' + item.models.join(' · '), 'muted'));

          const acts = el('div', undefined, 'actions');
          add(acts,
            button('Edit', async () => openProductModal(item, h, () => renderProductCatalog(root, h)), 'secondary-btn'),
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
    const { api, edit, field, onRefreshState } = h;
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
  }

  /* =========================================================================
     3. AI MODEL & TOKEN CONNECTORS + CRM/ERP CONNECTORS
     ========================================================================= */
  async function renderConnectors(root, h) {
    const { api, el, add, button, badge, notice, paragraph } = h;
    
    // Strict clean mount
    root.replaceChildren();

    paragraph(root, 'Configure API access tokens for proprietary LLMs (Gemini, OpenAI, DeepSeek, Anthropic) or self-hosted local models (Ollama, vLLM, LM Studio), plus CRM/ERP product connectors.', 'lead');

    let envInfo = {};
    try {
      const [mRes, sRes] = await Promise.all([api('/studio/connectors/models'), api('/studio/settings')]);
      aiProviders = mRes.items || [];
      envInfo = mRes;
      studioSettings = sRes;
    } catch (e) {
      root.append(el('p', 'Failed to load connectors: ' + e.message, 'muted'));
      return;
    }

    // Section A: AI Model & Token Connectors
    const aiCard = el('div', undefined, 'card');
    const aiHead = el('div', undefined, 'studio-toolbar');
    const titleDiv = el('div');
    titleDiv.append(el('h3', '🤖 AI Agent Model & Token Connectors'));
    if (envInfo.env_gemini_configured) {
      titleDiv.append(badge(`System Fallback Active: ${envInfo.env_gemini_model || 'gemini-flash-latest'}`, 'live-badge'));
    }
    const addModelBtn = button('+ Add AI Model / Local Endpoint', async () => {
      openProviderModal(null, h, () => renderConnectors(root, h));
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
        add(acts,
          button('⚡ Test', async () => {
            notice(`Testing ${p.name}...`);
            const r = await api(`/studio/connectors/models/${p.id}/test`, 'POST');
            notice(`OK (${r.latency_ms}ms): ${r.reply}`);
          }, 'primary-btn'),
          button('Edit', async () => openProviderModal(p, h, () => renderConnectors(root, h)), 'secondary-btn'),
          button('Delete', async () => {
            await api(`/studio/connectors/models/${p.id}`, 'DELETE');
            notice('AI connector removed');
            renderConnectors(root, h);
          }, 'secondary-btn')
        );
        tdActs.append(acts);
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

    // Section B: CRM / ERP / CMS Connectors
    const crm = studioSettings?.connectors || {};
    const crmCard = el('div', undefined, 'card');
    crmCard.append(el('h3', '🔌 CRM, ERP & CMS Product Platform Connectors'));
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
      button('💾 Save Connectors', async () => {
        await api('/studio/settings/connectors', 'PUT', {
          bitrix24_webhook: fB24.value.trim(),
          moysklad_token: fMS.value.trim(),
          onec_odata_url: f1C.value.trim(),
          cms_feed_url: fCMS.value.trim(),
        });
        notice('CRM / ERP connectors saved');
        renderConnectors(root, h);
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
        ['all', 'All Operations (Default)'],
        ['web_research', 'Product Web Research'],
        ['audience_generation', 'Audience & ICP Analysis'],
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
     4. COMPANY PROFILE & COMMERCIAL OFFERS (DEDICATED TAB)
     ========================================================================= */
  async function renderCompanyProfile(root, h) {
    const { api, el, add, button, notice, paragraph, onRefreshState } = h;
    
    // Strict clean mount
    root.replaceChildren();

    paragraph(root, 'Set your company identity and default commercial offer PDFs. The AI Voice Agent automatically introduces itself from this company on every call.', 'lead');

    try {
      studioSettings = await api('/studio/settings');
    } catch (e) {
      root.append(el('p', 'Failed to load company profile: ' + e.message, 'muted'));
      return;
    }

    const company = studioSettings.company || {};
    const wrap = (lbl, node) => { const l = el('label', lbl); l.append(node); return l; };

    const compCard = el('div', undefined, 'card studio-highlight');
    compCard.append(el('h3', '🏢 Our Company Identity & Commercial Offer PDFs'));
    const cName = el('input'); cName.value = company.company_name || '';
    const cBin = el('input'); cBin.value = company.bin || '';
    const cCity = el('input'); cCity.value = company.city || '';
    const cWeb = el('input'); cWeb.value = company.website || '';
    const cMgr = el('input'); cMgr.value = company.manager_name || '';
    const cPhone = el('input'); cPhone.value = company.manager_phone || '';
    const cDesc = el('textarea'); cDesc.value = company.description || '';
    const cPdfKz = el('input'); cPdfKz.value = company.default_pdf_kz || '';
    const cPdfRu = el('input'); cPdfRu.value = company.default_pdf_ru || '';

    const cGrid = el('div', undefined, 'studio-form-grid');
    add(cGrid,
      wrap('Company Name (AI introduces itself from here)', cName),
      wrap('Company BIN', cBin),
      wrap('City', cCity),
      wrap('Website', cWeb),
      wrap('Manager Name (for Call Forwarding)', cMgr),
      wrap('Manager Phone', cPhone),
      wrap('Default Commercial Offer PDF URL (KZ)', cPdfKz),
      wrap('Default Commercial Offer PDF URL (RU)', cPdfRu)
    );
    compCard.append(cGrid, wrap('Company Description / Value Proposition', cDesc));
    const cActs = el('div', undefined, 'actions');
    cActs.append(button('💾 Save Company Profile', async () => {
      await api('/studio/settings/company', 'PUT', {
        company_name: cName.value.trim(),
        bin: cBin.value.trim(),
        city: cCity.value.trim(),
        website: cWeb.value.trim(),
        manager_name: cMgr.value.trim(),
        manager_phone: cPhone.value.trim(),
        description: cDesc.value.trim(),
        default_pdf_kz: cPdfKz.value.trim(),
        default_pdf_ru: cPdfRu.value.trim(),
      });
      notice('Company profile saved! AI will now introduce itself as ' + cName.value.trim());
      if (onRefreshState) await onRefreshState(false);
    }, 'primary-btn'));
    compCard.append(cActs);
    root.append(compCard);
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
    renderConnectors,
    renderCompanyProfile,
    renderTelephonyAndScript,
  };
})();
