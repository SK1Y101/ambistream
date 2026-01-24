let state = null;
let selected = null;

// things we set in the backend

const WEATHER_VALUES = [
    "clear", "clouds", "rain", "drizzle", "thunderstorm", "snow",
    "mist", "smoke", "haze", "dust", "fog", "sand", "ash", "squall", "tornado"
];

const TIME_VALUES = [
    "dawn", "morning", "day", "dusk", "evening", "night"
];

const STATUS_VALUES = [
    "unknown", "startup", "running", "shutdown", "error",
    // just for the frontent<>backend
    "connected", "connecting", "connection-lost", "no-data"
];

const PLACEHOLDER = "placeholder.jpg";
const NO_SONG = "Select a song";

let backendStatus = "";
let player_running = null;
let lastState = new Date();
let SongLoadInFlight = false;

// random utils

function capitalise(text) {
    return text[0].toUpperCase() + text.slice(1).toLowerCase();
}

function qs(id) {
    return document.getElementById(id);
}

function setStatus(label, status) {
    const dot = qs(`${label}-status`);
    const text = qs(`${label}-status-text`);

    if (!STATUS_VALUES.includes(status)) {
        status = "unknown";
    };

    if (status == text.textContent.toLowerCase()) { return; };

    dot.className = "status-dot";
    dot.classList.add(status);

    text.textContent = capitalise(status);
};


const thumbnailCache = new Map(); // filepath -> objectURL | null

async function getThumbnail(filepath) {
    if (!filepath) return PLACEHOLDER;

    if (thumbnailCache.has(filepath)) {
        return thumbnailCache.get(filepath) ?? PLACEHOLDER;
    }

    try {
        const res = await fetch(`/songs/thumbnail?filepath=${encodeURIComponent(filepath)}`);
        if (!res.ok) {
            thumbnailCache.set(filepath, null);
            return PLACEHOLDER;
        }

        const blob = await res.blob();
        const objectUrl = URL.createObjectURL(blob);
        thumbnailCache.set(filepath, objectUrl);
        return objectUrl;

    } catch {
        thumbnailCache.set(filepath, null);
        return PLACEHOLDER;
    }
}

// update all the information

function render() {
    renderStatus();
    renderWeather();
    renderHistory();
    renderQueue();
    renderCurrent();
    populateDBSongs();
}

function renderStatus() {
    setStatus("backend", backendStatus);
    const old_player = player_running;

    if (backendStatus == "connection-lost") {
        setStatus("player", "unknown");
        setStatus("weather", "unknown");
        player_running = false;
    } else {
        setStatus("player", state?.statuses?.player);
        setStatus("weather", state?.statuses?.weather);
        player_running = state?.statuses?.player == "running";
    };

    if (old_player != player_running) {
        qs("load-url-queue").disabled = !player_running;
        qs("load-db-queue").disabled = !player_running;
        qs("enqueue").disabled = !player_running;
    };
}

function estimateTime() {
    const now = new Date();
    if (now.getHours() < 6) {
        return "night";
    }
    if (now.getHours() < 7 && now.getMinutes() <= 30) {
        return "dawn";
    }
    if (now.getHours() < 12) {
        return "morning";
    }
    if (now.getHours() < 18) {
        return "day";
    }
    if (now.getHours() < 19 && now.getMinutes() <= 30) {
        return "dusk";
    }
    if (now.getHours() < 20) {
        return "dusk";
    }
    return "night";
}

function renderWeather() {
    let weather = "missing weather";
    let time = estimateTime();

    if (state.weather) {
        weather = state.weather.description ?? "unknown weather condition";
        time = state.weather.timeperiod ?? "unknown time period";
    }

    qs("weather").textContent = `${capitalise(weather)} - ${capitalise(time)}`;
}

async function renderCurrent() {
    const el = qs("current-song");
    el.innerHTML = "";

    if (!state.current) {
        el.textContent = "Nothing playing";
        return;
    }

    const imgSrc = await getThumbnail(state.current.filepath);

    el.innerHTML = `
        <div class="song-card">
            <img src="${imgSrc}" alt="">
            <span>${state.current.title}</span>
        </div>
    `;
}

async function renderQueue() {
    const ul = qs("queue-list");
    ul.innerHTML = "";

    if (!state.queue) return;

    for (const item of state.queue) {
        const li = document.createElement("li");
        const imgSrc = await getThumbnail(item.filepath);

        li.innerHTML = `
            <div class="song-card">
                <img src="${imgSrc}" alt="">
                <span>${item.title}</span>
            </div>
        `;

        li.onclick = () => loadIntoModify(item);
        ul.appendChild(li);
    }
}

async function renderHistory() {
    const ul = qs("history-list");
    ul.innerHTML = "";

    if (!state.history) return;

    for (const item of state.history) {
        const li = document.createElement("li");
        const imgSrc = await getThumbnail(item.filepath);

        li.innerHTML = `
            <div class="song-card">
                <img src="${imgSrc}" alt="">
                <span>${item.title}</span>
            </div>
        `;

        li.onclick = () => loadIntoModify(item);
        ul.appendChild(li);
    }
}

// do the rules

function renderRuleCheckboxes(containerId, values, selectedValues = []) {
    const el = qs(containerId);
    el.innerHTML = "";

    values.forEach(v => {
        const id = `${containerId}-${v}`;

        const label = document.createElement("label");
        label.className = "rule-checkbox";

        const input = document.createElement("input");
        input.type = "checkbox";
        input.value = v;
        input.checked = selectedValues.includes(v);
        input.id = id;

        input.onchange = () => {
            if (!input.checked) return;

            const otherContainer =
                containerId.includes("prefer")
                    ? containerId.replace("prefer", "ban")
                    : containerId.replace("ban", "prefer");

            qs(otherContainer)
                .querySelectorAll(`input[value="${v}"]`)
                .forEach(i => i.checked = false);
        };


        const span = document.createElement("span");
        span.textContent = v;

        label.appendChild(input);
        label.appendChild(span);
        el.appendChild(label);
    });
}

function collectRule(containerId) {
    return [...qs(containerId).querySelectorAll("input:checked")]
        .map(i => i.value);
}

// the modifying area stuff

async function loadIntoModify(item) {
    selected = item;

    qs("mod-main").checked = item.main ?? false;
    qs("mod-fallback").checked = item.fallback ?? false;

    qs("mod-title").value = item.title ?? "";
    qs("mod-url").value = item.url ?? "";
    qs("mod-pinned").checked = item.pinned ?? false;

    renderRuleCheckboxes(
        "mod-weather-prefer",
        WEATHER_VALUES,
        item.weather?.prefer ?? []
    );

    renderRuleCheckboxes(
        "mod-weather-ban",
        WEATHER_VALUES,
        item.weather?.ban ?? []
    );

    renderRuleCheckboxes(
        "mod-time-prefer",
        TIME_VALUES,
        item.time?.prefer ?? []
    );

    renderRuleCheckboxes(
        "mod-time-ban",
        TIME_VALUES,
        item.time?.ban ?? []
    );

    const img = qs("mod-thumbnail");
    const imgSrc = await getThumbnail(item.filepath);
    img.src = imgSrc || PLACEHOLDER;
    img.style.display = imgSrc ? "block" : "none";

    disableModify(false);
    checkMeta();
}

function disableModify(disabled) {
    qs("modify-form").style.display = !disabled ? "grid" : "none";
    qs("modify-empty").style.display = disabled ? "block" : "none";

    qs("mod-main").disabled = disabled;
    qs("mod-fallback").disabled = disabled;
    if (disabled) {
        disableMetadata(true);
    }
}

function checkMeta() {
    const main = qs("mod-main");

    const allow = main.checked;
    if (allow) {
        disableMetadata(false);
    } else {
        disableMetadata(true);
    }
}

function disableMetadata(disabled) {
    qs("pinned-block").classList.toggle("disabled", disabled);
    qs("weather-block").classList.toggle("disabled", disabled);
    qs("time-block").classList.toggle("disabled", disabled);
}

// buttons and thingies

async function clearState() {
    disableModify(true);
}

async function saveState() {
    if (!selected) return;

    setTextStatus("Saving...", "save-status");

    const isMain = qs("mod-main").checked;
    const isFallback = qs("mod-fallback").checked;

    try {
        // put first
        if (isMain) {
            await fetch("/songs/main", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    url: selected.url,
                    title: selected.title,
                    pinned: qs("mod-pinned").checked,
                    duration: selected.duration,
                    filepath: selected.filepath,
                    artwork: selected.artwork,
                    weather: {
                        prefer: collectRule("mod-weather-prefer"),
                        ban: collectRule("mod-weather-ban")
                    },
                    time: {
                        prefer: collectRule("mod-time-prefer"),
                        ban: collectRule("mod-time-ban")
                    }
                })
            });
        }

        if (isFallback) {
            await fetch("/songs/fallback", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    url: selected.url,
                    title: selected.title,
                    duration: selected.duration,
                    filepath: selected.filepath,
                    artwork: selected.artwork,
                })
            });
        }

        // then delete
        if (!isMain) {
            const song_in_main = await fetch(`/songs/main?identifier=${encodeURIComponent(selected.title)}`);
            if (song_in_main.ok) {
                await fetch("/songs/main", {
                    method: "DELETE",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ identifier: selected.url })
                });
            }
        }

        if (!isFallback) {
            const song_in_fallback = await fetch(`/songs/fallback?identifier=${encodeURIComponent(selected.title)}`);
            if (song_in_fallback.ok) {
                await fetch("/songs/fallback", {
                    method: "DELETE",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ identifier: selected.url })
                });
            }
        }
        setTextStatus("Saved to DB", "save-status");
    } catch {
        setTextStatus("Saved failed", "save-status");
    }

    // ensure the database select is always up to date
    populateDBSongs();
}

async function enqueue(statusElement = "save-status") {
    if (!selected) return;

    if (backendStatus == "connection-lost") {
        setTextStatus("Cannot communicate with backend", statusElement ?? "save-status");
        return;
    };

    if (!player_running) {
        setTextStatus("Cannot queue, player not running", statusElement ?? "save-status");
        return;
    }

    try {
        setTextStatus("Queueing...", statusElement ?? "save-status");
        await fetch("/queue", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: selected.url })
        });
        setTextStatus("Queued song", statusElement ?? "save-status");
    } catch {
        setTextStatus("Queue failed", statusElement ?? "save-status");
    };
}

function setDLButtonsDisabled(disabled) {
    qs("load-url-modify").disabled = disabled;
    qs("load-url-queue").disabled = !player_running || disabled;
    qs("load-db-modify").disabled = disabled;
    qs("load-db-queue").disabled = !player_running || disabled;
}

function setTextStatus(text, element = "dl-status") {
    qs(element).textContent = `Status: ${capitalise(text)}`;
}

async function fetchSongByUrl(url) {
    if (SongLoadInFlight) return null;

    SongLoadInFlight = true;
    setDLButtonsDisabled(true);
    setTextStatus("Loading...");

    try {
        const res = await fetch(`/songs?url=${encodeURIComponent(url)}`);

        if (!res.ok) {
            if (res.status === 404) {
                setTextStatus("Not found");
            } else {
                setTextStatus("Error");
            }
            return null;
        }

        const song = await res.json();
        setTextStatus("Loaded");
        return song;

    } catch {
        setTextStatus("Connection error");
        return null;

    } finally {
        SongLoadInFlight = false;
        setDLButtonsDisabled(false);
    }
}
async function fetchSongByTitle(title) {
    if (SongLoadInFlight) return null;

    SongLoadInFlight = true;
    setDLButtonsDisabled(true);
    setTextStatus("Loading...");

    try {
        const res = await fetch(`/songs?title=${encodeURIComponent(title)}`);

        if (!res.ok) {
            if (res.status === 404) {
                setTextStatus("Not found");
            } else {
                setTextStatus("Error");
            }
            return null;
        }

        const song = await res.json();
        setTextStatus("Loaded");
        return song;

    } catch {
        setTextStatus("Connection error");
        return null;

    } finally {
        SongLoadInFlight = false;
        setDLButtonsDisabled(false);
    }
}

async function populateDBSongs() {
    const select = qs("db-select");
    const default_option = document.createElement("option");
    default_option.value = NO_SONG;
    default_option.textContent = NO_SONG;

    // clear existing options except placeholder
    while (select.firstChild) {
        select.removeChild(select.lastChild);
    }
    select.appendChild(default_option);

    const response = await fetch("/songs/all");
    if (!response.ok) {
        console.error("Failed to load songs");
        return;
    }

    const data = await response.json();

    if (data.songs.length > 0) {
        const mainGroup = document.createElement("optgroup");
        mainGroup.label = "Main Database";

        data.songs.forEach(song => {
            const option = document.createElement("option");
            option.value = song.title;
            option.textContent = song.title;
            mainGroup.appendChild(option);
        });

        if (select.lastChild && select.lastChild.label != mainGroup.label) {
            select.appendChild(mainGroup);
        };
    };

    if (data.fallback.length > 0) {
        const fallbackGroup = document.createElement("optgroup");
        fallbackGroup.label = "Fallback";

        data.fallback.forEach(song => {
            const option = document.createElement("option");
            option.value = song.title;
            option.textContent = song.title;
            fallbackGroup.appendChild(option);
        });

        if (select.lastChild && select.lastChild.label != fallbackGroup.label) {
            select.appendChild(fallbackGroup);
        };
    };
}


function wireSelectionButtons() {
    qs("load-url-modify").onclick = async () => {
        const url = qs("url-input").value.trim();
        if (!url) return;
        if (url == NO_SONG) return;

        const song = await fetchSongByUrl(url);
        if (song) {
            loadIntoModify(song);
        }
    };

    qs("load-url-queue").onclick = async () => {
        const url = qs("url-input").value.trim();
        if (!url) return;
        if (url == NO_SONG) return;

        const song = await fetchSongByUrl(url);
        if (song) {
            selected = song;
            await enqueue("dl-status");
        }
    };

    qs("load-db-modify").onclick = async () => {
        const title = qs("db-select").value;
        if (!title) return;
        if (title == NO_SONG) return;

        const song = await fetchSongByTitle(title);
        if (song) {
            loadIntoModify(song);
        }
    };

    qs("load-db-queue").onclick = async () => {
        const title = qs("db-select").value;
        if (!title) return;
        if (title == NO_SONG) return;

        const song = await fetchSongByTitle(title);
        if (song) {
            selected = song;
            await enqueue("dl-status");
        }
    };
}


// Start everything

function initialiseDateTime() {
    function setdate() {
        const date = new Date();
        qs("datetime").textContent = date.toLocaleTimeString("en-GB");
    }
    setdate();
    setInterval(setdate, 1000);
}

function prefetchThumbnails() {
    const seen = new Set();

    const collect = (song) => {
        if (song?.filepath && !seen.has(song.filepath)) {
            seen.add(song.filepath);
            getThumbnail(song.filepath);
        }
    };

    collect(state.current);
    state.queue?.forEach(collect);
    state.history?.forEach(collect);
}

function initialiseRuleCheckboxes() {
    renderRuleCheckboxes("mod-weather-prefer", WEATHER_VALUES, []);
    renderRuleCheckboxes("mod-weather-ban", WEATHER_VALUES, []);
    renderRuleCheckboxes("mod-time-prefer", TIME_VALUES, []);
    renderRuleCheckboxes("mod-time-ban", TIME_VALUES, []);
}

async function init() {
    initialiseDateTime();
    wireSelectionButtons();
    initialiseRuleCheckboxes();
    disableModify(true);

    qs("mod-main").onchange = () => { checkMeta(); };
    qs("mod-fallback").onchange = () => { checkMeta(); };

    const res = await fetch("/state");
    state = await res.json();

    prefetchThumbnails();
    render();

    const es = new EventSource("/events");
    es.onmessage = (e) => {
        const now = new Date();
        const msg = JSON.parse(e.data);

        switch (msg.event) {
            case "heartbeat":
                backendStatus = "connected";
                break;

            case "no_state":
                const seconds = (now.getTime() - lastState.getTime()) / 1000;
                if (seconds >= 5) {
                    backendStatus = "no-data";
                };
                break;

            case "state_update":
                lastState = now;
                state = msg.state;
                prefetchThumbnails();
                render();
                break;
        };
        renderStatus();
    };
    es.onopen = () => {
        backendStatus = "connecting";
        renderStatus();
    };
    es.onerror = () => {
        backendStatus = "connection-lost";
        renderStatus();
    };

    qs("save-state").onclick = saveState;
    qs("enqueue").onclick = enqueue;
    qs("clear").onclick = clearState;
}


init();
