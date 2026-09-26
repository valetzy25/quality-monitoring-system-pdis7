-- SQLite schema. Timestamps are UTC ISO-8601 strings with explicit offset.
CREATE TABLE IF NOT EXISTS learning_events (
    event_id       TEXT PRIMARY KEY NOT NULL,
    event_type     TEXT NOT NULL CHECK (event_type IN (
        'assignment_published', 'submission_created', 'autotest_completed',
        'activity_recorded', 'homework_reviewed',
        'support_ticket_opened', 'support_ticket_answered'
    )),
    occurred_at    TEXT NOT NULL,
    student_id     TEXT NOT NULL,
    group_id       TEXT NOT NULL,
    module_id      TEXT NOT NULL,
    entity_id      TEXT NOT NULL,
    attempt_no     INTEGER CHECK (attempt_no IS NULL OR attempt_no >= 1),
    passed         INTEGER CHECK (passed IS NULL OR passed IN (0, 1)),
    activity_kind  TEXT CHECK (activity_kind IS NULL OR activity_kind IN ('commit', 'view')),
    CHECK (event_type <> 'autotest_completed' OR (attempt_no IS NOT NULL AND passed IS NOT NULL)),
    CHECK (event_type <> 'activity_recorded' OR activity_kind IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_learning_scope_time
    ON learning_events (group_id, module_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_learning_student_time
    ON learning_events (student_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_learning_entity
    ON learning_events (event_type, entity_id);

