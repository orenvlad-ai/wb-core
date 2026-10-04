/* Buyer-support observation UI; network capabilities are GET-only. */
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
  function renderDetail(item) {
    const messages = item.messages || [];
    const claims = item.claims || [];
    return '<h3>' + esc(item.name) + '</h3><div class="bs-summary">Наблюдение. Решения бота пока не рассчитаны.</div>' +
      claims.map(renderClaim).join('') + (claims.length ? '' : '<p class="bs-muted">Связанная заявка в локальной истории отсутствует.</p>') +
      '<div class="bs-conversation">' + messages.map(m => '<div class="bs-message ' + (m.sender === 'seller' ? 'seller' : '') + '"><p class="bs-muted">' + (m.sender === 'seller' ? 'Продавец · история WB' : m.sender === 'client' ? 'Покупатель' : 'Событие WB') + ' · ' + esc(timeLabel(m.time)) + '</p><div class="bs-bubble">' + esc(m.text) + (m.media_count ? '<p class="bs-muted">Материалы: ' + esc(m.media_count) + ' · просмотр ещё не подключён</p>' : '') + '</div><p class="bs-muted">' + esc(messageLabels[m.purchase_link_state]) + '</p></div>').join('') + '</div>' +
      '<details class="bs-proof"><summary>Основание связи и состояние данных</summary><p>Связь проверяется только внутри кабинета: точное совпадение rid покупки и srid заявки. Несколько чатов или конфликт товара сохраняют неоднозначность.</p><p>Покупки чата: ' + esc((item.purchases || []).map(p => p.rid).join(', ') || 'не установлены') + '</p><p>Времена заявок WB сохранены как получены. Часовая зона и точный срок неизвестны. Отсутствие заявки в очередной загрузке не означает её завершение.</p><p>Решения, ответы и операции бота отсутствуют.</p></details>';
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
  if (typeof module !== 'undefined' && module.exports) module.exports = {esc, renderDetail, renderList};
  if (!global.document) return;
  const root = global.document.querySelector('#buyer-support');
  if (!root) return;
  const node = name => root.querySelector('[data-bs-' + name + ']');
  let loaded = false, offset = 0, total = 0, selected = '', listSeq = 0, detailSeq = 0, timer;
  async function get(path, params) {
    const response = await global.fetch(endpoint + path + '?' + new URLSearchParams(params), {method:'GET',credentials:'same-origin',cache:'no-store'});
    if (!response.ok) throw new Error('read_failed');
    const result = await response.json();
    if (result.error) throw new Error('read_failed');
    return result;
  }
  async function list() {
    const seq = ++listSeq;
    ++detailSeq; selected = ''; root.dataset.mobileDetail = 'false';
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
    selected = kind + ':' + id; root.dataset.mobileDetail = 'true';
    root.querySelectorAll('[data-bs-item]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.bsKind + ':' + b.dataset.bsItem === selected)));
    node('detail').innerHTML = '<div class="bs-empty">Загружаем обращение…</div>';
    try {
      const payload = await get('/detail', {kind,id});
      if (seq !== detailSeq) return;
      if (!Array.isArray(payload.claims) || !Array.isArray(payload.messages)) throw new Error('invalid_payload');
      node('detail').innerHTML = renderDetail(payload);
    } catch (_) {
      if (seq === detailSeq) node('detail').innerHTML = '<div class="bs-error">Не удалось прочитать обращение. Выберите его ещё раз.</div>';
    }
  }
  global.wbcBuyerSupportLoad = () => { if (!loaded) return list(); };
  if (!root.closest('[data-unified-tab-panel]').hidden) list();
  node('refresh').addEventListener('click', () => {offset = 0; list();});
  node('search').addEventListener('input', () => {clearTimeout(timer); timer = setTimeout(() => {offset = 0; list();},250);});
  node('period').addEventListener('change', () => {offset = 0; list();});
  node('filter').addEventListener('change', () => {offset = 0; list();});
  node('prev').addEventListener('click', () => {offset = Math.max(0, offset - 50); list();});
  node('next').addEventListener('click', () => {if (offset + 50 < total) {offset += 50; list();}});
  node('list').addEventListener('click', e => {const b = e.target.closest('[data-bs-item]'); if (b) detail(b.dataset.bsKind,b.dataset.bsItem);});
  node('back').addEventListener('click', () => {root.dataset.mobileDetail = 'false';});
  node('settings-toggle').addEventListener('click', () => {node('settings').hidden = !node('settings').hidden; node('settings-toggle').setAttribute('aria-expanded', String(!node('settings').hidden));});
})(typeof window === 'undefined' ? globalThis : window);
