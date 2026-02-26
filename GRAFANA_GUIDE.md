# 📊 Grafana + InfluxDB Integration Guide — Neko Signal

This guide explains how to connect Grafana to the InfluxDB v2 bucket written
by `metrics_exporter.py` and build a professional trading dashboard using **Flux**.

---

## 1. Prerequisites

### InfluxDB v2 Setup (Docker — quickest path)

```bash
docker run -d \
  --name influxdb \
  -p 8086:8086 \
  -v influxdb2-data:/var/lib/influxdb2 \
  influxdb:2.7

# Open http://localhost:8086 → complete the initial setup wizard:
#   Organisation : neko
#   Bucket       : neko_signal
#   Admin token  : copy into .env → INFLUXDB_TOKEN
```

### Grafana Setup (Docker)

```bash
docker run -d \
  --name grafana \
  -p 3000:3000 \
  grafana/grafana-oss:latest

# Open http://localhost:3000  (admin / admin on first login)
```

---

## 2. Add InfluxDB as a Grafana Data Source

1. Go to **Connections → Data sources → Add data source**.
2. Choose **InfluxDB**.
3. Fill in:

| Field | Value |
|---|---|
| Query Language | **Flux** |
| URL | `http://localhost:8086` |
| Organisation | `neko` |
| Token | *(your InfluxDB API token)* |
| Default Bucket | `neko_signal` |

4. Click **Save & Test** — you should see *"datasource is working. 1 buckets found"*.

---

## 3. Data Model Quick Reference

```
measurement : market_data
tags        : symbol (e.g. "BTCUSDT") | trade_state ("IDLE" | "LONG" | "SHORT")
fields      : close, volume, ofi, cvd, vwap, score
interval    : 1 minute per data point per symbol
```

---

## 4. Dashboard Panels — Flux Queries

> **Tip:** In Grafana, go to **Dashboards → New → Add visualization**.
> Select your InfluxDB datasource. Paste the Flux query into the query editor.

---

### Panel 1 — Price Curve + VWAP Overlay

**Visualization:** Time series (2 lines)

```flux
// ----- Close Price -----
from(bucket: "neko_signal")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "market_data")
  |> filter(fn: (r) => r["symbol"] == "${symbol}")   // use a Grafana variable
  |> filter(fn: (r) => r["_field"] == "close")
  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)
  |> yield(name: "close")
```

```flux
// ----- Session VWAP -----
from(bucket: "neko_signal")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "market_data")
  |> filter(fn: (r) => r["symbol"] == "${symbol}")
  |> filter(fn: (r) => r["_field"] == "vwap")
  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)
  |> yield(name: "vwap")
```

**Display settings:**
- Alias `close` → `Price`, colour `#74b9ff`
- Alias `vwap` → `VWAP`, colour `#fdcb6e`, dash style `Dashed`

---

### Panel 2 — OFI Bar Chart (Green / Red)

**Visualization:** Bar chart (or Time series with bars mode)

```flux
from(bucket: "neko_signal")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "market_data")
  |> filter(fn: (r) => r["symbol"] == "${symbol}")
  |> filter(fn: (r) => r["_field"] == "ofi")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> yield(name: "OFI")
```

**Thresholds (Value Mappings → Thresholds tab):**

| Condition | Colour |
|---|---|
| value ≥ 0 | Green (`#00b894`) |
| value < 0  | Red (`#d63031`) |

> In Grafana's **Overrides** tab, set the `OFI` series to **Bar chart** draw mode
> and enable **Threshold as fill colour**.

---

### Panel 3 — CVD Trend (Cumulative Volume Delta)

**Visualization:** Time series

```flux
from(bucket: "neko_signal")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "market_data")
  |> filter(fn: (r) => r["symbol"] == "${symbol}")
  |> filter(fn: (r) => r["_field"] == "cvd")
  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)
  |> yield(name: "CVD")
```

**Display settings:**
- Fill below line: **10%** transparency
- Colour: `#a29bfe` (rising = bullish purple)

---

### Panel 4 — Trade State Annotations (LONG / SHORT markers)

**Visualization:** Time series (add as Annotations to Panel 1)

Go to **Dashboard settings → Annotations → Add annotation query**:

```flux
// Detects every candle where trade_state transitions AWAY from IDLE
from(bucket: "neko_signal")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "market_data")
  |> filter(fn: (r) => r["symbol"] == "${symbol}")
  |> filter(fn: (r) => r["_field"] == "close")
  |> filter(fn: (r) => r["trade_state"] == "LONG" or r["trade_state"] == "SHORT")
  |> map(fn: (r) => ({
      r with
      _value: r["_value"],
      title: r["trade_state"],
      text: "Score: " + string(v: r["trade_state"])
  }))
  |> yield(name: "annotations")
```

**Annotation colour mapping:**
- `LONG`  → Green `#00b894` ▲
- `SHORT` → Red `#d63031` ▼

> **Grafana tip:** Under **Annotation settings**, map `title` to the label field
> and `text` to the description. This places a vertical coloured marker on
> every panel at the exact timestamp the signal fired.

---

### Panel 5 — Directional Score Heatmap

**Visualization:** Time series with threshold bands

```flux
from(bucket: "neko_signal")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "market_data")
  |> filter(fn: (r) => r["symbol"] == "${symbol}")
  |> filter(fn: (r) => r["_field"] == "score")
  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)
  |> yield(name: "Score")
```

**Thresholds:**

| Value | Colour | Meaning |
|---|---|---|
| ≥ +4 | Green | LONG signal zone |
| +1 to +3 | Yellow | Weak bullish |
| -3 to 0 | Grey | Neutral |
| ≤ -4 | Red | SHORT signal zone |

Add a **reference line** at `y = +4` and `y = -4` to show the threshold bands.

---

## 5. Grafana Dashboard Variable (Symbol Selector)

Add a **Dashboard variable** so you can switch pairs from a dropdown:

1. **Dashboard settings → Variables → Add variable**
2. Name: `symbol`
3. Type: **Custom**
4. Values: `BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT`
5. Multi-value: OFF | Include All: OFF

Use `${symbol}` in all queries above.

---

## 6. Recommended Dashboard Layout

```
┌──────────────────────────────────────────────────┐
│  [Symbol: BTCUSDT ▼]  [Time range: Last 3h ▼]   │
├──────────────────────────────────────────────────┤
│                                                  │
│   Panel 1: Price + VWAP  (tall, full width)      │
│   ← LONG ▲ and SHORT ▼ annotation markers        │
│                                                  │
├───────────────────────┬──────────────────────────┤
│  Panel 2: OFI Bars    │  Panel 3: CVD Trend      │
│  (green/red bars)     │  (purple line + fill)    │
├───────────────────────┴──────────────────────────┤
│   Panel 5: Score meter  (-5 ──── 0 ──── +5)     │
└──────────────────────────────────────────────────┘
```

---

## 7. Docker Compose (Full Stack)

Save as `docker-compose.monitoring.yml` in the project root for one-command startup:

```yaml
version: "3.9"

services:
  influxdb:
    image: influxdb:2.7
    container_name: neko_influxdb
    ports:
      - "8086:8086"
    volumes:
      - influxdb2_data:/var/lib/influxdb2
    environment:
      DOCKER_INFLUXDB_INIT_MODE: setup
      DOCKER_INFLUXDB_INIT_USERNAME: admin
      DOCKER_INFLUXDB_INIT_PASSWORD: nekopassword
      DOCKER_INFLUXDB_INIT_ORG: neko
      DOCKER_INFLUXDB_INIT_BUCKET: neko_signal
      DOCKER_INFLUXDB_INIT_ADMIN_TOKEN: ${INFLUXDB_TOKEN}
    restart: unless-stopped

  grafana:
    image: grafana/grafana-oss:latest
    container_name: neko_grafana
    ports:
      - "3000:3000"
    volumes:
      - grafana_data:/var/lib/grafana
    environment:
      GF_SECURITY_ADMIN_PASSWORD: nekopassword
    depends_on:
      - influxdb
    restart: unless-stopped

volumes:
  influxdb2_data:
  grafana_data:
```

```bash
# Start the full monitoring stack
docker compose -f docker-compose.monitoring.yml up -d

# Then run the bot (reads INFLUXDB_TOKEN from .env automatically)
python main_live.py
```

---

*Dashboard built with 🐾 Neko Signal telemetry pipeline.*
