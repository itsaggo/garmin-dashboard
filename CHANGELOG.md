# Changelog

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
