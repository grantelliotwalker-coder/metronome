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
    var b = this.buffer; if (!ids.has(b)) ids.set(b, ++n);
    window.__clicks.push({ t: t || 0, rate: this.playbackRate.value, buf: ids.get(b) });
    return start.apply(this, arguments);
  };
  var gain = AudioContext.prototype.createGain;
  AudioContext.prototype.createGain = function () { window.__ctx = this; return gain.apply(this, arguments); };
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
    s = dict(bpm=100, num=4, den=4, acc=None, sub=1, swing=50, poly=0, sound="click", countIn=0,
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
    for name in ["click", "wood", "rim", "cowbell", "hat", "beep"]:
        page.select_option("#sound", name)
    assert page.evaluate("window.__clicks.length") == 6, "each sound previews once"
    assert len({c["buf"] for c in page.evaluate("window.__clicks")}) == 6, "six distinct sounds"


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
    page = env.page(cfg())
    box = page.locator("#dial").bounding_box()
    cx, cy, r = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, box["width"] / 2
    page.mouse.move(cx, cy - r * 0.8); page.mouse.down()
    near(int(page.inner_text("#bpmOut")), 160, 2, "dial straight up")
    page.mouse.move(cx + r * 0.8, cy); page.mouse.up()
    near(int(page.inner_text("#bpmOut")), 253, 3, "dial pointing right")
    page.mouse.move(cx, cy); page.mouse.down(); page.mouse.up()
    near(int(page.inner_text("#bpmOut")), 253, 3, "touches in the middle are ignored")
    page.keyboard.press("ArrowUp"); near(int(page.inner_text("#bpmOut")), 254, 3)
    page.keyboard.press("Shift+ArrowDown"); near(int(page.inner_text("#bpmOut")), 244, 3)
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


ORDER = [
    test_basic_timing_and_accents, test_beat_accent_editing, test_time_signature_and_split, test_swing,
    test_count_in_and_polyrhythm, test_speed_trainer, test_silent_bars, test_sounds_exist,
    test_presets_and_share_link, test_preset_prev_next, test_persistence_and_migration,
    test_dial_and_controls, test_tap_tempo, test_stage_mode, test_stage_mode_no_flash_with_reduced_motion,
    test_midi_clock, test_midi_hidden_without_support, test_calibration, test_pwa_and_offline,
    test_layout_fits_phones_and_landscape, test_background_tab_keeps_time,
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
