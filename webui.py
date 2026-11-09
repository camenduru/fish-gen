"""fishgen in the browser:  py webui.py  ->  http://127.0.0.1:8766/

Same renderer as the CLI and the Tk studio: pick a kind and species, a seed, a size and the
sprite options, and the page shows the animal swimming and turning around, frame by frame out
of its sprite sheet. The sprite sheet PNG can be downloaded as-is.

    py webui.py --port 8766
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fishgen.animation import SHOWCASE_ORDER, generate
from fishgen.export import build_sheet
from fishgen.genome import KINDS
from fishgen.kinds import kind_label
from fishgen.species import RANDOM, SPECIES_NAMES, genome_for, make_genome, morphs_of, species_for

MAX_CACHE = 24
CACHE: "OrderedDict[str, dict]" = OrderedDict()
RENDER_LOCK = threading.Lock()  # one animal at a time; the renderer already uses threads


# --------------------------------------------------------------------------- options
def options_payload() -> dict:
    kinds = []
    for kind in KINDS:
        kinds.append({
            "kind": kind,
            "label": kind_label(kind),
            "species": [{"name": name, "morphs": list(morphs_of(name)) if name != RANDOM else []}
                        for name in species_for(kind)],
        })
    return {"kinds": kinds, "defaults": {"size": 64, "style": "pixel", "smoothness": 1}}


# --------------------------------------------------------------------------- render
def _num(params: dict, key: str, default, cast=int, lo=None, hi=None):
    raw = params.get(key)
    if raw in (None, ""):
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number") from None
    if lo is not None and value < lo:
        raise ValueError(f"{key} must be at least {lo}")
    if hi is not None and value > hi:
        raise ValueError(f"{key} must be at most {hi}")
    return value


def _flag(name: str) -> str:
    return name.lower().replace(" ", "_")


def render_sprite(params: dict) -> dict:
    species = (params.get("species") or "").strip() or None
    kind = (params.get("kind") or "").strip() or None
    morph = (params.get("morph") or "").strip() or None
    style = params.get("style") or "pixel"
    if style not in ("pixel", "smooth"):
        raise ValueError("style must be pixel or smooth")
    if species and species not in SPECIES_NAMES:
        raise ValueError(f"unknown species {species!r}")
    if kind and kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if morph and species in (None, RANDOM):
        raise ValueError("a color variety needs a species")

    seed = _num(params, "seed", random.randrange(1_000_000), lo=0, hi=10 ** 9)
    size = _num(params, "size", 64, lo=16, hi=256)
    smoothness = _num(params, "smoothness", 1.0, cast=float, lo=0.25, hi=4)
    colors = _num(params, "colors", None, lo=2, hi=256)
    outline = (params.get("outline") or "1").lower() not in ("0", "false", "off")
    scariness = params.get("scariness")
    scariness = _num(params, "scariness", None, cast=float, lo=0, hi=1) if scariness not in (None, "") else None

    if species and species != RANDOM:
        genome = make_genome(species, seed, morph=morph)
    else:
        genome = genome_for(seed, RANDOM if species == RANDOM else None, kind)
    if scariness is not None:
        genome.scariness = scariness

    started = time.perf_counter()
    sprite = generate(seed, size, size, smoothness=smoothness, genome=genome, style=style,
                      colors=colors, outline=outline)
    image, rects = build_sheet(sprite)
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    elapsed = time.perf_counter() - started

    name = _flag(species if species and species != RANDOM else kind_label(genome.kind))
    animations = {}
    for anim_name, anim in sprite.animations.items():
        animations[anim_name] = {
            "fps": anim.fps,
            "loop": anim.loop,
            "next": anim.next,
            "frames": [{"x": x, "y": y, "w": w, "h": h} for (x, y, w, h) in rects[anim_name]],
        }
    meta = {
        "seed": genome.seed,
        "species": species or RANDOM,
        "kind": genome.kind,
        "kind_label": kind_label(genome.kind),
        "morph": getattr(genome, "morph", None),
        "size": list(sprite.size),
        "sheet": [image.width, image.height],
        "style": style,
        "length_cm": round(float(genome.length_cm), 1),
        "frames": sum(len(a.frames) for a in sprite.animations.values()),
        "colors": int(len(sprite.palette)) if sprite.palette is not None else None,
        "filename": f"{name}_{sprite.size[0]}px_{genome.seed}.png",
        "render_ms": round(elapsed * 1000),
    }
    print(f"  rendered {meta['filename']}: {meta['frames']} frames, "
          f"{meta['size'][0]}x{meta['size'][1]}, {elapsed:.2f}s", flush=True)
    return {
        "meta": meta,
        "animations": animations,
        "order": [n for n in SHOWCASE_ORDER if n in animations],
        "png": buffer.getvalue(),
    }


def cached_render(params: dict) -> tuple[str, dict]:
    key = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
    with RENDER_LOCK:
        if key in CACHE:
            CACHE.move_to_end(key)
            return key, CACHE[key]
        payload = render_sprite(params)
        CACHE[key] = payload
        while len(CACHE) > MAX_CACHE:
            CACHE.popitem(last=False)
    return key, payload


# --------------------------------------------------------------------------- page
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>fishgen</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg: #0a1520; --panel: #0f1e2b; --aside: #0c1925; --line: #1d3446;
    --fg: #dbe9ee; --dim: #82a0b4; --accent: #45bede;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    background: radial-gradient(120% 85% at 50% 0%, #14374f 0%, #0a1520 62%);
    color: var(--fg); font: 13px/1.45 ui-sans-serif, system-ui, "Segoe UI", sans-serif;
    display: flex; flex-direction: column; height: 100vh; overflow: hidden;
  }
  header {
    display: flex; align-items: center; gap: 10px; padding: 9px 14px;
    background: var(--panel); border-bottom: 1px solid var(--line);
  }
  header h1 { font-size: 12px; letter-spacing: .09em; font-weight: 700; margin: 0; text-transform: uppercase; }
  header h1 span { color: var(--accent); }
  #info { color: var(--dim); margin-left: auto; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  main { flex: 1; display: flex; min-height: 0; }
  aside {
    width: 262px; flex: 0 0 auto; padding: 12px; overflow-y: auto;
    background: var(--aside); border-right: 1px solid var(--line);
  }
  .row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .row > label { color: var(--dim); flex: 0 0 60px; }
  .row > :is(select, input, .grow) { flex: 1; min-width: 0; }
  select, input[type=number] {
    background: #08131e; color: var(--fg); border: 1px solid var(--line);
    border-radius: 5px; padding: 5px 6px; font: inherit;
  }
  select:focus, input:focus { outline: none; border-color: #2f6d8a; }
  input[type=number] { -moz-appearance: textfield; }
  button {
    background: #0d2030; color: var(--fg); border: 1px solid var(--line);
    border-radius: 5px; padding: 5px 9px; font: inherit; cursor: pointer;
  }
  button:hover { border-color: #2d6a86; background: #102b3d; }
  .mini { padding: 4px 7px; color: var(--dim); }
  .mini:hover { color: var(--fg); }
  .check { display: flex; align-items: center; gap: 7px; color: var(--dim); margin-bottom: 8px; }
  .check input { accent-color: var(--accent); }
  input[type=range] { accent-color: var(--accent); flex: 1; min-width: 0; }
  #stage { flex: 1; min-width: 0; display: flex; align-items: center; justify-content: center; position: relative; overflow: hidden; }
  canvas { max-width: 100%; max-height: 100%; image-rendering: pixelated; }
  #busy {
    position: absolute; inset: 0; display: none; align-items: center; justify-content: center;
    background: #07121cbb; color: #b9d8e6; font-size: 12px; letter-spacing: .1em; text-transform: uppercase;
  }
  #busy.on { display: flex; }
  footer {
    display: flex; flex-wrap: wrap; align-items: center; gap: 7px;
    padding: 8px 12px; background: var(--panel); border-top: 1px solid var(--line);
  }
  .chip { border-radius: 999px; padding: 4px 11px; font-size: 12px; }
  .chip.on { border-color: var(--accent); background: #123a4d; color: #e6f7fd; }
  .spacer { flex: 1; }
  #dl { color: var(--dim); font-size: 12px; text-decoration: none; border-bottom: 1px dotted #2f5b72; }
  #dl:hover { color: var(--fg); }
  @media (max-width: 720px) {
    main { flex-direction: column-reverse; }
    aside { width: auto; border-right: none; border-top: 1px solid var(--line); max-height: 44vh; }
    #stage { min-height: 34vh; }
  }
</style>
</head>
<body>
<header>
  <h1>fish<span>gen</span></h1>
  <div id="info">loading…</div>
</header>
<main>
  <aside>
    <div class="row"><label for="kind">kind</label><select id="kind"></select></div>
    <div class="row"><label for="species">species</label><select id="species"></select></div>
    <div class="row" id="morphRow" style="display:none"><label for="morph">variety</label><select id="morph"></select></div>
    <div class="row">
      <label for="seed">seed</label>
      <input id="seed" type="number" min="0" max="1000000000" step="1" value="7">
      <button class="mini" id="prev" title="seed - 1">‹</button>
      <button class="mini" id="next" title="seed + 1">›</button>
      <button class="mini" id="dice" title="random seed">✻</button>
    </div>
    <div class="row"><label for="size">size</label><select id="size"></select></div>
    <div class="row"><label for="style">style</label><select id="style"></select></div>
    <div class="row"><label for="colors">colors</label><select id="colors"></select></div>
    <div class="row"><label for="smooth">smooth</label><select id="smooth"></select></div>
    <div class="check"><input type="checkbox" id="outline" checked><label for="outline">outline</label></div>
    <div class="check"><input type="checkbox" id="scaryOn"><label for="scaryOn">override scariness</label></div>
    <div class="row" id="scaryRow" style="display:none">
      <label for="scariness">scary</label>
      <input type="range" id="scariness" min="0" max="1" step="0.05" value="0.5">
    </div>
    <div class="row" style="margin-top:12px"><button id="random" style="flex:1">random fish</button></div>
  </aside>
  <div id="stage">
    <canvas id="canvas" width="64" height="64"></canvas>
    <div id="busy">rendering…</div>
  </div>
</main>
<footer>
  <button class="chip on" id="cycle">cycle</button>
  <div id="chips" style="display:contents"></div>
  <button class="chip" id="sheetToggle">sheet</button>
  <button class="chip" id="pause">pause</button>
  <span class="spacer"></span>
  <a id="dl" href="#" download>download sprite sheet</a>
</footer>
<script>
const $ = (s) => document.querySelector(s);
const canvas = $('#canvas');
const ctx = canvas.getContext('2d');

let OPTIONS = null, SPRITE = null, IMG = null, token = 0;
let anim = 'swim_right', frame = 0, acc = 0, mode = 'cycle', sheet = false, paused = false;
let last = performance.now();

const SIZES = [16, 24, 32, 48, 64, 96, 128];
const COLORS = ['', '8', '10', '12', '16', '24', '32'];
const SMOOTH = [['1', '1x'], ['2', '2x'], ['3', '3x'], ['4', '4x']];

function fillSelect(el, items) {
  el.innerHTML = items.map(([v, label]) => `<option value="${v}">${label}</option>`).join('');
}

async function boot() {
  OPTIONS = await (await fetch('/api/options')).json();
  const kinds = OPTIONS.kinds;
  fillSelect($('#kind'), kinds.map(k => [k.kind, k.label]));
  fillSelect($('#size'), SIZES.map(n => [String(n), n + '×' + n]));
  fillSelect($('#style'), [['pixel', 'pixel art'], ['smooth', 'smooth']]);
  fillSelect($('#colors'), COLORS.map(c => [c, c ? c + ' colors' : 'auto']));
  fillSelect($('#smooth'), SMOOTH);
  $('#size').value = '64';
  kindChanged();
  bind();
  requestRender();
}

function kindChanged(keep) {
  const kind = OPTIONS.kinds.find(k => k.kind === $('#kind').value) || OPTIONS.kinds[0];
  fillSelect($('#species'), kind.species.map(s => [s.name, s.name]));
  if (keep && kind.species.some(s => s.name === keep)) $('#species').value = keep;
  speciesChanged();
}

function speciesChanged() {
  const kind = OPTIONS.kinds.find(k => k.kind === $('#kind').value);
  const sp = kind && kind.species.find(s => s.name === $('#species').value);
  const morphs = sp ? sp.morphs : [];
  $('#morphRow').style.display = morphs.length ? 'flex' : 'none';
  fillSelect($('#morph'), [['', 'from seed']].concat(morphs.map(m => [m, m])));
}

function query() {
  const p = new URLSearchParams();
  p.set('kind', $('#kind').value);
  p.set('species', $('#species').value);
  if ($('#morph').value) p.set('morph', $('#morph').value);
  p.set('seed', $('#seed').value || '0');
  p.set('size', $('#size').value);
  p.set('style', $('#style').value);
  p.set('smoothness', $('#smooth').value);
  if ($('#colors').value) p.set('colors', $('#colors').value);
  p.set('outline', $('#outline').checked ? '1' : '0');
  if ($('#scaryOn').checked) p.set('scariness', $('#scariness').value);
  return p.toString();
}

async function requestRender() {
  const mine = ++token;
  const t0 = performance.now();
  $('#busy').classList.add('on');
  $('#busy').textContent = 'rendering…';
  try {
    const response = await fetch('/api/render?' + query());
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    if (mine !== token) return;
    const image = new Image();
    image.onload = () => {
      if (mine !== token) return;
      SPRITE = data;
      IMG = image;
      sheet = false;
      $('#sheetToggle').classList.remove('on');
      mode = 'cycle';
      setAnim('swim_right', true);
      chips();
      applyMeta(Math.round(performance.now() - t0));
      layout();
      draw();
      $('#busy').classList.remove('on');
    };
    image.onerror = () => { if (mine === token) fail('could not load the sprite sheet'); };
    image.src = '/api/sheet?id=' + data.id;
  } catch (err) {
    if (mine === token) fail(err.message);
  }
}

function fail(message) {
  $('#busy').classList.add('on');
  $('#busy').textContent = 'error: ' + message;
}

function applyMeta(ms) {
  const m = SPRITE.meta;
  const bits = [
    'seed ' + m.seed,
    m.species === 'Random' ? (m.kind_label + ' (random)') : m.species.toLowerCase(),
    m.morph ? 'variety ' + m.morph : null,
    m.size[0] + '×' + m.size[1],
    m.frames + ' frames',
    m.colors ? m.colors + ' colors' : 'full color',
    m.length_cm + ' cm',
    'rendered in ' + (m.render_ms / 1000).toFixed(2) + 's',
  ].filter(Boolean);
  $('#info').textContent = bits.join('  ·  ');
  $('#dl').href = '/api/sheet?id=' + SPRITE.id + '&download=1';
  $('#dl').setAttribute('download', m.filename);
  $('#dl').textContent = 'download ' + m.filename;
  // Pixel art stays blocky when zoomed; smooth renders keep their anti-aliasing.
  canvas.style.imageRendering = m.style === 'smooth' ? 'auto' : 'pixelated';
}

function chips() {
  const holder = $('#chips');
  holder.innerHTML = SPRITE.order
    .map(n => `<button class="chip" data-anim="${n}">${n.replace(/_/g, ' ')}</button>`)
    .join('');
  [...holder.querySelectorAll('[data-anim]')].forEach(btn => {
    btn.addEventListener('click', () => {
      mode = 'single';
      setAnim(btn.dataset.anim, true);
    });
  });
  highlight();
}

function highlight() {
  const cycleOn = mode === 'cycle';
  $('#cycle').classList.toggle('on', cycleOn);
  document.querySelectorAll('#chips [data-anim]').forEach(b => {
    b.classList.toggle('on', !cycleOn && b.dataset.anim === anim);
  });
  $('#pause').classList.toggle('on', paused);
  $('#sheetToggle').classList.toggle('on', sheet);
}

function setAnim(name, reset) {
  if (!SPRITE || !SPRITE.animations[name]) return;
  anim = name;
  if (reset) { frame = 0; acc = 0; }
  highlight();
}

function layout() {
  if (!SPRITE) return;
  const box = $('#stage').getBoundingClientRect();
  const w = sheet ? SPRITE.meta.sheet[0] : SPRITE.meta.size[0];
  const h = sheet ? SPRITE.meta.sheet[1] : SPRITE.meta.size[1];
  const zoom = Math.max(1, Math.floor(Math.min((box.width - 26) / w, (box.height - 26) / h)));
  canvas.width = w * zoom;
  canvas.height = h * zoom;
}

function draw() {
  if (!IMG || !SPRITE) return;
  ctx.imageSmoothingEnabled = false;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (sheet) {
    ctx.drawImage(IMG, 0, 0, IMG.width, IMG.height, 0, 0, canvas.width, canvas.height);
    return;
  }
  const a = SPRITE.animations[anim];
  if (!a) return;
  const f = a.frames[Math.min(frame, a.frames.length - 1)];
  ctx.drawImage(IMG, f.x, f.y, f.w, f.h, 0, 0, canvas.width, canvas.height);
}

function advance() {
  const a = SPRITE.animations[anim];
  frame += 1;
  if (frame < a.frames.length) return;
  frame = 0;
  if (!a.loop) {
    if (a.next) setAnim(a.next, false);
    return;
  }
  if (mode === 'cycle' && (anim === 'swim_right' || anim === 'swim_left')) {
    setAnim(anim === 'swim_right' ? 'turn_right_to_left' : 'turn_left_to_right', false);
  }
}

function tick(now) {
  const dt = Math.min((now - last) / 1000, 0.12);
  last = now;
  if (SPRITE && IMG && !paused && !sheet) {
    const a = SPRITE.animations[anim];
    if (a) {
      acc += dt;
      let guard = 0;
      while (acc >= 1 / a.fps && guard++ < 8) {
        acc -= 1 / a.fps;
        advance();
      }
    }
    draw();
  }
  requestAnimationFrame(tick);
}

function bind() {
  let timer = null;
  const later = () => { clearTimeout(timer); timer = setTimeout(requestRender, 220); };
  $('#kind').addEventListener('change', () => { kindChanged(); requestRender(); });
  $('#species').addEventListener('change', () => { speciesChanged(); requestRender(); });
  ['#morph', '#size', '#style', '#colors', '#smooth'].forEach(sel =>
    $(sel).addEventListener('change', () => { colorsState(); requestRender(); }));
  $('#outline').addEventListener('change', requestRender);
  $('#scaryOn').addEventListener('change', () => {
    $('#scaryRow').style.display = $('#scaryOn').checked ? 'flex' : 'none';
    requestRender();
  });
  $('#scariness').addEventListener('input', later);
  $('#prev').addEventListener('click', () => stepSeed(-1));
  $('#next').addEventListener('click', () => stepSeed(1));
  $('#dice').addEventListener('click', randomSeed);
  $('#random').addEventListener('click', randomSeed);
  $('#cycle').addEventListener('click', () => { mode = 'cycle'; setAnim('swim_right', true); });
  $('#sheetToggle').addEventListener('click', () => {
    sheet = !sheet;
    layout();
    draw();
    highlight();
  });
  $('#pause').addEventListener('click', () => { paused = !paused; highlight(); });
  window.addEventListener('resize', () => { layout(); draw(); });
  window.addEventListener('keydown', (ev) => {
    if (ev.target.matches('input, select, textarea')) return;
    if (ev.key === ' ') { ev.preventDefault(); randomSeed(); }
    else if (ev.key === 'ArrowLeft') stepSeed(-1);
    else if (ev.key === 'ArrowRight') stepSeed(1);
    else if (ev.key === 's' || ev.key === 'g') $('#sheetToggle').click();
    else if (ev.key === 'p') $('#pause').click();
  });
  colorsState();
}

function colorsState() {
  const pixel = $('#style').value === 'pixel';
  $('#colors').disabled = !pixel;
  $('#outline').disabled = !pixel;
}

function stepSeed(delta) {
  const next = Math.max(0, (parseInt($('#seed').value, 10) || 0) + delta);
  $('#seed').value = next;
  requestRender();
}

function randomSeed() {
  $('#seed').value = Math.floor(Math.random() * 1_000_000);
  $('#kind').value = [...$('#kind').options][Math.floor(Math.random() * $('#kind').options.length)].value;
  kindChanged();
  randomSpecies();
  requestRender();
}

function randomSpecies() {
  const options = [...$('#species').options];
  $('#species').value = options[Math.floor(Math.random() * options.length)].value;
  speciesChanged();
}

requestAnimationFrame(tick);
boot().then(() => { if (OPTIONS) draw(); });
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "fishgen-web"

    def log_message(self, fmt, *args):  # keep the console for render lines only
        pass

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict):
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        try:
            if parsed.path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif parsed.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            elif parsed.path == "/api/options":
                self._json(200, options_payload())
            elif parsed.path == "/api/render":
                key, payload = cached_render(params)
                body = {"id": key, "meta": payload["meta"], "animations": payload["animations"],
                        "order": payload["order"]}
                self._json(200, body)
            elif parsed.path == "/api/sheet":
                key = params.get("id", "")
                with RENDER_LOCK:
                    payload = CACHE.get(key)
                if not payload:
                    self._json(404, {"error": "unknown sprite; render it again"})
                    return
                extra = {}
                if params.get("download"):
                    name = payload["meta"]["filename"]
                    extra["Content-Disposition"] = f'attachment; filename="{name}"'
                self._send(200, payload["png"], "image/png", extra)
            else:
                self._json(404, {"error": "not found"})
        except ValueError as err:
            self._json(400, {"error": str(err)})
        except Exception as err:  # noqa: BLE001 - a bad render must not kill the server
            self._json(500, {"error": f"{type(err).__name__}: {err}"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fishgen in the browser")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"fishgen web UI on http://{args.host}:{args.port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
