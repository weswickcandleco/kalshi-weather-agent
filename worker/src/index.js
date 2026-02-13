// Kalshi Weather Data Worker
// Fetches NWS forecasts + Kalshi market data, returns bundled JSON

const CITY_CONFIGS = {
  CHI: { name: "Chicago Midway", station: "KMDW", lat: 41.786, lon: -87.752, high: "KXHIGHCHI", low: "KXLOWTCHI", tz: "America/Chicago" },
  NYC: { name: "New York (Central Park)", station: "KNYC", lat: 40.7789, lon: -73.9692, high: "KXHIGHNYC", low: "KXLOWTNYC", tz: "America/New_York" },
  MIA: { name: "Miami", station: "KMIA", lat: 25.7959, lon: -80.287, high: "KXHIGHMIA", low: "KXLOWTMIA", tz: "America/New_York" },
  LAX: { name: "Los Angeles", station: "KLAX", lat: 33.9425, lon: -118.4081, high: "KXHIGHLAX", low: "KXLOWTLAX", tz: "America/Los_Angeles" },
  AUS: { name: "Austin", station: "KAUS", lat: 30.1945, lon: -97.6699, high: "KXHIGHAUS", low: "KXLOWTAUS", tz: "America/Chicago" },
  DEN: { name: "Denver", station: "KDEN", lat: 39.8561, lon: -104.6737, high: "KXHIGHDEN", low: "KXLOWTDEN", tz: "America/Denver" },
  PHIL: { name: "Philadelphia", station: "KPHL", lat: 39.8721, lon: -75.2411, high: "KXHIGHPHI", low: "KXLOWTPHI", tz: "America/New_York" },
};

const NWS_HEADERS = { "User-Agent": "KalshiWeatherAgent/2.0 (cloudflare-worker)" };
const KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2";

function cToF(c) { return c * 9 / 5 + 32; }

// Format date as Kalshi ticker code: YYMMMDD (e.g., 26FEB13)
function toTickerDate(dateStr) {
  const d = new Date(dateStr + "T12:00:00Z");
  const months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
  const yy = String(d.getUTCFullYear()).slice(-2);
  const mmm = months[d.getUTCMonth()];
  const dd = String(d.getUTCDate()).padStart(2, "0");
  return yy + mmm + dd;
}

async function safeFetch(url, headers = {}) {
  const resp = await fetch(url, { headers });
  if (!resp.ok) throw new Error(`HTTP ${resp.status} from ${url}`);
  return resp.json();
}

async function fetchWeather(config, targetDate) {
  try {
    const pointsData = await safeFetch(
      `https://api.weather.gov/points/${config.lat},${config.lon}`,
      NWS_HEADERS
    );
    const hourlyUrl = pointsData.properties.forecastHourly;
    const forecastData = await safeFetch(hourlyUrl, NWS_HEADERS);
    const periods = forecastData.properties.periods || [];

    const targetPeriods = periods.filter(p => p.startTime.startsWith(targetDate));
    const temps = targetPeriods.map(p => p.temperature);
    const highF = temps.length > 0 ? Math.max(...temps) : null;
    const lowF = temps.length > 0 ? Math.min(...temps) : null;

    let highHour = null, lowHour = null;
    for (const p of targetPeriods) {
      if (p.temperature === highF && !highHour) highHour = p.startTime;
      if (p.temperature === lowF && !lowHour) lowHour = p.startTime;
    }

    const hourly = targetPeriods.map(p => ({
      time: p.startTime,
      temp_f: p.temperature,
      wind: p.windSpeed,
      desc: p.shortForecast,
    }));

    // Current conditions (optional)
    let currentTemp = null, currentDesc = null, observedAt = null;
    try {
      const obsData = await safeFetch(
        `https://api.weather.gov/stations/${config.station}/observations/latest`,
        NWS_HEADERS
      );
      const props = obsData.properties;
      if (props.temperature && props.temperature.value !== null) {
        currentTemp = Math.round(cToF(props.temperature.value) * 10) / 10;
      }
      currentDesc = props.textDescription || null;
      observedAt = props.timestamp || null;
    } catch (_) { /* ok */ }

    return {
      predicted_high_f: highF, predicted_low_f: lowF,
      high_hour: highHour, low_hour: lowHour,
      current_temp_f: currentTemp, current_desc: currentDesc,
      observed_at: observedAt, hourly, error: null,
    };
  } catch (e) {
    return {
      predicted_high_f: null, predicted_low_f: null,
      high_hour: null, low_hour: null,
      current_temp_f: null, current_desc: null,
      observed_at: null, hourly: [], error: e.message,
    };
  }
}

async function fetchMarkets(seriesTicker, targetDate) {
  try {
    const tickerDate = toTickerDate(targetDate);
    const url = `${KALSHI_BASE}/events?status=open&series_ticker=${seriesTicker}&with_nested_markets=true&limit=50`;
    const data = await safeFetch(url);

    const contracts = [];
    for (const event of (data.events || [])) {
      for (const m of (event.markets || [])) {
        if (!m.ticker.includes(tickerDate)) continue;
        contracts.push({
          ticker: m.ticker,
          title: m.title || "",
          yes_sub_title: m.yes_sub_title || "",
          no_sub_title: m.no_sub_title || "",
          close_time: m.close_time,
          yes_bid: m.yes_bid, yes_ask: m.yes_ask,
          no_bid: m.no_bid, no_ask: m.no_ask,
          last_price: m.last_price, volume: m.volume,
          open_interest: m.open_interest,
          orderbook: null,
        });
      }
    }

    // Fetch orderbooks in batches of 5
    for (let i = 0; i < contracts.length; i += 5) {
      const batch = contracts.slice(i, i + 5);
      const results = await Promise.allSettled(
        batch.map(c =>
          safeFetch(`${KALSHI_BASE}/markets/${c.ticker}/orderbook`)
            .then(d => ({ ticker: c.ticker, ob: d.orderbook || d }))
            .catch(e => ({ ticker: c.ticker, ob: { yes: [], no: [], error: e.message } }))
        )
      );
      for (const r of results) {
        if (r.status === "fulfilled") {
          const c = contracts.find(x => x.ticker === r.value.ticker);
          if (c) c.orderbook = r.value.ob;
        }
      }
    }

    return { contracts, error: null };
  } catch (e) {
    return { contracts: [], error: e.message };
  }
}

async function fetchCityBundle(code, config, targetDate) {
  const [weather, high, low] = await Promise.all([
    fetchWeather(config, targetDate),
    fetchMarkets(config.high, targetDate),
    fetchMarkets(config.low, targetDate),
  ]);
  return {
    city_name: config.name, station: config.station,
    weather,
    markets: {
      high: { series_ticker: config.high, ...high },
      low: { series_ticker: config.low, ...low },
    },
  };
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const headers = { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" };

    if (url.pathname === "/health") {
      return new Response(JSON.stringify({ status: "ok", cities: Object.keys(CITY_CONFIGS) }), { headers });
    }

    if (url.pathname === "/bundle") {
      const targetDate = url.searchParams.get("date");
      if (!targetDate || !/^\d{4}-\d{2}-\d{2}$/.test(targetDate)) {
        return new Response(JSON.stringify({ error: "date param required (YYYY-MM-DD)" }), { status: 400, headers });
      }

      const citiesParam = url.searchParams.get("cities");
      let codes;
      if (citiesParam) {
        codes = citiesParam.split(",").map(c => c.trim().toUpperCase()).filter(c => c in CITY_CONFIGS);
        if (!codes.length) {
          return new Response(JSON.stringify({ error: "No valid cities", available: Object.keys(CITY_CONFIGS) }), { status: 400, headers });
        }
      } else {
        codes = Object.keys(CITY_CONFIGS);
      }

      const results = await Promise.allSettled(
        codes.map(c => fetchCityBundle(c, CITY_CONFIGS[c], targetDate))
      );

      const cities = {};
      const errors = [];
      for (let i = 0; i < codes.length; i++) {
        if (results[i].status === "fulfilled") {
          cities[codes[i]] = results[i].value;
        } else {
          const msg = results[i].reason?.message || "Unknown error";
          errors.push({ city: codes[i], error: msg });
          cities[codes[i]] = {
            city_name: CITY_CONFIGS[codes[i]].name,
            weather: { error: msg },
            markets: { high: { contracts: [] }, low: { contracts: [] } },
          };
        }
      }

      return new Response(JSON.stringify({ generated_at: new Date().toISOString(), target_date: targetDate, cities, errors }), { headers });
    }

    return new Response(JSON.stringify({ error: "Use /bundle?date=YYYY-MM-DD or /health" }), { status: 404, headers });
  },
};
