# Garmin Dashboard for Unraid

A private, self-hosted dashboard for your own Garmin data.

- **Garmin's official Personal API** supplies steps, resting heart rate, sleep,
  HRV, Body Battery, stress and activities.
- **Optional Garmin Connect sign-in** adds what the official API leaves out:
  VO2 max, training status, training readiness, recovery time and training load.
- **Weather with heat-graded pace**: today's conditions for a city you pick,
  how much the heat and humidity slow you down, your easy, threshold and race
  paces adjusted for it, and the coolest time to run.
- **An AI coach**: connect Claude, Gemini, ChatGPT or a local Ollama model and
  get a written insight every morning, plus a box to ask it questions.
- **A dashboard you arrange yourself**: drag boxes between the main page, the
  Details tab and Hidden, and resize them. It has a phone layout with a bottom tab bar.
- **Race recaps for Instagram**: pick a run and get an animated 3D recap over an
  aerial photo of your course, styled like an arcade game, saved as an MP4 or a
  picture for Stories, Reels, the feed or a square post.

Everything stays on your server. The only things sent anywhere are the
requests to Garmin that fetch your data, the weather lookup for the place you
choose (to Open-Meteo, rounded to about 1 km), if you turn on the AI coach,
a name-free summary to the provider you pick, and, when you make a race recap,
the rectangle around that run (to USGS for the aerial photo and to OpenStreetMap
for building outlines; never the route itself).

![Dashboard screenshot (sample data)](docs/screenshot.png)

---

## Install on Unraid (about 10 minutes)

### 1. Create your Garmin Personal API key

On a computer, sign in at **https://connect.garmin.com/app/settings/personalApiToken**,
choose **Create Key**, accept the terms and pick **1 year**. Copy the key. Garmin
shows it only once. Keep it handy for step 4.

### 2. Add the template to Unraid

Open the Unraid terminal (the `>_` icon, top right), paste this, and press Enter:

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/my-garmin-dashboard.xml https://raw.githubusercontent.com/itsaggo/garmin-dashboard/main/unraid/garmin-dashboard.xml
```

### 3. Create the container

**Docker** tab → **Add Container** → open the **Template** dropdown → under
*User templates* pick **garmin-dashboard** → **Apply**. Picking it from the
dropdown matters: that's what brings in the port, data folder and security
settings.

It now appears in the Docker tab with your other containers, with its own icon,
Autostart switch and WebUI link.

### 4. Open it and follow the setup

Click the container's icon → **WebUI**, or go to `http://YOUR-SERVER-IP:8099`.
Use the IP shown in the Docker tab's *LAN IP:PORT* column.

1. **Create your login**: a username and a password of at least 12 characters.
   Do this right after installing. The first person to open the page creates
   the only account.
2. **Paste your Personal API key** from step 1. It's checked with Garmin before
   it's saved.
3. **Optionally sign in to Garmin Connect** for VO2 max, training status and
   recovery. Your Garmin password is used once and **never saved**. If you use
   two-factor sign-in, you'll be asked for the code.
4. **Set your preferences**: units, time zone and theme (defaults: miles and
   Central time), plus your goal race and goal time for the coach.

The first sync fetches up to a year of history and takes a few minutes. Charts
fill in as data arrives.

---

## Updating

Docker tab → **Check for Updates** → **Apply Update** on garmin-dashboard.
Your data, password and Garmin sign-ins are kept.

## Settings

Everything is in the dashboard's **Settings** page:

- **Race switch:** turn on "Training for a race" to set a name, date, distance
  and goal time. Off hides all of it.
- **Units, time zone and theme**, and whether to show cycling.
- **Garmin connections:** replace an expired API key, or sign in to Garmin Connect
  again or disconnect it.
- **AI coach:** Claude, Gemini, ChatGPT or local Ollama (see below).
- **Customize:** arrange and resize the dashboard boxes (see below).
- **Notifications:** Discord morning insight, weekly summary and warnings (see below).
- **Account:** change your password, or sign out other devices.
- **Your data:** download everything as a file, delete stored Garmin data, or
  delete everything and start over.

The container's **Edit** screen in the Docker tab only holds server-level
options: port, data folder, allowed host names, and sync timing.

**Locked out?** Reset the password from the Unraid terminal:
`docker exec -it garmin-dashboard python -m app.setup reset-password`
(`python -m app.setup` alone lists the other terminal commands).

**Things that expire:** the Personal API key (Garmin emails you a week before)
and the Garmin Connect sign-in (typically after months). When either lapses, the
dashboard shows a banner, you renew it in Settings, and the other data source
keeps working meanwhile.

## Weather and heat-graded pace

The weather box shows the current conditions for the place set in
**Settings → General → Weather location** (Houston by default), and grades
the heat with the runner's rule of thumb of adding the temperature and the dew
point in °F:

| Temp + dew point | Slow down by |
|---|---|
| 100 or less | nothing |
| 101–120 | up to 1% |
| 121–140 | 1–3% |
| 141–160 | 3–6% |
| 161–180 | 6–10% |
| over 180 | hard running not recommended |

It then shows your paces three ways: in cool weather, right now, and at the
coolest hour between 5 a.m. and 8 p.m. in the next day.
- **Easy and threshold** come from your 5K estimate (Daniels' formulas).
- **Race goal** is your goal time over the race distance, when a race is set.

A chart shows how much slower each of the next 18 hours runs.

Forecasts come from [Open-Meteo](https://open-meteo.com/) (free, no account or
key, data under CC BY 4.0). Only the place you pick, rounded to about 1 km, is
sent to them, at most about once every 30 minutes. The coach and Discord use the
same saved forecast. It's a rule of thumb for pacing, not a safety guarantee:
in serious heat, go by how you feel.

## Race recap

The **Recap** tab turns one run into a 14-second animated video:

1. Pick a run from the list (outdoor runs only; search by name or date).
   Nothing is downloaded until you pick one.
2. The first time, the server fetches what the recap needs and keeps it:
   - the run's original activity file, through the official Personal API, for
     the GPS track, heart rate, cadence and altitude along the run;
   - an aerial photo of the area from USGS (public domain, US mainland only);
   - building outlines near the route from OpenStreetMap.
   This takes from a few seconds to about a minute.
3. The preview plays in your browser. Choose the format (9:16 for Stories and
   Reels, 4:5 for the feed, 1:1 square), the ghost runner (your goal time from
   **Settings → General → Training for a race**, or an even pace that finishes with you),
   and how much of the start and finish to hide.
4. **Save video** makes a 1080-wide MP4 (H.264, 30 fps). **Save still** saves a
   PNG of the frame you're on. On a phone, **Share** opens the share sheet so
   you can post it straight to Instagram.

What the recap shows:

- The camera starts straight down on the start line over the real aerial photo.
  A shockwave turns the city into a pixel-art game board and raises the
  buildings, then the camera tilts into a chase view behind your runner.
- A coin at every mile (or km) with that split and how it compares to the one
  before; a **combo** for miles in a row that each got faster; a pink **boost**
  trail wherever you ran faster than your average pace.
- A ghost runner at the goal or even pace, with how far ahead or behind you are.
- Distance, time, current split pace, heart rate as a power meter coloured by
  zone, cadence and elevation.
- The finish pulls up over the whole course, then a high-score screen with your
  time, averages, result against your goal and your three best miles.

Heart-rate zones use Garmin's default bands (60/70/80/90% of max). Max heart
rate is taken as the highest from your runs in the year before this one.

The video renders on the device you're using, on its graphics card; the server
only supplies the data. Use a recent Chrome, Edge or Safari (they can encode
MP4). In 9:16, text stays out of the top and bottom 14% that Instagram Stories
covers. The photo and map credits are part of every frame, as their licences ask.

**Privacy.** **Hide start & finish** leaves the first and last 0.1 or 0.25 mi
(200 or 400 m) of the route undrawn, so the video doesn't point at your door.
The photo still shows the neighbourhood, so for runs from home keep it on. The
server keeps the 20 most recently used photos (a few MB each) in `/data/recap`.
**Settings → Your data → Delete stored data** removes them along with the rest.

## AI coach

In **Settings → AI coach**, pick a provider and either paste an API key or
enter your Ollama address. Then choose a model and save.

- **Claude:** key from console.anthropic.com
- **Gemini:** key from aistudio.google.com
- **ChatGPT:** key from platform.openai.com
- **Local:** an Ollama server on your network, e.g. `http://192.168.4.23:11434`.
  Use the server's LAN IP, not `localhost`.

Each morning after 6 a.m., after a fresh sync, the coach writes a short insight.
You can also ask it questions on the dashboard.

What it sends and costs:
- **Only a name-free summary** of your numbers goes to the provider: trends,
  recent activities, today's heat and your goal race.
- **Your API key stays private.** It's stored like your Garmin key and is never
  shown again.
- **Cloud providers bill you directly**, roughly a cent per insight. The
  dashboard caps use at 8 rewrites and 30 questions a day.

It's training guidance, not medical advice.

## Discord notifications

To set it up:
1. In Discord, open the channel you want and choose **Edit Channel →
   Integrations → Webhooks → New Webhook → Copy Webhook URL**.
2. Paste it in **Settings → Notifications** and click **Save & send test**.

Then pick which messages it sends:
- **Morning insight**, around 7 a.m.: the AI coach's note plus today's heat,
  readiness, sleep, VO2 max, 5K and race countdown. If the AI coach is
  on, it waits until 9 a.m. at the latest for that day's insight.
- **Weekly summary**, Monday morning: last week's workouts and mileage.
- **Problems & warnings:** a failed sync, an expired Garmin key or sign-in, no
  sync for over a day, or resting heart rate 5+ bpm above your normal for 3
  days. Each problem is sent once, not repeatedly.

The webhook link is stored privately, like your other keys. Messages never
ping anyone. They do contain your health numbers, so use a private channel or
server.

## Customize

**Settings → Customize** shows every box in three areas: **Main page**,
**Details tab** and **Hidden**. To arrange them:
- **Move a box:** drag it to another spot or area.
- **Resize a box:** drag its bottom-right corner.
- **Use the keyboard instead:** focus a box, then arrows resize it and Alt+arrows reorder it.

Press **Save layout** when you're done, or **Reset to default** to start over.
On a phone, boxes stack full-width in the same order.

## Security

What protects your data:

- **Login required for everything.** That includes the API and the page code.
  There's one username/password account, and the first visitor creates it, so
  finish setup right after installing. Passwords are stored only as a salted
  scrypt hash. Sessions are random
  server-side tokens in an `HttpOnly`, `SameSite=Strict` cookie. They last 30
  days, and you can revoke them.
- **Brute-force limits.** 5 attempts per address and 20 overall per 15 minutes.
  Each attempt is counted before the password is checked, so firing many at once
  doesn't earn extra tries. At most two password checks run at a time, so a
  flood can't exhaust the server's memory.
- **Cross-site protection.** Every form or button action is checked to make
  sure it came from the dashboard itself. A strict Content-Security-Policy
  allows only the dashboard's own scripts: no inline code and no third-party
  resources. The page can't be embedded in other sites. Requests addressed to
  unknown domain names are refused, which blocks "DNS rebinding" attacks from
  websites you visit.
- **No secrets in the container settings or environment variables.** `docker
  inspect` shows nothing sensitive. The API key, password hash and Garmin
  sign-in token live in `data/secrets/` (folder 0700, files 0600). The browser
  never gets them back: Settings only shows "Connected".
- **Your Garmin password is never stored.** It's passed to Garmin once, then
  dropped. Only the sign-in token Garmin issues is kept, and you can remove it
  in Settings. A half-finished two-factor sign-in waits in memory for at most 5
  minutes, never on disk. Garmin sign-in attempts are rate-limited so a mistake
  can't get your Garmin account locked.
- **Locked-down container.** The app runs as Unraid's unprivileged `nobody`
  user with no Linux capabilities. Starting from the Docker tab, the container
  briefly has three: enough to hand the data folder to `nobody` and switch to
  that user. It drops them before the app starts. The filesystem is read-only,
  privilege escalation is blocked, and process count and memory are capped.
- **Verified dependencies.** Every Python package is checked against SHA-256
  hashes when the image is built. Images are built by GitHub Actions from this
  repository's source, after the test suite passes. The Debian base image is
  pulled fresh at each release build and gets Debian's security updates. That
  trades exact reproducibility for staying patched.
- **Read-only toward Garmin.** Nothing in this app changes your Garmin account.

What it can't protect against, and what you should do:

- **Plain HTTP on your LAN.** Traffic on your home network isn't encrypted.
  Don't port-forward 8099 on your router. For access away from home, use
  **Tailscale** or **WireGuard** (Unraid has both) instead of exposing the port.
  - **Behind an HTTPS reverse proxy or tunnel:** see
    [Putting it on the internet](#putting-it-on-the-internet) below.
- **The lockout for strangers is shared.** Browsers that have never signed in
  share a limit of 20 tries per 15 minutes (5 per address). A browser that has
  signed in before is counted on its own, so someone spamming wrong passwords
  can't lock you out of a device you already use. A brand-new device would have
  to wait until the spam stops. Restarting the container clears the limits.
- **Anyone with root on your Unraid server can read the data folder.** That's
  true of every app on the server, so keep your Unraid root password strong.
- **The Garmin Connect part is unofficial.** It uses the community
  [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) library,
  not a Garmin-supported API. It only reads data, but Garmin could change or
  block it. If that happens, the official half keeps working.
- **The data folder isn't encrypted on disk.** If you want that, use Unraid's
  encrypted array or pool.

---

## Troubleshooting

- **The page times out**: use the IP in the Docker tab's *LAN IP:PORT* column. If
  your server has more than one network connection, it may be a different
  address from the one you expected.
- **"This address (…) isn't allowed"**: you opened it by a name rather than an
  IP. Add that name to *Allowed host names* (container → Edit), or use the IP.
- **Apply fails with "denied" or "not found" when pulling the image**: the image
  isn't public yet (see *Publishing* below), or the template still contains
  `itsaggo`.
- **A chart says "No data yet"**: open `/raw` in the dashboard to see exactly what
  Garmin sent. A field name Garmin hasn't documented may need adding to
  `app/extract.py`.
- **"Too many attempts" on the login page**: wait 15 minutes, or restart the container.
- **Forgot your password**: `docker exec -it garmin-dashboard python -m app.setup reset-password`.
- **AI coach says it can't reach Ollama**: use the server's LAN IP rather than
  `localhost`, since `localhost` inside the container is the container itself.
  Also check that Ollama listens on the network (`OLLAMA_HOST=0.0.0.0`).
- **Login says the server is busy**: someone else is trying passwords at the same
  moment. Wait a few seconds and try again.
- **Garmin Connect sign-in says "too many attempts"**: Garmin limits logins.
  Wait 15–30 minutes before trying again.

## Uninstall

1. Docker tab → click the icon → **Remove** (tick *also remove image*).
2. In the terminal:

   ```bash
   rm -rf /mnt/user/appdata/garmin-dashboard   # deletes your stored data
   rm /boot/config/plugins/dockerMan/templates-user/my-garmin-dashboard.xml
   ```
3. Delete the Personal API key at connect.garmin.com (Settings → Personal API).

---

## Publishing (for the maintainer)

**First time:**

1. Replace `itsaggo` everywhere with your GitHub username in
   **lowercase**. The container registry requires lowercase.
2. Push to GitHub.
3. On GitHub: **Releases** → **Draft a new release** → tag `v1.0.0` → **Publish**.
   The *Release image* workflow runs the tests, then builds and pushes
   `ghcr.io/<you>/garmin-dashboard` (watch it under **Actions**).
4. Make the image public so Unraid can download it. On your GitHub profile →
   **Packages** → **garmin-dashboard** → **Package settings** →
   **Change visibility** → **Public**.

**Every update:** commit and push your changes, then publish a new release with a
higher version (`v1.0.1`, `v1.1.0`, …). Unraid servers then show "update
available".

The *Tests* workflow runs on every push. To run them locally:

```bash
pip install --require-hashes --no-deps -r requirements.lock && pip install pytest httpx
python -m pytest -q tests
```

## Build from source instead (advanced)

To build the image on the server itself, without GitHub's image: copy this
folder to `/mnt/user/appdata/garmin-dashboard` and run
`bash /mnt/user/appdata/garmin-dashboard/install.sh`. Settings then live in
`config.env`, which is created from `config.example.env` on the first run.
Re-run the script to apply changes. A container made this way appears in the
Docker tab without an icon or update checks.

## Putting it on the internet

The safest way to reach the dashboard away from home is a private network like
Tailscale or WireGuard. If you'd rather give it a public address, for example
with a **Cloudflare Tunnel**, set these on the container (Unraid: edit the
container, *Show more settings*):

| Setting | Value |
|---|---|
| Allowed host names | your public name, e.g. `garmin.example.com` |
| Trusted proxy addresses | where the tunnel or proxy connects from, e.g. `172.17.0.0/16` (Docker's default network) or the proxy container's own IP |

With *Trusted proxy addresses* set, the dashboard:
- counts login attempts per real visitor (from Cloudflare's `CF-Connecting-IP`,
  or the last `X-Forwarded-For` entry) instead of treating the whole internet
  as one address;
- refuses to create the first account from outside, so nobody can claim a
  freshly installed or wiped dashboard before you do. Create the account from
  your home network, by the server's IP.

Cookies are marked secure and HTTPS is enforced (HSTS) automatically whenever
you sign in over HTTPS. The proxy has to pass the original `Host` header;
Cloudflare Tunnel, Nginx Proxy Manager and SWAG do.

Also recommended, on the Cloudflare side:
- **Cloudflare Access** (Zero Trust → Access → Applications) in front of the
  hostname, allowing only your own email address. Visitors then have to pass
  Cloudflare's sign-in before they can even see the dashboard's login page. It
  is free for personal use and is the single biggest improvement you can make.
- **Always Use HTTPS** on, and a rate-limiting rule on `/login`.
- Don't publish the container's port on your router as well. The tunnel is the
  only way in from outside.

The dashboard has one password and no two-factor sign-in of its own, so on a
public address use a long, unique passphrase.

## Project layout

```
app/
  main.py      web server, login, security headers, background scheduler
  auth.py      password hashing, sessions, known devices, login rate limiting
  net.py       outbound requests without redirects
  setup.py     terminal fallback (password reset etc.)
  prefs.py     Settings-page preferences (validated)
  garmin_login.py  Garmin Connect sign-in incl. two-factor (web and terminal)
  fitness.py   5K estimate, training paces, race card, facts for the AI coach
  weather.py   Open-Meteo forecast, temperature + dew point heat grade
  ai.py        AI providers (Claude, Gemini, ChatGPT, Ollama), daily insight, caps
  recap.py     race recap: activity file → track, USGS aerial photo, OSM buildings
  notify.py    Discord webhook notifications
  sync.py      official Personal API sync (chunked ranges, backoff, token refresh)
  connect.py   optional Garmin Connect sync (VO2 max, training status, readiness)
  extract.py   Garmin JSON → tidy rows
  db.py        SQLite storage
  static/      dashboard front end (plain JS + bundled Chart.js, three.js, Mediabunny)
unraid/        Docker-tab template and icon
entrypoint.sh  drops to the unprivileged user before the app starts
tests/         pytest suite + a fake Garmin API (tests/mock_garmin.py)
.github/       test and release workflows
```

MIT licensed (see `LICENSE`). Third-party components are listed in
`THIRD_PARTY_NOTICES.md`. Not affiliated with or endorsed by Garmin.
