import { useEffect, useMemo, useState, type CSSProperties } from "react";

type Series = { id: string; title: string; platform: string; description: string | null };
type Release = { release_type: string; release_at: string; url: string | null };
type Episode = {
  id: string; series_id: string; season_number: number | null; number: number | null;
  title: string; description: string | null; platform: string; releases: Release[];
};

const API_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ?? "http://localhost:8000";
const TIMEZONE = "Europe/Vienna";
const CACHE_PREFIX = "episode-calendar-cache-v1";

type CachedValue<T> = { timestamp: number; data: T };

function readLocal<T>(key: string, fallback: T): T {
  try {
    const value = localStorage.getItem(key);
    if (value === null) return fallback;
    try {
      return JSON.parse(value) as T;
    } catch {
      // Preferences created by older versions were stored as plain strings.
      return value as T;
    }
  } catch {
    return fallback;
  }
}

function readCache<T>(key: string): CachedValue<T> | null {
  try {
    const value = JSON.parse(localStorage.getItem(key) ?? "null") as CachedValue<T> | null;
    return value && Array.isArray(value.data) && typeof value.timestamp === "number" ? value : null;
  } catch {
    return null;
  }
}

function writeCache<T>(key: string, data: T) {
  try {
    localStorage.setItem(key, JSON.stringify({ timestamp: Date.now(), data }));
  } catch {
    // Caching is best effort (for example, private browsing may disable storage).
  }
}

function weekDays(week: "current" | "next") {
  const now = new Date();
  const day = (now.getDay() + 6) % 7;
  const monday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - day + (week === "next" ? 7 : 0));
  return Array.from({ length: 7 }, (_, offset) => {
    const date = new Date(monday); date.setDate(monday.getDate() + offset); return date;
  });
}

const dateKey = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
const dayLabel = new Intl.DateTimeFormat("de-AT", { weekday: "long", day: "numeric", month: "short" });
const timeLabel = new Intl.DateTimeFormat("de-AT", { hour: "2-digit", minute: "2-digit" });

const SERIES_COLORS = [
  "#f0c674", "#78c6e8", "#d98bd8", "#8bd49c", "#f28f8f", "#b6a0f5", "#f5a96b", "#74d4c3",
];

/** Returns a stable, repeatable color for a series without storing extra state. */
export function seriesColor(seriesId: string) {
  let hash = 0;
  for (const character of seriesId) hash = (hash * 31 + character.charCodeAt(0)) | 0;
  return SERIES_COLORS[Math.abs(hash) % SERIES_COLORS.length];
}

export function episodeStartLabel(episode: Pick<Episode, "season_number" | "number">) {
  if (episode.number !== 1) return null;
  return episode.season_number === null || episode.season_number === 1 ? "Neue Serie" : "Neue Staffel";
}

const platformLabels: Record<string, string> = {
  bbc_iplayer: "BBC iPlayer",
  channel4: "Channel 4",
  itvx: "ITVX",
  joyn: "Joyn",
  rtlplus: "RTL+",
};

function platformLabel(platform: string) {
  return platformLabels[platform] ?? platform;
}

function SeriesFilterPage({ series, selectedIds, onSave, onDiscard }: {
  series: Series[];
  selectedIds: string[];
  onSave: (ids: string[]) => void;
  onDiscard: () => void;
}) {
  const [draftIds, setDraftIds] = useState(selectedIds);
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLocaleLowerCase("de");
  const groups = useMemo(() => {
    const visible = series
      .filter((item) => item.title.toLocaleLowerCase("de").includes(normalizedQuery))
      .sort((left, right) => left.title.localeCompare(right.title, "de"));
    return [...new Set(visible.map((item) => item.platform))]
      .sort((left, right) => platformLabel(left).localeCompare(platformLabel(right), "de"))
      .map((platform) => ({ platform, series: visible.filter((item) => item.platform === platform) }));
  }, [normalizedQuery, series]);

  const toggle = (id: string) => setDraftIds((current) => current.includes(id)
    ? current.filter((item) => item !== id)
    : [...current, id]);

  return <main className="app-shell filter-page">
    <header className="hero"><p className="eyebrow">EPISODE CALENDAR</p><h1>Serien filtern</h1><p className="subtitle">Wähle aus, welche Serien im Kalender erscheinen.</p></header>
    <label className="filter-search">Serien durchsuchen
      <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="z. B. Celebrity" />
    </label>
    <section className="series-groups" aria-label="Serienauswahl">
      {groups.length === 0 && <p className="empty">Keine Serien gefunden.</p>}
      {groups.map((group) => <section className="series-group" key={group.platform}>
        <h2>{platformLabel(group.platform)}</h2>
        {group.series.map((item) => <label className="series-filter-option" key={item.id}>
          <input type="checkbox" checked={draftIds.includes(item.id)} onChange={() => toggle(item.id)} />
          <span className="series-dot" style={{ backgroundColor: seriesColor(item.id) }} aria-hidden="true" />
          <span>{item.title}</span>
        </label>)}
      </section>)}
    </section>
    <div className="filter-actions">
      <button className="secondary-button" type="button" onClick={onDiscard}>Verwerfen</button>
      <button className="primary-button" type="button" onClick={() => onSave(draftIds)}>Speichern</button>
    </div>
  </main>;
}

export function App() {
  const [week, setWeek] = useState<"current" | "next">(() => readLocal<"current" | "next">("episode-calendar-week", "current"));
  const [series, setSeries] = useState<Series[]>([]);
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [selectedSeriesIds, setSelectedSeriesIds] = useState<string[]>([]);
  const [seriesSelectionInitialized, setSeriesSelectionInitialized] = useState(false);
  const [knownSeriesIds, setKnownSeriesIds] = useState<string[]>([]);
  const [filterOpen, setFilterOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { localStorage.setItem("episode-calendar-week", week); }, [week]);
  useEffect(() => {
    if (!series.length || seriesSelectionInitialized) return;
    const saved = readLocal<unknown>("episode-calendar-series-selection", null);
    const currentIds = series.map((item) => item.id);
    if (Array.isArray(saved)) {
      setSelectedSeriesIds(saved.filter((id): id is string => typeof id === "string" && currentIds.includes(id)));
    } else if (saved && typeof saved === "object" && "selected" in saved && "known" in saved
      && Array.isArray(saved.selected) && Array.isArray(saved.known)) {
      const selected = saved.selected.filter((id): id is string => typeof id === "string" && currentIds.includes(id));
      const known = saved.known.filter((id): id is string => typeof id === "string");
      setSelectedSeriesIds([...selected, ...currentIds.filter((id) => !known.includes(id))]);
    } else {
      setSelectedSeriesIds(currentIds);
    }
    setKnownSeriesIds(currentIds);
    setSeriesSelectionInitialized(true);
  }, [series, seriesSelectionInitialized]);
  useEffect(() => {
    if (!seriesSelectionInitialized) return;
    const currentIds = series.map((item) => item.id);
    const newIds = currentIds.filter((id) => !knownSeriesIds.includes(id));
    if (newIds.length) setSelectedSeriesIds((current) => [...current, ...newIds]);
    if (newIds.length) setKnownSeriesIds(currentIds);
  }, [knownSeriesIds, series, seriesSelectionInitialized]);
  useEffect(() => {
    if (seriesSelectionInitialized) {
      localStorage.setItem("episode-calendar-series-selection", JSON.stringify({ selected: selectedSeriesIds, known: knownSeriesIds }));
    }
  }, [knownSeriesIds, selectedSeriesIds, seriesSelectionInitialized]);
  useEffect(() => {
    const seriesKey = `${CACHE_PREFIX}:${API_URL}:series`;
    const episodesKey = `${CACHE_PREFIX}:${API_URL}:episodes:${week}:${TIMEZONE}`;
    const cachedSeries = readCache<Series[]>(seriesKey);
    const cachedEpisodes = readCache<Episode[]>(episodesKey);
    if (cachedSeries) setSeries(cachedSeries.data);
    if (cachedEpisodes) setEpisodes(cachedEpisodes.data);

    const load = async () => {
      setLoading(!cachedSeries || !cachedEpisodes); setError(null);
      try {
        const [seriesResponse, episodeResponse] = await Promise.all([
          fetch(`${API_URL}/api/v1/series`).then(async (response) => {
            if (!response.ok) throw new Error(`Serien konnten nicht geladen werden (${response.status}).`);
            return response.json();
          }),
          fetch(`${API_URL}/api/v1/episodes/${week}-week?timezone=${encodeURIComponent(TIMEZONE)}`).then(async (response) => {
            if (!response.ok) throw new Error(`Episoden konnten nicht geladen werden (${response.status}).`);
            return response.json();
          }),
        ]);
        if (!Array.isArray(seriesResponse) || !Array.isArray(episodeResponse)) throw new Error("Ungültige API-Antwort");
        writeCache(seriesKey, seriesResponse);
        writeCache(episodesKey, episodeResponse);
        setSeries(seriesResponse); setEpisodes(episodeResponse);
      } catch (cause) {
        if (!cachedSeries || !cachedEpisodes) setError(cause instanceof Error ? cause.message : "Die API ist nicht erreichbar.");
      }
      finally { setLoading(false); }
    };
    void load();
  }, [week]);

  const filtered = episodes.filter((episode) => selectedSeriesIds.includes(episode.series_id));
  const byDay = new Map<string, Episode[]>();
  filtered.forEach((episode) => {
    const release = episode.releases.find((item) => item.release_type === "streaming") ?? episode.releases[0];
    if (!release) return;
    const key = dateKey(new Date(release.release_at));
    byDay.set(key, [...(byDay.get(key) ?? []), episode]);
  });
  const seriesMap = new Map(series.map((item) => [item.id, item]));
  const days = weekDays(week);

  if (filterOpen) return <SeriesFilterPage
    series={series}
    selectedIds={selectedSeriesIds}
    onSave={(ids) => { setSelectedSeriesIds(ids); setFilterOpen(false); }}
    onDiscard={() => setFilterOpen(false)}
  />;

 return <main className="app-shell">
    <header className="hero"><p className="eyebrow">EPISODE CALENDAR</p><h1>Was läuft diese Woche?</h1><p className="subtitle">Neue Episoden deiner Serien auf einen Blick.</p></header>
    <nav className="week-switch" aria-label="Woche"><button className={week === "current" ? "active" : ""} onClick={() => setWeek("current")}>Diese Woche</button><button className={week === "next" ? "active" : ""} onClick={() => setWeek("next")}>Nächste Woche</button></nav>
    <section className="filters" aria-label="Filter"><button className="filter-button" type="button" onClick={() => setFilterOpen(true)}>Filter</button><span>{selectedSeriesIds.length} von {series.length} Serien ausgewählt</span></section>
    {loading && <p className="status">Kalender wird geladen …</p>}
    {error && <p className="status error">{error}<br /><small>Prüfe, ob die API unter {API_URL} läuft.</small></p>}
    {!loading && !error && <section className="calendar">{days.map((day) => { const items = byDay.get(dateKey(day)) ?? []; return <article className="day" key={dateKey(day)}><h2>{dayLabel.format(day)}</h2>{items.length === 0 ? <p className="empty">Keine Episoden</p> : items.map((episode) => { const release = episode.releases.find((item) => item.release_type === "streaming") ?? episode.releases[0]; const show = seriesMap.get(episode.series_id); const color = seriesColor(episode.series_id); const colorStyle = { "--series-color": color } as CSSProperties; const startLabel = episodeStartLabel(episode); const className = `episode${release?.url ? " episode-link" : ""}${startLabel ? " episode-new" : ""}`; const content = <><div className="episode-time">{release && timeLabel.format(new Date(release.release_at))}</div><div><h3><span className="series-dot" style={{ backgroundColor: color }} aria-hidden="true" />{show?.title ?? "Unbekannte Serie"}</h3><p>{episode.season_number ? `S${episode.season_number} · ` : ""}{episode.number ? `E${episode.number} · ` : ""}{episode.title}</p><div className="episode-badges">{startLabel && <span className="new-badge">{startLabel}</span>}<span className={`badge ${episode.platform}`}>{episode.platform}</span></div></div></>; return release?.url ? <a className={className} style={colorStyle} href={release.url} target="_blank" rel="noreferrer" key={episode.id}>{content}</a> : <div className={className} style={colorStyle} key={episode.id}>{content}</div>; })}</article>; })}</section>}
    {!loading && !error && filtered.length === 0 && <p className="status">Für diese Filter wurden keine Episoden gefunden.</p>}
  </main>;
}
