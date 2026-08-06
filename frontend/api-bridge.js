/**
 * Caspian Monitor — связующий слой между веб-панелью и сервером.
 *
 * Скрипт подключается после основного сценария страницы и до события
 * DOMContentLoaded. Он подменяет источник данных: вместо генерации значений
 * в браузере панель запрашивает их у сервера, который принимает телеметрию
 * буёв по LoRaWAN через ChirpStack.
 *
 * Заменяются четыре функции:
 *   simulateNewData -> опрос /api/buoys и /api/packets;
 *   addPacket       -> журнал реально принятых кадров;
 *   decodePacket    -> разбор кадра серверным кодеком (/api/decode);
 *   testConnection  -> проверка доступности сервера (/health).
 *
 * Разметка и оформление страницы не затрагиваются.
 */

(function () {
  'use strict';

  const API = (window.CASPIAN_API_BASE || '').replace(/\/$/, '');
  const POLL_INTERVAL_MS = 5000;

  const RANGE_BY_LABEL = { '1ч': '1h', '6ч': '6h', '24ч': '24h', '1д': '24h', '7д': '7d', '30д': '30d', '90д': '90d' };

  let lastPacketId = 0;
  let firstLoadDone = false;
  let currentRange = '1h';

  const api = async (path) => {
    const response = await fetch(API + path, { cache: 'no-store' });
    if (!response.ok) throw new Error(`${path} -> HTTP ${response.status}`);
    return response.json();
  };

  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };

  const setOnline = (ok) => {
    const dot = document.getElementById('server-dot');
    const txt = document.getElementById('server-status-text');
    if (dot) dot.className = ok ? 'status-dot' : 'status-dot offline';
    if (txt) txt.textContent = ok ? 'СЕРВЕР' : 'НЕТ СВЯЗИ';
  };

  // ---------------------------------------------------------------- данные

  async function refreshBuoys() {
    const data = await api('/api/buoys');
    if (!Array.isArray(data) || data.length === 0) return;

    // Обновляем существующие объекты по месту, чтобы сохранить ссылки,
    // удерживаемые обработчиками страницы.
    data.forEach((remote, i) => {
      if (i >= BUOYS.length) return;
      Object.assign(BUOYS[i], {
        id: remote.id,
        name: remote.name,
        lat: remote.lat,
        lng: remote.lng,
        status: remote.status,
        pH: remote.pH,
        temp: remote.temp,
        DO: remote.DO,
        turb: remote.turb,
        ec: remote.ec,
        tds: remote.tds,
        orp: remote.orp,
        hc: remote.hc,
        battery: remote.battery,
        rssi: remote.rssi,
        snr: remote.snr,
        devEui: remote.dev_eui,
        lastSeen: remote.last_seen
      });

      const fmt = (v, d) => (v === null || v === undefined ? '—' : v.toFixed(d));
      const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
      set(`ri${i}-ph`, fmt(remote.pH, 2));
      set(`ri${i}-t`, fmt(remote.temp, 1));
      set(`ri${i}-do`, fmt(remote.DO, 1));
      set(`ri${i}-hc`, fmt(remote.hc, 3));
    });

    if (typeof updateSensorCards === 'function') updateSensorCards(BUOYS[selectedBuoy]);
    if (!firstLoadDone && typeof buildRoller === 'function') {
      buildRoller();
      firstLoadDone = true;
    }
  }

  async function refreshHistory(index) {
    const buoy = BUOYS[index];
    if (!buoy || !buoy.devEui) return;

    const data = await api(`/api/buoys/${buoy.devEui}/history?range=${currentRange}&points=60`);
    const labels = (data.labels || []).map((s) => (s.includes('T') ? s.split('T')[1] || s : s));
    const h = buoyHistory[index];
    if (!h) return;

    h.labels = labels;
    h.ph = data.ph || [];
    h.do_ = data.do_ || [];
    h.temp = data.temp || [];
    h.ec = data.ec || [];
    h.hc = data.hc || [];
    h.turb = data.turb || [];

    if (typeof reloadChartsForBuoy === 'function') reloadChartsForBuoy(index);
  }

  async function refreshPackets() {
    const packets = await api('/api/packets?limit=20');
    const list = document.getElementById('packet-list');
    if (!list || !Array.isArray(packets)) return;

    const fresh = packets.filter((p) => p.id > lastPacketId).reverse();
    if (packets.length) lastPacketId = Math.max(lastPacketId, packets[0].id);

    fresh.forEach((p) => {
      const item = document.createElement('div');
      item.className = 'packet-item' + (p.valid ? '' : ' packet-invalid');
      const time = new Date(p.ts).toLocaleTimeString('ru-RU');
      const hex = (p.payload_hex || '').toUpperCase().match(/.{1,4}/g);
      item.innerHTML =
        `<span class="packet-ts">${time}</span>` +
        `<span class="packet-id">${p.code || p.dev_eui.slice(-4)}</span>` +
        `<span class="packet-hex">${hex ? hex.slice(0, 4).join(' ') : ''}</span>` +
        `<span class="packet-rssi">${p.rssi !== null ? Math.round(p.rssi) : '—'}dBm</span>`;
      list.insertBefore(item, list.firstChild);
    });

    while (list.children.length > 30) list.removeChild(list.lastChild);
  }

  async function refreshNetwork() {
    const stats = await api('/api/network/stats');
    setText('lora-freq', `${stats.frequency_mhz} МГц`);
    setText('lora-rssi', stats.rssi_avg !== null ? `${stats.rssi_avg} дБм` : '— дБм');
    setText('lora-snr', stats.snr_avg !== null ? `+${stats.snr_avg} дБ` : '— дБ');
    setText('lora-pph', stats.packets_per_hour);
    setText('lora-loss', `${stats.loss_percent}%`);
    setText('stat-packets', stats.packets_per_hour);
    setText('sb-last-packet', new Date().toLocaleTimeString('ru-RU'));
  }

  // ------------------------------------------------------------ подмена

  async function pollBackend() {
    try {
      await refreshBuoys();
      await refreshPackets();
      await refreshNetwork();
      await refreshHistory(selectedBuoy);
      setOnline(true);
    } catch (err) {
      console.error('[caspian] сервер недоступен:', err.message);
      setOnline(false);
    }
  }

  window.simulateNewData = pollBackend;
  window.addPacket = function () {}; // журнал заполняется данными сервера

  window.decodePacket = async function () {
    const field = document.getElementById('test-hex');
    const output = document.getElementById('decode-result');
    if (!field || !output) return;
    try {
      const response = await fetch(API + '/api/decode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hex: field.value })
      });
      const body = await response.json();
      if (!body.ok) {
        output.textContent = 'Ошибка: ' + body.error;
        return;
      }
      const d = body.decoded;
      const row = (label, value, unit, color) =>
        `${label}: <span style="color:${color}">${value === null ? '—' : value}${value === null ? '' : ' ' + unit}</span><br>`;
      output.innerHTML =
        row('pH', d.ph, '', '#0cffcb') +
        row('Температура', d.temp, '°C', '#ff6b35') +
        row('DO', d.do, 'мг/л', '#00b4d8') +
        row('Мутность', d.turb, 'NTU', '#ffd60a') +
        row('EC', d.ec, 'мСм/см', '#0cffcb') +
        row('ΣHC', d.hc, 'мг/л', '#06d6a0') +
        row('ORP', d.orp, 'мВ', '#7ab3cc') +
        row('TDS', d.tds, 'мг/л', '#7ab3cc') +
        row('Батарея', d.battery, '%', '#7ab3cc') +
        (d.flags && d.flags.length
          ? `<span style="color:#ff2d55">Флаги: ${d.flags.join(', ')}</span>`
          : '');
    } catch (err) {
      output.textContent = 'Сервер недоступен: ' + err.message;
    }
  };

  window.testConnection = async function () {
    const dot = document.getElementById('server-dot');
    const txt = document.getElementById('server-status-text');
    if (dot) dot.className = 'status-dot warning';
    if (txt) txt.textContent = 'ПРОВЕРКА...';
    try {
      const health = await api('/health');
      const stats = await api('/api/network/stats');
      setOnline(health.status === 'ok');
      alert(
        `Сервер доступен\n` +
        `Версия: ${health.version}\n` +
        `База данных: ${health.database}\n` +
        `Буёв на связи: ${stats.devices_online} из ${stats.devices_total}\n` +
        `Кадров за час: ${stats.packets_per_hour}, потерь: ${stats.loss_percent}%`
      );
    } catch (err) {
      setOnline(false);
      alert('Сервер недоступен: ' + err.message);
    }
  };

  const originalSelectBuoy = window.selectBuoy;
  window.selectBuoy = function (idx) {
    if (typeof originalSelectBuoy === 'function') originalSelectBuoy(idx);
    refreshHistory(idx).catch(() => {});
  };

  // -------------------------------------------------------------- запуск

  window.addEventListener('DOMContentLoaded', () => {
    // Кнопки выбора глубины выборки переключают запрос к серверу
    document.querySelectorAll('.time-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        const range = RANGE_BY_LABEL[btn.textContent.trim()];
        if (!range) return;
        currentRange = range;
        const group = btn.parentElement;
        if (group) group.querySelectorAll('.time-btn').forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        refreshHistory(selectedBuoy).catch(() => {});
      });
    });

    pollBackend();
    setInterval(pollBackend, POLL_INTERVAL_MS);
  });

  console.info('[caspian] источник данных: сервер' + (API || ' (тот же адрес)'));
})();
