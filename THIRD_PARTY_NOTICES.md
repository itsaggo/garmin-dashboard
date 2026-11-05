# Third-party software

| Component | License | Notes |
|---|---|---|
| [Chart.js](https://www.chartjs.org/) 4.6.1 | MIT | bundled at `app/static/vendor/chart.umd.min.js` |
| [garmin-connect-personal-api-python](https://github.com/garmin/connect-personal-api-python) | Apache-2.0 | Garmin's official Personal API client (installed at build time) |
| [garminconnect](https://github.com/cyberjunky/python-garminconnect) | MIT | community library for the optional Garmin Connect metrics (installed at build time) |
| FastAPI, Starlette, Uvicorn, python-multipart | MIT / BSD | web server (installed at build time) |

The full pinned dependency list is in `requirements.lock`.

"Garmin" and "Garmin Connect" are trademarks of Garmin Ltd. or its subsidiaries.
This project is not affiliated with or endorsed by Garmin. Use of Garmin's APIs is
subject to Garmin's terms.
