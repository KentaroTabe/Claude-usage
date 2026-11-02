// 活動の再生画面。セッションを星、プロジェクトを星雲として描く。
// 層(奥から): 遠い星(2D・静止)→ 星雲(WebGL)→ 関係の線・星・火花・波紋・文字(2D)。光は加算合成し、縮小した層を重ねて にじみを出す。
// 星雲は最初に WebGL で高解像度の画像を生成して持ち、毎コマはその画像を「どこまで広がったか」で切り抜いて描く(成長の演出)。
// 星雲の姿は種類を 1 つに決めず、8 つの形の成分を、そのプロジェクトの特徴の比率で混ぜて作る(全く同じ星雲はできない)。
// どの星雲も同じ「生まれたての姿」で生まれ、使うほどそのプロジェクトの姿へ移り変わる。動きがなくなると、くすんだ赤茶の死んだ姿へ衰える。
// 再生の時刻 t は刻みの番号(小数)。星の定位置は固定で、生まれたときだけプロジェクトの中心から弾むように飛び出す。
// カメラは、それまでに生まれたものが入る範囲へ広がるだけで、狭まらない(小刻みに動かさない)。
(() => {
  "use strict";
  const data = JSON.parse(document.getElementById("activity-data").textContent);
  const TAU = Math.PI * 2;

  // 見た目の定数
  const HEAT_STOPS = [[58, 42, 107], [138, 42, 134], [217, 69, 63], [249, 154, 30], [255, 247, 200]];  // 星の温度
  const SPRITE_LEVELS = 40;      // 光の素材を用意する段階の数
  const BINS_PER_SECOND = 24;    // 標準の再生の速さ(1 日が約 4 秒)
  const IDLE_GAP_BINS = 8;       // 早送りを選んだとき: 次の応答までこの刻み数より空いていて、動いている星もなければ早送りする
  const IDLE_BOOST = 36;
  const ORBIT_RATE = TAU / (96 * 10);  // 星が星雲の中を回る速さ(10 日で 1 周)。位置は再生の時刻で決まるので、巻き戻しても同じ
  const MAX_FLARES = 120;
  const MAX_SPARKS = 1500;
  const SLOW_FRAME_MS = 42;      // 1 コマの平均がこれより遅ければ解像度を下げる
  const SPRING = 16, DAMPING = 4.5;  // 星が定位置へ戻るばね(減衰を弱くして弾ませる)
  const ACTIVE_LEVEL = 0.15;     // この段階以上を「動いている」とする
  const VERSION_CHECK_MS = 300000;  // 新しい記録がないか確かめる間隔
  const startAtNow = location.hash === "#now";   // 新しい記録を読み直したときは、再生せず今の状態から見る
  const TILE = 256;
  const YOUNG_KIND = 8, YOUNG_SEED = 4242;   // 生まれたての姿(シェーダーの kind の番号)と、その形の種              // 星雲の画像は小さく分けて生成し、1 コマの負荷を抑える
  const BAKE_BUDGET_MS = 10;     // 1 コマで星雲の生成に使う時間
  const $ = (id) => document.getElementById(id);
  const canvas = $("sky");
  const ctx = canvas.getContext("2d");
  const glow = document.createElement("canvas");     // 縮小した光の層(にじみ)
  const gx = glow.getContext("2d");
  const glowWide = document.createElement("canvas");  // さらに縮小した層(広いにじみ)
  const wx = glowWide.getContext("2d");
  const stars = $("stars");                           // 遠い星(静止)
  const deep = $("deep");                             // 星雲(WebGL)
  const gl = deep.getContext("webgl2", { alpha: true, premultipliedAlpha: true, antialias: false, powerPreference: "high-performance" });
  const logTop = Math.log1p(data.top);
  const start = new Date(data.start).getTime();
  const binMs = data.binMinutes * 60000;
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const lerp = (a, b, f) => a + (b - a) * f;
  const levelOf = (heat) => Math.min(1, Math.log1p(heat) / logTop);

  // 再現できる乱数(星雲の形が読み込みごとに変わらないように)
  function seeded(seed) {
    let s = seed >>> 0;
    return () => {
      s = (s + 0x6d2b79f5) >>> 0;
      let x = Math.imul(s ^ (s >>> 15), 1 | s);
      x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x;
      return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
    };
  }

  function heatRgb(level) {
    const f = clamp(level, 0, 0.9999) * (HEAT_STOPS.length - 1);
    const i = Math.floor(f);
    return HEAT_STOPS[i].map((c, k) => Math.round(lerp(c, HEAT_STOPS[i + 1][k], f - i)));
  }

  // 素材: 段階ごとの光の玉
  function makeSprite(rgb) {
    const size = 64;
    const c = document.createElement("canvas");
    c.width = c.height = size;
    const g = c.getContext("2d");
    const grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    grad.addColorStop(0, `rgba(${rgb},1)`);
    grad.addColorStop(0.22, `rgba(${rgb},0.55)`);
    grad.addColorStop(0.55, `rgba(${rgb},0.12)`);
    grad.addColorStop(1, `rgba(${rgb},0)`);
    g.fillStyle = grad;
    g.fillRect(0, 0, size, size);
    return c;
  }
  const sprites = Array.from({ length: SPRITE_LEVELS + 1 }, (_, i) => makeSprite(heatRgb(i / SPRITE_LEVELS)));
  const spriteFor = (level) => sprites[Math.round(clamp(level, 0, 1) * SPRITE_LEVELS)];
  const tone = (level) => Math.pow(level, 1.5);  // 段階を星の温度にする(白くなるのは本当に忙しいときだけ)

  // データの準備
  function series(rows) {
    const bins = rows.map((r) => r[0]);
    const cumN = [], cumUsd = [], heat = [];
    let n = 0, usd = 0, h = 0, prev = null;
    for (const [b, count, cost] of rows) {
      n += count;
      usd += cost;
      h = (prev === null ? 0 : h * Math.pow(data.decay, b - prev)) + count;
      prev = b;
      cumN.push(n); cumUsd.push(usd); heat.push(h);
    }
    return { bins, rows, cumN, cumUsd, heat };
  }
  function upper(sorted, value) {   // value 以下の要素の数
    let lo = 0, hi = sorted.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (sorted[mid] <= value) lo = mid + 1; else hi = mid;
    }
    return lo;
  }
  const heatAt = (r, time) => {
    const i = upper(r.bins, time) - 1;
    return i < 0 ? 0 : r.heat[i] * Math.pow(data.decay, time - r.bins[i]);
  };
  const cumAt = (values, r, time) => {
    const i = upper(r.bins, time) - 1;
    return i < 0 ? 0 : values[i];
  };

  const fadeHeat = Math.expm1(data.fade * logTop);
  const projects = data.projects.map((p, i) => {
    const r = series(p.b);
    return { i, name: p.name, x: p.x, y: p.y, r, radius: p.r, shape: p.shape, look: p.look, seed: p.seed, first: Infinity, cursor: 0, level: 0,
      flash: 0, usd: 0, born: false, growth: 0, maturity: 0, dead: 0, mix: p.mix, spin: (p.seed % 628) / 100, turn: (p.seed % 2 ? 1 : -1) * ORBIT_RATE * (0.7 + (p.seed % 7) / 10),
      texture: null };
  });
  const sessions = data.sessions.map((s, i) => {
    const r = series(s.b);
    const p = projects[s.p];
    const first = r.bins[0], last = r.bins[r.bins.length - 1];
    const lastHeat = r.heat[r.heat.length - 1];
    const gone = last + Math.max(0, Math.log(fadeHeat / lastHeat) / Math.log(data.decay));
    p.first = Math.min(p.first, first);
    return { i, p, r, first, last, gone, ux: s.ux, uy: s.uy, hx: p.x, hy: p.y, x: p.x, y: p.y, vx: 0, vy: 0, level: 0, cursor: 0, alive: false,
      label: s.label, phase: (i * 1.7) % TAU, links: new Set(), usd: 0, born: false };
  });
  for (const p of projects) {
    if (p.r.bins.length) p.first = Math.min(p.first, p.r.bins[0]);
    p.active = [...new Set([...p.r.bins, ...sessions.filter((s) => s.p === p).flatMap((s) => s.r.bins)])].sort((a, b) => a - b);
  }
  const edges = data.edges.map((e, i) => {
    const a = sessions[e.a], b = sessions[e.b];
    a.links.add(b); b.links.add(a);
    return { a, b, k: e.k, since: Math.max(a.first, b.first), until: Math.min(a.gone, b.gone), drawn: 0,
      bow: (i % 2 ? 1 : -1) * 0.16, phase: (i * 0.37) % 1 };
  });
  const STYLE = { continued: [1.6, []], pr: [1.2, []], branch: [1, [5, 5]], file: [0.8, [1.5, 5]] };
  const activeBins = [...new Set([...sessions, ...projects].flatMap((x) => x.r.bins))].sort((a, b) => a - b);
  const world = {
    minX: Math.min(...projects.map((p) => p.x - p.radius)), maxX: Math.max(...projects.map((p) => p.x + p.radius)),
    minY: Math.min(...projects.map((p) => p.y - p.radius)), maxY: Math.max(...projects.map((p) => p.y + p.radius)),
  };

  // 再生の状態
  const state = { t: 0, playing: false, speed: 1, boost: 1, from: 0, to: data.bins, shownBin: -1, skipIdle: false };
  const cam = { x: 0, y: 0, z: 1, manual: false, box: null };
  const sparks = [], ripples = [], flares = [];
  let W = 0, H = 0, DPR = 1, maxDpr = 2, fit = 1, hovered = null, pointer = null, clock = 0, last = performance.now();
  let frameMs = 16, touched = false;   // touched: 利用者が操作した(最初の自動再生をしない)

  function placeHome(s, time) {   // 星の定位置: 星雲の中をゆっくり回る。星雲が育つと外へ広がる
    const when = Math.min(time, s.gone);
    const a = s.p.turn * when, c = Math.cos(a), n = Math.sin(a);
    const reach = s.p.radius * Math.max(s.p.growth, 0.22);
    s.hx = s.p.x + (s.ux * c - s.uy * n) * reach;
    s.hy = s.p.y + (s.ux * n + s.uy * c) * reach;
  }
  function ignite(s) {    // 星が生まれる: プロジェクトの中心から弾むように飛び出す
    placeHome(s, state.t);
    s.x = s.p.x; s.y = s.p.y;
    const a = Math.random() * TAU;
    s.vx = Math.cos(a) * 120; s.vy = Math.sin(a) * 120;
    ripples.push({ x: s.hx, y: s.hy, r: 4, life: 1, speed: 90, level: 0.8 });
  }
  function sparkle(x, y, count, level) {
    for (let i = 0; i < count && sparks.length < MAX_SPARKS; i++) {
      const a = Math.random() * TAU, v = 40 + Math.random() * 130, life = 0.5 + Math.random() * 0.9;
      sparks.push({ x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, life, max: life, level: clamp(level + (Math.random() - 0.5) * 0.25, 0.25, 1) });
    }
  }
  // 活動の光: 同じ場所で爆発させず、その時点のガスの広がりの中のあちこちで灯る(星形成領域のように)
  function flare(x, y, spread, level, size) {
    if (flares.length >= MAX_FLARES) return;
    const a = Math.random() * TAU, d = spread * Math.sqrt(Math.random());
    const fx = x + Math.cos(a) * d, fy = y + Math.sin(a) * d;
    const life = 0.9 + Math.random() * 1.1;
    flares.push({ x: fx, y: fy, life, max: life, size: size * (0.7 + Math.random() * 0.6), level });
    sparkle(fx, fy, 2, level);
  }
  function advance(time) {   // 再生で通り過ぎた応答を火花にする
    for (const s of sessions) {
      const b = s.r.bins;
      while (s.cursor < b.length && b[s.cursor] <= time) {
        if (s.cursor === 0) ignite(s);
        const n = s.r.rows[s.cursor][1];
        const reach = s.p.radius * s.p.growth;
        const count = Math.min(4, 1 + Math.floor(Math.log2(1 + n) / 2));
        for (let i = 0; i < count; i++) flare(s.x, s.y, reach * 0.55, clamp(tone(s.level) + 0.3, 0.3, 1), 26 + Math.log2(1 + n) * 7);
        s.cursor++;
      }
    }
    for (const p of projects) {
      const b = p.r.bins;
      while (p.cursor < b.length && b[p.cursor] <= time) {   // 小さな実行は星雲の中心からの小さな閃光
        flare(p.x, p.y, p.radius * p.growth * 0.7, 0.5, 16);
        p.flash = Math.max(p.flash, 0.5);
        p.cursor++;
      }
    }
  }
  function seek(time) {     // 飛び移る: 通り過ぎた分の演出はせず、その時刻の状態にする
    state.t = clamp(time, 0, data.bins);
    sparks.length = 0; ripples.length = 0; flares.length = 0;
    for (const s of sessions) {
      s.cursor = upper(s.r.bins, state.t);
      s.vx = s.vy = 0;
      s.alive = state.t >= s.first && state.t <= s.gone;
      s.born = state.t >= s.first;
      s.level = levelOf(heatAt(s.r, state.t));
    }
    for (const p of projects) { p.cursor = upper(p.r.bins, state.t); p.flash = 0; p.born = state.t >= p.first; }
    for (const e of edges) e.drawn = state.t >= e.since ? 1 : 0;
    state.shownBin = -1;
    updateHud();
    for (const p of projects) { p.growth = growthOf(p); p.maturity = maturityOf(p); p.dead = deadOf(p); }
    for (const s of sessions) { placeHome(s, state.t); s.x = s.hx; s.y = s.hy; }
    if (!cam.manual) aimCamera(1);
    drawTimeline();
  }

  const fmtDate = new Intl.DateTimeFormat("ja-JP", { timeZone: data.timeZone, year: "numeric", month: "long", day: "numeric", weekday: "short" });
  const fmtTime = new Intl.DateTimeFormat("ja-JP", { timeZone: data.timeZone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const fmtNumber = new Intl.NumberFormat("ja-JP");

  function updateHud() {     // 文字は刻みが変わったときだけ更新する
    const bin = Math.floor(state.t);
    if (bin === state.shownBin) return;
    state.shownBin = bin;
    const when = new Date(start + bin * binMs);
    $("date").textContent = fmtDate.format(when);
    $("time").textContent = fmtTime.format(when);
    let n = 0, usd = 0, active = 0;
    for (const x of sessions) {
      n += cumAt(x.r.cumN, x.r, bin); x.usd = cumAt(x.r.cumUsd, x.r, bin); usd += x.usd;
      if (x.alive && levelOf(heatAt(x.r, state.t)) >= ACTIVE_LEVEL) active++;
    }
    let born = 0;
    for (const p of projects) {
      n += cumAt(p.r.cumN, p.r, bin); p.usd = cumAt(p.r.cumUsd, p.r, bin); usd += p.usd;
      if (state.t >= p.first) born++;
    }
    for (const s of sessions) s.p.usd += s.usd;
    $("active").textContent = active;
    $("projects").textContent = born;
    $("responses").textContent = fmtNumber.format(n);
    $("usd").textContent = "$" + fmtNumber.format(Math.round(usd));
  }

  // 星雲の広がり(0〜1)。半径は使った分だけで決まる絶対の値で、今の半径に対する割合まで広がる。これからも使うほど大きくなる
  const radiusAt = (usd) => data.radius[0] + data.radius[1] * Math.cbrt(usd);
  const growthOf = (p) => clamp(radiusAt(p.usd) / p.radius, 0.05, 1);
  // そのプロジェクトの姿への移り変わり(0: 生まれたて〜1: その姿)。使うほど進むので、小さな試しは生まれたての姿に近いまま
  const maturityOf = (p) => clamp(Math.log1p(p.usd) / Math.log1p(data.matureUsd), 0, 1);
  // 衰え(0〜1)。最後の応答からの時間で決まり、また使えば戻る
  const deadOf = (p) => {
    const i = upper(p.active, state.t) - 1;
    if (i < 0) return 0;
    const idle = state.t - p.active[i];
    const f = clamp((idle - data.dormantBins) / (data.deadBins - data.dormantBins), 0, 1);
    return f * f * (3 - 2 * f);
  };

  function isIdle() {
    if (!state.skipIdle) return false;
    const next = activeBins[upper(activeBins, state.t)];
    if (next === undefined || next - state.t < IDLE_GAP_BINS) return false;
    return !sessions.some((s) => s.alive && s.level >= ACTIVE_LEVEL);
  }

  function viewBox() {   // 今の時刻までに生まれたものが入る範囲
    let box = null;
    const add = (x, y, pad) => {
      box = box || { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity };
      box.minX = Math.min(box.minX, x - pad); box.maxX = Math.max(box.maxX, x + pad);
      box.minY = Math.min(box.minY, y - pad); box.maxY = Math.max(box.maxY, y + pad);
    };
    for (const p of projects) if (state.t >= p.first) add(p.x, p.y, radiusAt(p.usd) + 24);
    return box;
  }
  function aimCamera(rate) {  // 視界は広がるだけで、狭まらない。広がるときだけゆっくり動く
    const box = viewBox() || world;
    cam.box = !cam.box ? box : { minX: Math.min(cam.box.minX, box.minX), maxX: Math.max(cam.box.maxX, box.maxX),
      minY: Math.min(cam.box.minY, box.minY), maxY: Math.max(cam.box.maxY, box.maxY) };
    const b = cam.box;
    const z = clamp(Math.min(W / (b.maxX - b.minX), (H - 190) / (b.maxY - b.minY)), fit * 0.9, fit * 2.2);
    cam.x = lerp(cam.x, (b.minX + b.maxX) / 2, rate);
    cam.y = lerp(cam.y, (b.minY + b.maxY) / 2 + 30 / z, rate);
    cam.z = lerp(cam.z, z, rate);
  }

  const sx = (x) => W / 2 + (x - cam.x) * cam.z;
  const sy = (y) => H / 2 + (y - cam.y) * cam.z;

  function simulate(elapsed) {
    const dt = Math.min(0.05, elapsed);     // 動きの計算は小さな刻みで。再生の時刻は実際の経過時間で進める
    clock += dt;
    if (state.playing) {
      const target = isIdle() ? IDLE_BOOST : 1;
      state.boost = lerp(state.boost, target, Math.min(1, elapsed * (target > state.boost ? 1.5 : 8)));
      state.t += elapsed * BINS_PER_SECOND * state.speed * state.boost;
      if (state.t >= state.to) {
        state.t = state.to;
        setPlaying(false);
      }
      advance(state.t);
      drawTimeline();
    } else {
      state.boost = 1;
    }
    $("skip").hidden = state.boost < 4;
    for (const p of projects) {
      if (!p.born && state.t >= p.first) {   // 新しいプロジェクトの誕生は大きな波紋で見せる
        p.born = true;
        p.flash = 1.6;
        ripples.push({ x: p.x, y: p.y, r: 8, life: 1, speed: 210, level: 0.9 }, { x: p.x, y: p.y, r: 2, life: 0.8, speed: 120, level: 0.6 });
      }
      p.level = 0;
      p.flash = Math.max(0, p.flash - dt * 0.5);
      p.growth = lerp(p.growth, growthOf(p), Math.min(1, dt * 2.5));
      p.maturity = lerp(p.maturity, maturityOf(p), Math.min(1, dt * 1.5));
      const fading = deadOf(p);
      p.dead = lerp(p.dead, fading, Math.min(1, dt * (fading < p.dead ? 4 : 1.2)));   // 生き返るときは早く
    }
    for (const s of sessions) {
      const alive = state.t >= s.first && state.t <= s.gone;
      if (s.alive && !alive && state.playing) sparkle(s.x, s.y, 6, 0.4);  // 消える星は星雲へ溶ける
      s.alive = alive;
      s.born = s.born || state.t >= s.first;
      if (!alive) { s.level = 0; placeHome(s, state.t); s.x = s.hx; s.y = s.hy; continue; }
      placeHome(s, state.t);
      const target = levelOf(heatAt(s.r, state.t));
      s.level = lerp(s.level, target, Math.min(1, dt * 9));
      s.p.level = Math.max(s.p.level, s.level);
      const wob = 1.5 + s.level * 7;
      const hx = s.hx + Math.cos(clock * 0.7 + s.phase) * wob, hy = s.hy + Math.sin(clock * 0.9 + s.phase) * wob;
      s.vx += ((hx - s.x) * SPRING - s.vx * DAMPING) * dt;
      s.vy += ((hy - s.y) * SPRING - s.vy * DAMPING) * dt;
      s.x += s.vx * dt; s.y += s.vy * dt;
    }
    for (const e of edges) {
      if (state.t >= e.since && e.drawn < 1) e.drawn = Math.min(1, e.drawn + dt * 1.2);
    }
    for (let i = sparks.length - 1; i >= 0; i--) {
      const p = sparks[i];
      p.life -= dt;
      if (p.life <= 0) { sparks[i] = sparks[sparks.length - 1]; sparks.pop(); continue; }
      p.vx -= p.vx * 1.8 * dt; p.vy -= p.vy * 1.8 * dt;
      p.x += p.vx * dt; p.y += p.vy * dt;
    }
    for (let i = flares.length - 1; i >= 0; i--) {
      const f = flares[i];
      f.life -= dt;
      if (f.life <= 0) { flares[i] = flares[flares.length - 1]; flares.pop(); }
    }
    for (let i = ripples.length - 1; i >= 0; i--) {
      const r = ripples[i];
      r.life -= dt * 0.9; r.r += r.speed * dt;
      if (r.life <= 0) ripples.splice(i, 1);
    }
    if (!cam.manual) aimCamera(Math.min(1, dt * 0.5));
  }

  // 星雲: WebGL。生成(bake)のシェーダーは、ねじった雑音を重ねてガスの濃淡・暗い筋・細い筋・埋もれた星を作る
  const BAKE_VS = `#version 300 es
  out vec2 uv;
  void main() {
    vec2 p = vec2(float(gl_VertexID & 1) * 2. - 1., float((gl_VertexID >> 1) & 1) * 2. - 1.);
    uv = p;
    gl_Position = vec4(p, 0., 1.);
  }`;
  const BAKE_FS = `#version 300 es
  precision highp float;
  precision highp int;
  in vec2 uv;
  out vec4 o;
  uniform vec2 seed;
  uniform int kind;                      // 8: 生まれたての姿。それ以外は成分を混ぜた姿
  uniform float tilt, squash, shift;
  uniform vec4 w0, w1;                   // 形の成分の重み: (散光, 柱, 環状, 双極), (残骸, 反射, 渦巻, 暗黒)
  uniform float turb, dustAmt, fil, starAmt, core;   // 乱れ・塵・細い筋・埋もれた星・芯の明るさ(どれも 0〜1)
  uint mixHash(ivec2 c, uint salt) {
    uvec2 v = uvec2(c + ivec2(60000));
    uint h = (v.x * 1597334673U) ^ (v.y * 3812015801U) ^ (salt * 2798796415U);
    h ^= h >> 15; h *= 2246822519U; h ^= h >> 13; h *= 3266489917U; h ^= h >> 16;
    return h;
  }
  float rnd(ivec2 c, uint salt) { return float(mixHash(c, salt)) / 4294967296.; }
  vec2 grad(ivec2 c) {
    float a = rnd(c, 1U) * 6.2831853;
    return vec2(cos(a), sin(a));
  }
  float noise(vec2 p) {
    vec2 i = floor(p), f = p - i;
    vec2 u = f * f * f * (f * (f * 6. - 15.) + 10.);
    ivec2 c = ivec2(i);
    return 1.4 * mix(mix(dot(grad(c), f), dot(grad(c + ivec2(1, 0)), f - vec2(1., 0.)), u.x),
                     mix(dot(grad(c + ivec2(0, 1)), f - vec2(0., 1.)), dot(grad(c + ivec2(1, 1)), f - vec2(1., 1.)), u.x), u.y);
  }
  const mat2 TURN = mat2(1.62, 1.18, -1.18, 1.62);
  float fbm(vec2 p, int n) {
    float a = 0.5, s = 0.;
    for (int i = 0; i < n; i++) { s += a * noise(p); p = TURN * p + vec2(3.1, 1.7); a *= 0.52; }
    return s;
  }
  float ridged(vec2 p, int n) {
    float a = 0.5, s = 0., w = 1.;
    for (int i = 0; i < n; i++) {
      float v = 1. - abs(noise(p)) * 1.9;
      v *= v;
      s += a * v * w;
      w = clamp(v * 1.6, 0., 1.);
      p = TURN * p + vec2(1.3, 4.1);
      a *= 0.5;
    }
    return s;
  }
  vec3 points(vec2 p, float scale, float rate, float width, bool spikes) {
    vec2 q = p * scale;
    ivec2 c = ivec2(floor(q));
    float h = rnd(c, 7U);
    if (h < 1. - rate) return vec3(0.);
    vec2 d = fract(q) - 0.5 - (vec2(rnd(c, 11U), rnd(c, 13U)) - 0.5) * 0.7;
    float b = pow((h - (1. - rate)) / rate, 2.5);
    float light = exp(-dot(d, d) / (width * width)) * b;
    if (spikes) light += b * 0.55 * (exp(-abs(d.x) * 90.) * exp(-d.y * d.y * 34.) + exp(-abs(d.y) * 90.) * exp(-d.x * d.x * 34.));
    return light * mix(vec3(1., 0.84, 0.68), vec3(0.72, 0.84, 1.), rnd(c, 17U));
  }
  mat2 rot(float a) { float c = cos(a), s = sin(a); return mat2(c, -s, s, c); }
  vec3 hueShift(vec3 c, float a) {     // 灰色の軸のまわりに色を回す(同じ種類でも色合いが少しずつ違うように)
    const vec3 k = vec3(0.57735);
    float cs = cos(a), sn = sin(a);
    return c * cs + cross(k, c) * sn + k * dot(k, c) * (1. - cs);
  }
  // 成分ごとの形が使う共通の場(main で 1 度だけ計算する)
  vec2 D, P, W, Q;
  float RAD, ELL, BODY, FINE, CLOUD, WOB, DUST, RIM;
  vec3 emission(out float f) {      // 散光星雲(干潟星雲): 深紅のガスと青い心臓部
    float gas = exp(-WOB * WOB * 1.5) * (0.12 + 1.5 * pow(CLOUD, 2.4));
    f = WOB * 0.62 + 0.2 * (BODY + 0.3);
    return mix(vec3(0.9, 0.06, 0.16), vec3(1., 0.22, 0.42), CLOUD) * gas * 1.5 + vec3(0.15, 0.55, 0.95) * exp(-ELL * ELL * 7.) * (0.3 + CLOUD) * 0.8;
  }
  vec3 pillars(out float f) {       // 創造の柱(わし星雲): 青緑と金の空に、暗い柱が立つ
    float height = 0.12 + fbm(vec2(D.x * 3.4, D.y * 0.4) + seed, 7) * 1.25 + (FINE + BODY * 0.4) * 0.2;
    float top = D.y + 0.14 - height;
    float solid = smoothstep(0.1, -0.08, top);
    vec3 sky = mix(vec3(0.04, 0.5, 0.58), vec3(1., 0.56, 0.12), smoothstep(-0.45, 0.6, D.y + BODY * 1.2));
    float glow = exp(-RAD * RAD * 1.5);
    f = RAD * 0.72 + 0.14 * (BODY + 0.3);
    return sky * glow * (0.2 + 1.2 * CLOUD * CLOUD) * (1. - 0.9 * solid) + vec3(1., 0.62, 0.26) * exp(-top * top / 0.006) * (0.2 + CLOUD) * glow * 0.5
      + vec3(0.5, 0.14, 0.05) * solid * pow(CLOUD, 2.) * glow * 0.3;
  }
  vec3 ringNebula(out float f) {    // 環状星雲(らせん星雲): 中は青緑、外は赤
    float pe = ELL + 0.13 * BODY + 0.04 * FINE;
    float shell = pe - 0.46;
    float ring = exp(-shell * shell * 26.) * (0.35 + 1.25 * pow(CLOUD, 2.) + 0.5 * pow(ridged(Q * 2.2, 6), 2.));
    f = pe * 0.88 + 0.08 * BODY;
    return vec3(0.05, 0.42, 0.62) * exp(-pe * pe * 8.) * (0.25 + CLOUD) * 0.8
      + mix(vec3(0.22, 0.62, 0.5), vec3(0.95, 0.3, 0.12), smoothstep(0.3, 0.58, pe)) * ring * 1.1
      + vec3(0.7, 0.05, 0.14) * exp(-pow(pe - 0.76, 2.) * 11.) * pow(CLOUD, 2.) * 0.6
      + vec3(0.85, 0.92, 1.) * exp(-RAD * RAD * 1400.) * 2.;
  }
  vec3 bipolar(out float f) {       // 双極星雲(バタフライ星雲): 腰のくびれから両側へ開く翼
    float x = abs(D.x), y = D.y + 0.1 * BODY;
    float width = 0.05 + 0.55 * x * x + 0.1 * x;
    float wing = exp(-y * y / (width * width) * 1.1) * smoothstep(1.02, 0.45, x + 0.25 * CLOUD) * smoothstep(0., 0.14, x);
    float wall = exp(-pow(abs(y) - width * 0.8, 2.) / (width * width * 0.05 + 0.0004));
    float gas = wing * (0.2 + 1.25 * pow(CLOUD, 1.7)) + wall * wing * 0.65;
    f = x * 0.82 + 0.14 * (BODY + 0.3);
    return mix(vec3(1., 0.72, 0.28), vec3(0.95, 0.16, 0.1), smoothstep(0.12, 0.7, x)) * gas * 1.45 + vec3(0.2, 0.48, 1.) * smoothstep(0.55, 0.95, x) * gas * 0.95
      + vec3(1., 0.86, 0.6) * exp(-dot(D, D) * 60.) * 1.3;
  }
  vec3 remnant(out float f) {       // 超新星残骸(網状星雲): 赤と青の細い筋の殻
    float shell = WOB - 0.66;
    float threads = pow(ridged(Q * 1.7 + W * 1.2, 9), 4.6);
    float tint = smoothstep(-0.25, 0.25, fbm(P * 0.8 + vec2(7., 1.), 4));
    f = WOB * 0.62 + 0.2 * (BODY + 0.3);
    return mix(vec3(1., 0.14, 0.2), vec3(0.14, 0.68, 1.), tint) * exp(-shell * shell * 5.) * (threads * 1.7 + 0.08 * CLOUD) * 1.6
      + vec3(0.22, 0.36, 1.) * exp(-WOB * WOB * 3.) * 0.2 * CLOUD;
  }
  vec3 reflection(out float f) {    // 反射星雲(プレアデスのまわり): 青い光の流れと明るい星
    vec2 s = vec2(D.x * 0.85, D.y * 1.7) + W * 1.1 + seed;
    float streak = clamp(0.5 + fbm(s, 8) * 1.1 + FINE * 0.22, 0., 1.);
    f = WOB * 0.62 + 0.2 * (BODY + 0.3);
    return mix(vec3(0.06, 0.16, 0.8), vec3(0.42, 0.7, 1.), streak) * exp(-WOB * WOB * 1.9) * (0.1 + 1.3 * pow(streak, 2.4)) * 1.4
      + points(uv + seed * 0.4, 7., 0.22, 0.012, true) * 1.7;
  }
  vec3 spiral(out float f) {        // 渦巻(アンドロメダ): 金の芯、青い腕、桃色の星形成領域
    vec2 g = D * vec2(1., squash * 1.5);
    float gr = length(g) + 0.0001;
    float arm = pow(0.5 + 0.5 * cos(2. * atan(g.y, g.x) - 5.2 * log(gr + 0.12) + BODY * 1.3), 2.2);
    float disc = exp(-gr * 2.6);
    f = gr * 0.95 + 0.06 * BODY;
    return mix(vec3(0.3, 0.46, 1.), vec3(0.68, 0.8, 1.), arm) * disc * (0.14 + 1.4 * arm * (0.35 + CLOUD)) * 1.25 * (1. - 0.7 * arm * DUST)
      + vec3(1., 0.2, 0.48) * pow(max(FINE + 0.12, 0.), 2.) * arm * disc * 5. + vec3(1., 0.8, 0.5) * exp(-gr * gr * 22.) * 1.5;
  }
  vec3 darkCloud(out float f) {     // 暗黒星雲(馬頭星雲): 赤い幕に黒い影が立つ
    float curtain = exp(-WOB * WOB * 1.25) * (0.3 + 0.95 * pow(clamp(0.5 + fbm(vec2(D.x * 1.1, D.y * 3.) + W + seed, 8) * 1.2, 0., 1.), 1.4));
    float cut = fbm(Q * 1.15 + vec2(4., 1.), 8) * 1.7 + FINE * 0.35 + D.y * 0.8 - 0.18;
    f = WOB * 0.62 + 0.2 * (BODY + 0.3);
    return mix(vec3(0.74, 0.04, 0.12), vec3(1., 0.28, 0.36), CLOUD) * curtain * 1.3 * (1. - 0.95 * smoothstep(-0.1, 0.1, cut))
      + vec3(1., 0.42, 0.34) * exp(-cut * cut * 90.) * curtain * 0.22;
  }
  vec3 youngStar(out float f) {     // 生まれたての姿: 塵の繭に包まれた若い星。どのプロジェクトも初めはこの姿
    float cocoon = exp(-ELL * ELL * 4.2) * (0.35 + 0.9 * pow(CLOUD, 1.6));
    float jet = exp(-pow(D.x / (0.05 + 0.25 * abs(D.y)), 2.)) * exp(-abs(D.y) * 2.2) * 0.6;   // 両極へ噴き出すかすかな流れ
    f = ELL * 0.8 + 0.1 * BODY;
    return (mix(vec3(0.85, 0.32, 0.08), vec3(1., 0.66, 0.3), CLOUD) * cocoon * 1.2 + vec3(0.35, 0.55, 1.) * jet * CLOUD)
      * exp(-DUST * 1.6 * smoothstep(0.15, 0.5, ELL)) + vec3(1., 0.85, 0.6) * exp(-RAD * RAD * 160.) * 1.6;
  }
  void main() {
    RAD = length(uv);
    D = rot(tilt) * uv;                     // 星雲ごとの向き
    ELL = length(D * vec2(1., squash));
    P = uv * 1.2 + seed;
    W = vec2(fbm(P * 0.7, 4), fbm(P * 0.7 + vec2(5.2, 1.3), 4));
    Q = P + (0.45 + 0.75 * turb) * W;        // ねじれ: 使い方に波があるほど乱れる(強すぎると大理石の模様になる)
    BODY = fbm(Q, 8);
    FINE = fbm(Q * 2.3 + BODY * 1.1, 7);
    CLOUD = clamp(0.5 + BODY * 0.95 + FINE * 0.3, 0., 1.);
    WOB = ELL + 0.62 * fbm(P * 0.6 + vec2(3., 8.), 3) + 0.12 * W.x;
    float dustField = fbm(Q * 0.95 + vec2(2., 7.), 7);
    DUST = smoothstep(-0.02, 0.3, dustField);
    RIM = exp(-pow((dustField + 0.04) / 0.045, 2.));
    vec3 c = vec3(0.);
    float front = 0., total = 0., f;
    if (kind == 8) {
      c = youngStar(front);
      total = 1.;
    } else {                                // 成分を重みのとおりに足す(重みの小さい成分は計算しない)
      if (w0.x > 0.01) { c += emission(f) * w0.x; front += f * w0.x; total += w0.x; }
      if (w0.y > 0.01) { c += pillars(f) * w0.y; front += f * w0.y; total += w0.y; }
      if (w0.z > 0.01) { c += ringNebula(f) * w0.z; front += f * w0.z; total += w0.z; }
      if (w0.w > 0.01) { c += bipolar(f) * w0.w; front += f * w0.w; total += w0.w; }
      if (w1.x > 0.01) { c += remnant(f) * w1.x; front += f * w1.x; total += w1.x; }
      if (w1.y > 0.01) { c += reflection(f) * w1.y; front += f * w1.y; total += w1.y; }
      if (w1.z > 0.01) { c += spiral(f) * w1.z; front += f * w1.z; total += w1.z; }
      if (w1.w > 0.01) { c += darkCloud(f) * w1.w; front += f * w1.w; total += w1.w; }
      c /= max(total, 0.0001) * 0.55 + 0.45;   // 混ぜても暗くなりすぎないように
      c *= exp(-DUST * dustAmt * 1.6);                                            // 塵(休んでいた時間が長いほど多い)
      c += vec3(1., 0.5, 0.38) * RIM * CLOUD * exp(-WOB * WOB * 1.2) * 0.35 * dustAmt;
      if (fil > 0.05) c += mix(vec3(1., 0.2, 0.3), vec3(0.3, 0.7, 1.), CLOUD) * pow(ridged(Q * 1.7 + W * 1.2, 8), 4.6) * exp(-WOB * WOB * 1.6) * fil * 0.9;
      c += vec3(1., 0.9, 0.75) * exp(-RAD * RAD * 260.) * core * 1.1;             // 芯(使う勢いが強いほど明るい)
    }
    front /= max(total, 0.0001);
    c = max(hueShift(c, shift), 0.);
    c += (points(uv + seed, 60., 0.09, 0.07, false) * 0.7 + points(uv + seed * 1.3, 150., 0.14, 0.09, false) * 0.45) * (0.4 + 0.9 * starAmt)
      + points(uv + seed * 0.7, 11., 0.08, 0.018, true) * 1.1;
    c *= smoothstep(1., 0.56, RAD + 0.34 * abs(BODY) + 0.1 * FINE);   // 外周を不規則に消す(丸く切り抜いて見えないように)
    float peak = max(c.r, max(c.g, c.b));   // 色を保ったまま明るさだけを飽和させる(各色を別々に飽和させると白っぽく濁る)
    float shown = 1. - exp(-peak * 1.35);
    c *= shown / max(peak, 0.0001);
    c = mix(c, vec3(shown), smoothstep(0.78, 1., shown) * 0.5);
    c += (rnd(ivec2(gl_FragCoord.xy), 3U) - 0.5) / 255.;   // 暗い階調の段差を消す
    o = vec4(c, clamp(front, 0., 1.));
  }`;
  const DRAW_VS = `#version 300 es
  uniform vec2 center, halfSize, view;
  uniform float angle;
  out vec2 uv;
  void main() {
    vec2 p = vec2(float(gl_VertexID & 1) * 2. - 1., float((gl_VertexID >> 1) & 1) * 2. - 1.);
    uv = p * 0.5 + 0.5;
    float c = cos(angle), s = sin(angle);
    vec2 q = vec2(p.x * c - p.y * s, p.x * s + p.y * c) * halfSize + center;
    gl_Position = vec4(q.x / view.x * 2. - 1., 1. - q.y / view.y * 2., 0., 1.);
  }`;
  const DRAW_FS = `#version 300 es
  precision highp float;
  in vec2 uv;
  out vec4 o;
  uniform sampler2D tex, young;
  uniform float growth, gain, maturity, dead, turn;
  vec3 shown(vec4 s) {
    float a = clamp((growth - s.a) / 0.16, 0., 1.);
    return s.rgb * a * a * (3. - 2. * a);
  }
  void main() {
    vec2 c = uv - 0.5;
    float cs = cos(turn), sn = sin(turn);
    vec3 baby = shown(texture(young, vec2(c.x * cs - c.y * sn, c.x * sn + c.y * cs) + 0.5));  // 生まれたての姿は向きだけ変える
    vec3 col = mix(baby, shown(texture(tex, uv)), maturity) * gain;
    float l = dot(col, vec3(0.3, 0.55, 0.15));
    col = mix(col, vec3(l) * vec3(1., 0.64, 0.48), dead * 0.85) * (1. - 0.68 * dead);   // 死んだ姿: 色が抜けて暗いくすんだ赤茶へ
    o = vec4(col, max(col.r, max(col.g, col.b)));   // 透明度も明るさに合わせる(0 のままだと画面の合成で消える環境がある)
  }`;
  function program(vs, fs) {
    const make = (type, src) => {
      const sh = gl.createShader(type);
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(sh));
      return sh;
    };
    const pr = gl.createProgram();
    gl.attachShader(pr, make(gl.VERTEX_SHADER, vs));
    gl.attachShader(pr, make(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(pr);
    if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(pr));
    const u = new Proxy({}, { get: (cache, name) => (cache[name] ??= gl.getUniformLocation(pr, name)) });
    return { pr, u };
  }
  const baker = gl && program(BAKE_VS, BAKE_FS);
  const painter = gl && program(DRAW_VS, DRAW_FS);
  const framebuffer = gl && gl.createFramebuffer();
  const pixel = new Uint8Array(4);
  if (gl) gl.bindVertexArray(gl.createVertexArray());
  const bakeQueue = [];
  let youngTexture = null;
  function queueNebulae() {     // 早く生まれるものから順に、タイルに分けて生成する
    if (!gl) return;
    const limit = gl.getParameter(gl.MAX_TEXTURE_SIZE);
    const order = startAtNow ? (a, b) => b.radius - a.radius : (a, b) => a.first - b.first;
    const baby = { kind: YOUNG_KIND, seed: YOUNG_SEED, texSize: Math.min(limit, 1024) };
    for (const p of [baby, ...[...projects].sort(order)]) {
      const size = p.texSize || Math.min(limit, clamp(Math.round((p.radius * 7) / TILE) * TILE, 512, 2048));
      p.texSize = size;
      p.target = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, p.target);
      gl.texStorage2D(gl.TEXTURE_2D, Math.floor(Math.log2(size)) + 1, gl.RGBA8, size, size);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      for (let y = 0; y < size; y += TILE) for (let x = 0; x < size; x += TILE) bakeQueue.push({ p, x, y });
    }
    bakeQueue.baby = baby;
  }
  function bakeSome() {
    if (!bakeQueue.length) return;
    const begin = performance.now();
    gl.useProgram(baker.pr);
    gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
    gl.disable(gl.BLEND);
    gl.enable(gl.SCISSOR_TEST);
    while (bakeQueue.length && performance.now() - begin < BAKE_BUDGET_MS) {
      const { p, x, y } = bakeQueue.shift();
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, p.target, 0);
      gl.viewport(0, 0, p.texSize, p.texSize);
      gl.scissor(x, y, TILE, TILE);
      gl.uniform2f(baker.u.seed, 3.7 + (p.seed % 997) * 0.0137, 8.1 + (p.seed % 991) * 0.0113);
      const w = p.shape || [0, 0, 0, 0, 0, 0, 0, 0], look = p.look || {};
      gl.uniform1i(baker.u.kind, p.kind ?? -1);
      gl.uniform1f(baker.u.tilt, (p.seed % 628) / 100);
      gl.uniform1f(baker.u.squash, 1 + (p.seed % 50) / 100);
      // 色合い: Fable の割合が多いほど温かく、名前から決まる分だけ少しずらす
      gl.uniform1f(baker.u.shift, ((p.seed % 61) / 61 - 0.5) * 0.35 + ((look.warm ?? 0.3) - 0.3) * -0.45);
      gl.uniform4f(baker.u.w0, w[0], w[1], w[2], w[3]);
      gl.uniform4f(baker.u.w1, w[4], w[5], w[6], w[7]);
      gl.uniform1f(baker.u.turb, look.turb ?? 0.5);
      gl.uniform1f(baker.u.dustAmt, look.dust ?? 0.4);
      gl.uniform1f(baker.u.fil, look.fil ?? 0);
      gl.uniform1f(baker.u.starAmt, look.stars ?? 0.3);
      gl.uniform1f(baker.u.core, look.core ?? 0.4);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      gl.readPixels(x, y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);   // 実際に描き終わるまで待ち、時間を正しく測る
      if (!bakeQueue.length || bakeQueue[0].p !== p) {                // この星雲の生成が終わった
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        gl.bindTexture(gl.TEXTURE_2D, p.target);
        gl.generateMipmap(gl.TEXTURE_2D);
        gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
        p.texture = p.target;
        if (p === bakeQueue.baby) youngTexture = p.texture;
      }
    }
    gl.disable(gl.SCISSOR_TEST);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  }
  function drawNebulae() {
    if (!gl) return;
    bakeSome();
    gl.viewport(0, 0, deep.width, deep.height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE);
    gl.useProgram(painter.pr);
    gl.uniform2f(painter.u.view, W, H);
    gl.uniform1i(painter.u.tex, 0);
    gl.uniform1i(painter.u.young, 1);
    for (const p of projects) {
      if (!p.born || !youngTexture) continue;
      const size = p.radius * cam.z;
      const x = sx(p.x), y = sy(p.y);
      if (x < -size * 1.5 || x > W + size * 1.5 || y < -size * 1.5 || y > H + size * 1.5) continue;
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, youngTexture);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, p.texture || youngTexture);   // 種類の姿ができるまでは生まれたての姿
      gl.uniform1f(painter.u.maturity, p.texture ? p.maturity : 0);
      gl.uniform1f(painter.u.dead, p.dead);
      gl.uniform1f(painter.u.turn, p.spin);
      gl.uniform2f(painter.u.center, x, y);
      gl.uniform2f(painter.u.halfSize, size, size);
      gl.uniform1f(painter.u.angle, 0);
      gl.uniform1f(painter.u.growth, p.growth * 1.2);
      gl.uniform1f(painter.u.gain, 0.8 + tone(p.level) * 0.7 + p.flash * 0.35);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }
  }

  const emphasis = (s) => (!hovered ? 1 : s === hovered || hovered.links.has(s) ? 1 : 0.18);

  function drawEdges() {
    ctx.globalCompositeOperation = "lighter";
    ctx.lineCap = "round";
    for (const e of edges) {
      if (e.drawn <= 0) continue;
      const live = e.a.alive && e.b.alive;
      const x0 = sx(e.a.x), y0 = sy(e.a.y), x1 = sx(e.b.x), y1 = sy(e.b.y);
      const cx = (x0 + x1) / 2 - (y1 - y0) * e.bow, cy = (y0 + y1) / 2 + (x1 - x0) * e.bow;
      const heat = live ? tone(Math.sqrt(e.a.level * e.b.level)) : 0;
      const shown = Math.min(emphasis(e.a), emphasis(e.b)) * (live ? 1 : 0.32);
      const [width, dash] = STYLE[e.k];
      const f = e.drawn;       // 線は片方の星から伸びていく
      const qx = lerp(x0, cx, f), qy = lerp(y0, cy, f);
      const ex = lerp(qx, lerp(cx, x1, f), f), ey = lerp(qy, lerp(cy, y1, f), f);
      const [r, g, b] = heatRgb(0.35 + heat * 0.65);
      ctx.strokeStyle = `rgba(${r},${g},${b},${(0.16 + heat * 0.7) * shown})`;
      ctx.lineWidth = width * clamp(cam.z / fit, 1, 2);
      ctx.setLineDash(dash);
      ctx.beginPath(); ctx.moveTo(x0, y0); ctx.quadraticCurveTo(qx, qy, ex, ey); ctx.stroke();
      ctx.setLineDash([]);
      if (heat > 0.12 && f === 1) {  // 動いている組の間を光が行き来する
        const u = (clock * 0.45 + e.phase) % 1, v = 1 - u;
        const px = v * v * x0 + 2 * u * v * cx + u * u * x1, py = v * v * y0 + 2 * u * v * cy + u * u * y1;
        const size = 10 + heat * 16;
        ctx.globalAlpha = heat * shown;
        ctx.drawImage(spriteFor(0.85), px - size / 2, py - size / 2, size, size);
        ctx.globalAlpha = 1;
      }
    }
  }

  function radiusOf(s) {
    return (2.2 + Math.cbrt(s.usd) * 0.95) * clamp(cam.z / fit, 1, 1.9) + s.level * 5;
  }

  function drawGlowLayer() {
    gx.setTransform(1, 0, 0, 1, 0, 0);
    gx.clearRect(0, 0, glow.width, glow.height);
    gx.globalCompositeOperation = "lighter";
    gx.setTransform(glow.width / W, 0, 0, glow.height / H, 0, 0);
    for (const s of sessions) {
      if (!s.alive) continue;
      const heat = tone(s.level);
      const size = radiusOf(s) * (4 + heat * 7);
      gx.globalAlpha = (0.14 + heat * 0.46) * emphasis(s);
      gx.drawImage(spriteFor(heat), sx(s.x) - size / 2, sy(s.y) - size / 2, size, size);
    }
    for (const f of flares) {
      const age = f.life / f.max;
      const size = f.size * (1.4 - age * 0.4) * clamp(cam.z / fit, 0.8, 1.8);
      gx.globalAlpha = Math.sin(age * Math.PI) * 0.8;
      gx.drawImage(spriteFor(f.level), sx(f.x) - size / 2, sy(f.y) - size / 2, size, size);
    }
    for (const p of sparks) {
      const size = 8 + p.level * 8;
      gx.globalAlpha = (p.life / p.max) * 0.5;
      gx.drawImage(spriteFor(p.level), sx(p.x) - size / 2, sy(p.y) - size / 2, size, size);
    }
    gx.globalAlpha = 1;
    wx.setTransform(1, 0, 0, 1, 0, 0);
    wx.clearRect(0, 0, glowWide.width, glowWide.height);
    wx.drawImage(glow, 0, 0, glowWide.width, glowWide.height);
    ctx.globalCompositeOperation = "lighter";
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    ctx.globalAlpha = 0.75;
    ctx.drawImage(glow, 0, 0, W, H);
    ctx.globalAlpha = 0.6;
    ctx.drawImage(glowWide, 0, 0, W, H);
    ctx.globalAlpha = 1;
  }

  function drawStars() {
    ctx.globalCompositeOperation = "lighter";
    for (const s of sessions) {
      if (!s.born) continue;
      if (!s.alive) {         // 終わった星は星屑として残る(宇宙が育っていくように見える)
        const dust = 2.4 + radiusOf(s) * 0.5;
        ctx.globalAlpha = 0.42 * emphasis(s);
        ctx.drawImage(spriteFor(0.16), sx(s.x) - dust / 2, sy(s.y) - dust / 2, dust, dust);
        continue;
      }
      const heat = tone(s.level);
      const pulse = 1 + Math.sin(clock * 5 + s.phase) * 0.14 * s.level;
      const size = radiusOf(s) * 3.2 * pulse;
      ctx.globalAlpha = (0.5 + heat * 0.5) * emphasis(s);
      ctx.drawImage(spriteFor(Math.max(heat, 0.1)), sx(s.x) - size / 2, sy(s.y) - size / 2, size, size);
      if (heat > 0.35) {      // 忙しい星の芯は白く光る
        const core = size * 0.3;
        ctx.globalAlpha = (heat - 0.25) * emphasis(s);
        ctx.drawImage(spriteFor(1), sx(s.x) - core / 2, sy(s.y) - core / 2, core, core);
      }
    }
    for (const f of flares) {  // 活動の光の芯
      const age = f.life / f.max;
      const size = f.size * 0.42 * clamp(cam.z / fit, 0.8, 1.8);
      ctx.globalAlpha = Math.sin(age * Math.PI);
      ctx.drawImage(spriteFor(Math.min(1, f.level + 0.15)), sx(f.x) - size / 2, sy(f.y) - size / 2, size, size);
    }
    ctx.globalAlpha = 1;
    ctx.lineCap = "round";
    ctx.lineWidth = 1.1;
    for (const p of sparks) {  // 火花は短い尾を引く
      const [r, g, b] = heatRgb(Math.min(1, p.level + 0.15));
      ctx.strokeStyle = `rgba(${r},${g},${b},${(p.life / p.max) * 0.9})`;
      ctx.beginPath();
      ctx.moveTo(sx(p.x), sy(p.y));
      ctx.lineTo(sx(p.x - p.vx * 0.05), sy(p.y - p.vy * 0.05));
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    for (const r of ripples) {
      const [cr, cg, cb] = heatRgb(r.level);
      ctx.strokeStyle = `rgba(${cr},${cg},${cb},${r.life * 0.55})`;
      ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.arc(sx(r.x), sy(r.y), r.r * cam.z, 0, TAU); ctx.stroke();
    }
  }

  function drawLabels() {
    ctx.globalCompositeOperation = "source-over";
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    for (const p of projects) {
      if (!p.born) continue;
      const related = hovered && hovered.p === p;
      const base = (p.usd >= 20 ? 0.3 : 0) * (1 - p.dead * 0.6);   // 小さなプロジェクトの名前は動いているときだけ出す。死んだ星雲は薄く
      const a = clamp(Math.max(base, p.level * 1.6, p.flash * 1.6, related ? 1 : 0), 0, 1);
      const lx = sx(p.x), ly = sy(p.y + radiusAt(p.usd) * 0.8) + 14;   // 星雲に重ならないよう、下に書く
      if (a < 0.03 || lx < 40 || lx > W - 40 || ly < 96 || ly > H - 150) continue;   // 端の名前は文字や操作部と重なるので出さない
      ctx.globalAlpha = a;
      ctx.font = `${p.level > 0.3 ? 500 : 400} ${p.level > 0.3 ? 14 : 12}px -apple-system, "Hiragino Sans", sans-serif`;
      ctx.shadowColor = "rgba(0,0,0,0.85)";
      ctx.shadowBlur = 8;
      ctx.fillStyle = "#f3efe6";
      ctx.fillText(p.name, lx, ly);
    }
    ctx.shadowBlur = 0;
    ctx.globalAlpha = 1;
  }

  function frame(now) {
    if (!W || !H) {   // 隠れた枠の中など大きさが 0 の間は描かずに待つ(大きさ 0 の画像を描くと例外で再生が止まるため)。大きさが付けば resize で描き直す
      last = now;
      requestAnimationFrame(frame);
      return;
    }
    const elapsed = Math.min(0.25, (now - last) / 1000);
    frameMs = lerp(frameMs, now - last, 0.04);
    last = now;
    if (frameMs > SLOW_FRAME_MS && maxDpr > 1) {   // 遅い環境では解像度を下げて、なめらかさを優先する
      maxDpr = Math.max(1, maxDpr - 0.5);
      frameMs = 16;
      resize();
    }
    simulate(elapsed);
    updateHud();
    ctx.clearRect(0, 0, W, H);
    drawNebulae();
    drawEdges();
    drawGlowLayer();
    drawStars();
    drawLabels();
    updateTip();
    requestAnimationFrame(frame);
  }

  function resize() {
    DPR = Math.min(window.devicePixelRatio || 1, maxDpr);
    W = window.innerWidth; H = window.innerHeight;
    canvas.width = W * DPR; canvas.height = H * DPR;
    deep.width = W * DPR; deep.height = H * DPR;
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    glow.width = Math.ceil(W / 4); glow.height = Math.ceil(H / 4);
    glowWide.width = Math.ceil(W / 12); glowWide.height = Math.ceil(H / 12);
    fit = Math.min(W / (world.maxX - world.minX + 200), (H - 190) / (world.maxY - world.minY + 200));
    makeStarfield();
    sizeTimeline();
    if (!cam.manual) aimCamera(1);
  }

  function makeStarfield() {   // 遠い星は静止画として 1 度だけ描く。背景はいつも暗いまま
    stars.width = W * DPR; stars.height = H * DPR;
    const g = stars.getContext("2d");
    g.setTransform(DPR, 0, 0, DPR, 0, 0);
    g.fillStyle = "#04060d";
    g.fillRect(0, 0, W, H);
    const rand = seeded(42);
    const count = Math.round((W * H) / 900);
    for (let i = 0; i < count; i++) {
      const a = 0.06 + Math.pow(rand(), 4) * 0.8;
      const warm = rand();
      g.fillStyle = `rgba(${Math.round(190 + warm * 65)},${Math.round(200 + rand() * 40)},${Math.round(255 - warm * 70)},${a})`;
      g.beginPath();
      g.arc(rand() * W, rand() * H, 0.25 + Math.pow(rand(), 3) * 0.9, 0, TAU);
      g.fill();
    }
  }

  // 時間軸
  const tl = $("timeline");
  const tx = tl.getContext("2d");
  const maxDay = Math.max(1, ...data.days.map((d) => d.n));
  let drag = null;
  function sizeTimeline() {
    const r = tl.getBoundingClientRect();
    tl.width = r.width * DPR; tl.height = r.height * DPR;
    tx.setTransform(DPR, 0, 0, DPR, 0, 0);
    drawTimeline();
  }
  function drawTimeline() {
    const w = tl.clientWidth, h = tl.clientHeight;
    const base = h - 16;
    const px = (bin) => (bin / data.bins) * w;
    tx.clearRect(0, 0, w, h);
    if (state.from > 0 || state.to < data.bins) {
      tx.fillStyle = "rgba(249,154,30,0.10)";
      tx.fillRect(px(state.from), 0, px(state.to) - px(state.from), base);
    }
    data.days.forEach((d, i) => {
      const x0 = px(d.bin), x1 = i + 1 < data.days.length ? px(data.days[i + 1].bin) : w;
      if (d.n) {
        const bar = Math.max(1.5, Math.sqrt(d.n / maxDay) * (base - 2));
        const [r, g, b] = heatRgb(0.35 + 0.65 * Math.log1p(d.n) / Math.log1p(maxDay));
        tx.fillStyle = `rgba(${r},${g},${b},${d.bin <= state.t ? 1 : 0.32})`;
        tx.fillRect(x0, base - bar, Math.max(1, x1 - x0 - 1), bar);
      }
      if (d.date.endsWith("-01")) {
        tx.fillStyle = "#7f7c96";
        tx.font = '10px -apple-system, "Hiragino Sans", sans-serif';
        tx.fillText(`${Number(d.date.slice(5, 7))}月`, x0, h - 3);
      }
    });
    const head = px(state.t);
    tx.fillStyle = "#fff7c8";
    tx.fillRect(head - 0.75, 0, 1.5, base + 2);
    if (drag && drag.moved) {
      tx.fillStyle = "rgba(255,247,200,0.16)";
      tx.fillRect(Math.min(drag.x0, drag.x1), 0, Math.abs(drag.x1 - drag.x0), base);
    }
  }
  const binAt = (clientX) => {
    const r = tl.getBoundingClientRect();
    return clamp((clientX - r.left) / r.width, 0, 1) * data.bins;
  };
  tl.addEventListener("pointerdown", (ev) => {
    const r = tl.getBoundingClientRect();
    drag = { x0: ev.clientX - r.left, x1: ev.clientX - r.left, moved: false };
    tl.setPointerCapture(ev.pointerId);
  });
  tl.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    const r = tl.getBoundingClientRect();
    drag.x1 = ev.clientX - r.left;
    drag.moved = drag.moved || Math.abs(drag.x1 - drag.x0) > 5;
    drawTimeline();
  });
  tl.addEventListener("pointerup", (ev) => {
    if (!drag) return;
    const w = tl.clientWidth;
    if (drag.moved) {
      const a = (Math.min(drag.x0, drag.x1) / w) * data.bins, b = (Math.max(drag.x0, drag.x1) / w) * data.bins;
      setRange(a, b, "custom");
      setPlaying(true);
    } else {
      seek(clamp(binAt(ev.clientX), state.from, state.to));
    }
    drag = null;
    drawTimeline();
  });

  // 操作
  const playButton = $("play");
  function setPlaying(on) {
    state.playing = on;
    const ended = !on && state.t >= state.to;
    playButton.textContent = on ? "❚❚" : ended ? "↻" : "▶";
    playButton.setAttribute("aria-label", on ? "一時停止" : "再生");
    document.body.classList.remove("idle");
  }
  function togglePlay() {
    touched = true;
    if (!state.playing && state.t >= state.to) { cam.box = null; seek(state.from); }
    setPlaying(!state.playing);
  }
  function setRange(from, to, value) {
    touched = true;
    state.from = from; state.to = to;
    cam.box = null;      // 期間を選び直したら、視界もその始まりから広げ直す
    const select = $("range");
    if (value === "custom") {
      let opt = select.querySelector('option[value="custom"]');
      if (!opt) {
        opt = document.createElement("option");
        opt.value = "custom";
        select.appendChild(opt);
      }
      const label = (bin) => fmtDate.format(new Date(start + bin * binMs)).replace(/^\d+年/, "");
      opt.textContent = `指定: ${label(from)}〜${label(to)}`;
      select.value = "custom";
    }
    seek(from);
  }
  function fillRanges() {
    const select = $("range");
    const add = (parent, label, from, to) => {
      const o = document.createElement("option");
      o.textContent = label;
      o.value = `${from}-${to}`;
      parent.appendChild(o);
    };
    add(select, "全期間", 0, data.bins);
    const group = (label) => {
      const g = document.createElement("optgroup");
      g.label = label;
      select.appendChild(g);
      return g;
    };
    const best = group("見どころ(応答の多い日)");
    for (const h of data.highlights) add(best, h.label, h.from, h.to);
    const weeks = group("週");
    for (const w of [...data.weeks].reverse()) add(weeks, w.label, w.from, w.to);
    select.addEventListener("change", () => {
      if (select.value === "custom") return;
      const [from, to] = select.value.split("-").map(Number);
      setRange(from, to);
      setPlaying(true);
    });
  }
  function setSkipIdle(on) {
    state.skipIdle = on;
    const button = $("skipIdle");
    button.setAttribute("aria-pressed", on);
    button.textContent = on ? "静かな時間を飛ばす: オン" : "静かな時間を飛ばす: オフ";
  }

  playButton.addEventListener("click", togglePlay);
  $("speed").addEventListener("change", (ev) => { state.speed = Number(ev.target.value); });
  $("skipIdle").addEventListener("click", () => setSkipIdle(!state.skipIdle));
  $("full").addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen?.();
  });
  window.addEventListener("keydown", (ev) => {
    if (ev.target.tagName === "SELECT") return;
    if (ev.key === " ") { ev.preventDefault(); togglePlay(); }
    else if (ev.key === "ArrowRight") seek(state.t + 4);
    else if (ev.key === "ArrowLeft") seek(state.t - 4);
    else if (ev.key === "f") $("full").click();
  });

  // 星の詳細とカメラの手動操作
  let panning = null, idleTimer = 0;
  function wake() {
    document.body.classList.remove("idle");
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { if (state.playing) document.body.classList.add("idle"); }, 3000);
  }
  canvas.addEventListener("pointerdown", (ev) => {
    panning = { x: ev.clientX, y: ev.clientY, cx: cam.x, cy: cam.y };
    canvas.setPointerCapture(ev.pointerId);
  });
  canvas.addEventListener("pointerup", () => { panning = null; });
  canvas.addEventListener("pointermove", (ev) => {
    pointer = { x: ev.clientX, y: ev.clientY };
    if (panning) {
      cam.manual = true;
      cam.x = panning.cx - (ev.clientX - panning.x) / cam.z;
      cam.y = panning.cy - (ev.clientY - panning.y) / cam.z;
    }
  });
  canvas.addEventListener("pointerleave", () => { pointer = null; });
  window.addEventListener("pointermove", wake);
  canvas.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    cam.manual = true;
    cam.z = clamp(cam.z * Math.exp(-ev.deltaY * 0.0015), fit * 0.6, fit * 14);
  }, { passive: false });
  canvas.addEventListener("dblclick", () => { cam.manual = false; });   // 手で動かした視界を戻す

  function updateTip() {
    const tip = $("tip");
    hovered = null;
    if (pointer) {
      let best = 22 * 22;
      for (const s of sessions) {
        if (!s.born) continue;
        const dx = sx(s.x) - pointer.x, dy = sy(s.y) - pointer.y, d = dx * dx + dy * dy;
        if (d < best) { best = d; hovered = s; }
      }
    }
    if (!hovered) { tip.hidden = true; return; }
    const s = hovered, bin = Math.floor(state.t);
    tip.replaceChildren();
    const title = document.createElement("b");
    title.textContent = s.p.name;
    const rows = [`作るもの ${s.p.mix}`, `開始 ${s.label}`, `応答 ${fmtNumber.format(cumAt(s.r.cumN, s.r, bin))} 件`,
      `API 換算額 $${fmtNumber.format(Math.round(cumAt(s.r.cumUsd, s.r, bin)))}`,
      !s.alive ? "終わったセッション" : s.level >= ACTIVE_LEVEL ? "いま動いています" : "止まっています"];
    if (s.links.size) rows.push(`関係のあるセッション ${s.links.size} 件`);
    tip.append(title, ...rows.flatMap((r) => [document.createTextNode(r), document.createElement("br")]));
    tip.hidden = false;
    tip.style.left = `${clamp(pointer.x + 16, 8, W - 296)}px`;
    tip.style.top = `${clamp(pointer.y + 16, 8, H - 170)}px`;
  }

  window.addEventListener("resize", resize);
  // 新しい記録: 定期実行が画面を作り直すと、小さな版のファイルが変わる(file:// でも script 要素なら読める)
  function watchVersion() {
    if (!data.versionFile) return;
    setInterval(() => {
      const check = document.createElement("script");
      check.src = `${data.versionFile}?${Date.now()}`;
      check.onload = () => {
        check.remove();
        if (!window.activityVersion || window.activityVersion === data.version) return;
        if (!state.playing && state.t >= state.to) reloadAtNow(); else $("fresh").hidden = false;
      };
      check.onerror = () => check.remove();
      document.head.append(check);
    }, VERSION_CHECK_MS);
  }
  function reloadAtNow() {   // 新しい記録を読み込み、いまの状態から見る
    location.hash = "now";
    location.reload();
  }
  $("fresh").addEventListener("click", reloadAtNow);

  fillRanges();
  queueNebulae();
  resize();
  seek(0);
  setPlaying(false);
  if (startAtNow) {
    touched = true;
    history.replaceState(null, "", location.pathname);
    seek(data.bins);
    setPlaying(false);
  } else if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    setTimeout(() => { if (!touched) setPlaying(true); }, 600);
  }
  watchVersion();
  window.activityPlayer = { state, cam, seek, setPlaying, setRange, setSkipIdle, sessions, projects, flares, bakeQueue, bakeSome, webgl: Boolean(gl), touch: () => { touched = true; } };
  requestAnimationFrame(frame);
})();
