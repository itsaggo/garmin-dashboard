# Changelog

## v1.3.1

- **Pick your AI model from a list.** Settings → AI coach now has a searchable dropdown filled with the models
  your key (or Ollama server) can actually use, with friendly names, the recommended one marked, and sizes for
  local models. It loads by itself once a key is saved. Typing still works for a model that isn't listed.

Hardening for running behind a reverse proxy or Cloudflare Tunnel on a public address.
See "Putting it on the internet" in the README.

- **Login limits count real visitors.** New *Trusted proxy addresses* setting (`TRUSTED_PROXIES`). Requests from
  that proxy are believed about who the visitor is, so one person's wrong passwords no longer lock out everyone.
  IPv6 visitors are counted per /64.
- **Strangers can't lock you out.** A browser that has signed in before is rate-limited on its own, apart from
  the shared limit for unknown browsers.
- **Secure cookies and HSTS are automatic** when you sign in over HTTPS; no setting to remember.
- **The first account can't be created from outside** when a trusted proxy is configured.
- **Request size limit** (128 kB), so oversized uploads are refused before being read.
- **Guessable passwords are refused** when creating the account or changing the password.
- Outbound requests (AI providers, Discord, weather) no longer follow redirects.
- Changing your password or deleting data while signed in has its own attempt limit, so sign-in spam can't block it.
- The first account is refused from any request that passed through a proxy or tunnel, even if
  *Trusted proxy addresses* is missing or wrong. The log names the proxy address to add.
- The Ollama address must be a plain server address (no path, no link-local range), and its error text
  is no longer shown. The daily AI limit now holds under bursts of requests.

## v1.3.0

- **Weather and heat-graded pace.** A new box beside the coach's note shows the weather for a city you pick
  (Settings → General, Houston by default) and grades the heat with the temperature + dew point rule of thumb.
  - Your easy, threshold and race-goal paces in cool weather, right now, and at the coolest hour of the next day.
  - An hour-by-hour chart of how much the heat slows you down.
  - The AI coach and the Discord morning message know today's heat too.
  - Forecasts come from Open-Meteo: free, no account or key; only the place, rounded to about 1 km, is sent.
- **Fitness score removed**, from the dashboard, the AI coach and Discord. If your saved layout had it, the
  weather box takes its place and size.
- **New look.** The dashboard now reads like a morning paper: your coach's note is the headline, and boxes are
  separated by simple rules instead of cards.
  - Headlines and numbers in Newsreader, text in Inter. Both fonts are served by the app itself, nothing loads from outside.
  - Charts are drawn in one ink color, with the latest value marked in blue.
  - Race box is a strip across the page: days to go, goal time and pace, Garmin's prediction and how far ahead or behind you are.
  - Dark mode and the phone layout were redone to match.
  - Newspaper-style motion: on first load the rules draw across the page and each section inks in, in reading
    order, with the headline settling into focus and the big numbers counting up. Turned off automatically if
    your device is set to reduce motion.
- **New default layout.** The main page now also shows HRV, sleep stages and recent activities. If you've already
  saved your own layout it stays as it is; use Settings → Customize → Reset to default to get the new one.
- Labels from Garmin like "HIGH" or "PRODUCTIVE" now show as normal words.
- Fixed: in Settings → Customize, short boxes (2 rows tall) lost their title.

## v1.2.0

- **AI coach is now the main coach.** Connect Claude, Gemini, ChatGPT or a local Ollama model. It writes an
  insight every morning and answers questions. The rule-based coach cards were removed.
- **Fitness score (1–100)** from four parts (engine, recovery, training balance, consistency), with a history chart.
- **New design** focused on fitness score, VO2 max, training readiness, sleep score and 5K estimate,
  each with a trend chart. Separate phone layout with a bottom tab bar.
- **Customize:** drag and drop boxes between Main page, Details and Hidden, and resize them by dragging the corner.
- **5K estimate** from Garmin's race predictor (with Garmin Connect), otherwise calculated from your runs.
- **Race switch:** race settings are optional and hidden when off.
- **Resting heart rate fixed:** now read from Garmin's per-day heart-rate data, with Garmin Connect as a backup.
- Settings reorganized into sections: General, Garmin, AI coach, Customize, Account, Your data.
- **Discord notifications** through a channel webhook, set up in Settings → Notifications:
  - Morning insight with your key numbers.
  - Monday weekly summary.
  - Warnings for sync problems, an expired Garmin key or sign-in, and resting heart rate high for 3 days.
  - Each one can be turned on or off. Messages never ping anyone.

## v1.1.0

- **Setup in the browser.** On first visit you create a username and password, then a short
  wizard asks for your Personal API key, an optional Garmin Connect sign-in (two-factor supported),
  and your preferences. No terminal needed.
- **Settings page**: goal race (name, date, distance, goal time), units, time zone, theme, cycling,
  Garmin connections, change password, sign out other devices, export or delete your data.
- **Coach**: daily recommendation with reasons, goal-race pace / fitness estimate / training phase,
  weekly check-in. Optional local AI (Ollama) for written notes and questions.
- **Cycling**: weekly cycling distance chart and tile.
- Race name/date, units and time zone moved out of the Unraid container settings into the dashboard.

**Upgrading from v1.0:** your existing password keeps working. Sign in with the username
**`admin`**, then change it if you like with `python -m app.setup reset-password`.

## v1.0.1

- New logo.

## v1.0.0

- First release.
