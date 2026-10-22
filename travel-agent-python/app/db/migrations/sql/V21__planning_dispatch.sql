-- V21: durable dispatch intent and shared, bounded generation event replay.
ALTER TABLE planning_task ADD COLUMN request_json LONGTEXT NULL COMMENT '完整 GenerateCommand，旧任务由行程重建';
ALTER TABLE planning_task ADD COLUMN event_start_seq BIGINT NOT NULL DEFAULT 0;
ALTER TABLE itinerary_main ADD COLUMN generation_event_seq BIGINT NOT NULL DEFAULT 0;
ALTER TABLE itinerary_main ADD COLUMN event_wire_seq BIGINT NOT NULL DEFAULT 0;
CREATE INDEX idx_planning_task_claim ON planning_task (state, lease_expires_at);

CREATE TABLE planning_queue_guard (
  id INT NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO planning_queue_guard (id) VALUES (1);

CREATE TABLE generation_event (
  itinerary_id BIGINT NOT NULL,
  event_id BIGINT NOT NULL,
  envelope LONGTEXT NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (itinerary_id, event_id),
  KEY idx_generation_event_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
