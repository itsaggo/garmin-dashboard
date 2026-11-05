# Changelog

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
