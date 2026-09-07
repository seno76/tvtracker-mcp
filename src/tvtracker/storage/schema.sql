-- Схема tvtracker. Применяется идемпотентно при каждом открытии соединения.
--
-- Два решения, зашитые прямо здесь:
--   * корпус сериалов полный, эпизоды ленивые — грузятся только для отслеживаемых;
--   * FTS5 внешнего содержимого: индекс не дублирует тексты, а ссылается на `shows`
--     по rowid (https://www.sqlite.org/fts5.html#external_content_tables).

CREATE TABLE IF NOT EXISTS shows (
    slug           TEXT PRIMARY KEY,
    tvmaze_id      INTEGER NOT NULL UNIQUE,
    name           TEXT NOT NULL,
    name_local     TEXT NOT NULL DEFAULT '',
    type           TEXT,
    language       TEXT,
    genres_json    TEXT NOT NULL DEFAULT '[]',
    status         TEXT,
    premiered      TEXT,
    ended          TEXT,
    avg_runtime    INTEGER,
    rating         REAL,
    weight         INTEGER,
    network        TEXT,
    summary_clean  TEXT NOT NULL DEFAULT '',
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS shows_by_tvmaze_id ON shows(tvmaze_id);
CREATE INDEX IF NOT EXISTS shows_by_language  ON shows(language);
CREATE INDEX IF NOT EXISTS shows_by_status    ON shows(status);

CREATE VIRTUAL TABLE IF NOT EXISTS shows_fts USING fts5(
    name,
    name_local,
    genres_json,
    summary_clean,
    content='shows',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

-- Триггеры держат индекс в согласии с таблицей. Для внешнего содержимого удаление
-- оформляется специальной вставкой с command='delete' — иначе в индексе остаётся мусор.
CREATE TRIGGER IF NOT EXISTS shows_fts_ai AFTER INSERT ON shows BEGIN
    INSERT INTO shows_fts(rowid, name, name_local, genres_json, summary_clean)
    VALUES (new.rowid, new.name, new.name_local, new.genres_json, new.summary_clean);
END;

CREATE TRIGGER IF NOT EXISTS shows_fts_ad AFTER DELETE ON shows BEGIN
    INSERT INTO shows_fts(shows_fts, rowid, name, name_local, genres_json, summary_clean)
    VALUES ('delete', old.rowid, old.name, old.name_local, old.genres_json, old.summary_clean);
END;

CREATE TRIGGER IF NOT EXISTS shows_fts_au AFTER UPDATE ON shows BEGIN
    INSERT INTO shows_fts(shows_fts, rowid, name, name_local, genres_json, summary_clean)
    VALUES ('delete', old.rowid, old.name, old.name_local, old.genres_json, old.summary_clean);
    INSERT INTO shows_fts(rowid, name, name_local, genres_json, summary_clean)
    VALUES (new.rowid, new.name, new.name_local, new.genres_json, new.summary_clean);
END;

-- Эпизоды: только для отслеживаемых сериалов. `airstamp` — метка UTC, по ней и только
-- по ней решается вопрос «вышла уже или нет».
CREATE TABLE IF NOT EXISTS episodes (
    show_slug      TEXT NOT NULL REFERENCES shows(slug) ON DELETE CASCADE,
    season         INTEGER NOT NULL,
    number         INTEGER NOT NULL,
    name           TEXT NOT NULL DEFAULT '',
    airstamp       TEXT,
    runtime        INTEGER,
    rating         REAL,
    summary_clean  TEXT NOT NULL DEFAULT '',
    kind           TEXT NOT NULL DEFAULT 'regular',
    PRIMARY KEY (show_slug, season, number)
);

CREATE INDEX IF NOT EXISTS episodes_by_airstamp ON episodes(airstamp);

CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    name,
    summary_clean,
    content='episodes',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS episodes_fts_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, name, summary_clean)
    VALUES (new.rowid, new.name, new.summary_clean);
END;

CREATE TRIGGER IF NOT EXISTS episodes_fts_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, name, summary_clean)
    VALUES ('delete', old.rowid, old.name, old.summary_clean);
END;

CREATE TRIGGER IF NOT EXISTS episodes_fts_au AFTER UPDATE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, name, summary_clean)
    VALUES ('delete', old.rowid, old.name, old.summary_clean);
    INSERT INTO episodes_fts(rowid, name, summary_clean)
    VALUES (new.rowid, new.name, new.summary_clean);
END;

-- Личное состояние. Единственная таблица, которую меняет пользователь.
CREATE TABLE IF NOT EXISTS tracking (
    show_slug   TEXT PRIMARY KEY REFERENCES shows(slug) ON DELETE CASCADE,
    state       TEXT NOT NULL CHECK (state IN ('watching', 'paused', 'finished', 'dropped')),
    season      INTEGER,
    number      INTEGER,
    rating      INTEGER CHECK (rating IS NULL OR rating BETWEEN 1 AND 10),
    note        TEXT,
    started_at  TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
