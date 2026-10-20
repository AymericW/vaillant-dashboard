# Vaillant Heating Dashboard

A small app that runs on your PC and shows a dashboard in your browser for a Vaillant
heating system. It supports two kinds of Vaillant systems:

| You have… | App on your phone | Set in `config.toml` |
|---|---|---|
| **vSMART** or **eRELAX** (round thermostat made with Netatmo) | "vSMART" | `system = "vsmart"` |
| sensoCOMFORT / sensoHOME / VRC 700 with a VR 920/921/940f module | "myVAILLANT" | `system = "myvaillant"` |

It uses [vaillant-netatmo-api](https://pypi.org/project/vaillant-netatmo-api/) for vSMART
(the same library as the Home Assistant vSMART integration) and
[myPyllant](https://github.com/signalkraft/myPyllant) for myVAILLANT.

**What it shows:** room temperature, target temperature, mode and schedule, temporary
overrides, outdoor temperature, hot water setting, gas/electricity use per day, devices,
firmware, thermostat battery and signal, and a raw dump of everything the API returns.
myVAILLANT systems also show water pressure, flow temperature, heating curve, humidity
and fault codes. The app stores a history, so you get charts and a CSV export.

**What you can control:** temporary temperature for 1–24 h, mode (schedule, away,
hot water only, frost protection), hot water temperature, and hot water boost.
On myVAILLANT you can also set the day and night temperatures.

## Start it (Windows)

1. Install **Python 3.11 or newer** from https://www.python.org/downloads/ and tick
   "Add python.exe to PATH" during setup.
2. Double-click **`start.bat`**. The first time, it installs what it needs and opens
   `config.toml` in Notepad.
3. Fill in your details (see below), save the file, and double-click `start.bat` again.
   Your browser opens at http://127.0.0.1:8321.

To try it without your account, run `start.bat --demo` from a command prompt.

On macOS or Linux, use `./start.sh` (or `./start.sh --demo`).

Your password is stored only in `config.toml` on your PC, and the app sends it only to the
thermostat's cloud (Netatmo for vSMART, Vaillant for myVAILLANT).

## vSMART: what to fill in

In `config.toml` set `system = "vsmart"` and, in the `[vsmart]` section:

* **email** and **password**: the login of the **vSMART app**. A myVAILLANT account is
  a different account and will not work here.
* **client_secret**: a fixed key that identifies the vSMART app to the Netatmo cloud. It is
  the same for everyone, but it is not published officially. The Home Assistant community
  shares it in
  [this thread](https://community.home-assistant.io/t/added-support-for-vaillant-thermostat-how-to-integrate-in-official-release/31858)
  (search the thread for "client_secret").
* Leave `client_id`, `user_prefix` and `app_version` as they are.

After the first successful login, the app keeps a token in `data/vsmart_token.json`, so it
doesn't need to log in again on every start. vSMART updates every 5 minutes, because the
Netatmo cloud is strict about how often it is queried.

## Checking the thermostat with a second sensor

If you think the Vaillant thermostat shows the wrong temperature, you **don't need a
second thermostat**. A cheap Wi-Fi **temperature sensor** is enough. A good choice is a
**Shelly H&T Gen3** (around €30–40). It works on your local network and can send its readings
straight to this app.

1. In `config.toml`, set:
   ```toml
   [server]
   host = "0.0.0.0"          # so the sensor can reach your PC

   [external_sensor]
   mode = "push"
   name = "Living room sensor"
   ```
2. In the Shelly app or its web page, add an action or webhook for "temperature change" that calls
   `http://<your-PC-IP>:8321/api/external?temp=${ev.tC}`
   (on an older Gen1 H&T, use *Actions → Report sensor values* with `http://<your-PC-IP>:8321/api/external`).
3. Put the sensor where you actually sit, away from radiators, windows and direct sun.

The dashboard then shows both readings on one chart, along with the difference between them.
Let it run a few days before you decide anything.

Other sensor options are in `config.example.toml`: mains/USB-powered Shellys
(`mode = "shelly"`), or any device with a JSON URL such as Home Assistant, ESPHome or Tasmota
(`mode = "http_json"`).

### "Heat to the external sensor"

The Vaillant controller can only regulate on its **own** sensor, and there is no way to
feed it another reading. When you switch this option on, the app works around that:

* It takes the temperature your schedule asks for (for example 21 °C).
* It adds the difference between the two sensors. If Vaillant reads 22 °C and your
  sensor reads 20 °C, it tells the controller 23 °C.
* It sends that as a **temporary override** (2 h by default) and renews it while the app runs.
  If the PC or app stops, the override expires and your normal schedule comes back by itself.
* It never shifts the target by more than ±3 °C, and changes it at most every 15 minutes.
  You can adjust both limits in `config.toml`.

This only works while the app is running, so the PC has to stay on. On vSMART, the app uses the thermostat's manual mode with an end time, which also expires by itself. A Raspberry Pi or a
small always-on PC works well for this.

Tip: many sensoCOMFORT/VRC 700 controllers (not vSMART) also have a fixed **room temperature offset**
setting in their menu. If your thermostat is off by a constant amount, that may be all you need.

## Activity log

The **Activity log** panel at the bottom of the dashboard shows what happened and who did it:

* **Command:** something this app sent to the thermostat, either from **you** (a button in the
  dashboard) or from the **algorithm** ("heat to the external sensor").
* **Change:** a setting that changed in the thermostat data. **App** means it was the result
  of a command from this app. **Thermostat** means it was changed on the thermostat itself,
  in the phone app, or by the thermostat's own schedule.
* **Decision:** the algorithm's reasoning, with all its numbers. For example: *"Thermostat 21.4 °C,
  sensor 20.1 °C → thermostat reads 1.3 °C warmer. Schedule wants 20.0 °C, so the thermostat
  should aim for 20.0 + 1.3 = 21.30 → 21.5 °C. Already set: nothing to do."* A decision is only
  logged when the reasoning changes, so the list doesn't fill up with identical lines.
* **Error / Sensor / App:** connection problems, sensor read failures and recoveries.

Use the filter to show only changes, only algorithm decisions, or errors. Use **CSV** to
download the full log. A technical log is also written to `data/app.log`.

If someone sets a temporary temperature by hand while "heat to the external sensor" is on
(on the thermostat, in the phone app, or in the dashboard), the algorithm steps back and
leaves that temperature alone until it ends. Then it takes over again.

## Notes

* Both clouds limit how often they can be queried. The default refresh (2 min, or 5 min for vSMART) is safe. Values in the cloud
  can lag a few minutes behind the boiler.
* History is stored in `data/history.sqlite`. Download it as CSV with the link above the chart.
* This is not an official Vaillant product. Vaillant can change its API, in which case
  updating the library (`mypyllant` or `vaillant-netatmo-api`) usually fixes it.
