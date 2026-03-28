-- Manager settings storage for persisted UI controls

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_by UUID REFERENCES profiles(id) ON DELETE SET NULL,
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

ALTER TABLE app_settings ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Authenticated users can read app settings" ON app_settings;
CREATE POLICY "Authenticated users can read app settings" ON app_settings
  FOR SELECT USING (auth.uid() IS NOT NULL);

DROP POLICY IF EXISTS "Managers can insert app settings" ON app_settings;
CREATE POLICY "Managers can insert app settings" ON app_settings
  FOR INSERT WITH CHECK (
    EXISTS (
      SELECT 1
      FROM profiles
      WHERE id = auth.uid() AND role IN ('manager', 'admin')
    )
  );

DROP POLICY IF EXISTS "Managers can update app settings" ON app_settings;
CREATE POLICY "Managers can update app settings" ON app_settings
  FOR UPDATE USING (
    EXISTS (
      SELECT 1
      FROM profiles
      WHERE id = auth.uid() AND role IN ('manager', 'admin')
    )
  )
  WITH CHECK (
    EXISTS (
      SELECT 1
      FROM profiles
      WHERE id = auth.uid() AND role IN ('manager', 'admin')
    )
  );

GRANT SELECT, INSERT, UPDATE ON app_settings TO authenticated;

INSERT INTO app_settings (key, value)
VALUES ('leaderboard_rank_by', 'won_units')
ON CONFLICT (key) DO NOTHING;

INSERT INTO app_settings (key, value)
VALUES ('advanced_analytics_top_n', '1')
ON CONFLICT (key) DO NOTHING;
-- Analytics Engine Database Schema Enhancement
-- This migration adds support for the comprehensive analytics and catch-up model

-- 1. Add outcome field to activities table for tracking call/activity results
ALTER TABLE activities ADD COLUMN IF NOT EXISTS outcome TEXT 
CHECK (outcome IN (
  'connected', 'no_answer', 'left_vm', 'appt_set', 'showed', 
  'no_show', 'sold', 'lost', 'negotiating', 'follow_up'
));

-- 2. Add lead source tracking to deals table
ALTER TABLE deals ADD COLUMN IF NOT EXISTS lead_source TEXT DEFAULT 'unknown';
ALTER TABLE deals ADD COLUMN IF NOT EXISTS lead_source_detail TEXT;

-- 3. Add analytics-specific fields to activities
ALTER TABLE activities ADD COLUMN IF NOT EXISTS contact_quality_score DECIMAL(3,2) DEFAULT 0.50;
ALTER TABLE activities ADD COLUMN IF NOT EXISTS response_time_minutes INTEGER;
ALTER TABLE activities ADD COLUMN IF NOT EXISTS follow_up_sequence INTEGER DEFAULT 1;

-- 4. Create indexes for analytics performance
CREATE INDEX IF NOT EXISTS idx_activities_outcome ON activities(outcome);
CREATE INDEX IF NOT EXISTS idx_activities_activity_type ON activities(activity_type);
CREATE INDEX IF NOT EXISTS idx_activities_completed_at ON activities(completed_at);
CREATE INDEX IF NOT EXISTS idx_activities_sales_rep_id ON activities(sales_rep_id);
CREATE INDEX IF NOT EXISTS idx_deals_lead_source ON deals(lead_source);
CREATE INDEX IF NOT EXISTS idx_deals_sales_rep_id ON deals(sales_rep_id);

-- 5. Create analytics aggregation view
CREATE OR REPLACE VIEW analytics_rep_performance AS
WITH monthly_activity_data AS (
  SELECT 
    sales_rep_id,
    DATE_TRUNC('month', completed_at) as period,
    COUNT(*) FILTER (WHERE activity_type = 'call') as total_calls,
    COUNT(DISTINCT deal_id) FILTER (WHERE activity_type IN ('call', 'email', 'text')) as unique_leads_attempted,
    COUNT(*) FILTER (WHERE activity_type IN ('call', 'email', 'text')) as total_attempts,
    COUNT(*) FILTER (WHERE outcome IN ('connected', 'appt_set', 'showed', 'sold')) as contacts,
    COUNT(*) FILTER (WHERE outcome = 'appt_set') as appointments_set,
    COUNT(*) FILTER (WHERE outcome = 'showed') as appointments_show,
    AVG(response_time_minutes) FILTER (WHERE response_time_minutes IS NOT NULL) as avg_response_time,
    COUNT(*) FILTER (WHERE activity_type = 'call' AND outcome = 'no_answer') as no_answers,
    COUNT(*) FILTER (WHERE activity_type = 'call' AND outcome = 'left_vm') as voicemails
  FROM activities 
  WHERE completed_at IS NOT NULL 
    AND completed_at >= CURRENT_DATE - INTERVAL '6 months'
  GROUP BY sales_rep_id, DATE_TRUNC('month', completed_at)
),
monthly_deal_data AS (
  SELECT 
    sales_rep_id,
    DATE_TRUNC('month', created_at) as period,
    lead_source,
    COUNT(*) as leads_count,
    COUNT(*) FILTER (WHERE status = 'closed_won') as units_sold,
    SUM(deal_amount) FILTER (WHERE status = 'closed_won') as revenue,
    SUM(gross_profit) FILTER (WHERE status = 'closed_won') as gross_profit
  FROM deals
  WHERE created_at >= CURRENT_DATE - INTERVAL '6 months'
  GROUP BY sales_rep_id, DATE_TRUNC('month', created_at), lead_source
),
lead_source_aggregated AS (
  SELECT 
    sales_rep_id,
    period,
    SUM(leads_count) as total_leads,
    SUM(units_sold) as units_sold,
    SUM(revenue) as revenue,
    SUM(gross_profit) as gross_profit,
    jsonb_object_agg(lead_source, leads_count) as leads_by_source
  FROM monthly_deal_data
  GROUP BY sales_rep_id, period
)
SELECT 
  COALESCE(a.sales_rep_id, d.sales_rep_id) as sales_rep_id,
  COALESCE(a.period, d.period) as period,
  COALESCE(d.units_sold, 0) as units_sold,
  COALESCE(d.leads_by_source, '{}'::jsonb) as leads_by_source,
  COALESCE(a.unique_leads_attempted, 0) as unique_leads_attempted,
  COALESCE(a.total_attempts, 0) as attempts,
  COALESCE(a.contacts, 0) as contacts,
  COALESCE(a.appointments_set, 0) as appointments_set,
  COALESCE(a.appointments_show, 0) as appointments_show,
  COALESCE(a.avg_response_time, 0) as first_response_time_minutes,
  COALESCE(d.revenue, 0) as revenue,
  COALESCE(d.gross_profit, 0) as gross_profit,
  
  -- Calculated rates
  CASE WHEN a.unique_leads_attempted > 0 
    THEN ROUND((a.contacts::decimal / a.unique_leads_attempted), 4) 
    ELSE 0 
  END as contact_rate,
  
  CASE WHEN a.contacts > 0 
    THEN ROUND((a.appointments_set::decimal / a.contacts), 4) 
    ELSE 0 
  END as appointment_set_rate,
  
  CASE WHEN a.appointments_set > 0 
    THEN ROUND((a.appointments_show::decimal / a.appointments_set), 4) 
    ELSE 0 
  END as show_rate,
  
  CASE WHEN a.appointments_show > 0 
    THEN ROUND((d.units_sold::decimal / a.appointments_show), 4) 
    ELSE 0 
  END as close_from_show_rate,
  
  CASE WHEN a.contacts > 0 
    THEN ROUND((d.units_sold::decimal / a.contacts), 4) 
    ELSE 0 
  END as close_from_contact_rate

FROM monthly_activity_data a
FULL OUTER JOIN lead_source_aggregated d 
  ON a.sales_rep_id = d.sales_rep_id AND a.period = d.period;

-- 6. Create store-wide source weights view  
CREATE OR REPLACE VIEW source_performance_weights AS
WITH source_totals AS (
  SELECT 
    lead_source,
    COUNT(*) as total_leads,
    COUNT(*) FILTER (WHERE status = 'closed_won') as total_units_sold,
    AVG(deal_amount) FILTER (WHERE status = 'closed_won') as avg_deal_amount
  FROM deals
  WHERE created_at >= CURRENT_DATE - INTERVAL '3 months'
    AND lead_source IS NOT NULL
  GROUP BY lead_source
)
SELECT 
  lead_source,
  total_leads,
  total_units_sold,
  avg_deal_amount,
  CASE WHEN total_leads > 0 
    THEN ROUND((total_units_sold::decimal / total_leads), 4) 
    ELSE 0 
  END as conversion_weight,
  CASE WHEN total_leads > 0 
    THEN ROUND((total_units_sold::decimal * avg_deal_amount / total_leads), 2) 
    ELSE 0 
  END as revenue_weight
FROM source_totals
WHERE total_leads >= 5  -- Only include sources with meaningful volume
ORDER BY conversion_weight DESC;

-- 7. Create catch-up targets view
CREATE OR REPLACE VIEW catch_up_targets AS
WITH rolling_performance AS (
  SELECT 
    sales_rep_id,
    AVG(units_sold) as avg_units_3mo,
    AVG(revenue) as avg_revenue_3mo,
    COUNT(*) as periods_with_data
  FROM analytics_rep_performance
  WHERE period >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '3 months')
  GROUP BY sales_rep_id
  HAVING COUNT(*) >= 2  -- At least 2 months of data
),
top_performer AS (
  SELECT MAX(avg_units_3mo) as top_units
  FROM rolling_performance
)
SELECT 
  rp.sales_rep_id,
  rp.avg_units_3mo as current_units,
  tp.top_units as top_performer_units,
  (tp.top_units - rp.avg_units_3mo) as gap,
  GREATEST(
    CEIL(rp.avg_units_3mo + (tp.top_units - rp.avg_units_3mo) * 0.25),
    rp.avg_units_3mo + 1
  ) as target_units,
  ROUND(rp.avg_units_3mo / tp.top_units, 4) as performance_index,
  rp.periods_with_data
FROM rolling_performance rp
CROSS JOIN top_performer tp;

-- 8. Update existing activities with realistic outcomes for demonstration
UPDATE activities 
SET outcome = CASE 
  WHEN activity_type = 'call' THEN 
    CASE 
      WHEN (EXTRACT(EPOCH FROM created_at)::bigint % 100) < 25 THEN 'no_answer'
      WHEN (EXTRACT(EPOCH FROM created_at)::bigint % 100) < 40 THEN 'left_vm' 
      WHEN (EXTRACT(EPOCH FROM created_at)::bigint % 100) < 75 THEN 'connected'
      ELSE 'appt_set'
    END
  WHEN activity_type = 'meeting' THEN
    CASE 
      WHEN (EXTRACT(EPOCH FROM created_at)::bigint % 100) < 15 THEN 'no_show'
      WHEN (EXTRACT(EPOCH FROM created_at)::bigint % 100) < 70 THEN 'showed'
      WHEN (EXTRACT(EPOCH FROM created_at)::bigint % 100) < 90 THEN 'negotiating'
      ELSE 'sold'
    END
  WHEN activity_type = 'email' THEN 'follow_up'
  WHEN activity_type = 'demo' THEN 
    CASE 
      WHEN (EXTRACT(EPOCH FROM created_at)::bigint % 100) < 15 THEN 'no_show'
      WHEN (EXTRACT(EPOCH FROM created_at)::bigint % 100) < 70 THEN 'showed'
      WHEN (EXTRACT(EPOCH FROM created_at)::bigint % 100) < 90 THEN 'negotiating'
      ELSE 'sold'
    END
  ELSE 'connected'
END
WHERE outcome IS NULL;

-- 9. Update deals with realistic lead sources
UPDATE deals 
SET lead_source = CASE 
  WHEN (EXTRACT(EPOCH FROM created_at)::bigint % 100) < 20 THEN 'referral'
  WHEN (EXTRACT(EPOCH FROM created_at)::bigint % 100) < 35 THEN 'service'
  WHEN (EXTRACT(EPOCH FROM created_at)::bigint % 100) < 50 THEN 'phone'
  WHEN (EXTRACT(EPOCH FROM created_at)::bigint % 100) < 75 THEN 'internet'
  ELSE 'walkin'
END
WHERE lead_source = 'unknown' OR lead_source IS NULL;

-- 10. Grant access to new views
GRANT SELECT ON analytics_rep_performance TO authenticated;
GRANT SELECT ON source_performance_weights TO authenticated;
GRANT SELECT ON catch_up_targets TO authenticated;

-- 11. Create function to get rep analytics data
CREATE OR REPLACE FUNCTION get_rep_analytics(rep_id UUID, target_period TEXT DEFAULT NULL)
RETURNS TABLE (
  rep_data jsonb,
  source_weights jsonb,
  store_baselines jsonb,
  catch_up_data jsonb
) 
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  analysis_period TEXT;
BEGIN
  -- Default to current month if no period specified
  analysis_period := COALESCE(target_period, TO_CHAR(CURRENT_DATE, 'YYYY-MM'));
  
  RETURN QUERY
  SELECT 
    to_jsonb(arp.*) as rep_data,
    (SELECT jsonb_object_agg(lead_source, conversion_weight) FROM source_performance_weights) as source_weights,
    (SELECT jsonb_build_object(
      'contact_rate', AVG(contact_rate),
      'appointment_set_rate', AVG(appointment_set_rate),
      'show_rate', AVG(show_rate)
    ) FROM analytics_rep_performance WHERE period >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '1 month')) as store_baselines,
    to_jsonb(ct.*) as catch_up_data
  FROM analytics_rep_performance arp
  LEFT JOIN catch_up_targets ct ON ct.sales_rep_id = arp.sales_rep_id
  WHERE arp.sales_rep_id = rep_id
    AND TO_CHAR(arp.period, 'YYYY-MM') = analysis_period
  LIMIT 1;
END;
$$;-- Forecast-first schema additions
-- Adds monthly cached stats and forecast outputs used by the domain forecast engine.

CREATE TABLE IF NOT EXISTS rep_month_stats (
  rep_id UUID REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  month DATE NOT NULL,
  leads INTEGER NOT NULL DEFAULT 0,
  contacts INTEGER NOT NULL DEFAULT 0,
  appts_set INTEGER NOT NULL DEFAULT 0,
  appts_show INTEGER NOT NULL DEFAULT 0,
  sold_units INTEGER NOT NULL DEFAULT 0,
  close_rate NUMERIC(6,4) NOT NULL DEFAULT 0,
  contact_rate NUMERIC(6,4) NOT NULL DEFAULT 0,
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
  PRIMARY KEY (rep_id, month)
);

CREATE TABLE IF NOT EXISTS rep_month_forecast (
  rep_id UUID REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  month DATE NOT NULL,
  quota_units INTEGER NOT NULL,
  projected_units NUMERIC(10,2) NOT NULL DEFAULT 0,
  quota_hit_probability NUMERIC(6,4) NOT NULL DEFAULT 0,
  expected_future_deals NUMERIC(10,4) NOT NULL DEFAULT 0,
  next_best_action JSONB NOT NULL DEFAULT '{}'::jsonb,
  model_version TEXT NOT NULL DEFAULT 'v1-binomial',
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
  PRIMARY KEY (rep_id, month)
);

ALTER TABLE rep_month_stats ENABLE ROW LEVEL SECURITY;
ALTER TABLE rep_month_forecast ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Rep can view own month stats" ON rep_month_stats;
CREATE POLICY "Rep can view own month stats" ON rep_month_stats
  FOR SELECT USING (rep_id = auth.uid());

DROP POLICY IF EXISTS "Rep can upsert own month stats" ON rep_month_stats;
CREATE POLICY "Rep can upsert own month stats" ON rep_month_stats
  FOR ALL USING (rep_id = auth.uid()) WITH CHECK (rep_id = auth.uid());

DROP POLICY IF EXISTS "Managers can view all month stats" ON rep_month_stats;
CREATE POLICY "Managers can view all month stats" ON rep_month_stats
  FOR SELECT USING (
    EXISTS (
      SELECT 1 FROM profiles
      WHERE id = auth.uid() AND role IN ('manager', 'admin')
    )
  );

DROP POLICY IF EXISTS "Rep can view own month forecast" ON rep_month_forecast;
CREATE POLICY "Rep can view own month forecast" ON rep_month_forecast
  FOR SELECT USING (rep_id = auth.uid());

DROP POLICY IF EXISTS "Rep can upsert own month forecast" ON rep_month_forecast;
CREATE POLICY "Rep can upsert own month forecast" ON rep_month_forecast
  FOR ALL USING (rep_id = auth.uid()) WITH CHECK (rep_id = auth.uid());

DROP POLICY IF EXISTS "Managers can view all month forecast" ON rep_month_forecast;
CREATE POLICY "Managers can view all month forecast" ON rep_month_forecast
  FOR SELECT USING (
    EXISTS (
      SELECT 1 FROM profiles
      WHERE id = auth.uid() AND role IN ('manager', 'admin')
    )
  );

CREATE INDEX IF NOT EXISTS idx_rep_month_stats_month ON rep_month_stats(month);
CREATE INDEX IF NOT EXISTS idx_rep_month_forecast_month ON rep_month_forecast(month);
CREATE INDEX IF NOT EXISTS idx_rep_month_forecast_prob ON rep_month_forecast(quota_hit_probability);

-- helper used by incremental stats trigger: ensures a row exists for rep/month
CREATE OR REPLACE FUNCTION ensure_rep_month_stats(rep UUID, mon DATE) RETURNS VOID AS $$
BEGIN
  INSERT INTO rep_month_stats(rep_id, month)
  VALUES (rep, mon)
  ON CONFLICT (rep_id, month) DO NOTHING;
END;
$$ LANGUAGE plpgsql;

-- trigger function to update stats on event insert
CREATE OR REPLACE FUNCTION events_to_stats_trigger() RETURNS trigger AS $$
DECLARE
  m DATE;
BEGIN
  -- micro-helpers
  m := date_trunc('month', NEW.created_at)::date;
  PERFORM ensure_rep_month_stats(NEW.sales_rep_id, m);

  IF NEW.type = 'deal.created' THEN
    -- New deal counts as a lead
    UPDATE rep_month_stats
    SET leads = leads + 1,
        updated_at = timezone('utc', now())
    WHERE rep_id = NEW.sales_rep_id AND month = m;

  ELSIF NEW.type = 'deal.status_changed' THEN
    -- Only count closed_won deals
    IF (NEW.payload->>'new_status') = 'closed_won' THEN
      UPDATE rep_month_stats
      SET sold_units = sold_units + 1,
          updated_at = timezone('utc', now())
      WHERE rep_id = NEW.sales_rep_id AND month = m;
    END IF;

  ELSIF NEW.type = 'activity.completed' THEN
    -- Count outcomes that represent contact/interaction
    IF (NEW.payload->>'outcome') IN ('connected','appt_set','showed','sold','negotiating','follow_up') THEN
      UPDATE rep_month_stats
      SET contacts = contacts + 1,
          updated_at = timezone('utc', now())
      WHERE rep_id = NEW.sales_rep_id AND month = m;
    END IF;
    -- Count appointments set
    IF (NEW.payload->>'outcome') = 'appt_set' THEN
      UPDATE rep_month_stats
      SET appts_set = appts_set + 1,
          updated_at = timezone('utc', now())
      WHERE rep_id = NEW.sales_rep_id AND month = m;
    END IF;
    -- Count appointments that showed
    IF (NEW.payload->>'outcome') = 'showed' THEN
      UPDATE rep_month_stats
      SET appts_show = appts_show + 1,
          updated_at = timezone('utc', now())
      WHERE rep_id = NEW.sales_rep_id AND month = m;
    END IF;

  ELSIF NEW.type = 'rep_quota_updated' THEN
    -- quota updates don't affect rep_month_stats
    -- (they're stored separately if needed)
    NULL;

  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- attach trigger to events table
DROP TRIGGER IF EXISTS stats_from_events ON events;
CREATE TRIGGER stats_from_events
AFTER INSERT ON events
FOR EACH ROW EXECUTE PROCEDURE events_to_stats_trigger();
-- Monte Carlo + Bayesian engine schema additions for TOPREP MVP.
-- This migration is additive and keeps existing phase-0 forecast tables intact.

CREATE TABLE IF NOT EXISTS stores (
  id UUID PRIMARY KEY,
  dealership_id TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  active BOOLEAN NOT NULL DEFAULT true,
  config JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
);

ALTER TABLE stores ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can read their store" ON stores;
CREATE POLICY "Users can read their store" ON stores
  FOR SELECT USING (
    EXISTS (
      SELECT 1
      FROM profiles
      WHERE id = auth.uid()
        AND (profiles.store_id = stores.id OR profiles.role IN ('manager', 'admin'))
    )
  );

DROP POLICY IF EXISTS "Managers can insert stores" ON stores;
CREATE POLICY "Managers can insert stores" ON stores
  FOR INSERT WITH CHECK (
    EXISTS (
      SELECT 1
      FROM profiles
      WHERE id = auth.uid() AND role IN ('manager', 'admin')
    )
  );

DROP POLICY IF EXISTS "Managers can update stores" ON stores;
CREATE POLICY "Managers can update stores" ON stores
  FOR UPDATE USING (
    EXISTS (
      SELECT 1
      FROM profiles
      WHERE id = auth.uid() AND role IN ('manager', 'admin')
    )
  )
  WITH CHECK (
    EXISTS (
      SELECT 1
      FROM profiles
      WHERE id = auth.uid() AND role IN ('manager', 'admin')
    )
  );

GRANT SELECT, INSERT, UPDATE ON stores TO authenticated;

CREATE TABLE IF NOT EXISTS reps (
  id UUID PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
  store_id UUID,
  first_active_date DATE,
  active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
);

CREATE TABLE IF NOT EXISTS leads (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  external_crm_id TEXT,
  store_id UUID,
  rep_id UUID REFERENCES profiles(id) ON DELETE SET NULL,
  source TEXT NOT NULL CHECK (source IN ('internet', 'phone', 'showroom')),
  created_at TIMESTAMPTZ NOT NULL,
  first_response_at TIMESTAMPTZ,
  contacted_at TIMESTAMPTZ,
  appointment_set_at TIMESTAMPTZ,
  appointment_show_at TIMESTAMPTZ,
  sold_at TIMESTAMPTZ,
  lost_at TIMESTAMPTZ,
  status TEXT,
  call_count INTEGER,
  text_count INTEGER,
  email_count INTEGER,
  total_touch_count INTEGER,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
);

CREATE INDEX IF NOT EXISTS idx_leads_rep_created ON leads(rep_id, created_at);
CREATE INDEX IF NOT EXISTS idx_leads_store_created ON leads(store_id, created_at);

CREATE TABLE IF NOT EXISTS quotas (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  rep_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  period_start DATE NOT NULL,
  period_end DATE NOT NULL,
  quota_units INTEGER NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_quotas_rep_period ON quotas(rep_id, period_start, period_end);

CREATE TABLE IF NOT EXISTS rep_stage_stats (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  rep_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  source TEXT NOT NULL CHECK (source IN ('internet', 'phone', 'showroom')),
  stage TEXT NOT NULL CHECK (stage IN ('contact', 'appointment_set', 'appointment_show', 'sold')),
  window_start DATE NOT NULL,
  window_end DATE NOT NULL,
  trials INTEGER NOT NULL DEFAULT 0,
  successes INTEGER NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_rep_stage_stats_unique
  ON rep_stage_stats(rep_id, source, stage, window_start, window_end);

CREATE TABLE IF NOT EXISTS source_stage_priors (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  store_id UUID,
  source TEXT NOT NULL CHECK (source IN ('internet', 'phone', 'showroom')),
  stage TEXT NOT NULL CHECK (stage IN ('contact', 'appointment_set', 'appointment_show', 'sold')),
  prior_alpha NUMERIC(12,6) NOT NULL,
  prior_beta NUMERIC(12,6) NOT NULL,
  baseline_mean NUMERIC(8,6) NOT NULL,
  prior_strength NUMERIC(10,4) NOT NULL DEFAULT 40,
  computed_from_start DATE,
  computed_from_end DATE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_source_stage_priors_unique
  ON source_stage_priors(store_id, source, stage);

CREATE TABLE IF NOT EXISTS rep_stage_posteriors (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  rep_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  source TEXT NOT NULL CHECK (source IN ('internet', 'phone', 'showroom')),
  stage TEXT NOT NULL CHECK (stage IN ('contact', 'appointment_set', 'appointment_show', 'sold')),
  alpha NUMERIC(12,6) NOT NULL,
  beta NUMERIC(12,6) NOT NULL,
  posterior_mean NUMERIC(8,6) NOT NULL,
  lower_80 NUMERIC(8,6),
  upper_80 NUMERIC(8,6),
  lower_95 NUMERIC(8,6),
  upper_95 NUMERIC(8,6),
  trial_count INTEGER NOT NULL DEFAULT 0,
  success_count INTEGER NOT NULL DEFAULT 0,
  confidence_label TEXT NOT NULL CHECK (confidence_label IN ('low', 'medium', 'high')),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_rep_stage_posteriors_unique
  ON rep_stage_posteriors(rep_id, source, stage);

CREATE TABLE IF NOT EXISTS rep_experience (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  rep_id UUID NOT NULL UNIQUE REFERENCES profiles(id) ON DELETE CASCADE,
  tenure_days INTEGER NOT NULL DEFAULT 0,
  total_leads INTEGER NOT NULL DEFAULT 0,
  total_contacts INTEGER NOT NULL DEFAULT 0,
  total_appointments_set INTEGER NOT NULL DEFAULT 0,
  total_appointments_show INTEGER NOT NULL DEFAULT 0,
  total_sold INTEGER NOT NULL DEFAULT 0,
  source_breadth_count INTEGER NOT NULL DEFAULT 0,
  monthly_activity_consistency NUMERIC(8,6) NOT NULL DEFAULT 0,
  experience_score NUMERIC(8,6) NOT NULL DEFAULT 0,
  experience_level TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
);

CREATE TABLE IF NOT EXISTS forecast_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  rep_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  period_start DATE NOT NULL,
  period_end DATE NOT NULL,
  quota_units INTEGER NOT NULL,
  simulation_count INTEGER NOT NULL,
  expected_sales NUMERIC(12,4) NOT NULL,
  median_sales NUMERIC(12,4) NOT NULL,
  p10_sales NUMERIC(12,4) NOT NULL,
  p90_sales NUMERIC(12,4) NOT NULL,
  quota_probability NUMERIC(8,6) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
);

CREATE INDEX IF NOT EXISTS idx_forecast_runs_rep_period ON forecast_runs(rep_id, period_start, period_end);

CREATE TABLE IF NOT EXISTS scenario_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  forecast_run_id UUID NOT NULL REFERENCES forecast_runs(id) ON DELETE CASCADE,
  scenario_name TEXT NOT NULL,
  scenario_type TEXT NOT NULL,
  scenario_payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  expected_sales NUMERIC(12,4) NOT NULL,
  quota_probability NUMERIC(8,6) NOT NULL,
  delta_quota_probability NUMERIC(8,6) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
);

CREATE INDEX IF NOT EXISTS idx_scenario_runs_forecast_run_id ON scenario_runs(forecast_run_id);

ALTER TABLE rep_month_forecast
  ADD COLUMN IF NOT EXISTS simulation_count INTEGER,
  ADD COLUMN IF NOT EXISTS expected_sales NUMERIC(12,4),
  ADD COLUMN IF NOT EXISTS median_sales NUMERIC(12,4),
  ADD COLUMN IF NOT EXISTS p10_sales NUMERIC(12,4),
  ADD COLUMN IF NOT EXISTS p90_sales NUMERIC(12,4),
  ADD COLUMN IF NOT EXISTS top_scenario JSONB;

CREATE INDEX IF NOT EXISTS idx_rep_month_forecast_rep_month ON rep_month_forecast(rep_id, month);
-- Enhanced schema for pacing tracker
-- Add outcome field to activities table

-- First, add the outcome column if it doesn't exist
ALTER TABLE activities ADD COLUMN IF NOT EXISTS outcome TEXT 
CHECK (outcome IN (
  'connected', 'no_answer', 'left_vm', 'appt_set', 'showed', 
  'no_show', 'sold', 'lost', 'negotiating', 'follow_up'
));

-- Create index for better query performance
CREATE INDEX IF NOT EXISTS idx_activities_outcome ON activities(outcome);
CREATE INDEX IF NOT EXISTS idx_activities_activity_type ON activities(activity_type);
CREATE INDEX IF NOT EXISTS idx_activities_completed_at ON activities(completed_at);

-- Update existing activities with realistic outcomes based on activity type
UPDATE activities 
SET outcome = CASE 
  WHEN activity_type = 'call' THEN 
    CASE 
      WHEN random() < 0.3 THEN 'no_answer'
      WHEN random() < 0.5 THEN 'left_vm' 
      WHEN random() < 0.8 THEN 'connected'
      ELSE 'appt_set'
    END
  WHEN activity_type = 'meeting' THEN
    CASE 
      WHEN random() < 0.2 THEN 'no_show'
      WHEN random() < 0.7 THEN 'showed'
      WHEN random() < 0.9 THEN 'negotiating'
      ELSE 'sold'
    END
  WHEN activity_type = 'email' THEN 'follow_up'
  WHEN activity_type = 'demo' THEN 
    CASE 
      WHEN random() < 0.6 THEN 'showed'
      WHEN random() < 0.9 THEN 'negotiating'
      ELSE 'sold'
    END
  ELSE 'connected'
END
WHERE outcome IS NULL;

-- Add appointment-related fields to deals table if they don't exist
ALTER TABLE deals ADD COLUMN IF NOT EXISTS appointment_date TIMESTAMP WITH TIME ZONE;
ALTER TABLE deals ADD COLUMN IF NOT EXISTS appointment_showed BOOLEAN DEFAULT FALSE;
ALTER TABLE deals ADD COLUMN IF NOT EXISTS lead_source_detail TEXT;

-- Create view for pacing calculations
CREATE OR REPLACE VIEW pacing_metrics AS
WITH call_metrics AS (
  SELECT 
    sales_rep_id,
    DATE_TRUNC('month', completed_at) as month_year,
    COUNT(*) FILTER (WHERE activity_type = 'call') as total_calls,
    COUNT(*) FILTER (WHERE activity_type = 'call' AND outcome IN ('connected', 'appt_set')) as connected_calls,
    COUNT(*) FILTER (WHERE outcome = 'appt_set') as appointments_set,
    COUNT(*) FILTER (WHERE outcome = 'showed') as appointments_showed
  FROM activities 
  WHERE completed_at IS NOT NULL
  GROUP BY sales_rep_id, DATE_TRUNC('month', completed_at)
),
deal_metrics AS (
  SELECT 
    sales_rep_id,
    DATE_TRUNC('month', created_at) as month_year,
    COUNT(*) FILTER (WHERE status = 'closed_won') as deals_closed,
    SUM(deal_amount) FILTER (WHERE status = 'closed_won') as revenue_closed,
    AVG(deal_amount) FILTER (WHERE status = 'closed_won') as avg_deal_size
  FROM deals
  GROUP BY sales_rep_id, DATE_TRUNC('month', created_at)
)
SELECT 
  COALESCE(c.sales_rep_id, d.sales_rep_id) as sales_rep_id,
  COALESCE(c.month_year, d.month_year) as month_year,
  COALESCE(c.total_calls, 0) as total_calls,
  COALESCE(c.connected_calls, 0) as connected_calls,
  COALESCE(c.appointments_set, 0) as appointments_set,
  COALESCE(c.appointments_showed, 0) as appointments_showed,
  COALESCE(d.deals_closed, 0) as deals_closed,
  COALESCE(d.revenue_closed, 0) as revenue_closed,
  COALESCE(d.avg_deal_size, 0) as avg_deal_size,
  
  -- Calculate rates
  CASE WHEN c.total_calls > 0 
    THEN ROUND((c.connected_calls::decimal / c.total_calls * 100), 2) 
    ELSE 0 
  END as connection_rate,
  
  CASE WHEN c.connected_calls > 0 
    THEN ROUND((c.appointments_set::decimal / c.connected_calls * 100), 2) 
    ELSE 0 
  END as appointment_rate,
  
  CASE WHEN c.appointments_set > 0 
    THEN ROUND((c.appointments_showed::decimal / c.appointments_set * 100), 2) 
    ELSE 0 
  END as show_rate,
  
  CASE WHEN c.appointments_showed > 0 
    THEN ROUND((d.deals_closed::decimal / c.appointments_showed * 100), 2) 
    ELSE 0 
  END as closing_rate

FROM call_metrics c
FULL OUTER JOIN deal_metrics d ON c.sales_rep_id = d.sales_rep_id AND c.month_year = d.month_year;

-- Grant permissions
GRANT SELECT ON pacing_metrics TO authenticated;-- Monte Carlo performance + schema hardening
-- Safe to run multiple times.

-- 1) Ensure outcome exists for accurate stage mapping.
ALTER TABLE activities
  ADD COLUMN IF NOT EXISTS outcome TEXT;

-- Optional guardrail without blocking uncommon values already in historical data.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'activities_outcome_allowed_values'
  ) THEN
    ALTER TABLE activities
      ADD CONSTRAINT activities_outcome_allowed_values
      CHECK (
        outcome IS NULL OR outcome IN (
          'connected',
          'no_answer',
          'left_vm',
          'appt_set',
          'showed',
          'no_show',
          'sold',
          'lost',
          'negotiating',
          'follow_up'
        )
      );
  END IF;
END $$;

-- 2) Auto-populate outcome for NEW rows when caller does not provide one.
CREATE OR REPLACE FUNCTION set_activity_outcome_default()
RETURNS trigger AS $$
BEGIN
  IF NEW.outcome IS NULL OR btrim(NEW.outcome) = '' THEN
    NEW.outcome := CASE lower(COALESCE(NEW.activity_type, ''))
      WHEN 'call' THEN 'connected'
      WHEN 'meeting' THEN 'appt_set'
      WHEN 'demo' THEN 'showed'
      WHEN 'email' THEN 'follow_up'
      WHEN 'note' THEN 'follow_up'
      ELSE 'follow_up'
    END;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_set_activity_outcome_default ON activities;
CREATE TRIGGER trg_set_activity_outcome_default
BEFORE INSERT OR UPDATE OF activity_type, outcome ON activities
FOR EACH ROW
EXECUTE FUNCTION set_activity_outcome_default();

-- 3) High-value indexes for forecast/recompute query paths.
CREATE INDEX IF NOT EXISTS idx_deals_sales_rep_created_at
  ON deals(sales_rep_id, created_at);

CREATE INDEX IF NOT EXISTS idx_deals_sales_rep_status_created_at
  ON deals(sales_rep_id, status, created_at);

CREATE INDEX IF NOT EXISTS idx_activities_sales_rep_completed_at
  ON activities(sales_rep_id, completed_at);

CREATE INDEX IF NOT EXISTS idx_activities_deal_completed_at
  ON activities(deal_id, completed_at);

CREATE INDEX IF NOT EXISTS idx_activities_outcome
  ON activities(outcome);

CREATE INDEX IF NOT EXISTS idx_rep_stage_posteriors_rep_updated_at
  ON rep_stage_posteriors(rep_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_forecast_runs_rep_period_created_at
  ON forecast_runs(rep_id, period_start, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_scenario_runs_forecast_created_at
  ON scenario_runs(forecast_run_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_rep_month_forecast_rep_month_updated_at
  ON rep_month_forecast(rep_id, month, updated_at DESC);
-- ---------------------------------------------------------------------------
-- forecast_queue
-- Persistent job queue for async forecast recomputation.
-- The HTTP event handler enqueues a job; a separate cron-triggered endpoint
-- (POST /api/jobs/process-forecast) claims and processes jobs in batches.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS forecast_queue (
  id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  rep_id      text        NOT NULL,
  month       text        NOT NULL,   -- "YYYY-MM-01"
  status      text        NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'processing', 'done', 'failed')),
  attempts    int         NOT NULL DEFAULT 0,
  error       text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- One active (pending or processing) job per rep/month de-duplicates bursts of events.
CREATE UNIQUE INDEX IF NOT EXISTS forecast_queue_active_rep_month
  ON forecast_queue (rep_id, month)
  WHERE status IN ('pending', 'processing');

-- Fast lookup of the pending work list.
CREATE INDEX IF NOT EXISTS forecast_queue_pending_idx
  ON forecast_queue (created_at)
  WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- Atomic claim function (SKIP LOCKED prevents concurrent processor collision)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION claim_forecast_jobs(batch_size int DEFAULT 5)
RETURNS SETOF forecast_queue
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  UPDATE forecast_queue
  SET
    status   = 'processing',
    attempts = attempts + 1,
    updated_at = now()
  WHERE id IN (
    SELECT id
    FROM   forecast_queue
    WHERE  status = 'pending'
    ORDER  BY created_at
    LIMIT  batch_size
    FOR UPDATE SKIP LOCKED
  )
  RETURNING *;
$$;

GRANT EXECUTE ON FUNCTION claim_forecast_jobs(int) TO service_role;

-- ---------------------------------------------------------------------------
-- RLS: service_role (used by the job processor) bypasses these policies.
-- No direct user access is necessary — the queue is only touched server-side.
-- ---------------------------------------------------------------------------

ALTER TABLE forecast_queue ENABLE ROW LEVEL SECURITY;

-- Allow inserts from authenticated server calls (events API route).
CREATE POLICY "server can insert forecast jobs"
  ON forecast_queue FOR INSERT
  TO authenticated
  WITH CHECK (true);
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS store_id UUID;
