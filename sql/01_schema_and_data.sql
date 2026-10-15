-- =====================================================================
-- Smart Waste Collection Tracker
-- Oracle schema (from ER diagram), business logic and mock data
-- Target: Oracle 19c / 21c. Run as one script:  @smart_waste_oracle.sql
-- =====================================================================

SET DEFINE OFF
SET SERVEROUTPUT ON

-- ---------------------------------------------------------------------
-- 0. Clean-up so the script can be re-run
-- ---------------------------------------------------------------------
BEGIN
  FOR t IN (SELECT table_name FROM user_tables
            WHERE table_name IN ('COMPLAINT','COLLECTION_EVENT','BATCH',
                                 'TRANSFER_STATION','WORKER','VEHICLE','BIN','ZONE')) LOOP
    EXECUTE IMMEDIATE 'DROP TABLE ' || t.table_name || ' CASCADE CONSTRAINTS PURGE';
  END LOOP;
END;
/

BEGIN EXECUTE IMMEDIATE 'DROP VIEW v_bin_status';   EXCEPTION WHEN OTHERS THEN NULL; END;
/
BEGIN EXECUTE IMMEDIATE 'DROP SEQUENCE seq_batch';  EXCEPTION WHEN OTHERS THEN NULL; END;
/

-- ---------------------------------------------------------------------
-- 1. Tables (strong entities first, then weak entities)
-- ---------------------------------------------------------------------
CREATE TABLE zone (
  zone_id        NUMBER(10)    CONSTRAINT pk_zone PRIMARY KEY,
  name           VARCHAR2(100) NOT NULL,
  parent_zone_id NUMBER(10)    CONSTRAINT fk_zone_parent REFERENCES zone (zone_id),
  zone_type      VARCHAR2(10)  NOT NULL
                 CONSTRAINT ck_zone_type CHECK (zone_type IN ('city','zone','ward','street')),
  CONSTRAINT ck_zone_not_self CHECK (parent_zone_id IS NULL OR parent_zone_id <> zone_id)
);

CREATE TABLE bin (
  bin_id       NUMBER(10)   CONSTRAINT pk_bin PRIMARY KEY,
  bin_type     VARCHAR2(12) NOT NULL
               CONSTRAINT ck_bin_type CHECK (bin_type IN ('wet','dry','recyclable')),
  latitude     NUMBER(9,6)  NOT NULL,
  longitude    NUMBER(9,6)  NOT NULL,
  household_id VARCHAR2(30),
  zone_id      NUMBER(10)   NOT NULL CONSTRAINT fk_bin_zone REFERENCES zone (zone_id)
);

CREATE TABLE vehicle (
  vehicle_id     NUMBER(10)   CONSTRAINT pk_vehicle PRIMARY KEY,
  vehicle_type   VARCHAR2(30) NOT NULL,
  capacity_kg    NUMBER(10,2) NOT NULL CONSTRAINT ck_vehicle_cap CHECK (capacity_kg > 0),
  current_status VARCHAR2(12) DEFAULT 'idle' NOT NULL
                 CONSTRAINT ck_vehicle_status CHECK (current_status IN ('in_transit','idle','maintenance'))
);

CREATE TABLE worker (
  worker_id           NUMBER(10)    CONSTRAINT pk_worker PRIMARY KEY,
  name                VARCHAR2(100) NOT NULL,
  phone               VARCHAR2(15),
  shift               VARCHAR2(10)
                      CONSTRAINT ck_worker_shift CHECK (shift IN ('morning','evening','night')),
  assigned_vehicle_id NUMBER(10)    CONSTRAINT fk_worker_vehicle REFERENCES vehicle (vehicle_id)
);

CREATE TABLE transfer_station (
  station_id   NUMBER(10)    CONSTRAINT pk_station PRIMARY KEY,
  name         VARCHAR2(100) NOT NULL,
  latitude     NUMBER(9,6)   NOT NULL,
  longitude    NUMBER(9,6)   NOT NULL,
  capacity_kg  NUMBER(12,2)  NOT NULL CONSTRAINT ck_station_cap CHECK (capacity_kg > 0),
  station_type VARCHAR2(12)  NOT NULL
               CONSTRAINT ck_station_type CHECK (station_type IN ('transfer','landfill','recycling'))
);

CREATE TABLE batch (
  batch_id     NUMBER(10)   CONSTRAINT pk_batch PRIMARY KEY,
  vehicle_id   NUMBER(10)   NOT NULL CONSTRAINT fk_batch_vehicle REFERENCES vehicle (vehicle_id),
  route_id     VARCHAR2(20),
  batch_date   DATE         NOT NULL,
  total_weight NUMBER(10,2) DEFAULT 0 NOT NULL
               CONSTRAINT ck_batch_weight CHECK (total_weight >= 0),
  station_id   NUMBER(10)   CONSTRAINT fk_batch_station REFERENCES transfer_station (station_id),
  disposed_at  TIMESTAMP,
  CONSTRAINT ck_batch_disposal CHECK (disposed_at IS NULL OR station_id IS NOT NULL)
);

-- Weak entity: identified by bin_id + event_timestamp
CREATE TABLE collection_event (
  bin_id          NUMBER(10)   CONSTRAINT fk_event_bin REFERENCES bin (bin_id),
  event_timestamp TIMESTAMP    NOT NULL,
  worker_id       NUMBER(10)   NOT NULL CONSTRAINT fk_event_worker REFERENCES worker (worker_id),
  batch_id        NUMBER(10)   NOT NULL CONSTRAINT fk_event_batch REFERENCES batch (batch_id),
  status          VARCHAR2(12) NOT NULL
                  CONSTRAINT ck_event_status CHECK (status IN ('collected','missed','overflowing')),
  remarks         VARCHAR2(200),
  CONSTRAINT pk_collection_event PRIMARY KEY (bin_id, event_timestamp)
);

-- Weak entity: identified by bin_id + complaint_seq_no
CREATE TABLE complaint (
  bin_id           NUMBER(10)    CONSTRAINT fk_complaint_bin REFERENCES bin (bin_id),
  complaint_seq_no NUMBER(6),
  date_raised      DATE          DEFAULT TRUNC(SYSDATE) NOT NULL,
  description      VARCHAR2(500) NOT NULL,
  status           VARCHAR2(10)  DEFAULT 'open' NOT NULL
                   CONSTRAINT ck_complaint_status CHECK (status IN ('open','resolved')),
  resolved_by      NUMBER(10)    CONSTRAINT fk_complaint_worker REFERENCES worker (worker_id),
  CONSTRAINT pk_complaint PRIMARY KEY (bin_id, complaint_seq_no),
  CONSTRAINT ck_complaint_resolved CHECK (status = 'open' OR resolved_by IS NOT NULL)
);

CREATE INDEX ix_bin_zone         ON bin (zone_id);
CREATE INDEX ix_batch_vehicle    ON batch (vehicle_id);
CREATE INDEX ix_event_batch      ON collection_event (batch_id);
CREATE INDEX ix_event_worker     ON collection_event (worker_id);
CREATE INDEX ix_complaint_status ON complaint (status);

CREATE SEQUENCE seq_batch START WITH 13 INCREMENT BY 1 NOCACHE;

-- ---------------------------------------------------------------------
-- 2. Functions
-- ---------------------------------------------------------------------

-- Days since the bin was last collected successfully (NULL if never)
CREATE OR REPLACE FUNCTION fn_days_since_last_collection (p_bin_id IN NUMBER)
RETURN NUMBER
AS
  v_last TIMESTAMP;
BEGIN
  SELECT MAX(event_timestamp) INTO v_last
  FROM collection_event
  WHERE bin_id = p_bin_id AND status = 'collected';

  IF v_last IS NULL THEN
    RETURN NULL;
  END IF;
  RETURN ROUND(SYSDATE - CAST(v_last AS DATE), 1);
END;
/

-- Percentage of successful collections for a zone AND all its sub-zones
-- (uses a hierarchical query on the self-referencing ZONE table)
CREATE OR REPLACE FUNCTION fn_zone_collection_rate (
  p_zone_id IN NUMBER,
  p_from    IN DATE,
  p_to      IN DATE
) RETURN NUMBER
AS
  v_rate NUMBER;
BEGIN
  SELECT ROUND(100 * SUM(CASE WHEN ce.status = 'collected' THEN 1 ELSE 0 END)
               / NULLIF(COUNT(*), 0), 2)
  INTO v_rate
  FROM collection_event ce
  JOIN bin b ON b.bin_id = ce.bin_id
  WHERE b.zone_id IN (
          SELECT z.zone_id
          FROM zone z
          START WITH z.zone_id = p_zone_id
          CONNECT BY PRIOR z.zone_id = z.parent_zone_id)
    AND ce.event_timestamp >= CAST(p_from AS TIMESTAMP)
    AND ce.event_timestamp <  CAST(p_to + 1 AS TIMESTAMP);

  RETURN v_rate;
END;
/

-- Load of a batch as a percentage of its vehicle's capacity
CREATE OR REPLACE FUNCTION fn_batch_utilization_pct (p_batch_id IN NUMBER)
RETURN NUMBER
AS
  v_pct NUMBER;
BEGIN
  SELECT ROUND(100 * b.total_weight / v.capacity_kg, 2)
  INTO v_pct
  FROM batch b
  JOIN vehicle v ON v.vehicle_id = b.vehicle_id
  WHERE b.batch_id = p_batch_id;

  RETURN v_pct;
END;
/

-- ---------------------------------------------------------------------
-- 3. Procedures
-- ---------------------------------------------------------------------

-- Raise a complaint for a bin; returns the generated complaint_seq_no
CREATE OR REPLACE PROCEDURE sp_raise_complaint (
  p_bin_id        IN  NUMBER,
  p_description   IN  VARCHAR2,
  p_complaint_seq OUT NUMBER
)
AS
  v_exists NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_exists FROM bin WHERE bin_id = p_bin_id;
  IF v_exists = 0 THEN
    RAISE_APPLICATION_ERROR(-20010, 'Bin ' || p_bin_id || ' does not exist');
  END IF;

  SELECT NVL(MAX(complaint_seq_no), 0) + 1
  INTO p_complaint_seq
  FROM complaint
  WHERE bin_id = p_bin_id;

  INSERT INTO complaint (bin_id, complaint_seq_no, description)
  VALUES (p_bin_id, p_complaint_seq, p_description);
END;
/

-- Resolve an open complaint, recording the worker who resolved it
CREATE OR REPLACE PROCEDURE sp_resolve_complaint (
  p_bin_id    IN NUMBER,
  p_seq_no    IN NUMBER,
  p_worker_id IN NUMBER
)
AS
  v_status    complaint.status%TYPE;
  v_worker_ok NUMBER;
BEGIN
  BEGIN
    SELECT status INTO v_status
    FROM complaint
    WHERE bin_id = p_bin_id AND complaint_seq_no = p_seq_no;
  EXCEPTION
    WHEN NO_DATA_FOUND THEN
      RAISE_APPLICATION_ERROR(-20011, 'Complaint not found');
  END;

  IF v_status = 'resolved' THEN
    RAISE_APPLICATION_ERROR(-20012, 'Complaint is already resolved');
  END IF;

  SELECT COUNT(*) INTO v_worker_ok FROM worker WHERE worker_id = p_worker_id;
  IF v_worker_ok = 0 THEN
    RAISE_APPLICATION_ERROR(-20013, 'Worker ' || p_worker_id || ' does not exist');
  END IF;

  UPDATE complaint
  SET status = 'resolved', resolved_by = p_worker_id
  WHERE bin_id = p_bin_id AND complaint_seq_no = p_seq_no;
END;
/

-- Start a new collection batch for a vehicle
CREATE OR REPLACE PROCEDURE sp_create_batch (
  p_vehicle_id   IN  NUMBER,
  p_route_id     IN  VARCHAR2,
  p_batch_date   IN  DATE,
  p_new_batch_id OUT NUMBER
)
AS
  v_status vehicle.current_status%TYPE;
BEGIN
  SELECT current_status INTO v_status
  FROM vehicle
  WHERE vehicle_id = p_vehicle_id;

  IF v_status = 'maintenance' THEN
    RAISE_APPLICATION_ERROR(-20030, 'Vehicle ' || p_vehicle_id || ' is under maintenance');
  ELSIF v_status = 'in_transit' THEN
    RAISE_APPLICATION_ERROR(-20031, 'Vehicle ' || p_vehicle_id || ' is already on an open batch');
  END IF;

  p_new_batch_id := seq_batch.NEXTVAL;

  INSERT INTO batch (batch_id, vehicle_id, route_id, batch_date)
  VALUES (p_new_batch_id, p_vehicle_id, p_route_id, p_batch_date);

  UPDATE vehicle SET current_status = 'in_transit' WHERE vehicle_id = p_vehicle_id;
END;
/

-- Record a bin service event. The worker must belong to the batch's vehicle.
CREATE OR REPLACE PROCEDURE sp_record_collection (
  p_bin_id    IN NUMBER,
  p_worker_id IN NUMBER,
  p_batch_id  IN NUMBER,
  p_status    IN VARCHAR2,
  p_remarks   IN VARCHAR2 DEFAULT NULL
)
AS
  v_vehicle_id batch.vehicle_id%TYPE;
  v_disposed   batch.disposed_at%TYPE;
  v_assigned   worker.assigned_vehicle_id%TYPE;
BEGIN
  SELECT vehicle_id, disposed_at INTO v_vehicle_id, v_disposed
  FROM batch
  WHERE batch_id = p_batch_id;

  IF v_disposed IS NOT NULL THEN
    RAISE_APPLICATION_ERROR(-20021, 'Batch ' || p_batch_id || ' is already closed');
  END IF;

  SELECT assigned_vehicle_id INTO v_assigned
  FROM worker
  WHERE worker_id = p_worker_id;

  IF v_assigned IS NULL OR v_assigned <> v_vehicle_id THEN
    RAISE_APPLICATION_ERROR(-20022, 'Worker is not assigned to the vehicle of this batch');
  END IF;

  INSERT INTO collection_event (bin_id, event_timestamp, worker_id, batch_id, status, remarks)
  VALUES (p_bin_id, SYSTIMESTAMP, p_worker_id, p_batch_id, p_status, p_remarks);
END;
/

-- Close a batch at a transfer station with the final weight
CREATE OR REPLACE PROCEDURE sp_close_batch (
  p_batch_id     IN NUMBER,
  p_station_id   IN NUMBER,
  p_total_weight IN NUMBER
)
AS
  v_disposed batch.disposed_at%TYPE;
  v_station  NUMBER;
BEGIN
  SELECT disposed_at INTO v_disposed FROM batch WHERE batch_id = p_batch_id;

  IF v_disposed IS NOT NULL THEN
    RAISE_APPLICATION_ERROR(-20040, 'Batch ' || p_batch_id || ' is already closed');
  END IF;

  SELECT COUNT(*) INTO v_station FROM transfer_station WHERE station_id = p_station_id;
  IF v_station = 0 THEN
    RAISE_APPLICATION_ERROR(-20041, 'Station ' || p_station_id || ' does not exist');
  END IF;

  UPDATE batch
  SET station_id   = p_station_id,
      total_weight = p_total_weight,
      disposed_at  = SYSTIMESTAMP
  WHERE batch_id = p_batch_id;
END;
/

-- ---------------------------------------------------------------------
-- 4. Triggers
-- ---------------------------------------------------------------------

-- Batch weight must fit the vehicle; disposal cannot precede the batch date
CREATE OR REPLACE TRIGGER trg_batch_validate
BEFORE INSERT OR UPDATE OF total_weight, vehicle_id, disposed_at, batch_date ON batch
FOR EACH ROW
DECLARE
  v_capacity vehicle.capacity_kg%TYPE;
BEGIN
  SELECT capacity_kg INTO v_capacity
  FROM vehicle
  WHERE vehicle_id = :NEW.vehicle_id;

  IF :NEW.total_weight > v_capacity THEN
    RAISE_APPLICATION_ERROR(-20050,
      'Batch weight ' || :NEW.total_weight || ' exceeds vehicle capacity ' || v_capacity);
  END IF;

  IF :NEW.disposed_at IS NOT NULL
     AND :NEW.disposed_at < CAST(:NEW.batch_date AS TIMESTAMP) THEN
    RAISE_APPLICATION_ERROR(-20051, 'Disposal time cannot be before the batch date');
  END IF;
END;
/

-- When a batch is closed, its vehicle becomes available again
CREATE OR REPLACE TRIGGER trg_batch_release_vehicle
AFTER UPDATE OF disposed_at ON batch
FOR EACH ROW
WHEN (NEW.disposed_at IS NOT NULL AND OLD.disposed_at IS NULL)
BEGIN
  UPDATE vehicle
  SET current_status = 'idle'
  WHERE vehicle_id = :NEW.vehicle_id;
END;
/

-- A missed collection automatically raises an open complaint for that bin
CREATE OR REPLACE TRIGGER trg_event_missed_complaint
AFTER INSERT ON collection_event
FOR EACH ROW
WHEN (NEW.status = 'missed')
DECLARE
  v_seq complaint.complaint_seq_no%TYPE;
BEGIN
  SELECT NVL(MAX(complaint_seq_no), 0) + 1
  INTO v_seq
  FROM complaint
  WHERE bin_id = :NEW.bin_id;

  INSERT INTO complaint (bin_id, complaint_seq_no, date_raised, description, status)
  VALUES (:NEW.bin_id, v_seq, TRUNC(CAST(:NEW.event_timestamp AS DATE)),
          'Auto-raised: bin missed during collection', 'open');
END;
/

-- Resolved complaints are immutable (audit safety)
CREATE OR REPLACE TRIGGER trg_complaint_locked
BEFORE UPDATE ON complaint
FOR EACH ROW
BEGIN
  IF :OLD.status = 'resolved' THEN
    RAISE_APPLICATION_ERROR(-20060, 'Resolved complaints cannot be modified');
  END IF;
END;
/

-- ---------------------------------------------------------------------
-- 5. View (joins + function calls)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_bin_status AS
SELECT b.bin_id,
       b.bin_type,
       z.name                              AS zone_name,
       z.zone_type,
       fn_days_since_last_collection(b.bin_id) AS days_since_collection,
       (SELECT COUNT(*)
        FROM complaint c
        WHERE c.bin_id = b.bin_id AND c.status = 'open') AS open_complaints
FROM bin b
JOIN zone z ON z.zone_id = b.zone_id;

-- ---------------------------------------------------------------------
-- 6. Mock data
-- ---------------------------------------------------------------------

-- Zones (city > zone > ward > street)
INSERT INTO zone (zone_id, name, parent_zone_id, zone_type) VALUES (1, 'Central City',  NULL, 'city');
INSERT INTO zone (zone_id, name, parent_zone_id, zone_type) VALUES (2, 'North Zone',    1,    'zone');
INSERT INTO zone (zone_id, name, parent_zone_id, zone_type) VALUES (3, 'South Zone',    1,    'zone');
INSERT INTO zone (zone_id, name, parent_zone_id, zone_type) VALUES (4, 'Ward 12',       2,    'ward');
INSERT INTO zone (zone_id, name, parent_zone_id, zone_type) VALUES (5, 'Ward 15',       3,    'ward');
INSERT INTO zone (zone_id, name, parent_zone_id, zone_type) VALUES (6, 'Lake Street',   4,    'street');
INSERT INTO zone (zone_id, name, parent_zone_id, zone_type) VALUES (7, 'Temple Street', 4,    'street');
INSERT INTO zone (zone_id, name, parent_zone_id, zone_type) VALUES (8, 'Market Street', 5,    'street');
INSERT INTO zone (zone_id, name, parent_zone_id, zone_type) VALUES (9, 'Station Road',  5,    'street');

-- Vehicles
INSERT INTO vehicle (vehicle_id, vehicle_type, capacity_kg, current_status) VALUES (1, 'Compactor Truck', 1500, 'idle');
INSERT INTO vehicle (vehicle_id, vehicle_type, capacity_kg, current_status) VALUES (2, 'Compactor Truck', 1200, 'in_transit');
INSERT INTO vehicle (vehicle_id, vehicle_type, capacity_kg, current_status) VALUES (3, 'Mini Truck',     2000, 'idle');
INSERT INTO vehicle (vehicle_id, vehicle_type, capacity_kg, current_status) VALUES (4, 'Tipper',         1000, 'maintenance');
INSERT INTO vehicle (vehicle_id, vehicle_type, capacity_kg, current_status) VALUES (5, 'Mini Truck',     1800, 'idle');

-- Workers (every vehicle has at least one worker)
INSERT INTO worker (worker_id, name, phone, shift, assigned_vehicle_id) VALUES (1, 'Arjun Mehta',  '9000000001', 'morning', 1);
INSERT INTO worker (worker_id, name, phone, shift, assigned_vehicle_id) VALUES (2, 'Priya Nair',   '9000000002', 'evening', 1);
INSERT INTO worker (worker_id, name, phone, shift, assigned_vehicle_id) VALUES (3, 'Suresh Babu',  '9000000003', 'morning', 2);
INSERT INTO worker (worker_id, name, phone, shift, assigned_vehicle_id) VALUES (4, 'Lakshmi Rao',  '9000000004', 'night',   2);
INSERT INTO worker (worker_id, name, phone, shift, assigned_vehicle_id) VALUES (5, 'Imran Shaikh', '9000000005', 'morning', 3);
INSERT INTO worker (worker_id, name, phone, shift, assigned_vehicle_id) VALUES (6, 'Meena Iyer',   '9000000006', 'evening', 4);
INSERT INTO worker (worker_id, name, phone, shift, assigned_vehicle_id) VALUES (7, 'Vikram Singh', '9000000007', 'morning', 5);
INSERT INTO worker (worker_id, name, phone, shift, assigned_vehicle_id) VALUES (8, 'Kavya Reddy',  '9000000008', 'night',   5);

-- Transfer stations
INSERT INTO transfer_station (station_id, name, latitude, longitude, capacity_kg, station_type)
VALUES (1, 'Eastside Transfer Station', 12.9800, 77.6000, 5000,  'transfer');
INSERT INTO transfer_station (station_id, name, latitude, longitude, capacity_kg, station_type)
VALUES (2, 'Riverbank Landfill',        12.9500, 77.5700, 20000, 'landfill');
INSERT INTO transfer_station (station_id, name, latitude, longitude, capacity_kg, station_type)
VALUES (3, 'Green Recycling Hub',       12.9900, 77.5500, 3000,  'recycling');

-- 30 bins spread over the four street zones (6..9)
BEGIN
  FOR i IN 1 .. 30 LOOP
    INSERT INTO bin (bin_id, bin_type, latitude, longitude, household_id, zone_id)
    VALUES (i,
            CASE MOD(i, 3) WHEN 0 THEN 'wet' WHEN 1 THEN 'dry' ELSE 'recyclable' END,
            12.9700 + i * 0.0010,
            77.5900 + i * 0.0012,
            'HH-' || LPAD(i, 4, '0'),
            6 + MOD(i, 4));
  END LOOP;
END;
/

-- 12 batches: batches 1-11 closed, batch 12 still open (vehicle 2 in transit)
BEGIN
  FOR i IN 1 .. 12 LOOP
    IF i < 12 THEN
      INSERT INTO batch (batch_id, vehicle_id, route_id, batch_date, total_weight, station_id, disposed_at)
      VALUES (i, MOD(i - 1, 5) + 1, 'R-' || LPAD(i, 2, '0'),
              DATE '2026-09-01' + 3 * i,
              400 + MOD(i * 97, 500),
              MOD(i, 3) + 1,
              CAST(DATE '2026-09-01' + 3 * i AS TIMESTAMP) + INTERVAL '16' HOUR);
    ELSE
      INSERT INTO batch (batch_id, vehicle_id, route_id, batch_date, total_weight, station_id, disposed_at)
      VALUES (i, MOD(i - 1, 5) + 1, 'R-' || LPAD(i, 2, '0'),
              DATE '2026-09-01' + 3 * i,
              0, NULL, NULL);
    END IF;
  END LOOP;
END;
/

-- Collection events: each batch services about a third of the bins on its day.
-- Missed events fire the trg_event_missed_complaint trigger automatically.
DECLARE
  v_worker worker.worker_id%TYPE;
BEGIN
  FOR b IN (SELECT batch_id, vehicle_id, batch_date FROM batch ORDER BY batch_id) LOOP
    SELECT MIN(worker_id) INTO v_worker
    FROM worker
    WHERE assigned_vehicle_id = b.vehicle_id;

    FOR bn IN (SELECT bin_id FROM bin WHERE MOD(bin_id + b.batch_id, 3) = 0) LOOP
      INSERT INTO collection_event (bin_id, event_timestamp, worker_id, batch_id, status, remarks)
      VALUES (
        bn.bin_id,
        CAST(b.batch_date AS TIMESTAMP)
          + NUMTODSINTERVAL(6 + MOD(bn.bin_id, 10), 'HOUR')
          + NUMTODSINTERVAL(MOD(bn.bin_id * 7, 60), 'MINUTE'),
        v_worker,
        b.batch_id,
        CASE
          WHEN MOD(bn.bin_id + b.batch_id, 10) = 0 THEN 'missed'
          WHEN MOD(bn.bin_id + b.batch_id, 7)  = 0 THEN 'overflowing'
          ELSE 'collected'
        END,
        NULL);
    END LOOP;
  END LOOP;
  COMMIT;
END;
/

-- Manual complaints through the procedures (sequence numbers generated by the procedure)
DECLARE
  v_seq NUMBER;
BEGIN
  sp_raise_complaint(5, 'Bin lid broken, waste spilling onto the road', v_seq);
  sp_resolve_complaint(5, v_seq, 1);

  sp_raise_complaint(12, 'Bin not collected for two weeks', v_seq);

  sp_raise_complaint(20, 'Foul smell, bin area needs cleaning', v_seq);
  sp_resolve_complaint(20, v_seq, 4);
  COMMIT;
END;
/

-- ---------------------------------------------------------------------
-- 7. Verification
-- ---------------------------------------------------------------------
SELECT 'ZONE' AS table_name, COUNT(*) AS row_count FROM zone
UNION ALL SELECT 'BIN',               COUNT(*) FROM bin
UNION ALL SELECT 'VEHICLE',           COUNT(*) FROM vehicle
UNION ALL SELECT 'WORKER',            COUNT(*) FROM worker
UNION ALL SELECT 'TRANSFER_STATION',  COUNT(*) FROM transfer_station
UNION ALL SELECT 'BATCH',             COUNT(*) FROM batch
UNION ALL SELECT 'COLLECTION_EVENT',  COUNT(*) FROM collection_event
UNION ALL SELECT 'COMPLAINT',         COUNT(*) FROM complaint;

-- City-wide collection rate (uses the zone hierarchy)
SELECT fn_zone_collection_rate(1, DATE '2026-09-01', DATE '2026-10-14') AS city_collection_pct
FROM dual;

-- Utilisation of each batch against its vehicle capacity
SELECT batch_id, total_weight, fn_batch_utilization_pct(batch_id) AS utilization_pct
FROM batch
ORDER BY batch_id;

-- Bins with open complaints, joined with zone information
SELECT * FROM v_bin_status
WHERE open_complaints > 0
ORDER BY open_complaints DESC, bin_id;

-- Worker-level summary: events, missed count and complaints they resolved
SELECT w.worker_id,
       w.name,
       v.vehicle_type,
       COUNT(ce.bin_id)                                        AS events_handled,
       SUM(CASE WHEN ce.status = 'missed' THEN 1 ELSE 0 END)   AS missed_events,
       (SELECT COUNT(*) FROM complaint c WHERE c.resolved_by = w.worker_id) AS complaints_resolved
FROM worker w
JOIN vehicle v              ON v.vehicle_id = w.assigned_vehicle_id
LEFT JOIN collection_event ce ON ce.worker_id = w.worker_id
GROUP BY w.worker_id, w.name, v.vehicle_type
ORDER BY events_handled DESC;

-- ---------------------------------------------------------------------
-- 8. Example calls to test the business rules (uncomment to try)
-- ---------------------------------------------------------------------
-- Should fail: worker 1 is not on vehicle 2 (batch 3 belongs to vehicle 3)
-- BEGIN sp_record_collection(7, 1, 3, 'collected', 'test'); END;
-- /
-- Should fail: batch 4 is already closed
-- BEGIN sp_record_collection(7, 1, 4, 'collected', 'test'); END;
-- /
-- Should fail: vehicle 4 is under maintenance
-- DECLARE v_id NUMBER; BEGIN sp_create_batch(4, 'R-99', SYSDATE, v_id); END;
-- /
-- Open a new batch on vehicle 1, record a collection, then close it
-- DECLARE v_id NUMBER; BEGIN
--   sp_create_batch(1, 'R-13', SYSDATE, v_id);
--   sp_record_collection(7, 1, v_id, 'collected', 'demo');
--   sp_close_batch(v_id, 1, 900);
--   COMMIT;
-- END;
-- /
