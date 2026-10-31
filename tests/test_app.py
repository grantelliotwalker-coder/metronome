"""End-to-end tests for the metronome, run in real Chromium via Playwright.

The scheduler is verified by hooking AudioBufferSourceNode.start, so the tests check the exact
audio-clock times at which every click is scheduled, not just what the screen shows.

    pip install playwright && playwright install chromium
    python3 tests/test_app.py
"""
import functools
import http.server
import json
import os
import re
import socketserver
import statistics
import sys
import threading
import traceback

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOOK = """
(function () {
  window.__clicks = [];
  var ids = new WeakMap(), n = 0;
  var start = AudioBufferSourceNode.prototype.start;
  AudioBufferSourceNode.prototype.start = function (t) {
    var b = this.buffer; if (b && b.length <= 1) return start.apply(this, arguments);   // silent unlock sample
    if (!ids.has(b)) ids.set(b, ++n);
    window.__clicks.push({ t: t || 0, rate: this.playbackRate.value, buf: ids.get(b), name: b.label || '' });
    return start.apply(this, arguments);
  };
  var gain = AudioContext.prototype.createGain;
  AudioContext.prototype.createGain = function () { window.__ctx = this; return gain.apply(this, arguments); };
})();
"""

# Emulates iPhone WebKit: the audio context stays suspended until we flip __allowResume (a real tap).
LOCKED_AUDIO = """
(function () {
  window.__allowResume = false;
  var Orig = window.AudioContext, resume = Orig.prototype.resume;
  Orig.prototype.resume = function () { return window.__allowResume ? resume.call(this) : Promise.resolve(); };
  var Locked = function (o) { var c = new Orig(o); c.suspend(); return c; };
  Locked.prototype = Orig.prototype;
  window.AudioContext = Locked;
})();
"""

MIDI_MOCK = """
(function () {
  window.__midi = [];
  var out = { id: 'mock1', name: 'Mock Synth', send: function (d, ts) { window.__midi.push({ d: Array.prototype.slice.call(d), ts: ts }); }, clear: function () {} };
  navigator.requestMIDIAccess = function () {
    return Promise.resolve({ outputs: new Map([['mock1', out]]), onstatechange: null });
  };
})();
"""


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve():
    handler = functools.partial(Quiet, directory=ROOT)
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/index.html"


def cfg(**o):
    s = dict(bpm=100, num=4, den=4, acc=None, sub=1, swing=50, poly=0, sound="click", hat=2, drum=[], hatTone=50, hatDelay=0, sweep=True, countIn=0,
             trainer=dict(on=False, step=5, every=4, max=160), gap=dict(on=False, play=4, mute=1),
             vol=80, sync=0, haptics=False, midiOut="")
    s.update(o)
    if s["acc"] is None:
        s["acc"] = [2] + [1] * (s["num"] - 1)
    return s


def clicks(page):
    cs = sorted(page.evaluate("window.__clicks"), key=lambda c: c["t"])
    for c in cs:
        c["rate"] = round(c["rate"], 2)      # playbackRate is a 32-bit float (1.3 reads back as 1.29999995)
    return cs


def near(a, b, tol, what=""):
    assert abs(a - b) <= tol, f"{what}: expected {b} +/- {tol}, got {a}"


def diffs(xs):
    return [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]


class Env:
    def __init__(self, browser, url):
        self.browser, self.url, self.errors = browser, url, []

    def page(self, state=None, init=(), reduced=False, grant=False, viewport=None):
        kw = dict(viewport=viewport or {"width": 390, "height": 844}, has_touch=True)
        if reduced:
            kw["reduced_motion"] = "reduce"
        ctx = self.browser.new_context(**kw)
        if grant:
            ctx.grant_permissions(["clipboard-read", "clipboard-write"])
        # Web fonts come from Google; the sandbox has no route there, so serve nothing and let the fallbacks show.
        ctx.route(re.compile(r"https://fonts\.(googleapis|gstatic)\.com/.*"),
                  lambda r: r.fulfill(status=200, content_type="text/css", body=""))
        page = ctx.new_page()
        for s in (HOOK, *init):
            page.add_init_script(s)
        page.on("console", lambda m: self.errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        page.goto(self.url)
        if state is not None:
            page.evaluate("s => localStorage.setItem('metronome.v3', JSON.stringify(s))", state)
            page.reload()
        page.wait_for_timeout(300)
        return page

    def run_clicks(self, state, seconds):
        page = self.page(state)
        page.click("#go")
        page.wait_for_timeout(int(seconds * 1000))
        page.click("#go")
        return page, clicks(page)


# ---------------------------------------------------------------- tests

def test_basic_timing_and_accents(env):
    page, c = env.run_clicks(cfg(bpm=240), 2.3)
    main = c[:8]
    for d in diffs([x["t"] for x in main]):
        near(d, 0.25, 1e-4, "beat spacing at 240 BPM")
    assert [x["rate"] for x in main] == [1.3, 1, 1, 1, 1.3, 1, 1, 1], "accent on beat 1 only"
    assert page.inner_text("#go") == "Start"


def test_beat_accent_editing(env):
    page = env.page(cfg(bpm=240))
    dot = page.locator("#beats .dot").nth(1)
    assert "lvl1" in dot.get_attribute("class")
    dot.click(); assert "lvl2" in dot.get_attribute("class"), "normal -> accent"
    dot.click(); assert "lvl0" in dot.get_attribute("class"), "accent -> off"
    page.wait_for_timeout(100)
    page.click("#go"); page.wait_for_timeout(2300); page.click("#go")
    c = clicks(page)[:6]
    # beat 2 is off, so intervals are 0.5, 0.25, 0.25, 0.5 ...
    ds = diffs([x["t"] for x in c])
    near(ds[0], 0.5, 1e-4); near(ds[1], 0.25, 1e-4); near(ds[2], 0.25, 1e-4); near(ds[3], 0.5, 1e-4)
    dot.click(); assert "lvl1" in dot.get_attribute("class"), "off -> normal"


def test_time_signature_and_split(env):
    page, c = env.run_clicks(cfg(bpm=240, num=3, sub=2, acc=[2, 1, 1]), 2.3)
    rates = [x["rate"] for x in c[:12]]
    assert rates[:6] == [1.3, 0.8, 1, 0.8, 1, 0.8], rates
    assert rates[6] == 1.3, "bar of 3 beats restarts the accent"
    for d in diffs([x["t"] for x in c[:6]]):
        near(d, 0.125, 1e-4, "split 2 spacing")


def test_swing(env):
    page, c = env.run_clicks(cfg(bpm=120, num=2, sub=2, swing=67), 2.3)
    t = [x["t"] for x in c[:6]]
    near(t[1] - t[0], 0.335, 1e-3, "swung off-beat position"); near(t[2] - t[0], 0.5, 1e-4, "next beat")
    near(t[3] - t[2], 0.335, 1e-3, "swung off-beat position")


def test_count_in_and_polyrhythm(env):
    page, c = env.run_clicks(cfg(bpm=240, num=4, countIn=1, poly=3), 3.0)
    main_buf = c[0]["buf"]
    count_in = [x for x in c if x["t"] < c[0]["t"] + 1.0 - 1e-6]
    assert len(count_in) == 4 and all(x["buf"] == main_buf for x in count_in), "count-in bar has no cross-rhythm"
    bar0 = [x for x in c if c[0]["t"] + 1.0 - 1e-6 <= x["t"] < c[0]["t"] + 2.0 - 1e-6]
    poly = [x for x in bar0 if x["buf"] != main_buf]
    assert len([x for x in bar0 if x["buf"] == main_buf]) == 4 and len(poly) == 3, (len(bar0), len(poly))
    for d in diffs([x["t"] for x in poly]):
        near(d, 1 / 3, 1e-4, "3-against-4 spacing")
    near(poly[0]["t"], c[4]["t"], 1e-4, "cross-rhythm starts on the downbeat")


def test_speed_trainer(env):
    state = cfg(bpm=240, num=2, trainer=dict(on=True, step=20, every=1, max=300))
    page = env.page(state)
    page.click("#go"); page.wait_for_timeout(2500)
    shown = int(page.inner_text("#bpmOut"))
    assert shown > 240, f"tempo should have risen, shows {shown}"
    page.click("#go")
    c = [x["t"] for x in clicks(page)]
    ds = diffs(c)[:8]
    want = [60 / 240] * 2 + [60 / 260] * 2 + [60 / 280] * 2 + [60 / 300] * 2
    for got, w in zip(ds, want):
        near(got, w, 1e-4, "trainer beat spacing")
    assert page.inner_text("#bpmOut") == "240", "tempo returns to the starting value on stop"


def test_silent_bars(env):
    page, c = env.run_clicks(cfg(bpm=240, num=2, gap=dict(on=True, play=1, mute=1)), 2.6)
    ds = diffs([x["t"] for x in c[:6]])
    near(ds[0], 0.25, 1e-4); near(ds[1], 0.75, 1e-4); near(ds[2], 0.25, 1e-4); near(ds[3], 0.75, 1e-4)


def test_sounds_exist(env):
    page = env.page(cfg())
    page.click("#pSound summary")
    for name in ["click", "wood", "rim", "cowbell", "hat", "beep", "drums", "brushes"]:
        page.select_option("#sound", name)
    assert page.evaluate("window.__clicks.length") == 13, "each sound previews once; drums play kick, snare; brushes a little phrase"
    assert len({c["buf"] for c in page.evaluate("window.__clicks")}) == 12, "every sound and kit part is distinct"
    assert page.locator("#soundHelp").is_visible(), "drum help is shown while Drum beat is selected"
    page.select_option("#sound", "beep")
    assert not page.locator("#soundHelp").is_visible()


def test_presets_and_share_link(env):
    page = env.page(cfg(bpm=132, num=7, den=8, sub=2, swing=60), grant=True)
    page.click("#pPresets summary")
    page.click("#pRhythm summary")
    page.fill("#pName", "Seven eight")
    page.click("#pSave")
    page.select_option("#num", "3")
    assert "Seven eight" in page.inner_text("#pSel")
    page.select_option("#pSel", "0")
    assert page.input_value("#num") == "7" and page.inner_text("#bpmOut") == "132", "preset restores settings"
    page.click("#pLink")
    link = page.evaluate("navigator.clipboard.readText()")
    assert "#s=" in link, link
    other = env.page()
    other.goto(link)
    other.wait_for_timeout(300)
    assert other.input_value("#num") == "7" and other.input_value("#den") == "8" and other.inner_text("#bpmOut") == "132"
    assert "Loaded shared settings" in other.inner_text("#msg")
    assert "#" not in other.url, "hash is cleared after loading"
    other.goto(link.split("#")[0] + "#s=!!!")
    other.wait_for_timeout(200)
    assert "could not be read" in other.inner_text("#msg")
    page.click("#pDel")
    assert "No presets yet" in page.inner_text("#pSel")


def test_preset_prev_next(env):
    page = env.page(cfg(bpm=90))
    page.click("#pPresets summary")
    for name, bpm in [("A", 90), ("B", 120), ("C", 150)]:
        page.evaluate(f"document.getElementById('slider').value={bpm}; document.getElementById('slider').dispatchEvent(new Event('input'))")
        page.fill("#pName", name); page.click("#pSave")
    page.click("#pNext")
    assert page.inner_text("#bpmOut") == "90", "wraps to the first preset"
    page.click("#pNext"); assert page.inner_text("#bpmOut") == "120"
    page.click("#pPrev"); assert page.inner_text("#bpmOut") == "90"


def test_persistence_and_migration(env):
    page = env.page(cfg(bpm=77, num=5, sound="rim"))
    assert page.inner_text("#bpmOut") == "77" and page.input_value("#sound") == "rim" and page.locator("#beats .dot").count() == 5
    old = env.page()
    old.evaluate("localStorage.removeItem('metronome.v3'); localStorage.setItem('metronome.v2', JSON.stringify({bpm: 88, bpb: 6, sub: 2, sound: 'wood', vol: 50}))")
    old.reload(); old.wait_for_timeout(200)
    assert old.inner_text("#bpmOut") == "88" and old.input_value("#num") == "6" and old.input_value("#sound") == "wood"


def test_dial_and_controls(env):
    import math
    page = env.page(cfg(bpm=100))
    box = page.locator("#dial").bounding_box()
    cx, cy, r = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, box["width"] / 2 * 0.8
    pt = lambda a: (cx + r * math.sin(math.radians(a)), cy - r * math.cos(math.radians(a)))
    knob = -135 + (100 - 20) / 280 * 270                       # where the knob sits at 100 BPM
    bpm = lambda: int(page.inner_text("#bpmOut"))
    page.mouse.click(*pt(120))                                  # a plain tap on the ring changes nothing
    assert bpm() == 100, "tapping the ring must not jump the tempo"
    page.mouse.click(*pt(-100)); assert bpm() == 100
    page.mouse.move(*pt(knob)); page.mouse.down()
    assert bpm() == 100, "grabbing the ring does not change tempo"
    for a in (-30, 0): page.mouse.move(*pt(a))
    near(bpm(), 160, 2, "dragged from the knob to straight up")
    for a in (45, 90): page.mouse.move(*pt(a))
    near(bpm(), 253, 3, "dragged on to the right")
    for a in (60, 20, -40): page.mouse.move(*pt(a))
    near(bpm(), 100 + (-40 - knob) * 280 / 270, 3, "and back again")
    page.mouse.up()
    before = bpm()
    page.mouse.click(cx, cy); assert bpm() == before, "touches in the middle are ignored"
    page.keyboard.press("ArrowUp"); assert bpm() == before + 1
    page.keyboard.press("Shift+ArrowDown"); assert bpm() == before - 9
    page.keyboard.press("Space"); page.wait_for_timeout(200)
    assert page.inner_text("#go") == "Stop"
    page.keyboard.press("Space"); assert page.inner_text("#go") == "Start"


def test_tap_tempo(env):
    page = env.page(cfg())
    for _ in range(5):
        page.dispatch_event("#tap", "pointerdown", {"button": 0}); page.wait_for_timeout(500)
    near(int(page.inner_text("#bpmOut")), 120, 4, "tap tempo")


def test_stage_mode(env):
    page = env.page(cfg(bpm=120))
    page.click("#stageBtn")
    assert page.evaluate("document.getElementById('stageView').classList.contains('open')")
    assert page.evaluate("document.getElementById('app').inert"), "page behind stage mode is inert"
    page.click("#stGo")
    peak = 0
    for _ in range(50):
        page.wait_for_timeout(40)
        peak = max(peak, float(page.evaluate("getComputedStyle(document.getElementById('stageView')).getPropertyValue('--flash') || '0'") or 0))
    assert peak > 0.1, f"stage flashes on the beat (peak {peak})"
    page.click("#stGo")
    page.keyboard.press("Escape")
    assert not page.evaluate("document.getElementById('stageView').classList.contains('open')")
    assert not page.evaluate("document.getElementById('app').inert")


def test_stage_mode_no_flash_with_reduced_motion(env):
    page = env.page(cfg(bpm=120), reduced=True)
    page.click("#stageBtn"); page.click("#stGo")
    peak = 0
    for _ in range(40):
        page.wait_for_timeout(40)
        peak = max(peak, float(page.evaluate("getComputedStyle(document.getElementById('stageView')).getPropertyValue('--flash') || '0'") or 0))
    page.click("#stGo")
    assert peak == 0, f"reduced motion should not flash (peak {peak})"


def test_midi_clock(env):
    page = env.page(cfg(bpm=120, num=4), init=(MIDI_MOCK,))
    assert page.locator("#pMidi").is_visible()
    page.click("#pMidi summary")
    page.check("#midiOn")
    page.wait_for_timeout(200)
    assert page.input_value("#midiOut") == "mock1"
    page.click("#go"); page.wait_for_timeout(2100); page.click("#go")
    msgs = page.evaluate("window.__midi")
    status = [m["d"][0] for m in msgs]
    assert 0xFA in status and status.index(0xFA) < status.index(0xF8), "Start precedes the first clock"
    assert status[-1] == 0xFC, "Stop is sent last"
    clk = [m for m in msgs if m["d"][0] == 0xF8]
    assert 95 <= len(clk) <= 112, f"about 48 clocks a second at 120 BPM, got {len(clk)} in ~2.1s"
    span = (clk[-1]["ts"] - clk[0]["ts"]) / (len(clk) - 1)
    near(span, 1000 / 48, 0.3, "clock spacing in ms")


def test_midi_hidden_without_support(env):
    page = env.page(cfg(), init=("delete Navigator.prototype.requestMIDIAccess; navigator.requestMIDIAccess = undefined;",))
    assert not page.locator("#pMidi").is_visible()


def test_calibration(env):
    page = env.page(cfg())
    page.click("#pSound summary")
    page.click("#calBtn")
    assert page.evaluate("document.getElementById('calDlg').open")
    page.click("#calStart")
    page.wait_for_timeout(100)
    cl = clicks(page)
    assert len(cl) == 12, len(cl)
    for d in diffs([x["t"] for x in cl]):
        near(d, 0.6, 1e-4, "calibration click spacing")
    # Simulate someone who hears each click 120 ms after it was scheduled.
    page.evaluate("""() => {
      const ctx = window.__ctx, pad = document.getElementById('calPad');
      window.__clicks.forEach(c => {
        const wait = (c.t - ctx.currentTime + 0.12) * 1000;
        setTimeout(() => pad.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true})), Math.max(0, wait));
      });
    }""")
    page.wait_for_timeout(9000)
    out = page.inner_text("#calOut")
    assert "Suggested visual delay" in out, out
    assert page.locator("#calApply").is_enabled()
    latency = page.evaluate("(window.__ctx.outputLatency || 0) * 1000")
    page.click("#calApply")
    applied = int(page.input_value("#sync"))
    near(applied, max(0, 120 - latency), 45, "calibration result")
    assert not page.evaluate("document.getElementById('calDlg').open")


def test_pwa_and_offline(env):
    page = env.page(cfg())
    page.wait_for_timeout(500)
    man = page.evaluate("fetch('manifest.webmanifest').then(r => r.json())")
    assert man["display"] == "standalone" and man["start_url"] and man["name"]
    sizes = {i["sizes"]: i for i in man["icons"]}
    assert "192x192" in sizes and "512x512" in sizes, "installable icon sizes"
    assert any(i.get("purpose") == "maskable" for i in man["icons"]), "maskable icon"
    for i in man["icons"]:
        r = page.evaluate("src => fetch(src).then(r => [r.status, r.headers.get('content-type')])", i["src"])
        assert r[0] == 200, (i["src"], r)
    page.reload(); page.wait_for_timeout(600)
    assert page.evaluate("!!navigator.serviceWorker.controller"), "service worker controls the page"
    page.context.set_offline(True)
    page.reload(); page.wait_for_timeout(500)
    assert page.title() == "Metronome" and page.locator("#go").is_visible(), "works offline"
    page.context.set_offline(False)


def test_layout_fits_phones_and_landscape(env):
    for vp in [{"width": 360, "height": 740}, {"width": 390, "height": 844}, {"width": 740, "height": 360}]:
        page = env.page(cfg(num=12), viewport=vp)
        sw = page.evaluate("document.documentElement.scrollWidth")
        assert sw <= vp["width"], f"horizontal overflow at {vp}: {sw}"


def test_background_tab_keeps_time(env):
    page = env.page(cfg(bpm=240))
    page.click("#go")
    page.wait_for_timeout(500)
    other = page.context.new_page()           # backgrounds the metronome tab
    other.goto("about:blank")
    page.wait_for_timeout(4000)
    page.bring_to_front()
    page.wait_for_timeout(300)
    page.click("#go")
    ts = [x["t"] for x in clicks(page)]
    assert len(ts) >= 18, f"clicks kept being scheduled in the background ({len(ts)})"
    for d in diffs(ts):
        near(d, 0.25, 1e-4, "no gaps while backgrounded")


def arm_angle(page):
    import re
    return float(re.match(r"rotate\((-?[\d.]+)", page.get_attribute("#arm", "transform")).group(1))


def swipe(page, dx, dy=0):
    box = page.locator("#stageCard").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] * 0.6
    page.mouse.move(x, y); page.mouse.down()
    page.mouse.move(x + dx / 2, y + dy / 2); page.mouse.move(x + dx, y + dy)
    page.mouse.up()


def test_swipe_pendulum_to_start(env):
    page = env.page(cfg(bpm=100))
    swipe(page, 20)                       # too short
    page.wait_for_timeout(200)
    assert page.inner_text("#go") == "Start", "a short drag does not start it"
    assert page.get_attribute("#arm", "transform").startswith("rotate(0.00"), "arm springs back"
    swipe(page, 8, 90)                    # mostly vertical (a scroll)
    page.wait_for_timeout(200)
    assert page.inner_text("#go") == "Start", "a vertical drag does not start it"
    swipe(page, 90)
    page.wait_for_timeout(250)
    assert page.inner_text("#go") == "Stop", "a swipe right starts it"
    assert arm_angle(page) > 5, "first swing is on the right"
    page.click("#go")
    assert page.inner_text("#go") == "Start"


def test_swipe_left_starts_on_the_left(env):
    page = env.page(cfg(bpm=100))
    swipe(page, -90)
    page.wait_for_timeout(250)
    assert page.inner_text("#go") == "Stop"
    assert arm_angle(page) < -5, "first swing is on the left"
    page.click("#go")
    page.click("#go"); page.wait_for_timeout(250)
    assert arm_angle(page) > 5, "a normal start still begins on the right"
    page.click("#go")


def test_swipe_ignored_while_playing(env):
    page = env.page(cfg(bpm=100))
    page.click("#go"); page.wait_for_timeout(300)
    swipe(page, 90)
    page.wait_for_timeout(200)
    assert page.inner_text("#go") == "Stop", "swiping while playing does not stop or restart it"
    page.click("#go")


def test_time_signature_badge(env):
    page = env.page(cfg())
    badge = page.locator("#sigBadge")
    assert badge.inner_text() == "4/4"
    page.click("#sigBadge")
    assert page.evaluate("document.getElementById('sigDlg').open")
    assert page.get_attribute("#sigPresets button:has-text('4/4')", "aria-pressed") == "true"
    page.click("#sigPresets button:has-text('6/8')")
    assert not page.evaluate("document.getElementById('sigDlg').open"), "a preset applies and closes"
    assert badge.inner_text() == "6/8"
    assert page.locator("#beats .dot").count() == 6
    assert page.input_value("#num") == "6" and page.input_value("#den") == "8", "Rhythm panel stays in sync"
    page.click("#sigBadge")
    page.select_option("#sigNum", "5")
    assert badge.inner_text() == "5/8" and page.locator("#beats .dot").count() == 5, "custom picker applies live"
    page.select_option("#sigDen", "16")
    assert badge.inner_text() == "5/16"
    page.keyboard.press("Escape")
    assert not page.evaluate("document.getElementById('sigDlg').open")
    page.reload(); page.wait_for_timeout(300)
    assert page.locator("#sigBadge").inner_text() == "5/16", "persists"


def test_badge_change_while_playing_and_keys_blocked(env):
    page = env.page(cfg(bpm=240))
    page.click("#go"); page.wait_for_timeout(400)
    page.click("#sigBadge")
    assert page.evaluate("document.activeElement.id") == "sigDone", "focus starts on Done"
    page.keyboard.press("ArrowUp"); page.keyboard.press("t")
    assert page.inner_text("#bpmOut") == "240", "tempo keys are inert behind an open dialog"
    page.click("#sigPresets button:has-text('3/4')")
    page.wait_for_timeout(1500)
    page.click("#go")
    c = [x for x in clicks(page) if x["rate"] == 1.3]
    ds = diffs([x["t"] for x in c])
    assert any(abs(d - 0.75) < 1e-3 for d in ds), f"bars become 3 beats long: {ds}"


def touch_swipe(page, dx, dy=0, steps=8):
    box = page.locator("#stageCard").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] * 0.6
    cdp = page.context.new_cdp_session(page)
    send = lambda t, pts: cdp.send("Input.dispatchTouchEvent", {"type": t, "touchPoints": pts})
    send("touchStart", [{"x": x, "y": y, "id": 1}])
    for i in range(1, steps + 1):
        send("touchMove", [{"x": x + dx * i / steps, "y": y + dy * i / steps, "id": 1}])
        page.wait_for_timeout(12)
    send("touchEnd", [])


def test_touch_swipe_on_a_phone(env):
    page = env.page(cfg(bpm=100))
    touch_swipe(page, 100)
    page.wait_for_timeout(300)
    assert page.inner_text("#go") == "Stop", "a finger swipe starts it"
    page.click("#go")
    touch_swipe(page, 5, 120)                 # a vertical finger drag is a scroll, not a start
    page.wait_for_timeout(300)
    assert page.inner_text("#go") == "Start", "vertical finger drag does not start it"
    touch_swipe(page, -100)
    page.wait_for_timeout(300)
    assert page.inner_text("#go") == "Stop" and arm_angle(page) < -5
    page.click("#go")


def test_swipe_while_audio_locked_shows_gate_that_swallows_the_tap(env):
    page = env.page(cfg(bpm=240), init=(LOCKED_AUDIO,))
    swipe(page, -90)                                    # first thing the user does is a swipe, as on iPhone
    page.wait_for_timeout(500)
    assert page.locator("#gate").is_visible(), "told to tap"
    assert page.inner_text("#go") == "Start", "does not pretend to be playing while audio is locked"
    box = page.locator("#dial").bounding_box()          # tap right on the tempo dial, like the screenshot
    tap_at = (box["x"] + box["width"] * 0.2, box["y"] + box["height"] * 0.75)
    page.mouse.click(*tap_at)                           # the OS refuses to unlock this time
    page.wait_for_timeout(900)
    assert page.inner_text("#bpmOut") == "240", "the tap was swallowed by the gate, tempo untouched"
    assert page.locator("#gate").is_visible() and "still blocked" in page.inner_text("#gateText"), page.inner_text("#gate")
    assert "locked" in page.inner_text("#gateSub") or "suspended" in page.inner_text("#gateSub")
    page.evaluate("window.__allowResume = true")        # now the OS accepts a real tap
    page.mouse.click(*tap_at)
    page.wait_for_timeout(700)
    assert not page.locator("#gate").is_visible(), "gate closes"
    assert page.inner_text("#bpmOut") == "240", "tempo still untouched"
    assert page.inner_text("#go") == "Stop", "starts after the tap"
    assert page.evaluate("window.__ctx.state") == "running"
    seen = {arm_angle(page)}
    for _ in range(8):
        page.wait_for_timeout(60); seen.add(arm_angle(page))
    assert len(seen) > 4, "pendulum is swinging"
    page.click("#go")
    swipe(page, 90)                                     # audio is unlocked now: no gate any more
    page.wait_for_timeout(400)
    assert page.inner_text("#go") == "Stop" and not page.locator("#gate").is_visible()
    page.click("#go")


def test_gate_remembers_swipe_direction_and_escape_cancels(env):
    page = env.page(cfg(bpm=100), init=(LOCKED_AUDIO,))
    swipe(page, -90); page.wait_for_timeout(500)
    page.evaluate("window.__allowResume = true")
    page.mouse.click(200, 400); page.wait_for_timeout(350)
    assert page.inner_text("#go") == "Stop" and arm_angle(page) < -5, "first swing on the side that was swiped"
    page.click("#go")
    page2 = env.page(cfg(bpm=100), init=(LOCKED_AUDIO,))
    swipe(page2, 90); page2.wait_for_timeout(500)
    assert page2.locator("#gate").is_visible()
    page2.keyboard.press("Escape")
    assert not page2.locator("#gate").is_visible() and page2.inner_text("#go") == "Start"


def test_any_tap_unlocks_audio_before_start(env):
    page = env.page(cfg(bpm=240), init=(LOCKED_AUDIO,))
    assert not page.evaluate("!!window.__ctx"), "no audio context before any interaction"
    page.evaluate("window.__allowResume = true")
    page.mouse.click(8, 500)
    page.wait_for_timeout(300)
    assert page.evaluate("window.__ctx.state") == "running", "a tap anywhere unlocks audio"
    swipe(page, 90)
    page.wait_for_timeout(500)
    assert page.inner_text("#go") == "Stop" and not page.locator("#gate").is_visible(), "later swipe starts cleanly"
    page.click("#go")


def test_start_button_works_even_if_audio_was_locked(env):
    page = env.page(cfg(bpm=240), init=(LOCKED_AUDIO,))
    page.evaluate("window.__allowResume = true")
    page.click("#go"); page.wait_for_timeout(600)
    assert page.evaluate("window.__ctx.state") == "running" and page.inner_text("#go") == "Stop"
    page.click("#go")


def test_tap_pendulum_starts_without_any_message(env):
    page = env.page(cfg(bpm=100), init=(LOCKED_AUDIO,))
    page.evaluate("window.__allowResume = true")       # a real tap is accepted
    box = page.locator("#stageCard").bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] * 0.6)
    page.wait_for_timeout(500)
    assert page.inner_text("#go") == "Stop", "a tap on the pendulum starts it"
    assert not page.locator("#gate").is_visible(), "no gate when the first touch is a tap"
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] * 0.6)
    page.wait_for_timeout(200)
    assert page.inner_text("#go") == "Stop", "tapping while playing does not stop it"
    page.click("#go")
    page.mouse.click(box["x"] + 30, box["y"] + 22)     # the badge opens the dialog and does not start
    assert page.evaluate("document.getElementById('sigDlg').open") and page.inner_text("#go") == "Start"
    page.keyboard.press("Escape")


def test_swipe_then_release_does_not_double_start(env):
    page = env.page(cfg(bpm=240))
    swipe(page, 90)                                      # a mouse drag also fires a click on release
    page.wait_for_timeout(1100)
    page.click("#go")
    ts = [x["t"] for x in clicks(page) if x["rate"] in (1.3, 1)]
    for d in diffs(ts[:6]):
        near(d, 0.25, 1e-4, "one steady stream, not two overlapping starts")


def split_drums(c):
    """Separate kick/snare from the hi-hat, which is the most common sound in a drum bar."""
    from collections import Counter
    hat = Counter(x["buf"] for x in c).most_common(1)[0][0]
    return [x for x in c if x["buf"] != hat], [x for x in c if x["buf"] == hat]


def test_drum_beat_backbeat_with_hats(env):
    page, c = env.run_clicks(cfg(bpm=240, num=4, sound="drums"), 2.3)
    beats, hats = split_drums(c)
    b = [x["buf"] for x in beats[:8]]
    assert b[0] == b[2] == b[4] == b[6] and b[1] == b[3] == b[5] == b[7] and b[0] != b[1], f"kick snare kick snare: {b}"
    for d in diffs([x["t"] for x in beats[:8]]):
        near(d, 0.25, 1e-4, "drum beats sit on the grid")
    ht = [h["t"] for h in hats]
    for x in beats[:6]:
        assert any(abs(h - x["t"]) < 1e-4 for h in ht), "a hat plays under every kick and snare"
        assert any(abs(h - (x["t"] + 0.125)) < 1e-4 for h in ht), "and a hat plays between the beats"
    page.reload(); page.wait_for_timeout(300)
    assert page.input_value("#sound") == "drums", "the setting survives a reload"


def test_drum_beat_waltz_and_compound(env):
    _, c = env.run_clicks(cfg(bpm=240, num=3, sound="drums"), 1.6)
    beats, _ = split_drums(c)
    b = [x["buf"] for x in beats[:3]]
    assert b[1] == b[2] and b[0] != b[1], f"3/4 is kick snare snare: {b}"
    _, c = env.run_clicks(cfg(bpm=240, num=6, den=8, sound="drums"), 1.9)
    in_bar = [x for x in c if x["t"] < c[0]["t"] + 1.5 - 1e-6]
    assert len(in_bar) == 6, "compound meters already have hats on the eighths, so none are added"
    b = [x["buf"] for x in in_bar]
    assert b[1] == b[2] == b[4] == b[5] and len({b[0], b[3], b[1]}) == 3, f"6/8 is kick hat hat snare hat hat: {b}"


def test_drum_beat_sixteenth_hats(env):
    _, c = env.run_clicks(cfg(bpm=240, num=4, hat=4, sound="drums"), 2.3)
    beats, hats = split_drums(c)
    t0 = beats[0]["t"]
    one_beat = sorted(h["t"] for h in hats if t0 - 1e-4 <= h["t"] < t0 + 0.25 - 1e-6)
    assert len(one_beat) == 4, f"four hats per beat at sixteenths: {one_beat}"
    for d in diffs(one_beat):
        near(d, 0.0625, 1e-4, "sixteenth spacing")


def test_drum_beat_respects_accents_and_mutes(env):
    _, c = env.run_clicks(cfg(bpm=240, num=4, acc=[2, 0, 1, 1], sound="drums"), 2.3)
    beats, _ = split_drums(c)
    ds = diffs([x["t"] for x in beats[:3]])
    near(ds[0], 0.5, 1e-4, "muted snare is skipped"); near(ds[1], 0.25, 1e-4)


def test_hat_rate_setting(env):
    _, c = env.run_clicks(cfg(bpm=240, num=4, sound="drums", hat=0), 2.3)
    assert len({x["buf"] for x in c}) == 2, "no hats when the hi-hat is off"
    _, c = env.run_clicks(cfg(bpm=240, num=4, sound="drums", hat=1), 2.3)
    beats, hats = split_drums(c)
    assert all(any(abs(h["t"] - b["t"]) < 1e-4 for b in beats) for h in hats[:8]), "hats only on the beats"
    _, c = env.run_clicks(cfg(bpm=240, num=4, sound="drums", hat=3), 2.3)
    beats, hats = split_drums(c)
    t0 = beats[0]["t"]
    one = sorted(h["t"] for h in hats if t0 - 1e-4 <= h["t"] < t0 + 0.25 - 1e-6)
    assert len(one) == 3, f"triplet hats: {one}"
    for d in diffs(one):
        near(d, 0.25 / 3, 1e-4, "triplet spacing")
    _, c = env.run_clicks(cfg(bpm=120, num=2, sound="drums", hat=2, swing=67), 2.3)
    beats, hats = split_drums(c)
    for b in beats[:4]:
        assert any(abs(h["t"] - (b["t"] + 0.5 * 0.67)) < 1e-3 for h in hats), "swung off-beat hat"


def test_split_is_ignored_by_drum_kits(env):
    _, c = env.run_clicks(cfg(bpm=240, num=4, sub=4, sound="drums", hat=1), 2.3)
    beats, hats = split_drums(c)
    assert all(any(abs(h["t"] - b["t"]) < 1e-4 for b in beats) for h in hats[:8]), "Split each beat does not add hats"


def by_name(c, name):
    return [x for x in c if x["name"] == name]


def test_brush_kit(env):
    page, c = env.run_clicks(cfg(bpm=240, num=4, sound="brushes"), 2.3)
    beats = sorted(by_name(c, "bkick") + by_name(c, "bsnare"), key=lambda x: x["t"])
    b = [x["name"] for x in beats[:8]]
    assert b == ["bkick", "bsnare"] * 4, f"same backbeat: {b}"
    for d in diffs([x["t"] for x in beats[:8]]):
        near(d, 0.25, 1e-4, "brush beats sit on the grid")
    taps, sweeps = by_name(c, "bhat"), by_name(c, "bsweep")
    assert len(taps) >= 7 and all(any(abs(h["t"] - (x["t"] + 0.125)) < 1e-4 for x in beats) for h in taps[:7]), \
        "brush taps fill between the beats, not on top of them"
    assert len(sweeps) >= 8 and all(any(abs(s["t"] - x["t"]) < 1e-4 for x in beats) for s in sweeps[:8]), "a sweep on every beat"
    page.reload(); page.wait_for_timeout(300)
    assert page.input_value("#sound") == "brushes"


def test_jazz_waltz_brushes(env):
    _, c = env.run_clicks(cfg(bpm=120, num=3, sound="brushes", swing=62), 3.2)
    beats = sorted(by_name(c, "bkick") + by_name(c, "bsnare"), key=lambda x: x["t"])
    assert [x["name"] for x in beats[:3]] == ["bkick", "bsnare", "bsnare"], "jazz waltz: feathered kick, then two taps"
    for x in beats[:5]:
        assert any(abs(h["t"] - (x["t"] + 0.5 * 0.62)) < 1e-3 for h in by_name(c, "bhat")), "swung in-between tap"
    _, c = env.run_clicks(cfg(bpm=120, num=3, sound="brushes", sweep=False), 2.0)
    assert not by_name(c, "bsweep") and by_name(c, "bsnare"), "the sweep can be turned off"
    _, c = env.run_clicks(cfg(bpm=120, num=3, sound="drums"), 2.0)
    assert not by_name(c, "bsweep"), "no sweep in the normal drum kit"
    page = env.page(cfg())
    page.click("#pSound summary"); page.select_option("#sound", "brushes")
    assert page.locator("#sweepRow").is_visible() and page.inner_text("#hatLabel") == "Brush taps"
    assert page.evaluate("JSON.parse(localStorage.getItem('metronome.v3')).swing") == 62, "brushes start with a gentle swing"
    page.uncheck("#sweep"); page.reload(); page.wait_for_timeout(300)
    assert page.evaluate("JSON.parse(localStorage.getItem('metronome.v3')).sweep") is False
    page.click("#pSound summary"); page.select_option("#sound", "drums")
    assert not page.locator("#sweepRow").is_visible() and page.inner_text("#hatLabel") == "Hi-hat"


def test_edit_drum_per_beat(env):
    page = env.page(cfg(bpm=240, num=3, sound="drums", hat=0))
    dots = page.locator("#beats .dot")
    part = lambda i: dots.nth(i).get_attribute("data-part")
    assert [part(0), part(1), part(2)] == ["K", "S", "S"], "3/4 default is kick snare snare"
    dots.nth(2).click(); assert part(2) == "H"
    dots.nth(2).click(); assert part(2) is None and "lvl0" in dots.nth(2).get_attribute("class"), "off"
    dots.nth(2).click(); assert part(2) == "K"
    page.click("#go"); page.wait_for_timeout(2000); page.click("#go")
    b = [x["buf"] for x in clicks(page)[:6]]
    assert b[0] == b[2] == b[3] == b[5] and b[1] == b[4] and b[0] != b[1], f"kick snare kick, twice: {b}"
    page.reload(); page.wait_for_timeout(300)
    assert page.locator("#beats .dot").nth(2).get_attribute("data-part") == "K", "edits are saved"
    page.click("#sigBadge"); page.click("#sigPresets button:has-text('4/4')")
    parts = [page.locator("#beats .dot").nth(i).get_attribute("data-part") for i in range(4)]
    assert parts == ["K", "S", "K", "S"], "a new meter starts from its default pattern"


def test_drum_edits_travel_in_share_links(env):
    page = env.page(cfg(num=3, sound="brushes", hat=3, drum=[None, "s", "h"], hatTone=80, hatDelay=25), grant=True)
    assert [page.locator("#beats .dot").nth(i).get_attribute("data-part") for i in range(3)] == ["K", "S", "H"]
    page.click("#pPresets summary"); page.click("#pLink")
    link = page.evaluate("navigator.clipboard.readText()")
    other = env.page(); other.goto(link); other.wait_for_timeout(300)
    assert other.input_value("#sound") == "brushes" and other.input_value("#hatRate") == "3"
    assert other.input_value("#hatTone") == "80" and other.input_value("#hatDelay") == "25", "tone and timing travel too"
    assert [other.locator("#beats .dot").nth(i).get_attribute("data-part") for i in range(3)] == ["K", "S", "H"]


def test_drum_ui_adapts(env):
    page = env.page(cfg(sound="drums", hat=2))
    page.click("#pSound summary"); page.click("#pRhythm summary")
    assert page.locator("#hatRow").is_visible() and page.locator("#soundHelp").is_visible()
    assert page.locator("#sub").is_disabled(), "Split each beat is replaced by the Hi-hat setting"
    assert not page.locator("#swingAmt").is_disabled(), "swing works on eighth hats"
    assert "Tap a beat to change its drum" in page.inner_text("#dotNote")
    page.select_option("#hatRate", "3")
    assert page.locator("#swingAmt").is_disabled()
    page.select_option("#sound", "click")
    assert not page.locator("#hatRow").is_visible() and not page.locator("#sub").is_disabled()
    assert "normal, accent, off" in page.inner_text("#dotNote")
    assert page.locator("#beats .dot").nth(0).get_attribute("data-part") is None, "letters only in drum kits"


def test_hat_tone(env):
    _, c = env.run_clicks(cfg(bpm=240, num=4, sound="drums"), 2.3)
    beats, hats = split_drums(c)
    assert {h["rate"] for h in hats} == {1.0} and {b["rate"] for b in beats} == {1.0}, "normal tone is unchanged"
    _, c = env.run_clicks(cfg(bpm=240, num=4, sound="drums", hatTone=100), 2.3)
    beats, hats = split_drums(c)
    assert {h["rate"] for h in hats} == {1.52}, "brighter hats play faster"
    assert {b["rate"] for b in beats} == {1.0}, "kick and snare keep their pitch"
    _, c = env.run_clicks(cfg(bpm=240, num=4, sound="brushes", hatTone=0), 2.3)
    hats = by_name(c, "bhat")
    assert {h["rate"] for h in hats} == {0.66}, "darker hats play slower"


def test_hat_timing_off_the_grid(env):
    _, c = env.run_clicks(cfg(bpm=240, num=4, sound="drums", hatDelay=30), 2.3)
    beats, hats = split_drums(c)
    for d in diffs([x["t"] for x in beats[:8]]):
        near(d, 0.25, 1e-4, "kick and snare stay on the grid")
    ht = [h["t"] for h in hats]
    for b in beats[:6]:
        assert any(abs(h - (b["t"] + 0.030)) < 1e-4 for h in ht), "on-beat hat sits 30 ms behind"
        assert any(abs(h - (b["t"] + 0.125 + 0.030)) < 1e-4 for h in ht), "off-beat hat sits 30 ms behind"
    _, c = env.run_clicks(cfg(bpm=240, num=4, sound="drums", hatDelay=-20), 2.3)
    beats, hats = split_drums(c)
    ht = [h["t"] for h in hats]
    for b in beats[1:6]:
        assert any(abs(h - (b["t"] - 0.020)) < 1e-4 for h in ht), "negative timing plays the hat early"


def test_hat_sliders_ui(env):
    page = env.page(cfg(sound="drums"))
    page.click("#pSound summary")
    assert page.locator("#hatTone").is_visible() and page.locator("#hatDelay").is_visible()
    assert page.inner_text("#hatToneOut") == "Normal" and page.inner_text("#hatDelayOut") == "On the grid"
    page.eval_on_selector("#hatTone", "e => { e.value = 80; e.dispatchEvent(new Event('input', {bubbles: true})); }")
    page.eval_on_selector("#hatDelay", "e => { e.value = 25; e.dispatchEvent(new Event('input', {bubbles: true})); }")
    assert page.inner_text("#hatToneOut") == "Brighter" and page.inner_text("#hatDelayOut") == "25 ms behind"
    page.eval_on_selector("#hatTone", "e => { e.value = 20; e.dispatchEvent(new Event('input', {bubbles: true})); }")
    page.eval_on_selector("#hatDelay", "e => { e.value = -15; e.dispatchEvent(new Event('input', {bubbles: true})); }")
    assert page.inner_text("#hatToneOut") == "Darker" and page.inner_text("#hatDelayOut") == "15 ms ahead"
    page.reload(); page.wait_for_timeout(300)
    assert page.input_value("#hatTone") == "20" and page.input_value("#hatDelay") == "-15", "saved across reloads"
    page.click("#pSound summary")
    page.select_option("#sound", "click")
    assert not page.locator("#hatTone").is_visible() and not page.locator("#hatDelay").is_visible()


def test_reset_to_defaults_with_undo(env):
    page = env.page(cfg(bpm=173, num=7, den=8, sound="drums", hat=4, hatTone=90, hatDelay=60, swing=60,
                        drum=["s", "h", None, "k", "s", "h", "k"], countIn=2, vol=40, sync=120,
                        trainer=dict(on=True, step=3, every=2, max=200)))
    page.evaluate("localStorage.setItem('metronome.presets', JSON.stringify([{name:'Keep me', s:{bpm:90}}]))")
    page.reload(); page.wait_for_timeout(300)
    assert page.locator("#resetAll").is_visible() and not page.locator("#undoReset").is_visible()
    page.click("#resetAll")
    st = page.evaluate("JSON.parse(localStorage.getItem('metronome.v3'))")
    assert (st["bpm"], st["num"], st["den"], st["sound"], st["hat"], st["hatTone"], st["hatDelay"], st["countIn"]) == \
        (100, 4, 4, "click", 2, 50, 0, 0), st
    assert not any(st["drum"]) and st["acc"] == [2, 1, 1, 1] and st["swing"] == 50 and not st["trainer"]["on"]
    assert st["vol"] == 80 and st["sync"] == 120, "volume resets, the visual delay calibration is kept"
    assert page.inner_text("#bpmOut") == "100" and page.inner_text("#sigBadge") == "4/4"
    assert page.input_value("#sound") == "click" and "reset" in page.inner_text("#msg").lower()
    assert "Keep me" in page.text_content("#pSel"), "presets survive a reset"
    page.click("#undoReset")
    st = page.evaluate("JSON.parse(localStorage.getItem('metronome.v3'))")
    assert (st["bpm"], st["num"], st["sound"], st["hatTone"], st["hatDelay"], st["vol"]) == (173, 7, "drums", 90, 60, 40), "undo"
    assert st["drum"][:2] == ["s", "h"] and not page.locator("#undoReset").is_visible()


def test_reset_while_playing_keeps_time(env):
    page = env.page(cfg(bpm=200, num=3, sound="wood"))
    page.click("#go"); page.wait_for_timeout(700)
    page.click("#resetAll"); page.wait_for_timeout(1500); page.click("#go")
    c = clicks(page)
    late = [x for x in c if x["t"] > c[-1]["t"] - 1.0]
    for d in diffs([x["t"] for x in late]):
        near(d, 0.6, 1e-3, "plays on at the default 100 BPM after a reset")


def test_site_header_links_home(env):
    page = env.page()
    hrefs = page.eval_on_selector_all(".site-bar a", "as => as.map(a => a.href)")
    assert hrefs and all(h.startswith("https://music.gwstudio.com.au") for h in hrefs)
    assert page.locator(".logo").is_visible()


def test_hats_only_in_drum_kits(env):
    kit = {"kick", "snare", "hat", "bkick", "bsnare", "bhat", "bsweep"}
    for snd in ["click", "wood", "rim", "cowbell", "beep"]:
        _, c = env.run_clicks(cfg(bpm=240, sound=snd, hat=4, sweep=True, swing=62), 1.2)
        assert {x["name"] for x in c} == {snd}, f"{snd} plays only itself, no hats or sweep"
    page = env.page(cfg(bpm=240, sound="brushes", hat=4))
    page.click("#go"); page.wait_for_timeout(700)
    page.click("#pSound summary"); page.select_option("#sound", "click"); page.evaluate("window.__clicks = []")
    page.wait_for_timeout(1200); page.click("#go")
    assert not ({x["name"] for x in clicks(page)} & kit), "switching to Click while playing stops the hats at once"


ORDER = [
    test_basic_timing_and_accents, test_beat_accent_editing, test_time_signature_and_split, test_swing,
    test_count_in_and_polyrhythm, test_speed_trainer, test_silent_bars, test_sounds_exist,
    test_presets_and_share_link, test_preset_prev_next, test_persistence_and_migration,
    test_dial_and_controls, test_tap_tempo, test_stage_mode, test_stage_mode_no_flash_with_reduced_motion,
    test_midi_clock, test_midi_hidden_without_support, test_calibration, test_pwa_and_offline,
    test_layout_fits_phones_and_landscape, test_background_tab_keeps_time,
    test_swipe_pendulum_to_start, test_swipe_left_starts_on_the_left, test_swipe_ignored_while_playing,
    test_time_signature_badge, test_badge_change_while_playing_and_keys_blocked,
    test_touch_swipe_on_a_phone,
    test_swipe_while_audio_locked_shows_gate_that_swallows_the_tap, test_gate_remembers_swipe_direction_and_escape_cancels,
    test_any_tap_unlocks_audio_before_start, test_start_button_works_even_if_audio_was_locked,
    test_tap_pendulum_starts_without_any_message, test_swipe_then_release_does_not_double_start,
    test_drum_beat_backbeat_with_hats, test_drum_beat_waltz_and_compound, test_drum_beat_sixteenth_hats,
    test_drum_beat_respects_accents_and_mutes,
    test_hat_rate_setting, test_split_is_ignored_by_drum_kits, test_brush_kit, test_jazz_waltz_brushes, test_edit_drum_per_beat,
    test_drum_edits_travel_in_share_links, test_drum_ui_adapts,
    test_hat_tone, test_hat_timing_off_the_grid, test_hat_sliders_ui,
    test_hats_only_in_drum_kits, test_reset_to_defaults_with_undo, test_reset_while_playing_keeps_time, test_site_header_links_home,
]


def main():
    only = sys.argv[1:]
    srv, url = serve()
    failed = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        env = Env(browser, url)
        for t in ORDER:
            if only and not any(o in t.__name__ for o in only):
                continue
            before = len(env.errors)
            try:
                t(env)
                if len(env.errors) > before:
                    raise AssertionError("console errors: " + "; ".join(env.errors[before:]))
                print(f"PASS  {t.__name__}")
            except Exception:
                failed += 1
                print(f"FAIL  {t.__name__}")
                traceback.print_exc(limit=3)
        browser.close()
    srv.shutdown()
    print(f"\n{len(ORDER) if not only else 'selected'} tests, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
