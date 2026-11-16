# marlin-mks-wifi-dashboard

Unofficial local web dashboard for 3D printers running [Marlin](https://github.com/MarlinFirmware/Marlin) with an MKS WiFi module (`MKS_WIFI_MODULE`, ESP8266 with the stock MKS firmware), such as the MKS Robin Nano boards.

A browser can't talk to the module's raw TCP port, so a small Python server keeps that connection, polls the printer and serves the page. It can also relay Cura, so Cura and the dashboard share the module's single connection.

## Features

- Printer state, temperatures with a chart, print progress, elapsed and remaining time
- Heat, fan, jog and home controls
- SD card file list: print, pause, resume and stop
- Upload G-code to the SD card, with progress
- G-code preview in 2D (per layer) and 3D, following the print in real time
- Console to send G-code and read replies
- Cura relay: Cura's MKS WiFi plugin connects to the dashboard instead of the module

## Requirements

- Python 3.8 or newer. Only the standard library is used.
- A printer with Marlin and `MKS_WIFI_MODULE`, with the module joined to your network
- Internet access in the browser for the 3D view (three.js loads from a CDN)

## Usage

```sh
python server.py --printer <module-ip>
```

Then open http://localhost:8000. On Windows, edit the IP in `start.bat` and run it.

| Option | Default | Description |
|---|---|---|
| `--printer` | `192.168.100.116` | IP address of the MKS WiFi module |
| `--port` | `8000` | Local port for the dashboard |
| `--no-proxy` | off | Don't relay Cura on `127.0.0.1:8080` and `127.0.0.1:80` |

The server only listens on `127.0.0.1`.

### Cura

The module accepts one TCP client at a time, so Cura and the dashboard can't both connect to it directly. With the relay on, set the printer address in Cura's MKS WiFi plugin to `127.0.0.1`. Commands go through the dashboard's connection, and uploads go to the module's HTTP endpoint.

Cura's status requests (`M105`, `M997`, `M27`, ...) aren't forwarded, because the dashboard already polls the same data and every reply is relayed to Cura.

## Notes

- **Uploads:** any traffic on the module's TCP port during an HTTP upload stalls the module and breaks the transfer, so the dashboard holds all commands until the upload ends.
- **File names:** without `LONG_FILENAME_WRITE_SUPPORT`, Marlin stores uploads with short 8.3 names (`HM223~1.GCO`). Files that share the first characters can overwrite each other. The dashboard keeps a copy of every upload in `gcode_cache/` for the preview and maps it to the SD name.
- **Progress:** the module reports progress in whole percents. The preview moves smoothly about one report behind the printer.
- **Position, software endstops and machine info** (`M114`, `M211`, `M360`) need firmware that sends those replies back over WiFi. With stock Marlin, replies to queued commands go to the USB/serial port instead (see [MarlinFirmware/Marlin#28850](https://github.com/MarlinFirmware/Marlin/issues/28850)).

## Files

- `server.py`: TCP link to the module, polling, JSON API, upload and Cura relay
- `static/index.html`: the dashboard page
- `printer.json`: per-printer settings (travel limits, learned home position), created and updated automatically
- `gcode_cache/`: local copies of uploaded files (not tracked)
