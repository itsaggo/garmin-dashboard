# Garmin Dashboard for Unraid

A private, self-hosted dashboard for your own Garmin data.

- **Garmin's official Personal API** supplies steps, resting heart rate, sleep,
  HRV, Body Battery, stress and activities.
- **Optional Garmin Connect sign-in** adds what the official API leaves out:
  VO2 max, training status, training readiness, recovery time and training load.
- **A built-in coach** gives a daily recommendation, goal-race pace and taper
  guidance, and a weekly check-in, all computed on your server. You can also
  connect a local AI model (Ollama) for written notes and questions.

Everything stays on your server. The only things sent anywhere are the
requests to Garmin that fetch your data.

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

- **Goal race, units, time zone and theme**, and whether to show cycling.
- **Garmin connections:** replace an expired API key, or sign in to Garmin Connect
  again or disconnect it.
- **AI coach:** connect a local Ollama model (see below).
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

## The coach

The **Today** card makes one call, *Rest*, *Easy day*, *Normal training* or
*Good day for a hard workout*, and lists the reasons behind it. The reasons come
from your HRV and resting heart rate compared with your own normal, your sleep,
Body Battery, readiness, yesterday's run, and how this week's load compares with
the last four. The **Goal race** card shows the pace your goal needs, a
current-fitness estimate from your fastest recent runs, and the training phase
(base, build, peak, taper, race week). The **Weekly check-in** compares last
week with the one before and suggests a target for this week. It needs about
three weeks of data before "your normal" comparisons mean much.

It's training guidance, not medical advice. If your resting heart rate stays
high for days, it tells you to rest and see a doctor.

**Optional local AI:** install [Ollama](https://ollama.com) on a machine on
your network (for example as an Unraid container) and pull a model
(`ollama pull llama3.1:8b`). Then in **Settings → AI coach**, enter its address
(e.g. `http://192.168.4.23:11434`), click **Test connection**, pick the model and
save. The dashboard then gets a *Get today's note* button and a question box.
Only a short summary of the coach's numbers is sent, and only to that address.

---

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
  - **Behind an HTTPS reverse proxy:** set *Secure cookies* to `true`, and set
    *HTTPS reverse-proxy address* to its exact public address, e.g.
    `https://garmin.example.com`. The proxy also has to pass the original Host
    header. Nginx Proxy Manager and SWAG already do. For plain nginx, add
    `proxy_set_header Host $host;` and put the domain in *Allowed host names*.
- **The lockout is shared.** The 20-tries limit applies to everyone, so a device
  on your network spamming wrong passwords could lock you out for 15 minutes
  too. Restarting the container clears it.
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

## Project layout

```
app/
  main.py      web server, login, security headers, background scheduler
  auth.py      password hashing, sessions, login rate limiting
  setup.py     terminal fallback (password reset etc.)
  prefs.py     Settings-page preferences (validated)
  garmin_login.py  Garmin Connect sign-in incl. two-factor (web and terminal)
  coach.py     built-in coach rules + optional Ollama
  sync.py      official Personal API sync (chunked ranges, backoff, token refresh)
  connect.py   optional Garmin Connect sync (VO2 max, training status, readiness)
  extract.py   Garmin JSON → tidy rows
  db.py        SQLite storage
  static/      dashboard front end (plain JS + bundled Chart.js)
unraid/        Docker-tab template and icon
entrypoint.sh  drops to the unprivileged user before the app starts
tests/         pytest suite + a fake Garmin API (tests/mock_garmin.py)
.github/       test and release workflows
```

MIT licensed (see `LICENSE`). Third-party components are listed in
`THIRD_PARTY_NOTICES.md`. Not affiliated with or endorsed by Garmin.
