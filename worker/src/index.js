// Kalshi Weather Data Worker
// Fetches NWS forecasts + Kalshi market data, returns bundled JSON
// Uses authenticated Kalshi API requests (RSA-PSS signing)

const CITY_CONFIGS = {
  CHI: { name: "Chicago Midway", station: "KMDW", lat: 41.78412, lon: -87.75514, high: "KXHIGHCHI", low: "KXLOWTCHI", tz: "America/Chicago" },
  NYC: { name: "New York (Central Park)", station: "KNYC", lat: 40.77898, lon: -73.96925, high: "KXHIGHNYC", low: "KXLOWTNYC", tz: "America/New_York" },
  MIA: { name: "Miami", station: "KMIA", lat: 25.78805, lon: -80.31694, high: "KXHIGHMIA", low: "KXLOWTMIA", tz: "America/New_York" },
  LAX: { name: "Los Angeles", station: "KLAX", lat: 33.93816, lon: -118.38660, high: "KXHIGHLAX", low: "KXLOWTLAX", tz: "America/Los_Angeles" },
  AUS: { name: "Austin", station: "KAUS", lat: 30.18311, lon: -97.67989, high: "KXHIGHAUS", low: "KXLOWTAUS", tz: "America/Chicago" },
  DEN: { name: "Denver", station: "KDEN", lat: 39.84657, lon: -104.65623, high: "KXHIGHDEN", low: "KXLOWTDEN", tz: "America/Denver" },
  PHIL: { name: "Philadelphia", station: "KPHL", lat: 39.87326, lon: -75.22681, high: "KXHIGHPHI", low: "KXLOWTPHI", tz: "America/New_York" },
};

const NWS_HEADERS = { "User-Agent": "KalshiWeatherAgent/2.0 (cloudflare-worker)" };
const KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2";

function cToF(c) { return c * 9 / 5 + 32; }

function toTickerDate(dateStr) {
  const d = new Date(dateStr + "T12:00:00Z");
  const months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
  const yy = String(d.getUTCFullYear()).slice(-2);
  const mmm = months[d.getUTCMonth()];
  const dd = String(d.getUTCDate()).padStart(2, "0");
  return yy + mmm + dd;
}

// ---------------------------------------------------------------------------
// Kalshi RSA-PSS Auth
// ---------------------------------------------------------------------------

function pemToArrayBuffer(pem) {
  // Strip PEM headers and decode base64
  const b64 = pem
    .replace(/-----BEGIN RSA PRIVATE KEY-----/g, "")
    .replace(/-----END RSA PRIVATE KEY-----/g, "")
    .replace(/-----BEGIN PRIVATE KEY-----/g, "")
    .replace(/-----END PRIVATE KEY-----/g, "")
    .replace(/\s/g, "");
  const binary = atob(b64);
  const buf = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) buf[i] = binary.charCodeAt(i);
  return buf.buffer;
}

let _cachedKey = null;

async function getSigningKey(env) {
  if (_cachedKey) return _cachedKey;
  const pem = env.KALSHI_PRIVATE_KEY;
  if (!pem) throw new Error("KALSHI_PRIVATE_KEY secret not set");
  const keyData = pemToArrayBuffer(pem);

  // Try PKCS#8 first, fall back to PKCS#1
  try {
    _cachedKey = await crypto.subtle.importKey(
      "pkcs8", keyData,
      { name: "RSA-PSS", hash: "SHA-256" },
      false, ["sign"]
    );
  } catch (_) {
    // PKCS#1 keys need wrapping in PKCS#8 envelope or use a different approach
    // Cloudflare Workers only support PKCS#8, so we'll try as-is
    throw new Error("Key import failed. Key must be in PKCS#8 format (BEGIN PRIVATE KEY). Convert with: openssl pkcs8 -topk8 -inform PEM -outform PEM -nocrypt -in key.pem -out key-pkcs8.pem");
  }
  return _cachedKey;
}

async function makeKalshiHeaders(env, method, path) {
  const key = await getSigningKey(env);
  const apiKeyId = env.KALSHI_API_KEY_ID;
  if (!apiKeyId) throw new Error("KALSHI_API_KEY_ID secret not set");

  const ts = String(Date.now());
  const pathNoQuery = path.split("?")[0];
  const message = new TextEncoder().encode(`${ts}${method}${pathNoQuery}`);

  const signature = await crypto.subtle.sign(
    { name: "RSA-PSS", saltLength: 32 },
    key, message
  );

  const sigB64 = btoa(String.fromCharCode(...new Uint8Array(signature)));

  return {
    "KALSHI-ACCESS-KEY": apiKeyId,
    "KALSHI-ACCESS-SIGNATURE": sigB64,
    "KALSHI-ACCESS-TIMESTAMP": ts,
    "Content-Type": "application/json",
  };
}

// ---------------------------------------------------------------------------
// Fetch helpers
// ---------------------------------------------------------------------------

async function safeFetch(url, headers = {}) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const resp = await fetch(url, { headers });
    if (resp.ok) return resp.json();
    if (resp.status === 429 && attempt < 2) {
      await new Promise(r => setTimeout(r, 1500 * (attempt + 1)));
      continue;
    }
    throw new Error(`HTTP ${resp.status} from ${url}`);
  }
}

async function kalshiFetch(env, path) {
  const url = `${KALSHI_BASE}${path}`;
  for (let attempt = 0; attempt < 3; attempt++) {
    const headers = await makeKalshiHeaders(env, "GET", `/trade-api/v2${path}`);
    const resp = await fetch(url, { headers });
    if (resp.ok) return resp.json();
    if (resp.status === 429 && attempt < 2) {
      await new Promise(r => setTimeout(r, 1500 * (attempt + 1)));
      continue;
    }
    throw new Error(`HTTP ${resp.status} from ${url}`);
  }
}

// ---------------------------------------------------------------------------
// Data fetchers
// ---------------------------------------------------------------------------

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

async function fetchMarkets(env, seriesTicker, targetDate) {
  try {
    const tickerDate = toTickerDate(targetDate);
    const data = await kalshiFetch(env, `/events?status=open&series_ticker=${seriesTicker}&with_nested_markets=true&limit=50`);

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
          kalshiFetch(env, `/markets/${c.ticker}/orderbook`)
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

async function fetchEnsemble(config, targetDate) {
  try {
    const url = `https://ensemble-api.open-meteo.com/v1/ensemble?latitude=${config.lat}&longitude=${config.lon}&daily=temperature_2m_max,temperature_2m_min&temperature_unit=fahrenheit&start_date=${targetDate}&end_date=${targetDate}&models=ecmwf_ifs025,gfs_seamless,icon_seamless`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    const daily = data.daily || {};
    const highMembers = [];
    const lowMembers = [];

    // Collect all ensemble member values -- keys look like:
    //   temperature_2m_max_ecmwf_ifs025_ensemble          (control run)
    //   temperature_2m_max_member01_ecmwf_ifs025_ensemble  (perturbation)
    for (const key of Object.keys(daily)) {
      if (key === "time") continue;
      const vals = daily[key];
      if (!Array.isArray(vals) || vals.length === 0 || vals[0] === null) continue;
      if (key.startsWith("temperature_2m_max")) {
        highMembers.push(vals[0]);
      } else if (key.startsWith("temperature_2m_min")) {
        lowMembers.push(vals[0]);
      }
    }

    return {
      high_members: highMembers,
      low_members: lowMembers,
      member_count: Math.max(highMembers.length, lowMembers.length),
      error: null,
    };
  } catch (e) {
    return { high_members: [], low_members: [], member_count: 0, error: e.message };
  }
}

async function fetchCityBundle(env, _code, config, targetDate) {
  const [weather, high, low, ensemble] = await Promise.all([
    fetchWeather(config, targetDate),
    fetchMarkets(env, config.high, targetDate),
    fetchMarkets(env, config.low, targetDate),
    fetchEnsemble(config, targetDate),
  ]);
  return {
    city_name: config.name, station: config.station,
    weather: { ...weather, ensemble },
    markets: {
      high: { series_ticker: config.high, ...high },
      low: { series_ticker: config.low, ...low },
    },
  };
}

// ---------------------------------------------------------------------------
// Request handler
// ---------------------------------------------------------------------------

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const headers = { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" };

    if (url.pathname === "/health") {
      const hasAuth = !!(env.KALSHI_API_KEY_ID && env.KALSHI_PRIVATE_KEY);
      return new Response(JSON.stringify({ status: "ok", authenticated: hasAuth, cities: Object.keys(CITY_CONFIGS) }), { headers });
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

      // Fetch cities sequentially to avoid Kalshi rate limits
      const results = [];
      for (const c of codes) {
        try {
          const bundle = await fetchCityBundle(env, c, CITY_CONFIGS[c], targetDate);
          results.push({ status: "fulfilled", value: bundle });
        } catch (e) {
          results.push({ status: "rejected", reason: e });
        }
      }

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