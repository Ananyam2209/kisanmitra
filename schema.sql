-- =============================================================================
-- KisanMitra — Distress Guardian
-- PostgreSQL Database Schema
--
-- Tables:
--   farmers              Core farmer profiles
--   farmer_loans         Loan records (one farmer can have multiple)
--   weather_readings     Daily district-level rainfall snapshots
--   market_prices        Daily mandi price records per crop/district
--   risk_scores          FROS history — one row per farmer per day
--   scheme_eligibility   Which schemes each farmer qualifies for
--   alert_log            Record of every WhatsApp alert sent
--   officers             Agricultural officer registry per district
--   ngo_contacts         NGO/KVK volunteer registry per district
-- =============================================================================


-- ---------------------------------------------------------------------------
-- Extension: PostGIS for geo-matching (Unity Agent will use this too)
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";


-- ---------------------------------------------------------------------------
-- ENUM types
-- ---------------------------------------------------------------------------

CREATE TYPE crop_category     AS ENUM ('cash', 'food');
CREATE TYPE lender_type       AS ENUM ('bank', 'cooperative', 'moneylender', 'government');
CREATE TYPE farming_season    AS ENUM ('kharif', 'rabi', 'zaid');
CREATE TYPE risk_severity     AS ENUM ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL');
CREATE TYPE alert_type        AS ENUM ('farmer', 'officer', 'ngo');
CREATE TYPE alert_status      AS ENUM ('sent', 'failed', 'pending');
CREATE TYPE language_code     AS ENUM ('en', 'mr', 'hi', 'te', 'kn', 'ta');


-- ---------------------------------------------------------------------------
-- 1. FARMERS
-- ---------------------------------------------------------------------------

CREATE TABLE farmers (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    farmer_code         VARCHAR(20) UNIQUE NOT NULL,   -- e.g. MH-WD-001
    full_name           VARCHAR(120) NOT NULL,
    phone               VARCHAR(15) NOT NULL,          -- E.164 format
    preferred_language  language_code NOT NULL DEFAULT 'mr',

    -- Location
    district            VARCHAR(80) NOT NULL,
    block               VARCHAR(80),
    village             VARCHAR(80),
    state               VARCHAR(80) NOT NULL,
    geo_location        GEOGRAPHY(POINT, 4326),        -- lat/lon for geo-matching

    -- Farm details
    land_acres          NUMERIC(6, 2) NOT NULL,
    primary_crop        VARCHAR(60) NOT NULL,
    crop_category       crop_category NOT NULL,
    irrigation_type     VARCHAR(30) DEFAULT 'rain_fed', -- rain_fed | canal | borewell | drip

    -- Financials
    annual_income_inr   NUMERIC(12, 2),
    bank_account_no     VARCHAR(20),                   -- masked in application layer
    aadhaar_linked      BOOLEAN DEFAULT FALSE,

    -- Metadata
    onboarded_at        TIMESTAMPTZ DEFAULT NOW(),
    last_active_at      TIMESTAMPTZ,
    is_active           BOOLEAN DEFAULT TRUE,

    CONSTRAINT chk_land_positive CHECK (land_acres > 0)
);

CREATE INDEX idx_farmers_district  ON farmers(district);
CREATE INDEX idx_farmers_geo       ON farmers USING GIST(geo_location);
CREATE INDEX idx_farmers_crop      ON farmers(primary_crop);


-- ---------------------------------------------------------------------------
-- 2. FARMER LOANS
-- ---------------------------------------------------------------------------

CREATE TABLE farmer_loans (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    farmer_id           UUID NOT NULL REFERENCES farmers(id) ON DELETE CASCADE,

    lender_type         lender_type NOT NULL,
    lender_name         VARCHAR(120),
    principal_inr       NUMERIC(12, 2) NOT NULL,
    outstanding_inr     NUMERIC(12, 2) NOT NULL,
    interest_rate_pct   NUMERIC(5, 2),                -- annual %
    disbursed_on        DATE,
    due_date            DATE,
    purpose             VARCHAR(120),                 -- 'seed purchase', 'equipment', etc.
    is_active           BOOLEAN DEFAULT TRUE,

    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT chk_principal_positive CHECK (principal_inr > 0)
);

CREATE INDEX idx_loans_farmer  ON farmer_loans(farmer_id);
CREATE INDEX idx_loans_due     ON farmer_loans(due_date) WHERE is_active = TRUE;


-- ---------------------------------------------------------------------------
-- 3. WEATHER READINGS  (sourced from IMD API)
-- ---------------------------------------------------------------------------

CREATE TABLE weather_readings (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    district                VARCHAR(80) NOT NULL,
    state                   VARCHAR(80) NOT NULL,
    reading_date            DATE NOT NULL,
    season                  farming_season NOT NULL,

    normal_rainfall_mm      NUMERIC(8, 2) NOT NULL,   -- long period average
    actual_rainfall_mm      NUMERIC(8, 2) NOT NULL,   -- cumulative season rainfall
    deficit_pct             NUMERIC(6, 2) GENERATED ALWAYS AS (
                                CASE WHEN normal_rainfall_mm > 0
                                THEN ROUND(
                                    (normal_rainfall_mm - actual_rainfall_mm)
                                    / normal_rainfall_mm * 100, 2)
                                ELSE 0 END
                            ) STORED,

    source                  VARCHAR(30) DEFAULT 'IMD_API',
    fetched_at              TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (district, reading_date, season)
);

CREATE INDEX idx_weather_district_date ON weather_readings(district, reading_date);


-- ---------------------------------------------------------------------------
-- 4. MARKET PRICES  (sourced from eNAM API)
-- ---------------------------------------------------------------------------

CREATE TABLE market_prices (
    id                          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    crop                        VARCHAR(60) NOT NULL,
    district                    VARCHAR(80) NOT NULL,
    mandi_name                  VARCHAR(120),
    price_date                  DATE NOT NULL,

    msp_inr_per_quintal         NUMERIC(10, 2) NOT NULL,
    mandi_price_inr_per_quintal NUMERIC(10, 2) NOT NULL,
    gap_pct                     NUMERIC(6, 2) GENERATED ALWAYS AS (
                                    CASE WHEN msp_inr_per_quintal > 0
                                    THEN ROUND(
                                        (msp_inr_per_quintal - mandi_price_inr_per_quintal)
                                        / msp_inr_per_quintal * 100, 2)
                                    ELSE 0 END
                                ) STORED,

    source                      VARCHAR(30) DEFAULT 'eNAM_API',
    fetched_at                  TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (crop, district, price_date)
);

CREATE INDEX idx_prices_crop_district_date ON market_prices(crop, district, price_date);


-- ---------------------------------------------------------------------------
-- 5. RISK SCORES  (FROS history — one row per farmer per day)
-- ---------------------------------------------------------------------------

CREATE TABLE risk_scores (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    farmer_id           UUID NOT NULL REFERENCES farmers(id) ON DELETE CASCADE,
    computed_on         DATE NOT NULL DEFAULT CURRENT_DATE,

    -- Component scores
    rainfall_score      NUMERIC(5, 2) NOT NULL,
    msp_gap_score       NUMERIC(5, 2) NOT NULL,
    debt_score          NUMERIC(5, 2) NOT NULL,
    modifier_points     NUMERIC(5, 2) NOT NULL DEFAULT 0,

    -- Final score
    total_score         NUMERIC(5, 2) NOT NULL,
    severity            risk_severity NOT NULL,

    -- Input snapshot (for audit / explainability)
    flags               TEXT[],
    breakdown_json      JSONB,

    -- Reference data used
    weather_reading_id  UUID REFERENCES weather_readings(id),
    market_price_id     UUID REFERENCES market_prices(id),

    created_at          TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (farmer_id, computed_on),

    CONSTRAINT chk_score_range CHECK (total_score BETWEEN 0 AND 100)
);

CREATE INDEX idx_risk_farmer_date   ON risk_scores(farmer_id, computed_on DESC);
CREATE INDEX idx_risk_severity      ON risk_scores(severity, computed_on DESC);
CREATE INDEX idx_risk_high_alerts   ON risk_scores(computed_on DESC)
    WHERE severity IN ('HIGH', 'CRITICAL');


-- ---------------------------------------------------------------------------
-- 6. SCHEME ELIGIBILITY
-- ---------------------------------------------------------------------------

CREATE TABLE scheme_eligibility (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    farmer_id           UUID NOT NULL REFERENCES farmers(id) ON DELETE CASCADE,
    scheme_code         VARCHAR(30) NOT NULL,        -- 'PMFBY', 'PM_KISAN', 'KCC', etc.
    scheme_name         VARCHAR(120) NOT NULL,
    is_enrolled         BOOLEAN DEFAULT FALSE,
    identified_on       DATE NOT NULL DEFAULT CURRENT_DATE,
    enrolled_on         DATE,
    application_url     TEXT,
    notes               TEXT,

    UNIQUE (farmer_id, scheme_code)
);

CREATE INDEX idx_scheme_farmer       ON scheme_eligibility(farmer_id);
CREATE INDEX idx_scheme_not_enrolled ON scheme_eligibility(farmer_id)
    WHERE is_enrolled = FALSE;


-- ---------------------------------------------------------------------------
-- 7. ALERT LOG
-- ---------------------------------------------------------------------------

CREATE TABLE alert_log (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    farmer_id           UUID NOT NULL REFERENCES farmers(id) ON DELETE CASCADE,
    risk_score_id       UUID REFERENCES risk_scores(id),

    alert_type          alert_type NOT NULL,
    recipient_phone     VARCHAR(15) NOT NULL,
    message_body        TEXT NOT NULL,
    twilio_message_sid  VARCHAR(40),                  -- from Twilio API response
    status              alert_status NOT NULL DEFAULT 'pending',

    sent_at             TIMESTAMPTZ,
    error_message       TEXT,

    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_alert_farmer       ON alert_log(farmer_id, created_at DESC);
CREATE INDEX idx_alert_status       ON alert_log(status) WHERE status = 'failed';


-- ---------------------------------------------------------------------------
-- 8. OFFICERS (Agricultural officer registry)
-- ---------------------------------------------------------------------------

CREATE TABLE officers (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    full_name       VARCHAR(120) NOT NULL,
    phone           VARCHAR(15) NOT NULL,
    district        VARCHAR(80) NOT NULL,
    block           VARCHAR(80),
    designation     VARCHAR(80) DEFAULT 'Agricultural Officer',
    is_active       BOOLEAN DEFAULT TRUE,

    UNIQUE (phone)
);

CREATE INDEX idx_officers_district ON officers(district) WHERE is_active = TRUE;


-- ---------------------------------------------------------------------------
-- 9. NGO / KVK CONTACTS
-- ---------------------------------------------------------------------------

CREATE TABLE ngo_contacts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_name        VARCHAR(120) NOT NULL,
    contact_name    VARCHAR(120),
    phone           VARCHAR(15) NOT NULL,
    district        VARCHAR(80) NOT NULL,
    org_type        VARCHAR(30) DEFAULT 'NGO',       -- NGO | KVK | FPO
    is_active       BOOLEAN DEFAULT TRUE,

    UNIQUE (phone)
);

CREATE INDEX idx_ngo_district ON ngo_contacts(district) WHERE is_active = TRUE;


-- ---------------------------------------------------------------------------
-- Useful views
-- ---------------------------------------------------------------------------

-- Latest risk score per farmer
CREATE VIEW v_latest_risk AS
SELECT DISTINCT ON (farmer_id)
    farmer_id,
    computed_on,
    total_score,
    severity,
    flags
FROM risk_scores
ORDER BY farmer_id, computed_on DESC;


-- All HIGH/CRITICAL farmers with contact info (for officer dashboard)
CREATE VIEW v_high_risk_farmers AS
SELECT
    f.farmer_code,
    f.full_name,
    f.phone,
    f.district,
    f.primary_crop,
    f.land_acres,
    r.total_score,
    r.severity,
    r.computed_on,
    r.flags
FROM farmers f
JOIN v_latest_risk r ON r.farmer_id = f.id
WHERE r.severity IN ('HIGH', 'CRITICAL')
  AND f.is_active = TRUE
ORDER BY r.total_score DESC;


-- Scheme gap analysis — eligible but not enrolled
CREATE VIEW v_scheme_gaps AS
SELECT
    f.farmer_code,
    f.full_name,
    f.phone,
    f.district,
    se.scheme_code,
    se.scheme_name,
    se.application_url
FROM scheme_eligibility se
JOIN farmers f ON f.id = se.farmer_id
WHERE se.is_enrolled = FALSE
  AND f.is_active = TRUE
ORDER BY f.district, se.scheme_code;


-- ---------------------------------------------------------------------------
-- Seed data: scheme catalog
-- ---------------------------------------------------------------------------

CREATE TABLE scheme_catalog (
    code            VARCHAR(30) PRIMARY KEY,
    full_name       VARCHAR(120) NOT NULL,
    benefit         TEXT,
    apply_url       TEXT,
    eligible_crops  TEXT[] DEFAULT ARRAY['all'],
    is_active       BOOLEAN DEFAULT TRUE
);

INSERT INTO scheme_catalog (code, full_name, benefit, apply_url, eligible_crops) VALUES
    ('PMFBY',    'Pradhan Mantri Fasal Bima Yojana',
     'Crop insurance covering yield loss due to natural calamities',
     'https://pmfby.gov.in',
     ARRAY['cotton','soybean','rice','wheat','sugarcane']),

    ('PM_KISAN', 'Pradhan Mantri Kisan Samman Nidhi',
     '₹6,000/year direct income support in three instalments',
     'https://pmkisan.gov.in',
     ARRAY['all']),

    ('KCC',      'Kisan Credit Card',
     'Short-term credit at 4% interest (vs 24–36% moneylender rate)',
     'https://www.nabard.org/content1.aspx?id=572',
     ARRAY['all']),

    ('RKVY',     'Rashtriya Krishi Vikas Yojana',
     'Grant funding for farm mechanisation and irrigation',
     'https://rkvy.nic.in',
     ARRAY['all']),

    ('SMAM',     'Sub-Mission on Agricultural Mechanization',
     '50–80% subsidy on farm equipment purchase',
     'https://agrimachinery.nic.in',
     ARRAY['all']);
