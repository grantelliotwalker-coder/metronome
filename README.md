# Metronome

A precise, installable metronome that runs in the browser. No build step, no dependencies.

**Run it:** open `index.html` in Chrome, or serve the folder (`python3 -m http.server`) to get offline support and the install button.

## Features

- Tempo from 30 to 240 BPM: slider, +/- buttons (hold to repeat), or tap tempo
- 1 to 12 beats per bar with an accented first beat
- Subdivisions: eighths, triplets, sixteenths
- Three sounds: click, wood block, beep
- Pendulum and beat dots follow the audio clock, so what you see matches what you hear
- Remembers your settings between visits
- Keyboard: Space start/stop, Up/Down tempo (Shift for 10), T tap tempo

## Chrome notes (v2)

- **Stays on time in background tabs.** The scheduler runs in a Web Worker, because Chrome throttles main-thread timers in hidden tabs.
- **Audio autoplay policy.** Audio starts only from a click or key press, and the app recovers if Chrome suspends the audio context.
- **Screen wake lock** while playing, so a phone or tablet doesn't sleep mid-practice.
- **Installable PWA** with offline support (manifest, service worker, maskable icons). Needs http(s) or localhost, not `file://`.
- Respects `prefers-reduced-motion`, dark mode, notches and safe areas, and landscape phones.

## Versions

- `v2.0.0` current: Chrome hardening, PWA, sounds, saved settings
- `v1.0.0` first release: single-file metronome (`git checkout v1.0.0`)
