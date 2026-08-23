(function () {
  // --- 示範行情／選擇權鏈（結構與真實 API 對齊，之後可替換） ---
  function hash(s) {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }
  function mulberry32(a) {
    return function () {
      let t = (a += 0x6d2b79f5);
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function roundStrike(x, step) {
    return Math.round(x / step) * step;
  }
  function normCdf(x) {
    const t = 1 / (1 + 0.2316419 * Math.abs(x));
    const d = 0.3989423 * Math.exp(-0.5 * x * x);
    let p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))));
    if (x > 0) p = 1 - p;
    return p;
  }
  // 簡化 Black-Scholes call 近似，只為示範排序／著色
  function callApprox(S, K, T, sigma, r) {
    if (T <= 0) return Math.max(0, S - K);
    const vol = Math.max(0.05, sigma);
    const sqrtT = Math.sqrt(T);
    const d1 = (Math.log(S / K) + (r + 0.5 * vol * vol) * T) / (vol * sqrtT);
    const d2 = d1 - vol * sqrtT;
    const nd1 = normCdf(d1);
    const premium = S * nd1 - K * Math.exp(-r * T) * normCdf(d2);
    return { premium: Math.max(0.01, premium), delta: Math.max(0.01, Math.min(0.99, nd1)) };
  }

  function buildMockChain(symbol) {
    const rand = mulberry32(hash(symbol.toUpperCase() + ':cc1'));
    const baseMap = { TSLA: 342.6, AAPL: 228.4, NVDA: 178.2, '2330.TW': 980 };
    const S = baseMap[symbol.toUpperCase()] || 100 + (hash(symbol) % 400);
    const chgPct = (rand() - 0.48) * 4;
    const iv = 0.38 + rand() * 0.18; // 38%~56%
    const ivChg = (rand() - 0.4) * 6;

    const today = new Date();
    const expiries = [];
    [14, 28, 35, 42, 56, 70].forEach(function (dte) {
      const d = new Date(today);
      d.setDate(d.getDate() + dte);
      // 對齊到週五感
      const day = d.getDay();
      const add = (5 - day + 7) % 7;
      d.setDate(d.getDate() + add);
      const realDte = Math.max(1, Math.round((d - today) / 86400000));
      expiries.push({ date: d.toISOString().slice(0, 10), dte: realDte });
    });

    const step = S > 500 ? 10 : S > 200 ? 5 : 2.5;
    const strikes = [];
    for (let i = -6; i <= 12; i++) {
      strikes.push(+roundStrike(S * (1 + i * 0.025), step).toFixed(2));
    }
    // unique sorted
    const uniq = Array.from(new Set(strikes)).sort(function (a, b) { return a - b; });

    const calls = [];
    expiries.forEach(function (ex) {
      const T = ex.dte / 365;
      uniq.forEach(function (K) {
        const approx = callApprox(S, K, T, iv, 0.04);
        // 加一點噪音與 OI
        const noise = 1 + (rand() - 0.5) * 0.08;
        const premium = +(approx.premium * noise).toFixed(2);
        const delta = +Math.min(0.95, Math.max(0.02, approx.delta + (rand() - 0.5) * 0.03)).toFixed(3);
        const oi = Math.floor(200 + rand() * 18000 + (Math.abs(K - S) < S * 0.05 ? rand() * 12000 : 0));
        const volume = Math.floor(rand() * oi * 0.35);
        const otmPct = +(((K - S) / S) * 100).toFixed(2);
        const premPct = +((premium / S) * 100).toFixed(2);
        calls.push({
          expiry: ex.date,
          dte: ex.dte,
          strike: K,
          premium: premium,
          delta: delta,
          oi: oi,
          volume: volume,
          otmPct: otmPct,
          premPct: premPct,
        });
      });
    });

    const support = +(S * (0.88 + rand() * 0.04)).toFixed(2);
    const resist = +(S * (1.12 + rand() * 0.06)).toFixed(2);
    const stretch = +(S * (1.28 + rand() * 0.08)).toFixed(2);
    const sma20 = +(S * (0.94 + rand() * 0.04)).toFixed(2);

    return {
      symbol: symbol.toUpperCase(),
      source: 'mock',
      asOf: new Date().toISOString(),
      quote: {
        price: +S.toFixed(2),
        changePct: +chgPct.toFixed(2),
        iv: +(iv * 100).toFixed(1),
        ivChg: +ivChg.toFixed(1),
      },
      expiries: expiries,
      strikes: uniq,
      calls: calls,
      levels: [
        { role: '延伸／偏樂觀', price: stretch },
        { role: '壓力', price: resist },
        { role: '現價', price: +S.toFixed(2) },
        { role: 'SMA20（示意）', price: sma20 },
        { role: '支撐', price: support },
      ],
    };
  }

  const RISK = [
    { key: 'conservative', label: '保守', targetDelta: 0.2, dteMin: 28, dteMax: 45, minOtm: 8 },
    { key: 'balanced', label: '平衡', targetDelta: 0.3, dteMin: 28, dteMax: 45, minOtm: 5 },
    { key: 'aggressive', label: '積極', targetDelta: 0.35, dteMin: 21, dteMax: 45, minOtm: 2 },
  ];

  function scoreCall(c, risk, spot) {
    // 越接近目標 delta、落在 DTE 窗、有足夠價外、權利金別太薄
    const dDelta = Math.abs(c.delta - risk.targetDelta);
    const inDte = c.dte >= risk.dteMin && c.dte <= risk.dteMax ? 0 : Math.min(Math.abs(c.dte - 35) / 35, 1);
    const otmPenalty = c.otmPct < risk.minOtm ? (risk.minOtm - c.otmPct) / 10 : 0;
    const itmPenalty = c.strike <= spot ? 2 : 0;
    const premBoost = Math.min(c.premPct / 3, 1); // 權利金厚度加分，但權重低於不被叫走
    return dDelta * 2.2 + inDte * 0.8 + otmPenalty * 1.2 + itmPenalty - premBoost * 0.25;
  }

  function pickRecommendations(chain, riskIdx) {
    const risk = RISK[riskIdx];
    const spot = chain.quote.price;
    const ranked = chain.calls
      .filter(function (c) { return c.strike > spot; })
      .map(function (c) { return { c: c, score: scoreCall(c, risk, spot) }; })
      .sort(function (a, b) { return a.score - b.score; });

    const picked = [];
    const usedExp = {};
    for (let i = 0; i < ranked.length && picked.length < 3; i++) {
      const item = ranked[i];
      // 盡量分散到期日
      if (usedExp[item.c.expiry] && picked.length < 2) continue;
      usedExp[item.c.expiry] = true;
      picked.push(item);
    }
    return picked;
  }

  function inComfortBand(c, riskIdx, spot) {
    const risk = RISK[riskIdx];
    if (c.strike <= spot) return false;
    if (c.dte < risk.dteMin - 7 || c.dte > risk.dteMax + 14) return false;
    return Math.abs(c.delta - risk.targetDelta) <= 0.08;
  }

  // --- UI ---
  const els = {
    symbol: document.getElementById('symbol-input'),
    btn: document.getElementById('btn-load'),
    price: document.getElementById('spot-price'),
    chg: document.getElementById('spot-chg'),
    meta: document.getElementById('spot-meta'),
    slider: document.getElementById('risk-slider'),
    tags: document.querySelectorAll('.risk-tags span'),
    recList: document.getElementById('rec-list'),
    chainHead: document.querySelector('#chain-table thead'),
    chainBody: document.querySelector('#chain-table tbody'),
    chainHint: document.getElementById('chain-hint'),
    levelsBody: document.querySelector('#levels-table tbody'),
    alerts: document.getElementById('alerts-list'),
    sourcePill: document.getElementById('data-source-pill'),
  };

  let chain = null;

  function riskIdx() {
    return +els.slider.value;
  }

  function syncRiskTags() {
    const v = riskIdx();
    els.tags.forEach(function (t) {
      t.classList.toggle('on', +t.getAttribute('data-v') === v);
    });
  }

  function renderQuote() {
    const q = chain.quote;
    els.price.textContent = q.price.toFixed(2);
    const up = q.changePct >= 0;
    els.chg.textContent = (up ? '+' : '') + q.changePct.toFixed(2) + '%';
    els.chg.className = 'chg ' + (up ? 'up' : 'down');
    els.meta.textContent = 'ATM IV ' + q.iv + '%（近況 ' + (q.ivChg >= 0 ? '+' : '') + q.ivChg + '）';
    els.sourcePill.textContent = chain.source === 'mock' ? '示範資料' : chain.source;
  }

  function renderRecs() {
    const picks = pickRecommendations(chain, riskIdx());
    els.recList.innerHTML = '';
    if (!picks.length) {
      els.recList.innerHTML = '<div class="muted">目前沒有符合條件的候選</div>';
      return;
    }
    picks.forEach(function (p, i) {
      const c = p.c;
      const div = document.createElement('div');
      div.className = 'rec' + (i === 0 ? ' top' : '');
      const keepApprox = Math.round((1 - c.delta) * 100);
      div.innerHTML =
        '<div class="title">' + (i === 0 ? '首選 · ' : '候選 ' + (i + 1) + ' · ') +
        c.expiry + ' / ' + c.strike + '</div>' +
        '<div class="row"><span>價外</span><b>' + c.otmPct + '%</b></div>' +
        '<div class="row"><span>DTE</span><b>' + c.dte + ' 天</b></div>' +
        '<div class="row"><span>Delta</span><b>' + c.delta + '</b></div>' +
        '<div class="row"><span>權利金（示意）</span><b>$' + c.premium + '（' + c.premPct + '%）</b></div>' +
        '<div class="row"><span>粗估「留股」機會</span><b>約 ' + keepApprox + '%</b></div>' +
        '<div class="why">較接近你選的「' + RISK[riskIdx()].label + '」尺：目標 Δ≈' +
        RISK[riskIdx()].targetDelta + '，並落在常見 30～45 天附近。</div>';
      els.recList.appendChild(div);
    });
  }

  function heatColor(premPct, maxP) {
    const t = Math.max(0, Math.min(1, premPct / (maxP || 1)));
    // dark green -> bright
    const g = Math.round(60 + t * 140);
    return 'rgba(34,' + g + ',94,' + (0.15 + t * 0.55) + ')';
  }

  function renderChain() {
    const exps = chain.expiries.slice().sort(function (a, b) { return a.dte - b.dte; });
    const byKey = {};
    let maxPremPct = 0;
    chain.calls.forEach(function (c) {
      byKey[c.expiry + '|' + c.strike] = c;
      if (c.premPct > maxPremPct) maxPremPct = c.premPct;
    });

    let head = '<tr><th>履約價 \\ 到期</th>';
    exps.forEach(function (e) {
      head += '<th>' + e.date.slice(5) + '<br><span style="color:#8b9bb0;font-weight:500">' + e.dte + 'd</span></th>';
    });
    head += '</tr>';
    els.chainHead.innerHTML = head;

    const spot = chain.quote.price;
    let body = '';
    chain.strikes.forEach(function (K) {
      body += '<tr><td class="strike">' + K + '</td>';
      exps.forEach(function (e) {
        const c = byKey[e.date + '|' + K];
        if (!c) {
          body += '<td>—</td>';
          return;
        }
        const band = inComfortBand(c, riskIdx(), spot);
        const near = Math.abs(K - spot) / spot < 0.02;
        const cls = 'cell' + (band ? ' in-band' : '') + (near ? ' near-spot' : '');
        const title = 'Δ ' + c.delta + ' · 權利金 $' + c.premium + ' · OI ' + c.oi;
        body +=
          '<td class="' + cls + '" style="background:' + heatColor(c.premPct, maxPremPct) + '" title="' + title + '">' +
          '<div>$' + c.premium + '</div>' +
          '<div style="opacity:.8">Δ' + c.delta + '</div>' +
          '</td>';
      });
      body += '</tr>';
    });
    els.chainBody.innerHTML = body;
    els.chainHint.textContent =
      '黃色靠左線≈接近現價；藍框≈落在目前風險偏好的舒適帶。數字為示意權利金與 Delta。';
  }

  function renderLevels() {
    const spot = chain.quote.price;
    els.levelsBody.innerHTML = '';
    chain.levels.forEach(function (lv) {
      const dist = ((lv.price - spot) / spot) * 100;
      const tr = document.createElement('tr');
      tr.innerHTML =
        '<td>' + lv.role + '</td><td>' + lv.price.toFixed(2) + '</td><td>' +
        (dist >= 0 ? '+' : '') + dist.toFixed(1) + '%</td>';
      els.levelsBody.appendChild(tr);
    });
  }

  function renderAlerts() {
    const q = chain.quote;
    const items = [];
    if (q.iv >= 45) {
      items.push({ cls: 'ok', text: 'IV 偏高（' + q.iv + '%）：權利金相對較厚，賣方可留意，但仍要防大波動。' });
    } else {
      items.push({ cls: '', text: 'IV 中等（' + q.iv + '%）：權利金普通，可比較各到期哪個比較厚。' });
    }
    if (q.ivChg >= 3) {
      items.push({ cls: 'warn', text: '近況 IV 上升較快（+' + q.ivChg + '）：市場在為更大波動定價，被叫走／回撤風險同步上升。' });
    }
    // 找 OI 特別高的 call
    const topOi = chain.calls.slice().sort(function (a, b) { return b.oi - a.oi; })[0];
    if (topOi) {
      items.push({
        cls: 'warn',
        text: 'Call OI 較高堆疊示意：' + topOi.expiry + ' / ' + topOi.strike + '（OI≈' + topOi.oi.toLocaleString() + '）。此帶可能成為磁鐵價。',
      });
    }
    items.push({ cls: '', text: '第一版尚無財報日曆；之後可自動標出「跨越財報的到期」提醒避開。' });

    els.alerts.innerHTML = '';
    items.forEach(function (it) {
      const li = document.createElement('li');
      li.className = it.cls;
      li.textContent = it.text;
      els.alerts.appendChild(li);
    });
  }

  function renderAll() {
    renderQuote();
    renderRecs();
    renderChain();
    renderLevels();
    renderAlerts();
  }

  function fetchChain(symbol) {
    // 只有透過本機 server 開啟時才有真實資料
    return fetch('/api/chain?symbol=' + encodeURIComponent(symbol))
      .then(function (res) {
        return res.json().then(function (data) {
          if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
          return data;
        });
      });
  }

  function loadSymbol(sym) {
    const s = String(sym || 'TSLA').trim().toUpperCase() || 'TSLA';
    els.symbol.value = s;
    els.meta.textContent = '載入中…';
    els.sourcePill.textContent = '載入中';
    fetchChain(s)
      .then(function (data) {
        chain = data;
        // 統一欄位保險
        if (!chain.levels) chain.levels = [];
        renderAll();
      })
      .catch(function (err) {
        console.warn('真實資料失敗，改用示範資料', err);
        chain = buildMockChain(s);
        chain.note = '真實資料暫不可用（' + (err.message || err) + '），已改用示範資料。請用「開啟網站.bat」啟動。';
        renderAll();
        const tip = document.createElement('li');
        tip.className = 'warn';
        tip.textContent = chain.note;
        els.alerts.insertBefore(tip, els.alerts.firstChild);
      });
  }

  els.btn.addEventListener('click', function () { loadSymbol(els.symbol.value); });
  els.symbol.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') loadSymbol(els.symbol.value);
  });
  els.slider.addEventListener('input', function () {
    syncRiskTags();
    if (chain) {
      renderRecs();
      renderChain();
    }
  });

  syncRiskTags();
  loadSymbol('TSLA');
})();
