-- The crawler's structured data (case_progress database). Only storage.py touches it.
-- Order PDFs/HTML and the KMC PDFs stay on disk (pdfs/, case_details/).
-- Re-runnable: storage.init_db() applies this file at start-up.

-- ---- crawl bookkeeping ------------------------------------------------------------------------

-- courts.json: every court the crawler knows, by id
CREATE TABLE IF NOT EXISTS courts (
    court_id    text PRIMARY KEY,
    data        jsonb NOT NULL
);

-- state.json: what each court's crawler has already downloaded (key = PDF url, or "<commission>/<date>")
CREATE TABLE IF NOT EXISTS crawl_state (
    court       text NOT NULL,
    key         text NOT NULL,
    value       jsonb NOT NULL,
    PRIMARY KEY (court, key)
);

-- links.json: hand-entered links per case, in the order shown
CREATE TABLE IF NOT EXISTS case_links (
    court       text NOT NULL,
    case_key    text NOT NULL,
    links       jsonb NOT NULL,
    PRIMARY KEY (court, case_key)
);

-- ---- what the portals gave us -----------------------------------------------------------------

-- causelists/consumer/<commission>/<date>[_N].json: one e-Jagriti cause list (a revised list is a new version)
CREATE TABLE IF NOT EXISTS cause_lists (
    commission_id   text NOT NULL,
    list_date       date NOT NULL,
    version         integer NOT NULL,
    doc             jsonb NOT NULL,
    saved_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (commission_id, list_date, version)
);

-- case_details/consumer/<case key>/{status,history,meta,classification,no_orders}.json
CREATE TABLE IF NOT EXISTS case_documents (
    case_key    text NOT NULL,
    name        text NOT NULL,
    doc         jsonb NOT NULL,
    saved_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (case_key, name)
);

-- case_details/consumer/<case key>/versions/<name>_<stamp>.json: earlier copies of a changed document
CREATE TABLE IF NOT EXISTS case_document_versions (
    case_key    text NOT NULL,
    name        text NOT NULL,
    stamp       text NOT NULL,
    doc         jsonb NOT NULL,
    PRIMARY KEY (case_key, name, stamp)
);

-- ---- the analysis (rebuilt wholesale by analysis.py / analysis_consumer.py, one court at a time) ---
-- Columns are the ones the former CSVs had, text as the viewer expects ('' for none, ISO dates, 'yes'/'no').

CREATE TABLE IF NOT EXISTS analysis_runs (
    court       text PRIMARY KEY,
    run_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS analysis_status (
    id              bigserial PRIMARY KEY,
    court           text NOT NULL,
    case_key        text NOT NULL,
    enquiry_id      text NOT NULL,
    status          text NOT NULL DEFAULT '',
    status_since    text NOT NULL DEFAULT '',
    label           text NOT NULL DEFAULT '',
    label_reason    text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS analysis_status_court ON analysis_status (court, id);

CREATE TABLE IF NOT EXISTS analysis_parties (
    id              bigserial PRIMARY KEY,
    court           text NOT NULL,
    enquiry_id      text NOT NULL,
    party           text NOT NULL,
    party_type      text NOT NULL DEFAULT '',
    role            text NOT NULL,
    party_as_of     text NOT NULL DEFAULT '',
    case_key        text NOT NULL
);
CREATE INDEX IF NOT EXISTS analysis_parties_court ON analysis_parties (court, id);

CREATE TABLE IF NOT EXISTS analysis_advocates (
    id              bigserial PRIMARY KEY,
    court           text NOT NULL,
    enquiry_id      text NOT NULL,
    case_key        text NOT NULL,
    role            text NOT NULL,
    advocate        text NOT NULL,
    source          text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS analysis_advocates_court ON analysis_advocates (court, id);

CREATE TABLE IF NOT EXISTS analysis_timeline (
    id                      bigserial PRIMARY KEY,
    court                   text NOT NULL,
    enquiry_id              text NOT NULL,
    hearing_date            text NOT NULL,
    hearing_time            text NOT NULL DEFAULT '',
    time_source             text NOT NULL DEFAULT '',
    list                    text NOT NULL DEFAULT '',
    held                    text NOT NULL DEFAULT '',
    purpose_tags            text NOT NULL DEFAULT '',
    purpose_text            text NOT NULL DEFAULT '',
    outcome_text            text NOT NULL DEFAULT '',
    next_date               text NOT NULL DEFAULT '',
    postponed_from          text NOT NULL DEFAULT '',
    next_stage_tags         text NOT NULL DEFAULT '',
    cross_examination_of    text NOT NULL DEFAULT '',
    raw_text                text NOT NULL DEFAULT '',
    case_key                text NOT NULL
);
CREATE INDEX IF NOT EXISTS analysis_timeline_court ON analysis_timeline (court, id);

-- enquiries.json: everything about each enquiry/case, as the analysis built it
CREATE TABLE IF NOT EXISTS analysis_enquiries (
    court       text NOT NULL,
    enquiry_id  text NOT NULL,
    doc         jsonb NOT NULL,
    PRIMARY KEY (court, enquiry_id)
);

-- ---- labels for slicing (metrics.py rebuilds both wholesale; see its docstring) -----------------------

CREATE TABLE IF NOT EXISTS case_metrics (
    court               text NOT NULL,
    case_key            text NOT NULL,
    enquiry_id          text NOT NULL,
    year                integer,
    kind                text NOT NULL DEFAULT '',
    outcome             text NOT NULL,              -- open / disposed / allowed / dismissed
    provider_side       text NOT NULL DEFAULT '',   -- which party is the hospital/doctor: complainant / respondent / ''
    label               text NOT NULL DEFAULT '',   -- medical / not_medical / unclear / ''
    hospitals           text NOT NULL DEFAULT '',
    n_hearings          integer NOT NULL,
    n_held              integer NOT NULL,
    first_hearing       date,
    last_hearing        date,
    n_gaps              integer NOT NULL,
    median_gap_days     integer,
    max_gap_days        integer,
    time_asked_patient  integer NOT NULL,           -- hearings where the record says the patient side asked for time
    time_asked_hospital integer NOT NULL,
    absent_patient      integer NOT NULL,
    absent_hospital     integer NOT NULL,
    filing_date         date,                           -- the portal's caseFilingDate (consumer cases; NULL for the KMC)
    days_to_close       integer,                        -- filing_date to last_hearing, for a closed case (NULL while open); the last hearing stands in for the decision date
    computed_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (court, case_key)
);
-- for a database made before these columns existed
ALTER TABLE case_metrics ADD COLUMN IF NOT EXISTS filing_date date;
ALTER TABLE case_metrics ADD COLUMN IF NOT EXISTS days_to_close integer;

CREATE TABLE IF NOT EXISTS hearing_metrics (
    id              bigserial PRIMARY KEY,
    court           text NOT NULL,
    case_key        text NOT NULL,
    hearing_date    date,
    held            boolean NOT NULL,
    gap_days        integer,                        -- days since the previous hearing day (NULL for the first)
    time_asked_by   text NOT NULL DEFAULT '',       -- patient / hospital / both / '' (not stated, or provider unknown)
    absent_side     text NOT NULL DEFAULT '',
    provider_side   text NOT NULL DEFAULT '',
    from_history    boolean NOT NULL DEFAULT false  -- the hearing is known only from the portal's history, not a cause list
);
CREATE INDEX IF NOT EXISTS hearing_metrics_case ON hearing_metrics (court, case_key);

-- ---- how each case's judgement PDF reads (judgements.py; the daily crawl keeps it current) ----------------

CREATE TABLE IF NOT EXISTS case_judgements (
    case_key       text PRIMARY KEY,
    file           text NOT NULL,                   -- the judgement PDF classified (name and size say whether it has changed)
    size           bigint NOT NULL,
    class          text NOT NULL,                   -- dismissed / part / full / unclear / scanned
    reason         text NOT NULL DEFAULT '',        -- why, for a dismissal (see DISMISSAL_REASONS)
    version        integer NOT NULL,                -- judgements.CLASSIFIER_VERSION it was classified with
    classified_at  timestamptz NOT NULL DEFAULT now()
);

-- ---- the "At a glance" panel, ready to show (dashboard_stats.py; the daily crawl rebuilds it after judgements.py) ----
-- One row per what a visitor can ask for: the court selection (kmc / consumer / all), whether not_medical cases are
-- shown, and a year (0 = all years). doc is viewer.analytics()'s result, so the page need not walk every case.

CREATE TABLE IF NOT EXISTS dashboard_stats (
    sel            text NOT NULL,
    non_medical    boolean NOT NULL,
    year           integer NOT NULL,
    doc            jsonb NOT NULL,
    computed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sel, non_medical, year)
);
