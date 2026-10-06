/* Buyer-support observation and explicitly confirmed manual pilot. */
(function(global) {
  'use strict';
  const endpoint = '/v1/sheet-vitrina-v1/feedbacks/buyer-support';
  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const linkLabels = {linked:'Связь покупки подтверждена',orphan:'Заявка без связанного чата',unlinked:'Связь покупки не установлена',ambiguous:'Связь требует проверки'};
  const messageLabels = {inline:'Покупка указана в сообщении',observed_single_purchase:'В истории чата пока одна покупка; покупка сообщения не подтверждена',ambiguous:'В чате несколько покупок; покупка сообщения неизвестна',unlinked:'Покупка сообщения не установлена'};
  function timeLabel(value, withYear) {
    const text = String(value || '');
    if (/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(text)) {
      const date = new Date(text);
      if (!Number.isNaN(date.getTime())) return new Intl.DateTimeFormat('ru-RU', {day:'2-digit',month:'2-digit',year:withYear ? 'numeric' : undefined,hour:'2-digit',minute:'2-digit'}).format(date);
    }
    const naive = text.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2})/);
    return naive ? naive[3] + '.' + naive[2] + ' ' + naive[4] + ' · время WB' : text;
  }
  function renderClaim(c) {
    return '<div class="bs-claim"><strong>' + (c.archive ? 'Архив WB' : 'Активная заявка WB') + '</strong><p>' + esc(c.title) + '</p><p>' + esc(c.comment) + '</p><p class="bs-muted">' + esc(linkLabels[c.link_state]) + ' · точный срок неизвестен</p><p class="bs-muted">Последнее чтение: ' + esc(timeLabel(c.seen_at)) + '</p><details><summary class="bs-muted">Сведения WB</summary><p class="bs-muted">Номер: ' + esc(c.id) + '</p><p class="bs-muted">Коды состояния: ' + esc(c.status == null ? 'неизвестен' : c.status) + ' / ' + esc(c.status_ex == null ? 'неизвестен' : c.status_ex) + '</p><p class="bs-muted">Создана: ' + esc(timeLabel(c.created_at)) + ' · часовая зона неизвестна</p></details></div>';
  }
  const claimMethods = {autorefund1:'Без сдачи товара',approve2:'Со сдачей товара'};
  const operationLabels = {confirmed:'Подтверждено WB',succeeded:'Подтверждено WB',unknown:'Результат WB неизвестен',pending:'Проверка не завершена',pending_readback:'Ожидает проверки результата WB',write_started:'Результат WB ещё не подтверждён',failed:'Операция завершилась ошибкой',blocked:'Действие заблокировано',prepared:'Подготовлено',sending:'Проверяется результат WB'};
  function usableDraft(item) {
    const draft = (item.workflow || {}).latest_draft;
    return draft && draft.context_version === item.context_version && draft.status === 'ready' ? draft : null;
  }
  function renderPilot(item) {
    const pilot = item.pilot || {}, workflow = item.workflow || {}, draft = usableDraft(item);
    const blocks = pilot.blocked_codes || [];
    const canRefresh = pilot.enabled && pilot.allowlisted && pilot.wb_configured;
    const canWrite = canRefresh;
    const canPropose = canRefresh && pilot.provider_configured && !blocks.some(code => !['source_refresh_required','generation_result_unknown'].includes(code));
    let html = '<section class="bs-pilot"><strong>Автоответы: OFF</strong><p class="bs-muted">В пилоте автоматические действия включить нельзя. Каждое действие подтверждается вручную.</p>';
    if (!pilot.enabled) html += '<p class="bs-muted">Режим наблюдения. Ручной пилот отключён.</p>';
    else if (!pilot.allowlisted) html += '<p class="bs-muted">Это обращение доступно только для наблюдения. Пилот разрешён для тестового обращения владельца.</p>';
    else if (!pilot.wb_configured) html += '<p class="bs-muted">Подключение WB ещё не настроено.</p>';
    else if (!pilot.provider_configured) html += '<p class="bs-muted">Провайдер ответов ещё не настроен.</p>';
    if (canRefresh) html += '<button class="bs-button" type="button" data-bs-action="refresh">Обновить из WB</button> ';
    if (canPropose) html += '<button class="bs-button" type="button" data-bs-action="propose"' + (pilot.source_fresh && !pilot.generation_blocked ? '' : ' disabled data-bs-unavailable="true"') + '>Предложить ответ</button>';
    if (canPropose && !pilot.source_fresh) html += '<p class="bs-muted">Сначала обновите историю из WB. Проверьте новые сообщения и состояние заявки перед подготовкой ответа.</p>';
    if (pilot.generation_blocked) html += '<p class="bs-muted">Результат подготовки ответа для этой истории неизвестен. Повторная генерация заблокирована. Можно обновить историю из WB.</p>';
    if (pilot.source_refreshed_at) html += '<p class="bs-muted">Проверено в WB: ' + esc(timeLabel(pilot.source_refreshed_at)) + '</p>';
    if (workflow.latest_draft && !draft && workflow.latest_draft.status !== 'consumed' && workflow.latest_draft.status !== 'sent') html += '<p class="bs-error">Черновик устарел или недоступен. Обновите обращение и предложите ответ заново.</p>';
    if (draft) {
      html += '<div class="bs-draft"><strong>Предложенный ответ</strong><p class="bs-muted">Проверьте текст перед отправкой. Черновик не отправлен покупателю.</p><div class="bs-bubble" data-bs-draft-text>' + esc(draft.text || 'Ответ не требуется.') + '</div>';
      if (canWrite && draft.text) html += '<button class="bs-button bs-primary" type="button" data-bs-action="send">Отправить</button>';
      if (draft.return_proposal) {
        const proposal = draft.return_proposal;
        html += '<div class="bs-return"><strong>Предложение по возврату</strong><p>Способ: ' + esc(claimMethods[proposal.action] || 'Недоступный способ') + '</p><p>Заявка: ' + esc(proposal.claim_id) + '</p><p>' + esc(proposal.basis) + '</p><p class="bs-muted">Решение по заявке подтверждается отдельно от отправки ответа. Сообщать об одобрении можно после проверки результата WB.</p>';
        if (canWrite && claimMethods[proposal.action] && proposal.basis_type === 'text_sufficient') html += '<button class="bs-button" type="button" data-bs-review-return>Одобрить возврат</button><dialog class="bs-return-dialog" data-bs-return-confirm><strong>Одобрить возврат?</strong><p>Заявка: ' + esc(proposal.claim_id) + '</p><p><strong>Способ: ' + esc(claimMethods[proposal.action]) + '</strong></p><label class="bs-basis"><input type="checkbox" data-bs-text-basis> Для этого решения достаточно сведений из переписки; проверка фото не нужна</label><div class="bs-controls"><button class="bs-button" type="button" data-bs-cancel-return>Отмена</button><button class="bs-button bs-primary" type="button" data-bs-action="claim" disabled data-bs-unavailable="true">Подтвердить решение WB</button></div></dialog>';
        else if (canWrite) html += '<p class="bs-muted">Для этого решения требуется проверка материалов. Просмотр фото/видео в пилоте недоступен.</p>';
        html += '</div>';
      }
      html += '</div>';
    }
    html += (workflow.operations || []).map(op => {
      const pendingReply = op.kind === 'claim_decision' && op.state === 'confirmed' && op.response_draft_state === 'draft_pending';
      const canCheck = op.can_reconcile && (pendingReply || ['unknown','pending','sending','pending_readback','write_started'].includes(op.state));
      const label = (op.kind === 'chat_send' || op.kind === 'send' ? 'Ответ: ' : op.kind === 'claim_decision' || op.kind === 'claim' ? 'Возврат: ' : '') + (operationLabels[op.state] || 'Состояние операции: ' + op.state);
      return '<div class="bs-operation"><p>' + esc(label) + '</p><p class="bs-muted">Номер: ' + esc(op.operation_id) + '</p>' +
        (pendingReply ? '<p class="bs-muted">Ответ после решения ещё не подготовлен. Подготовка требует доступного провайдера и включённого ручного пилота.</p>' : op.response_draft_state === 'read_limit' ? '<p class="bs-muted">Подготовка ответа остановлена: не удалось получить достаточные сведения.</p>' : '') +
        (canCheck ? '<button class="bs-button" type="button" data-bs-action="reconcile" data-bs-operation="' + esc(op.operation_id) + '">' + (pendingReply ? 'Продолжить подготовку ответа' : 'Проверить результат WB') + '</button>' : '') + '</div>';
    }).join('');
    return html + '<p class="bs-muted" data-bs-pilot-message role="status"></p><div data-bs-recovery></div></section>';
  }
  function renderDetail(item) {
    const messages = item.messages || [];
    const claims = item.claims || [];
    return '<h3>' + esc(item.name) + '</h3><div class="bs-summary">' + (item.pilot && item.pilot.enabled && item.pilot.allowlisted ? 'Ручной пилот. Каждое действие требует подтверждения.' : 'Наблюдение. Решения бота пока не рассчитаны.') + '</div>' +
      claims.map(renderClaim).join('') + (claims.length ? '' : '<p class="bs-muted">Связанная заявка в локальной истории отсутствует.</p>') +
      '<div class="bs-conversation">' + messages.map(m => '<div class="bs-message ' + (m.sender === 'seller' ? 'seller' : '') + '"><p class="bs-muted">' + (m.sender === 'seller' ? 'Продавец · история WB' : m.sender === 'client' ? 'Покупатель' : 'Событие WB') + ' · ' + esc(timeLabel(m.time)) + '</p><div class="bs-bubble">' + esc(m.text) + (m.media_count ? '<p class="bs-muted">Материалы: ' + esc(m.media_count) + ' · просмотр ещё не подключён</p>' : '') + '</div><p class="bs-muted">' + esc(messageLabels[m.purchase_link_state]) + '</p></div>').join('') + '</div>' +
      renderPilot(item) + '<details class="bs-proof"><summary>Основание связи и состояние данных</summary><p>Связь проверяется только внутри кабинета: точное совпадение rid покупки и srid заявки. Несколько чатов или конфликт товара сохраняют неоднозначность.</p><p>Покупки чата: ' + esc((item.purchases || []).map(p => p.rid).join(', ') || 'не установлены') + '</p><p>Времена заявок WB сохранены как получены. Часовая зона и точный срок неизвестны. Отсутствие заявки в очередной загрузке не означает её завершение.</p></details>';
  }
  function renderList(payload, selected) {
    if (!payload.configured) return '<div class="bs-empty">Кабинет для наблюдения ещё не настроен.</div>';
    if (!payload.items.length) {
      const state = payload.history && payload.history.state;
      const note = state === 'not_loaded' ? 'История сообщений ещё не загружена.' :
        state === 'error' ? 'Загрузка истории завершилась ошибкой. Сохранённая часть не содержит обращений по этим условиям.' :
        state === 'partial' || state === 'running' ? 'История загружена частично. В сохранённой части обращений по этим условиям нет.' :
        'Обращений по выбранным условиям в сохранённой истории нет.';
      return '<div class="bs-empty">' + note + '</div>';
    }
    return payload.items.map(i => '<button type="button" class="bs-entry" data-bs-item="' + esc(i.id) + '" data-bs-kind="' + esc(i.kind) + '" aria-pressed="' + String(selected === i.kind + ':' + i.id) + '"><strong>' + esc(i.name) + '</strong><span class="bs-preview">' + esc(i.preview) + '</span><span class="bs-muted">' + esc(linkLabels[i.link_state]) + '</span><span class="bs-muted">' + esc(timeLabel(i.time)) + '</span></button>').join('');
  }
  // Pure renderers allow synthetic security/UI regression checks without a live account.
  if (typeof module !== 'undefined' && module.exports) module.exports = {esc, renderDetail, renderList, renderPilot, usableDraft};
  if (!global.document) return;
  const root = global.document.querySelector('#buyer-support');
  if (!root) return;
  const node = name => root.querySelector('[data-bs-' + name + ']');
  let loaded = false, offset = 0, total = 0, selected = '', listSeq = 0, detailSeq = 0, timer;
  let current = null, busy = false;
  const pendingKey = 'wbcBuyerSupportPendingV1';
  let pending, blockedGenerations = {};
  try { blockedGenerations = JSON.parse(global.sessionStorage.getItem('wbcBuyerSupportGenerationUnknownV1') || '{}'); } catch (_) {}
  function blockGeneration(selection, version) { if (!version) return; blockedGenerations[selection] = version; try {global.sessionStorage.setItem('wbcBuyerSupportGenerationUnknownV1', JSON.stringify(blockedGenerations));} catch (_) {} }
  try { pending = JSON.parse(global.sessionStorage.getItem(pendingKey) || 'null'); } catch (_) { pending = null; }
  function savePending(value) { pending = value; try { value ? global.sessionStorage.setItem(pendingKey, JSON.stringify(value)) : global.sessionStorage.removeItem(pendingKey); } catch (_) {} }
  function requestId() { return global.crypto.randomUUID(); }
  async function get(path, params) {
    const response = await global.fetch(endpoint + path + '?' + new URLSearchParams(params), {method:'GET',credentials:'same-origin',cache:'no-store'});
    if (!response.ok) throw new Error('read_failed');
    const result = await response.json();
    if (result.error && !(path === '/pilot/operation' && (result.status === 'generation_unknown' || (result.state === 'failed' && result.write_attempted === false)))) throw new Error('read_failed');
    return result;
  }
  async function list() {
    const seq = ++listSeq;
    ++detailSeq; current = null; selected = ''; root.dataset.mobileDetail = 'false';
    node('detail').innerHTML = '<div class="bs-empty">Выберите обращение слева.</div>';
    node('list').innerHTML = '<div class="bs-empty">Загружаем локальную историю…</div>';
    node('prev').disabled = true; node('next').disabled = true;
    try {
      const payload = await get('/list', {q:node('search').value,filter:node('filter').value,period:node('period').value,offset,limit:50});
      if (seq !== listSeq) return;
      if (!Array.isArray(payload.items) || !Array.isArray(payload.sync) || !Number.isInteger(payload.total)) throw new Error('invalid_payload');
      total = payload.total; loaded = true;
      node('list').innerHTML = renderList(payload, selected);
      node('count').textContent = total ? (offset + 1) + '–' + Math.min(offset + 50,total) + ' из ' + total : '0';
      node('prev').disabled = offset === 0; node('next').disabled = offset + 50 >= total;
      const history = payload.history || {};
      node('history').textContent = history.event_count ?
        'Сохранённые сообщения: ' + timeLabel(history.oldest_message_at,true) + ' — ' + timeLabel(history.newest_message_at,true) +
        ' · ' + history.event_count + ' сообщений · данные сохранены: ' + timeLabel(history.last_sync_at,true) :
        history.state === 'complete' ? 'Загрузка сообщений завершена; сообщений нет.' : 'История сообщений ещё не загружена.';
      node('sync').textContent = payload.sync.map(s => ({chats:'Чаты',events:'Сообщения',claims_active:'Активные заявки',claims_archive:'Архив заявок'}[s.source] || 'Данные') + ': ' + ({complete:'загрузка завершена',partial:'частичная загрузка',error:'ошибка загрузки',running:'загрузка начата'}[s.state] || 'неизвестно') + ' · ' + timeLabel(s.updated_at)).join(' / ') || 'Загрузок WB ещё не было.';
    } catch (_) {
      if (seq !== listSeq) return;
      loaded = false; node('count').textContent = '';
      node('history').textContent = 'Свежесть истории сейчас проверить не удалось.';
      node('sync').textContent = '';
      node('list').innerHTML = '<div class="bs-error">Не удалось прочитать историю. Нажмите «Обновить экран» для повторной проверки.</div>';
    }
  }
  async function detail(kind, id) {
    const seq = ++detailSeq;
    current = null; selected = kind + ':' + id; root.dataset.mobileDetail = 'true';
    root.querySelectorAll('[data-bs-item]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.bsKind + ':' + b.dataset.bsItem === selected)));
    node('detail').innerHTML = '<div class="bs-empty">Загружаем обращение…</div>';
    try {
      const payload = await get('/detail', {kind,id});
      if (seq !== detailSeq) return;
      if (!Array.isArray(payload.claims) || !Array.isArray(payload.messages)) throw new Error('invalid_payload');
      current = payload; node('detail').innerHTML = renderDetail(payload); showPending();
    } catch (_) {
      if (seq === detailSeq) node('detail').innerHTML = '<div class="bs-error">Не удалось прочитать обращение. Выберите его ещё раз.</div>';
    }
  }
  function generationBlocked() {
    if (!current) return false;
    return current.pilot && typeof current.pilot.generation_blocked === 'boolean' ? current.pilot.generation_blocked : blockedGenerations[selected] === current.context_version;
  }
  function setBusy(value) {
    busy = value;
    root.querySelectorAll('[data-bs-action]').forEach(button => {button.disabled = value || !!pending || button.dataset.bsUnavailable === 'true' || (button.dataset.bsAction === 'propose' && generationBlocked());});
    const status = node('pilot-message');
    if (status && value) status.textContent = 'Выполняем действие… Дождитесь результата.';
  }
  function showPending() {
    const recovery = node('recovery');
    if (recovery && pending) recovery.innerHTML = '<p class="bs-error">Результат команды ещё не подтверждён. Повторная отправка заблокирована.</p><button class="bs-button" type="button" data-bs-check>Проверить результат команды</button>';
    const status = node('pilot-message');
    if (status && !busy && generationBlocked()) status.textContent = 'Результат подготовки ответа для этой истории неизвестен. Повторный запрос заблокирован. Можно обновить историю из WB.';
    setBusy(busy);
  }
  async function reloadCurrent(selection) {
    const [kind, ...id] = selection.split(':');
    if (selected === selection) await detail(kind, id.join(':'));
  }
  async function runAction(action, button) {
    if (busy || pending || !current) return;
    const snapshot = current, selection = selected, draft = usableDraft(snapshot);
    const body = {request_id:requestId()};
    if (action === 'refresh') Object.assign(body,{kind:snapshot.kind || selection.split(':')[0],item_id:snapshot.id || selection.slice(selection.indexOf(':')+1)});
    else if (action === 'propose') Object.assign(body,{kind:snapshot.kind || selection.split(':')[0],item_id:snapshot.id || selection.slice(selection.indexOf(':')+1),expected_context_version:snapshot.context_version});
    else if (action === 'reconcile') body.operation_id = button.dataset.bsOperation;
    else {
      if (!draft) return;
      Object.assign(body,{draft_id:draft.draft_id,expected_context_version:snapshot.context_version,confirmed:true});
      if (action === 'send') {
        if (!draft.text || !global.confirm('Отправить покупателю этот текст?\n\n' + draft.text)) return;
      } else if (action === 'claim') {
        const proposal = draft.return_proposal;
        if (!proposal || proposal.basis_type !== 'text_sufficient' || !node('text-basis') || !node('text-basis').checked || !claimMethods[proposal.action] || !node('return-confirm').open) return;
        node('return-confirm').close();
        Object.assign(body,{claim_id:proposal.claim_id,action:proposal.action,expected_claim_version:proposal.claim_version,text_basis_confirmed:true});
      } else return;
    }
    savePending({request_id:body.request_id,selection,action,context_version:snapshot.context_version}); setBusy(true);
    let note = '';
    try {
      const response = await global.fetch(endpoint + '/pilot/' + action,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-WB-Buyer-Support-CSRF':'1'},body:JSON.stringify(body)});
      const payload = await response.json();
      if (!response.ok) {
        if (response.status >= 500 && !(payload.not_accepted === true && payload.write_attempted === false && payload.request_id === body.request_id)) throw new Error('unclear');
        note = payload.code === 'source_refresh_required' ? 'Сначала обновите историю из WB, затем проверьте обращение.' : response.status === 409 ? 'Данные изменились или действие недоступно. Проверьте обновлённое обращение.' : 'Действие не принято. Проверьте обновлённое обращение.';
      } else if (payload.status === 'generation_unknown' || payload.state === 'request_result_unknown') {
        note = 'Результат подготовки ответа для этой истории неизвестен. Повторный запрос заблокирован. Можно обновить историю из WB.';
        if (payload.status === 'generation_unknown') {blockGeneration(selection, snapshot.context_version); savePending(null);}
      } else {
        note = action === 'refresh' ? 'История и состояние заявки обновлены из WB. Проверьте их перед подготовкой ответа.' : action === 'propose' ? (payload.status === 'ready' ? 'Предложение готово. Проверьте его перед подтверждением.' : 'Контекст изменился. Черновик недоступен для отправки.') : 'Команда обработана. Фактическое состояние показано ниже.';
      }
      if (payload.status !== 'generation_unknown' && payload.state !== 'request_result_unknown') savePending(null);
    } catch (_) { note = 'Ответ сервера не получен. Проверьте результат той же команды.'; }
    finally {
      await reloadCurrent(selection);
      setBusy(false); showPending();
      const status = node('pilot-message'); if (status && selected === selection) status.textContent = note;
    }
  }
  async function checkPending() {
    if (busy || !pending) return;
    const receipt = pending; setBusy(true);
    let note = 'Результат пока недоступен. Повторная отправка остаётся заблокированной.';
    try {
      const payload = await get('/pilot/operation',{request_id:receipt.request_id});
      if (payload && payload.state === 'failed' && payload.write_attempted === false && payload.request_id === receipt.request_id) {
        savePending(null); note = 'Команда не выполнена: отправки покупателю или решения по заявке не было. Проверьте обращение перед новым действием.';
      } else if (payload && receipt.action === 'refresh' && payload.kind === 'refresh' && payload.write_attempted === false && payload.state === 'request_result_unknown') {
        savePending(null); note = 'Обновление из WB не завершилось. Можно запустить новое обновление истории.';
      } else if (payload && payload.status === 'generation_unknown') {
        blockGeneration(receipt.selection, receipt.context_version); savePending(null);
        note = 'Результат подготовки ответа для этой истории неизвестен. Повторный запрос заблокирован. Можно обновить историю из WB.';
      } else if (payload && (payload.operation_id || payload.draft_id || (receipt.action === 'refresh' && payload.context_version && Array.isArray(payload.messages))) && payload.state !== 'pending') {
        savePending(null); note = 'Результат команды найден. Проверьте состояние обращения.';
      }
    } catch (_) {}
    finally {
      await reloadCurrent(receipt.selection); setBusy(false); showPending();
      const status = node('pilot-message'); if (status && selected === receipt.selection) status.textContent = note;
    }
  }
  node('detail').addEventListener('change', e => {
    if (e.target.matches('[data-bs-text-basis]')) {
      const button = root.querySelector('[data-bs-action="claim"]');
      if (button) {button.dataset.bsUnavailable = String(!e.target.checked); setBusy(busy);}
    }
  });
  node('detail').addEventListener('click', e => {
    if (e.target.closest('[data-bs-review-return]') && !busy && !pending) node('return-confirm').showModal();
    if (e.target.closest('[data-bs-cancel-return]')) {node('text-basis').checked = false; const confirmButton = root.querySelector('[data-bs-action="claim"]'); confirmButton.dataset.bsUnavailable = 'true'; setBusy(busy); node('return-confirm').close();}
    const button = e.target.closest('[data-bs-action]');
    if (button) runAction(button.dataset.bsAction, button);
    if (e.target.closest('[data-bs-check]')) checkPending();
  });
  global.wbcBuyerSupportLoad = () => { if (!loaded) return list(); };
  if (!root.closest('[data-unified-tab-panel]').hidden) list();
  node('refresh').addEventListener('click', () => {if (current && current.pilot && current.pilot.enabled && current.pilot.allowlisted && current.pilot.wb_configured) runAction('refresh', node('refresh')); else if (selected) reloadCurrent(selected); else {offset = 0; list();}});
  node('search').addEventListener('input', () => {clearTimeout(timer); timer = setTimeout(() => {offset = 0; list();},250);});
  node('period').addEventListener('change', () => {offset = 0; list();});
  node('filter').addEventListener('change', () => {offset = 0; list();});
  node('prev').addEventListener('click', () => {offset = Math.max(0, offset - 50); list();});
  node('next').addEventListener('click', () => {if (offset + 50 < total) {offset += 50; list();}});
  node('list').addEventListener('click', e => {const b = e.target.closest('[data-bs-item]'); if (b) detail(b.dataset.bsKind,b.dataset.bsItem);});
  node('back').addEventListener('click', () => {root.dataset.mobileDetail = 'false';});
  node('settings-toggle').addEventListener('click', () => {node('settings').hidden = !node('settings').hidden; node('settings-toggle').setAttribute('aria-expanded', String(!node('settings').hidden));});
})(typeof window === 'undefined' ? globalThis : window);
