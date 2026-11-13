# Third-party software

| Component | License | Notes |
|---|---|---|
| [Chart.js](https://www.chartjs.org/) 4.6.1 | MIT | bundled at `app/static/vendor/chart.umd.min.js` |
| [Inter](https://github.com/rsms/inter) (via Fontsource 5.3.0) | SIL OFL 1.1 | text typeface, latin subset, bundled at `app/static/vendor/fonts/` with `LICENSE-Inter.txt` |
| [Newsreader](https://github.com/productiontype/Newsreader) (via Fontsource 5.3.0) | SIL OFL 1.1 | headline typeface, latin subset, bundled at `app/static/vendor/fonts/` with `LICENSE-Newsreader.txt` |
| [three.js](https://threejs.org/) r128 (0.128.0) | MIT | 3D rendering for the race recap, bundled at `app/static/vendor/three.min.js` with `LICENSE-three.txt` |
| [Mediabunny](https://mediabunny.dev/) 1.64.0 | MPL-2.0 | MP4 writing for recap videos, unmodified, bundled at `app/static/vendor/mediabunny.min.mjs` with `LICENSE-mediabunny.txt`; source at https://github.com/Vanilagy/mediabunny |
| [Press Start 2P](https://github.com/googlefonts/Press-Start-2P) (via Fontsource) | SIL OFL 1.1 | recap HUD typeface, latin subset, bundled at `app/static/vendor/fonts/` with `LICENSE-PressStart2P.txt` |
| [Silkscreen](https://github.com/googlefonts/silkscreen) (via Fontsource) | SIL OFL 1.1 | recap HUD labels, latin subset, bundled at `app/static/vendor/fonts/` with `LICENSE-Silkscreen.txt` |
| [fitdecode](https://github.com/polyvertex/fitdecode) 0.11.0 | MIT | reads Garmin FIT activity files (installed at build time) |
| [garmin-connect-personal-api-python](https://github.com/garmin/connect-personal-api-python) | Apache-2.0 | Garmin's official Personal API client (installed at build time) |
| [garminconnect](https://github.com/cyberjunky/python-garminconnect) | MIT | community library for the optional Garmin Connect metrics (installed at build time) |
| FastAPI, Starlette, Uvicorn, python-multipart | MIT / BSD | web server (installed at build time) |

Weather data comes from [Open-Meteo.com](https://open-meteo.com/) under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); the dashboard credits it in the weather box.

Race recap aerial photos come from the [USGS National Map NAIP imagery service](https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer)
(U.S. public domain; credited as "USGS, USDA, The National Map"). Building outlines come from
[OpenStreetMap](https://www.openstreetmap.org/copyright) through the Overpass API, © OpenStreetMap contributors,
under the [ODbL](https://opendatacommons.org/licenses/odbl/). Each recap video credits both in its corner.

The full pinned dependency list is in `requirements.lock`.

"Garmin" and "Garmin Connect" are trademarks of Garmin Ltd. or its subsidiaries.
This project is not affiliated with or endorsed by Garmin. Use of Garmin's APIs is
subject to Garmin's terms.
