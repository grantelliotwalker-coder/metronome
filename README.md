# Metronome

A precise, installable metronome for the browser. No build step, no dependencies, one `index.html`.

**Run it:** open `index.html` in Chrome, or serve the folder (`python3 -m http.server`) for offline support and the install button.

## Practice
- **Count-in** of 1 or 2 bars
- **Speed trainer:** add N BPM every N bars up to a ceiling; tempo returns to your starting value when you stop
- **Silent bars:** click for some bars, then mute for some, to test your own time

## Rhythm
- Time signatures from 1 to 16 over 2, 4, 8 or 16. The tempo counts the bottom-number note
- **Tap any beat to cycle** normal, accent, off
- Split each beat into 2, 3 or 4, with adjustable **swing**
- **Cross-rhythm:** a second voice with 2 to 9 clicks per bar (3 against 4 and so on)

## Sound and feel
- Eight synthesized sounds: click, wood block, rimshot, cowbell, hi-hat, beep, **Drum beat** and a softer **Brush kit**
- **Drum kits** play a backbeat that follows the time signature (4/4 kick snare kick snare, 3/4 kick snare snare, 6/8 and other compound meters on dotted-quarter groups). **Tap any beat dot to change its drum**: kick, snare, hi-hat or off
- **Hi-hat setting** with its own rate: off, on the beats, eighths, triplets or sixteenths (swing applies to eighth hats)
- **Hat tone** (darker to brighter) and **Hat timing** (40 ms ahead to 80 ms behind) sliders to loosen the hats off the grid; kick and snare stay put
- Every setting, including the hat sliders, is remembered in the browser between visits
- **Visual delay** slider plus **calibrate by tapping**, so the pendulum matches what you hear on Bluetooth speakers
- Optional vibration on each beat (Android Chrome)

## Workflow
- **Swipe the pendulum** left or right to start; the arm follows your finger and the first beat lands on the side you swiped
- **Time signature badge** in the corner of the pendulum: tap it to pick 2/4, 3/4, 4/4, 5/4, 6/8, 7/8, 9/8, 12/8 or any custom signature
- Tempo dial, +/- (hold to repeat), slider, tap tempo
- **Presets** with previous/next for setlists, and a **share link** that carries all musical settings
- **Stage mode:** huge beat number with a soft flash, full screen. Flashing stays at or below 3 per second and is off with reduced motion
- **MIDI clock out** (Web MIDI): start, stop and 24 ppq clock for drum machines and DAWs
- Keyboard: Space start/stop, Up/Down tempo (Shift for 10), T tap, S stage

## Built for Chrome
- Scheduler runs in a **Web Worker**, so timing holds in background tabs where Chrome throttles page timers
- Audio starts from a gesture (autoplay policy) and recovers if Chrome suspends it
- **Wake lock** while playing, visuals aligned to the audio output latency
- **Installable PWA** with offline support and maskable icons (needs https or localhost)

## Tests
The suite drives real Chromium and checks the exact audio-clock time of every scheduled click (timing, accents, swing, count-in, trainer, silent bars, cross-rhythm), plus presets, share links, MIDI clock, stage mode, calibration, offline and background-tab behaviour.

```
pip install playwright && playwright install chromium
python3 tests/test_app.py          # all
python3 tests/test_app.py swing    # by name
```

## Versions
- `v3` current: everything above
- `v2.0.0` Chrome hardening, PWA, sounds, saved settings
- `v1.0.0` first single-file release
